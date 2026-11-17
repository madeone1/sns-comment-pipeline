"""Load the comment rulebook from Notion, then the last good copy, then the file.

The bundled sns_comment/rulebook_v2.3.json stays in git as the last fallback.
A successful Notion read is cached at moderation/rulebook-cache.json. The
classifier still sees the same in-memory book it uses today.
"""

import datetime as dt
import json
import re

from .api import PilotError
from .moderation import load_rulebook, policy_sha


CACHE_NAME = "moderation/rulebook-cache.json"
DEFAULT_DATABASE_ID = "41b0761a512d8172823bffc0cf69ca21"
DEFAULT_DATA_SOURCE = "collection://41b0761a-512d-814c-aec7-000b844d9f30"
DEFAULT_PAGE_ID = "41b0761a512d81259c81d50af0fec6a3"
COLUMNS = ("이름", "코드", "결과", "순서", "설명", "예외", "사용")
RESULT_LABEL = {"숨김": "삭제", "사람 판단": "사람판단", "유지": "유지", "폐지": "폐지"}
ACTIVE_RESULTS = {"숨김", "사람 판단", "유지"}
CODE = re.compile(r"[A-Z][0-9]{1,2}")
SOURCE_URL = re.compile(r"collection://[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
DATABASE_ID = re.compile(r"[0-9a-f]{32}")


class RulebookError(PilotError):
    """The Notion table was fetched but is not a usable rulebook."""


def rulebook_source(settings):
    value = ((settings.get("moderation") or {}).get("rulebook") or {}).get("source") if isinstance(settings, dict) else None
    if not isinstance(value, str) or not value.strip():
        return "notion"
    return value.strip().casefold()


def _log(event, **values):
    print(json.dumps({"event": event, **values}, ensure_ascii=False), flush=True)


def _text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value).strip()
    if isinstance(value, list):
        return " ".join(_text(item) for item in value).strip()
    if isinstance(value, dict):
        if isinstance(value.get("name"), str):
            return value["name"].strip()
        return _text(value.get("plain_text") or value.get("text") or value.get("content"))
    return str(value).strip()


def _checked(value):
    if value is True:
        return True
    if isinstance(value, str) and value.strip().casefold() in {"true", "yes", "__yes__", "1"}:
        return True
    return False


def _order(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = _text(value)
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _header():
    book = load_rulebook()
    return {key: book[key] for key in ("version", "updated", "source", "decision_order", "labels")}


def build_rulebook(rows, edited=""):
    """Turn Notion rows into the classifier book. 폐지 rows stay listed but are not applied."""
    if not isinstance(rows, list):
        raise RulebookError("분류 기준표 행 형식이 올바르지 않습니다.")
    seen = {}
    parsed = []
    for row in rows:
        if not isinstance(row, dict):
            raise RulebookError("분류 기준표 행 형식이 올바르지 않습니다.")
        missing = [name for name in ("코드", "결과", "사용") if name not in row]
        if missing:
            raise RulebookError("분류 기준표에 필수 열이 없습니다: " + ", ".join(missing))
        if not _checked(row.get("사용")):
            continue
        code = _text(row.get("코드")).upper()
        result = _text(row.get("결과"))
        if not CODE.fullmatch(code):
            raise RulebookError("분류 기준표의 규칙 코드를 확인해주세요.")
        if result not in RESULT_LABEL:
            raise RulebookError("분류 기준표의 결과 값을 확인해주세요: " + (result or "(빈 값)"))
        if code in seen:
            raise RulebookError("분류 기준표에 같은 규칙 코드가 두 번 있습니다: " + code)
        seen[code] = True
        active = result in ACTIVE_RESULTS
        if active and "설명" not in row:
            raise RulebookError("분류 기준표에 필수 열이 없습니다: 설명")
        if active and "순서" not in row:
            raise RulebookError("분류 기준표에 필수 열이 없습니다: 순서")
        definition = _text(row.get("설명"))
        order = _order(row.get("순서"))
        if active and not definition:
            raise RulebookError(code + " 규칙의 설명이 비어 있습니다.")
        if active and order is None:
            raise RulebookError(code + " 규칙의 순서가 없습니다.")
        try:
            from .count_rules import ConditionSkip, parse_condition
            condition = parse_condition(row, code)
        except ConditionSkip as skipped:
            _log("moderation_rule_skipped", severity="WARNING", code=skipped.code, message=skipped.message)
            continue
        parsed.append({
            "order": order if order is not None else 10_000,
            "id": code,
            "label": RESULT_LABEL[result],
            "name": _text(row.get("이름")) or code,
            "definition": definition,
            "exceptions": _text(row.get("예외")),
            "condition": condition,
        })
    parsed.sort(key=lambda item: (item["order"], item["id"]))
    rules = [{key: item[key] for key in ("id", "label", "name", "definition", "exceptions")} for item in parsed]
    conditions = []
    for item in parsed:
        if not item.get("condition"):
            continue
        conditions.append({"id": item["id"], "label": item["label"], "name": item["name"],
                           "order": item["order"], "reason": item["name"], **item["condition"]})
    hide_ids = [rule["id"] for rule in rules if rule["label"] == "삭제"]
    keep_ids = [rule["id"] for rule in rules if rule["label"] == "유지"]
    if not hide_ids or not keep_ids:
        raise RulebookError("사용 중인 숨김 규칙과 유지 규칙이 모두 필요합니다.")
    book = _header()
    book["hide_rule_ids"] = hide_ids
    book["abolished"] = [rule["id"] for rule in rules if rule["label"] == "폐지"]
    book["rules"] = rules
    if conditions:
        book["conditions"] = conditions
    ref_time = edited.strip() if isinstance(edited, str) and edited.strip() else "unknown"
    book["policy_ref"] = "notion:" + ref_time + ":" + policy_sha(book)[:12]
    return book


def _file_book():
    book = load_rulebook()
    book = dict(book)
    book["policy_ref"] = "file:" + book["version"] + ":" + policy_sha(book)[:12]
    return book


def _data_source(settings):
    configured = ((settings.get("moderation") or {}).get("rulebook") or {}) if isinstance(settings, dict) else {}
    source = configured.get("data_source_url") or configured.get("data_source_id") or DEFAULT_DATA_SOURCE
    if isinstance(source, str) and DATABASE_ID.fullmatch(source.replace("-", "")):
        source = "collection://" + source if source.startswith("collection://") else "collection://" + _dashed(source.replace("-", ""))
    if not isinstance(source, str) or not SOURCE_URL.fullmatch(source):
        raise RulebookError("분류 기준표 Notion 주소가 올바르지 않습니다.")
    database_id = configured.get("database_id") or DEFAULT_DATABASE_ID
    if not isinstance(database_id, str) or not DATABASE_ID.fullmatch(database_id.replace("-", "").lower()):
        raise RulebookError("분류 기준표 Notion 주소가 올바르지 않습니다.")
    return database_id.replace("-", "").lower(), source


def _dashed(compact):
    return compact[0:8] + "-" + compact[8:12] + "-" + compact[12:16] + "-" + compact[16:20] + "-" + compact[20:32]


_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")
_EDIT_ATTR = re.compile(
    r"(?:page_last_edited_at|last_edited_time|last_edited_at|lastEditedTime|last-edited-time)"
    r"""["'\s:=]+["']?(""" + _TIME.pattern + r")",
    re.I)
_TRASH_ATTR = re.compile(r"(?:in[-_]trash|archived|is[-_]archived)\s*=\s*[\"']?\s*true\b", re.I)


def _time_value(value):
    if not isinstance(value, str):
        return ""
    match = _TIME.search(value.strip())
    return match.group(0) if match else ""


def _edited(payload):
    """Prefer the database or data source edit time over a row or a parent page."""
    if isinstance(payload, dict):
        for key in ("page_last_edited_at", "last_edited_time", "last_edited_at", "lastEditedTime"):
            found = _time_value(payload.get(key))
            if found:
                return found
        for key in ("metadata", "database", "data_source", "page"):
            if isinstance(payload.get(key), dict):
                found = _edited(payload[key])
                if found:
                    return found
        text = payload.get("text")
        if isinstance(text, str):
            return _edited(text)
        return ""
    if isinstance(payload, str):
        match = _EDIT_ATTR.search(payload)
        return match.group(1) if match else ""
    return ""


def _truthy(value):
    if value is True:
        return True
    return isinstance(value, str) and value.strip().casefold() in {"true", "yes", "1"}


def _trashed(payload):
    """A database inside a trashed page can still return rows. Do not use them."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key.replace("-", "_").casefold() in {"in_trash", "archived", "is_archived"} and _truthy(value):
                return True
            if key in {"metadata", "database", "data_source", "page"} and isinstance(value, dict) and _trashed(value):
                return True
        text = payload.get("text")
        return isinstance(text, str) and _trashed(text)
    if isinstance(payload, str):
        return _TRASH_ATTR.search(payload) is not None
    return False


def _page_id(settings):
    configured = ((settings.get("moderation") or {}).get("rulebook") or {}) if isinstance(settings, dict) else {}
    page_id = configured.get("page_id") or DEFAULT_PAGE_ID
    if not isinstance(page_id, str) or not DATABASE_ID.fullmatch(page_id.replace("-", "").lower()):
        raise RulebookError("분류 기준표 Notion 주소가 올바르지 않습니다.")
    return page_id.replace("-", "").lower()


def _optional_fetch(client, identifier):
    try:
        return client.call("fetch", {"id": identifier})
    except PilotError:
        return None


def fetch_rows(client, settings):
    database_id, source = _data_source(settings)
    page_id = _page_id(settings)
    fetched = client.call("fetch", {"id": database_id})
    source_fetched = _optional_fetch(client, source)
    page_fetched = client.call("fetch", {"id": page_id})
    for payload in (fetched, source_fetched, page_fetched):
        if payload is not None and _trashed(payload):
            raise RulebookError("분류 기준표가 휴지통 또는 보관 상태라 사용하지 않습니다.")
    base = 'SELECT "이름", "코드", "결과", "순서", "설명", "예외", "사용" FROM "' + source + '"'
    extended = base.replace(" FROM ", ', "조건 종류", "숫자", "대상", "범위" FROM ')
    try:
        result = client.call("query-data-sources", {"data": {
            "mode": "sql", "data_source_urls": [source], "query": extended, "params": []}})
    except PilotError as error:
        _log("moderation_rule_columns", severity="WARNING", message="조건 열을 읽지 못해 내용 규칙만 사용합니다.",
             reason=str(error))
        result = client.call("query-data-sources", {"data": {
            "mode": "sql", "data_source_urls": [source], "query": base, "params": []}})
    if isinstance(result, dict) and (result.get("has_more") or result.get("next_cursor")):
        raise RulebookError("분류 기준표가 한 번에 읽히지 않았습니다.")
    if isinstance(result, dict) and _trashed({key: value for key, value in result.items() if key != "results"}):
        raise RulebookError("분류 기준표가 휴지통 또는 보관 상태라 사용하지 않습니다.")
    rows = result.get("results") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        raise RulebookError("분류 기준표 조회 결과가 없습니다.")
    edited = _edited(fetched) or (_edited(source_fetched) if source_fetched else "") or _edited(page_fetched)
    if not edited and isinstance(result, dict):
        edited = _edited({key: value for key, value in result.items() if key != "results"})
    return rows, edited


def _load_cache(state):
    if state is None or not hasattr(state, "json_get"):
        return None
    try:
        current, _generation = state.json_get(CACHE_NAME, None)
    except (OSError, ValueError, TypeError):
        return None
    book = current.get("book") if isinstance(current, dict) else None
    if not isinstance(book, dict) or book.get("version") != "v2.3" or not isinstance(book.get("rules"), list) or not book["rules"]:
        return None
    if not isinstance(book.get("policy_ref"), str) or not book["policy_ref"]:
        return None
    return book


def _save_cache(state, book):
    if state is None or not hasattr(state, "json_put"):
        return
    current, generation = state.json_get(CACHE_NAME, {})
    if not isinstance(current, dict):
        generation = 0
    state.json_put(CACHE_NAME, {
        "book": book,
        "saved_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }, generation)


def resolve_rulebook(settings, state, client=None):
    """Notion first. A bad fetch uses the cached book, then the git file."""
    source = rulebook_source(settings)
    if source in {"file", "github", "json"}:
        book = _file_book()
        _log("moderation_rulebook", source="file", policy_ref=book["policy_ref"], rules=len(book["rules"]))
        return book
    try:
        if source != "notion":
            raise RulebookError("분류 기준표 출처 설정을 확인해주세요.")
        if client is None:
            raise RulebookError("Notion 연결이 없습니다.")
        rows, edited = fetch_rows(client, settings)
        book = build_rulebook(rows, edited)
        try:
            _save_cache(state, book)
        except (OSError, ValueError, TypeError, PilotError):
            _log("moderation_rulebook", severity="WARNING", source="notion",
                 message="검증된 기준표를 저장하지 못했습니다. 이번 실행은 Notion 기준표를 사용합니다.",
                 policy_ref=book["policy_ref"])
        _log("moderation_rulebook", source="notion", policy_ref=book["policy_ref"],
             rules=len(book["rules"]), edited=edited or "unknown")
        return book
    except Exception as error:
        reason = str(error) if isinstance(error, PilotError) else "분류 기준표를 읽지 못했습니다."
        cached = _load_cache(state)
        if cached is not None:
            _log("moderation_rulebook", severity="WARNING", source="cache", reason=reason,
                 policy_ref=cached["policy_ref"],
                 message="Notion 기준표를 쓰지 못해 마지막 성공 기준표를 사용합니다.")
            return cached
        book = _file_book()
        _log("moderation_rulebook", severity="WARNING", source="file", reason=reason,
             policy_ref=book["policy_ref"],
             message="Notion 기준표와 저장된 기준표가 없어 저장소 파일을 사용합니다.")
        return book
