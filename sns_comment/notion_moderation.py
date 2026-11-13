"""Daily Notion summary and Notion choices for comments that need a person.

The 09:00 job adds one toggle in the Develop section of today's 일일 업무 page.
It does not create that page. One shared decision database is reused. The 10-minute job, only when SNS_NOTION_DECISIONS=1, copies a Notion
숨김/유지 choice into the rulebook sheet when that cell is still empty, then
the existing sheet logic hides or confirms 유지. The sheet stays the source of
truth. Nothing is deleted and nothing is un-hidden.
"""

import datetime as dt
import json
import os
import re
import time

from .api import PilotError
from .human_decision import (
    COMMENT_COLUMN, DECISION_COLUMN, FIRST_DATA_ROW, MEMO_COLUMN, PENDING_MARKERS,
    REVIEW_TAB, find_comment_row, set_decision_if_blank,
)
from .moderation_sheet import HIDE_TAB, _read_rows, _text
from .notion_mcp import NoDailyPage, body


KST = dt.timezone(dt.timedelta(hours=9))
PARENT_PAGE_ID = "4120761a512d817fae8ff53edda31b9e"
DATABASE_TITLE = "인스타 댓글 사람 판단"
STATE_NAME = "moderation/notion-decision-database.json"
COVERAGE_NAME = "moderation/notion-summary-coverage.json"
MAX_COVERED_DAYS = 7
WEEKDAYS = "월화수목금토일"
HIDE_URL = "https://docs.google.com/spreadsheets/d/1496AQpUgceCZCNaahpJ89_SSMbiO6nmOtwFi6u4QzMY/edit#gid=123926565"
REVIEW_URL = "https://docs.google.com/spreadsheets/d/1496AQpUgceCZCNaahpJ89_SSMbiO6nmOtwFi6u4QzMY/edit#gid=715862237"
ACCOUNTS = ("서울", "부산", "속초", "토론토")
SCHEMA = (
    'CREATE TABLE ("댓글 원문" TITLE, "계정" SELECT(\'서울\':blue, \'부산\':green, \'속초\':orange, \'토론토\':purple), '
    '"작성자" RICH_TEXT, "작성일(KST)" RICH_TEXT, "게시물 링크" URL, "규칙/판정 이유" RICH_TEXT, '
    '"댓글 ID" RICH_TEXT, "결정" SELECT(\'숨김\':red, \'유지\':green), "상태" RICH_TEXT)'
)
NOTE_DAY = re.compile(r"^(\d{4}-\d{2}-\d{2})")
NOTE_CLOCK = re.compile(r"(\d{2}:\d{2})")
DATABASE_TAG = re.compile(r"<database\b([^>]*)>(.*?)</database>", re.S)
DATA_SOURCE_TAG = re.compile(r"<data-source\b([^>]*)/?>", re.I)
ATTR = re.compile(r'([\w:-]+)="([^"]*)"')
COLLECTION_URL = re.compile(r"collection://[0-9a-fA-F-]{32,36}")
PAGE_URL = re.compile(r"https://(?:www\.)?(?:app\.)?notion\.(?:com|so)/[^\s\"'<>]+")
DEVELOP_HEADING = re.compile(r"^###\s+(?:<b>)?Develop(?:</b>)?\s*$")
ROOT_HEADING = re.compile(r"^#{1,3}\s+\S")
REPORT_MARK = ("📊", "📣", "📈")


def notion_decisions_enabled():
    return os.environ.get("SNS_NOTION_DECISIONS", "").strip() == "1"


def _log(event, **values):
    import json
    print(json.dumps({"event": event, **values}, ensure_ascii=False), flush=True)


def _notion_settings(settings):
    return ((settings.get("moderation") or {}).get("notion") or {}) if isinstance(settings, dict) else {}


def parent_page_id(settings):
    value = _notion_settings(settings).get("parent_page_id") or PARENT_PAGE_ID
    compact = str(value).replace("-", "").lower()
    if not re.fullmatch(r"[0-9a-f]{32}", compact):
        raise PilotError("일일 업무 부모 페이지 ID를 확인해주세요.")
    return compact


def toggle_title(day):
    return "인스타 댓글 관리 (" + day + ")"


def _today(now):
    moment = now or dt.datetime.now(dt.timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(KST).date()


def _cell(row, index):
    return _text(row[index]) if len(row) > index else ""


def account_name(label):
    for name in ACCOUNTS:
        if label.startswith(name):
            return name
    return ""


def is_weekend(now=None):
    return _today(now).weekday() >= 5


def coverage_period(state, now=None):
    """Days to include: the day after the last summary through yesterday, at most one week."""
    today = _today(now)
    end = today - dt.timedelta(days=1)
    saved, _generation = state.json_get(COVERAGE_NAME) if state is not None else (None, 0)
    start = None
    if isinstance(saved, dict):
        try:
            start = dt.date.fromisoformat(saved.get("covered_through") or "") + dt.timedelta(days=1)
        except ValueError:
            start = None
    if start is None:
        start = end - dt.timedelta(days=2) if today.weekday() == 0 else end
    if start > end:
        start = end
    if (end - start).days + 1 > MAX_COVERED_DAYS:
        start = end - dt.timedelta(days=MAX_COVERED_DAYS - 1)
    return start.isoformat(), end.isoformat()


def remember_coverage(state, end):
    current, generation = state.json_get(COVERAGE_NAME)
    if isinstance(current, dict) and current.get("covered_through") == end:
        return
    state.json_put(COVERAGE_NAME, {"covered_through": end}, generation=generation)


def _noted_day(value):
    match = NOTE_DAY.match(value or "")
    return match.group(1) if match else ""


def hidden_between(rows, start, end):
    """Count 숨김 완료 rows whose note date falls in the inclusive range."""
    counts = {name: 0 for name in ACCOUNTS}
    for row in rows[FIRST_DATA_ROW:]:
        if _cell(row, 11) != "숨김 완료":
            continue
        day = _noted_day(_cell(row, 12))
        if not day or not (start <= day <= end):
            continue
        name = account_name(_cell(row, 1))
        if name:
            counts[name] += 1
    return {"total": sum(counts.values()), "by_account": counts, "start": start, "end": end}


def hidden_yesterday(rows, yesterday):
    """Count 숨김 완료 rows whose note date is yesterday. Other statuses stay out."""
    return hidden_between(rows, yesterday, yesterday)


def _korean_day(day):
    parsed = dt.date.fromisoformat(day)
    return str(parsed.month) + "/" + str(parsed.day) + "(" + WEEKDAYS[parsed.weekday()] + ")"


def hide_heading(hidden):
    start, end = hidden.get("start"), hidden.get("end")
    if not start or not end or start == end:
        return "어제 숨긴 댓글 " + str(hidden["total"]) + "건"
    return _korean_day(hidden["start"]) + "~" + _korean_day(hidden["end"]) + " 숨긴 댓글 " + str(hidden["total"]) + "건"


def pending_reviews(rows):
    """사람 판단 rows still waiting: blank choice and an automation note."""
    found = []
    for row in rows[FIRST_DATA_ROW:]:
        decision = _cell(row, DECISION_COLUMN)
        memo = _cell(row, MEMO_COLUMN)
        comment_id = _cell(row, COMMENT_COLUMN)
        if decision or not comment_id:
            continue
        if not any(marker in memo for marker in PENDING_MARKERS):
            continue
        if any(marker in memo for marker in ("숨김 완료", "유지 확정", "숨김 실패 (중단)")):
            continue
        found.append({
            "text": _cell(row, 4) or "(원문 없음)",
            "account": account_name(_cell(row, 1)),
            "author": _cell(row, 3),
            "written": _cell(row, 2),
            "permalink": _cell(row, 5),
            "reason": (_cell(row, 8) + " " + _cell(row, 9)).strip(),
            "comment_id": comment_id,
            "memo": memo,
        })
    return found


def period_pending(rows, start, end):
    """Pending rows detected in the range. A row with no date is still included."""
    chosen = []
    for item in pending_reviews(rows):
        day = _noted_day(item["memo"]) or _noted_day(item["written"])
        if day and not (start <= day <= end):
            continue
        chosen.append(item)
    return chosen


def sheet_memo_status(memo, now):
    if "숨김 완료" in memo:
        clock = NOTE_CLOCK.search(memo)
        if clock:
            return "숨김 완료 " + clock.group(1)
        moment = now or dt.datetime.now(dt.timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=dt.timezone.utc)
        return "숨김 완료 " + moment.astimezone(KST).strftime("%H:%M")
    if "유지 확정" in memo:
        return "유지 확정"
    failure = re.search(r"숨김 실패 \([^)]+\)", memo)
    if failure:
        return failure.group(0)
    if "숨김 실패" in memo:
        return "숨김 실패"
    if "숨김 예정(시험)" in memo:
        return "숨김 예정(시험)"
    return ""


def toggle_markdown(day, hidden, pending_count, database):
    lines = ['<toggle heading="h3">', "💬 <b>" + toggle_title(day) + "</b>", "",
             hide_heading(hidden), ""]
    for name in ACCOUNTS:
        lines.append("- " + name + " " + str(hidden["by_account"][name]) + "건")
    lines.extend(["", "[숨김 대상 시트](" + HIDE_URL + ")", "",
                  "사람 판단 필요 " + str(pending_count) + "건",
                  "아래 표에서 숨김 또는 유지를 고릅니다. 시트에 이미 결정이 있으면 시트를 유지합니다.",
                  "", "[사람 판단 시트](" + REVIEW_URL + ")"])
    if database and database.get("url"):
        lines.extend(["", '사람 판단 표: <mention url="' + database["url"] + '" type="database">'
                      + DATABASE_TITLE + "</mention>"])
    lines.append("</toggle>")
    return "\n".join(lines)


def _attrs(text):
    return {key: value for key, value in ATTR.findall(text)}


def _collection(value):
    text = str(value or "").strip()
    match = COLLECTION_URL.search(text)
    if match:
        return match.group(0)
    raw = text.replace("-", "")
    if re.fullmatch(r"[0-9a-fA-F]{32}", raw):
        dashed = raw.lower()
        return "collection://" + "-".join((dashed[0:8], dashed[8:12], dashed[12:16], dashed[16:20], dashed[20:32]))
    return ""


def _page_url(value):
    if not isinstance(value, str):
        return ""
    match = PAGE_URL.search(value)
    return match.group(0).rstrip(").,>") if match else ""


def _json_candidates(value, found):
    if isinstance(value, dict):
        sources = value.get("data_sources") if isinstance(value.get("data_sources"), list) else value.get("dataSources")
        source = ""
        if isinstance(sources, list):
            for item in sources:
                if isinstance(item, dict):
                    source = _collection(item.get("url") or item.get("data_source_url") or item.get("id") or "")
                    if source:
                        break
        if not source:
            source = _collection(value.get("data_source_url") or value.get("collection") or "")
        url = _page_url(value.get("url") or "")
        if not url and source and isinstance(value.get("id"), str):
            raw = value["id"].replace("-", "")
            if re.fullmatch(r"[0-9a-fA-F]{32}", raw):
                url = "https://app.notion.com/p/" + raw.lower()
        title = value.get("title") if isinstance(value.get("title"), str) else value.get("name") if isinstance(value.get("name"), str) else ""
        if url or source:
            found.append((title.strip(), {"url": url, "data_source_url": source,
                                           "database_id": value.get("id") if isinstance(value.get("id"), str) else ""}))
        for child in value.values():
            _json_candidates(child, found)
    elif isinstance(value, list):
        for item in value:
            _json_candidates(item, found)
    elif isinstance(value, str) and value.strip()[:1] in "{[":
        try:
            _json_candidates(json.loads(value), found)
        except ValueError:
            return


def _choose_database(found):
    complete = [item for item in found if item[1].get("url") and item[1].get("data_source_url")]
    titled = [item for item in complete if item[0] == DATABASE_TITLE]
    pool = titled or complete
    if len(pool) == 1:
        return pool[0][1]
    urls = list(dict.fromkeys(item[1]["url"] for item in found if item[1].get("url")))
    sources = list(dict.fromkeys(item[1]["data_source_url"] for item in found if item[1].get("data_source_url")))
    titled_urls = list(dict.fromkeys(item[1]["url"] for item in found if item[0] == DATABASE_TITLE and item[1].get("url")))
    chosen_urls = titled_urls or urls
    if len(chosen_urls) == 1 and len(sources) <= 1:
        identity = next((item[1].get("database_id") or "" for item in found if item[1].get("url") == chosen_urls[0]), "")
        return {"url": chosen_urls[0], "data_source_url": sources[0] if sources else "", "database_id": identity}
    if len(sources) == 1 and not chosen_urls:
        return {"url": "", "data_source_url": sources[0], "database_id": ""}
    raise PilotError("사람 판단 데이터베이스 주소를 확인하지 못했습니다.")


def parse_database(result):
    """Read a database address from either a Notion tag or the JSON create response."""
    found = []
    text = ""
    if isinstance(result, dict):
        _json_candidates(result, found)
        text = result.get("text") if isinstance(result.get("text"), str) else ""
    elif isinstance(result, str):
        text = result
    if text.strip()[:1] in "{[":
        _json_candidates(text, found)
    for attrs, title in DATABASE_TAG.findall(text or ""):
        values = _attrs(attrs)
        found.append((title.strip(), {"url": values.get("url") or "",
                                       "data_source_url": _collection(values.get("data-source-url") or ""),
                                       "database_id": ""}))
    for attrs in DATA_SOURCE_TAG.findall(text or ""):
        values = _attrs(attrs)
        source = _collection(values.get("url") or values.get("data-source-url") or "")
        if source:
            found.append(("", {"url": "", "data_source_url": source, "database_id": ""}))
    return _choose_database(found)


def _page_text(page):
    if not isinstance(page, dict):
        raise PilotError("Notion 페이지 응답을 확인하지 못했습니다.")
    return page.get("text") or ""


def find_database_on_page(page):
    """Find our database even when the page tag has a URL and no data-source attribute."""
    found = []
    for attrs, title in DATABASE_TAG.findall(_page_text(page)):
        if title.strip() != DATABASE_TITLE:
            continue
        values = _attrs(attrs)
        found.append((DATABASE_TITLE, {"url": values.get("url") or "",
                                        "data_source_url": _collection(values.get("data-source-url") or ""),
                                        "database_id": ""}))
    if not found:
        return None
    try:
        return _choose_database(found)
    except PilotError:
        return found[0][1]


def _finish_async(mcp, result, sleeper):
    task = {}
    if isinstance(result, dict):
        task = result.get("async_task") or (result if result.get("type") == "async_task" else {})
    if not task:
        return result
    task_id = task.get("task_id") or task.get("id")
    if not task_id:
        raise PilotError("Notion 비동기 작업 식별자를 확인하지 못했습니다.")
    for _ in range(30):
        sleeper(2)
        polled = mcp.call("get-async-task", {"task_id": task_id})
        if polled.get("status") == "succeeded":
            return polled.get("result") or {}
        if polled.get("status") == "failed":
            raise PilotError("Notion 비동기 반영이 실패했습니다.")
    raise PilotError("Notion 비동기 반영이 대기 중입니다. 결과 확인 후 재실행이 필요합니다.")


def _complete_database(mcp, database):
    if database.get("url") and database.get("data_source_url"):
        return database
    target = database.get("url") or database.get("data_source_url")
    if not target:
        raise PilotError("사람 판단 데이터베이스 주소를 확인하지 못했습니다.")
    parsed = parse_database(mcp.call("fetch", {"id": target}))
    url = database.get("url") or parsed.get("url")
    source = database.get("data_source_url") or parsed.get("data_source_url")
    if not url or not source:
        raise PilotError("사람 판단 데이터베이스 주소를 확인하지 못했습니다.")
    return {"url": url, "data_source_url": source,
            "database_id": database.get("database_id") or parsed.get("database_id") or "",
            "data_source_id": source.replace("collection://", "")}


def _remember_database(state, generation, database):
    saved = {"url": database["url"], "data_source_url": database["data_source_url"],
             "database_id": database.get("database_id") or "",
             "data_source_id": database["data_source_url"].replace("collection://", "")}
    state.json_put(STATE_NAME, saved, generation=generation)
    return saved


def _saved_database(state):
    current, generation = state.json_get(STATE_NAME)
    if isinstance(current, dict) and current.get("url") and current.get("data_source_url"):
        return current, generation
    return None, generation


def ensure_database(mcp, state, settings, sleeper=time.sleep, pages=()):
    """Reuse the saved database, or the one already on the page. Never create a second one."""
    from .cloud_state import NOTION_MODERATION_LEASE, lease_retry

    def locate(page_list):
        for page in page_list:
            if not page:
                continue
            found = find_database_on_page(page if isinstance(page, dict) else mcp.call("fetch", {"id": page}))
            if found and (found.get("url") or found.get("data_source_url")):
                titled = DATABASE_TITLE in _page_text(page if isinstance(page, dict) else {"text": ""}) or found.get("url")
                if titled or found.get("url"):
                    return _complete_database(mcp, found)
        return None

    def create():
        saved, generation = _saved_database(state)
        if saved:
            return saved
        parent = parent_page_id(settings)
        found = locate([parent, *pages])
        if found:
            return _remember_database(state, generation, found)
        response = _finish_async(mcp, mcp.call("create-database", {
            "parent": {"type": "page_id", "page_id": parent},
            "title": DATABASE_TITLE,
            "schema": SCHEMA,
        }), sleeper)
        created = None
        try:
            created = _complete_database(mcp, parse_database(response))
        except PilotError:
            created = locate([parent, *pages])
        if not created:
            raise PilotError("사람 판단 데이터베이스 주소를 확인하지 못했습니다.")
        return _remember_database(state, generation, created)

    if not hasattr(state, "lease"):
        return create()
    with lease_retry(state, 120, NOTION_MODERATION_LEASE, attempts=4, wait_seconds=2, sleeper=sleeper):
        return create()


def _source_id(database):
    return database["data_source_url"].replace("collection://", "")


def _query(mcp, database, where="", params=None):
    source = database["data_source_url"]
    sql = 'SELECT url, "댓글 ID", "결정", "상태", "작성일(KST)" FROM "' + source + '"'
    if where:
        sql += " WHERE " + where
    sql += " LIMIT 100"
    result = mcp.call("query-data-sources", {"data": {
        "mode": "sql", "data_source_urls": [source], "query": sql, "params": params or []}})
    rows = result.get("results") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        raise PilotError("사람 판단 데이터베이스 조회 결과를 확인하지 못했습니다.")
    return [_notion_row(row) for row in rows if isinstance(row, dict)]


def _plain(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return " ".join(_plain(item) for item in value).strip()
    if isinstance(value, dict):
        if isinstance(value.get("name"), str):
            return value["name"].strip()
        return _plain(value.get("plain_text") or value.get("text") or value.get("content"))
    return str(value).strip()


def _notion_row(row):
    return {"url": _plain(row.get("url")), "comment_id": _plain(row.get("댓글 ID")),
            "decision": _plain(row.get("결정")), "status": _plain(row.get("상태")),
            "written": _plain(row.get("작성일(KST)"))}


def _properties(item, status):
    values = {"댓글 원문": item["text"][:1800], "작성자": item.get("author") or "",
              "작성일(KST)": item.get("written") or "", "규칙/판정 이유": item.get("reason") or "",
              "댓글 ID": item["comment_id"], "상태": status}
    if item.get("account") in ACCOUNTS:
        values["계정"] = item["account"]
    if item.get("permalink", "").startswith("https://"):
        values["게시물 링크"] = item["permalink"]
    return values


def add_pending(mcp, database, items, known=None):
    known = known if known is not None else {row["comment_id"]: row for row in _query(mcp, database)}
    fresh = []
    for item in items:
        if item["comment_id"] in known:
            continue
        existing = _query(mcp, database, '"댓글 ID" = ?', [item["comment_id"]])
        if existing:
            known[item["comment_id"]] = existing[0]
            continue
        fresh.append(item)
        if len(fresh) >= 30:
            break
    if not fresh:
        return []
    result = _finish_async(mcp, mcp.call("create-pages", {
        "parent": {"type": "data_source_id", "data_source_id": _source_id(database)},
        "pages": [{"properties": _properties(item, "사람 판단 대기")} for item in fresh],
        "allow_async": False,
    }), time.sleep)
    if isinstance(result, dict) and result.get("warnings"):
        raise PilotError("사람 판단 행 추가에 Notion 경고가 있어 완료를 보류합니다.")
    _log("notion_pending_added", count=len(fresh))
    return fresh


def pull_decisions(sheet, mcp, database, state=None):
    """Copy a Notion choice into an empty sheet cell. Never replace a sheet choice."""
    outcomes = []
    for row in _query(mcp, database, '"결정" = ? OR "결정" = ?', ["숨김", "유지"]):
        if row["decision"] not in {"숨김", "유지"} or not row["comment_id"] or not row["url"]:
            continue
        if row["status"].startswith("시트와 다름"):
            outcomes.append({"comment_id": row["comment_id"], "result": "conflict"})
            continue
        outcome = set_decision_if_blank(sheet, row["comment_id"], row["decision"], state=state)
        outcomes.append({"comment_id": row["comment_id"], **outcome})
        if outcome["result"] == "missing" and row["status"] != "시트에서 댓글을 찾지 못함":
            _set_status(mcp, row["url"], "시트에서 댓글을 찾지 못함")
        elif outcome["result"] == "conflict":
            status = "시트와 다름: 시트는 " + (outcome["sheet_decision"] or "다른 값")
            if row["status"] != status:
                _set_status(mcp, row["url"], status)
            _log("notion_decision_conflict", comment_id=row["comment_id"], sheet=outcome["sheet_decision"])
        elif outcome["result"] == "written":
            _log("notion_decision_pulled", comment_id=row["comment_id"], decision=row["decision"])
    return outcomes


def _set_properties(mcp, page_id, properties):
    result = _finish_async(mcp, mcp.call("update-page", {
        "page_id": page_id, "command": "update_properties",
        "properties": properties, "allow_async": False,
    }), time.sleep)
    if isinstance(result, dict) and result.get("warnings"):
        raise PilotError("Notion 상태 기록에 경고가 있어 완료를 보류합니다.")


def _set_status(mcp, page_id, status):
    _set_properties(mcp, page_id, {"상태": status})


def _sheet_written(sheet, comment_id):
    for title in (REVIEW_TAB, HIDE_TAB):
        rows = _read_rows(sheet, title)
        index = find_comment_row(rows, comment_id)
        if index is None:
            continue
        written = _cell(rows[index], 2)
        if written:
            return written
    return ""


def fill_written_dates(sheet, mcp, database):
    """Copy a blank Notion 작성일 from the sheet's 댓글 작성일 column. Do not overwrite a value."""
    filled = []
    for row in _query(mcp, database):
        if row.get("written") or not row.get("comment_id") or not row.get("url"):
            continue
        written = _sheet_written(sheet, row["comment_id"])
        if not written:
            continue
        _set_properties(mcp, row["url"], {"작성일(KST)": written})
        filled.append(row["comment_id"])
        _log("notion_written_at_filled", comment_id=row["comment_id"])
    return filled


def push_results(sheet, mcp, database, now=None):
    """Write the sheet result back onto the Notion row. The sheet is not changed here."""
    rows = _read_rows(sheet, REVIEW_TAB)
    updated = []
    for notion in _query(mcp, database, '"결정" = ? OR "결정" = ?', ["숨김", "유지"]):
        if not notion["url"] or not notion["comment_id"]:
            continue
        index = find_comment_row(rows, notion["comment_id"])
        if index is None:
            status = "시트에서 댓글을 찾지 못함"
        else:
            sheet_decision = _cell(rows[index], DECISION_COLUMN)
            if sheet_decision and sheet_decision != notion["decision"]:
                status = "시트와 다름: 시트는 " + sheet_decision
            else:
                status = sheet_memo_status(_cell(rows[index], MEMO_COLUMN), now)
        if not status or status == notion["status"]:
            continue
        _set_status(mcp, notion["url"], status)
        updated.append({"comment_id": notion["comment_id"], "status": status})
        _log("notion_decision_status", comment_id=notion["comment_id"], status=status)
    return updated


def _develop_section(content):
    """Root ### Develop span, ending at the next root heading. Nested toggles stay inside."""
    depth = 0
    fence = None
    offset = 0
    start = None
    heading_end = None
    for line in content.splitlines(keepends=True):
        end = offset + len(line)
        marker = re.match(r"\s*(`{3,}|~{3,})", line)
        if marker and depth == 0:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            offset = end
            continue
        if fence is None:
            if depth == 0 and DEVELOP_HEADING.match(line.strip()):
                if start is None:
                    start = offset
                    heading_end = end
                else:
                    return start, offset, heading_end
            elif depth == 0 and start is not None and ROOT_HEADING.match(line.strip()):
                return start, offset, heading_end
            for match in re.finditer(r"<(/?)(?:toggle|callout|quote|columns|column|ul|ol|li|table|tr|td|th|synced-block)\b[^>]*?/?>", line):
                if match.group(0).endswith("/>"):
                    continue
                depth = max(0, depth - 1) if match.group(1) else depth + 1
        offset = end
    if start is None:
        raise PilotError("일일 업무의 Develop 구역을 찾지 못했습니다.")
    return start, len(content), heading_end


def _last_report_toggle(section):
    depth = 0
    offset = 0
    open_at = None
    title = None
    found = None
    for line in section.splitlines(keepends=True):
        stripped = line.strip()
        if depth == 0 and stripped.startswith("<toggle"):
            open_at = offset
            title = None
        if open_at is not None and title is None and depth == 1 and stripped and not stripped.startswith("<"):
            title = stripped
        for match in re.finditer(r"<(/?)toggle\b[^>]*?/?>", line):
            if match.group(0).endswith("/>"):
                continue
            if match.group(1):
                depth -= 1
                if depth == 0 and open_at is not None:
                    block = section[open_at:offset + match.end()]
                    label = re.sub(r"</?b>", "", title or "")
                    if label.startswith(REPORT_MARK) or "보고서" in label:
                        found = block
                    open_at = None
                    title = None
            else:
                depth += 1
        offset += len(line)
    return found


def _unsafe_anchor(text):
    """Blocks Notion refuses to round-trip: unknown blocks, file images, and database tags."""
    return "<" in text or "notion-file-block" in text


def _insert_anchor(content, report_block):
    """A unique plain-text edge after one report toggle. The toggle body is not rewritten."""
    at = content.find(report_block)
    if at < 0 or not report_block.endswith("</toggle>"):
        raise PilotError("보고서 토글 끝을 확인하지 못했습니다.")
    end = at + len(report_block)
    close = "</toggle>"
    prefix_end = end - len(close)
    previous_tag = content.rfind(">", 0, prefix_end)
    prefix = content[previous_tag + 1:prefix_end] if previous_tag >= 0 else ""
    # </toggle> then --- is repeated in later sections. Extend through the next
    # plain heading, such as ### Contents schedule, until the boundary is unique.
    tails = [""]
    gathered = ""
    pos = end
    while pos < len(content):
        newline = content.find("\n", pos)
        line = content[pos:] if newline < 0 else content[pos:newline + 1]
        if "<" in line or "notion-file-block" in line:
            break
        gathered += line
        tails.append(gathered)
        pos += len(line)
        if line.strip().startswith("#") or newline < 0:
            break
    for tail in tails:
        for index in range(len(prefix) + 1):
            piece = prefix[index:]
            if _unsafe_anchor(piece) or _unsafe_anchor(tail):
                continue
            anchor = piece + close + tail
            if len(anchor) <= len(close):
                continue
            if content.count(anchor) == 1:
                return anchor, tail
    raise PilotError("토글을 넣을 위치를 다른 블록을 건드리지 않고 정하지 못했습니다.")


def toggle_updates(content, day, hidden, pending_count, database):
    """Insert one toggle after the report toggles. Leave every other block unchanged."""
    if toggle_title(day) in content:
        return []
    start, end, heading_end = _develop_section(content)
    section = content[start:end]
    toggle = toggle_markdown(day, hidden, pending_count, database)
    if "<database" in toggle:
        raise PilotError("사람 판단 표를 새로 만들지 않습니다.")
    report = _last_report_toggle(section)
    if report:
        if content.count(report) != 1:
            raise PilotError("보고서 토글이 겹쳐 위치를 정하지 못했습니다.")
        anchor, tail = _insert_anchor(content, report)
        head = anchor[:-len(tail)] if tail else anchor
        new = head + "\n\n" + toggle + (("\n" + tail) if tail else "")
        return [{"old_str": anchor, "new_str": new}]
    heading = content[start:heading_end]
    if content.count(heading) != 1 or _unsafe_anchor(heading.replace("###", "", 1)):
        raise PilotError("Develop 제목만으로 토글 위치를 정하지 못했습니다.")
    return [{"old_str": heading, "new_str": heading + "\n" + toggle + "\n"}]


def _place_toggle(mcp, page, day, hidden, pending_count, database, sleeper):
    text = _page_text(page)
    if page.get("truncated") and toggle_title(day) not in text:
        raise PilotError("일일 업무 페이지가 잘려 오늘 토글이 있는지 확인하지 못했습니다.")
    updates = toggle_updates(text, day, hidden, pending_count, database)
    if not updates:
        return "unchanged"
    result = _finish_async(mcp, mcp.call("update-page", {
        "page_id": page["id"], "command": "update_content", "content_updates": updates, "allow_async": False,
    }), sleeper)
    if isinstance(result, dict) and result.get("warnings"):
        raise PilotError("일일 업무 토글 추가에 Notion 경고가 있어 완료를 보류합니다.")
    checked = mcp.call("fetch", {"id": page["id"]})
    if toggle_title(day) not in _page_text(checked):
        raise PilotError("일일 업무에 댓글 관리 토글이 반영됐는지 확인하지 못했습니다.")
    return "inserted"


def publish_daily_summary(settings, state, sheet, mcp, finder, now=None, attempts=None, wait_seconds=None, sleeper=time.sleep):
    """Append today's toggle. Retry when the daily page does not exist yet."""
    options = _notion_settings(settings)
    attempts = int(attempts if attempts is not None else options.get("summary_attempts") or 4)
    wait_seconds = int(wait_seconds if wait_seconds is not None else options.get("summary_wait_seconds") or 300)
    if is_weekend(now):
        _log("notion_summary_weekend_skip", message="weekend skip", day=_today(now).isoformat())
        return {"status": "weekend_skip", "day": _today(now).isoformat()}
    day = _today(now).isoformat()
    start, end = coverage_period(state, now)
    page = None
    for attempt in range(attempts):
        try:
            page = finder(day)
            break
        except NoDailyPage:
            _log("notion_summary_waiting", day=day, attempt=attempt + 1, attempts=attempts)
            if attempt + 1 >= attempts:
                return {"status": "waiting", "day": day}
            sleeper(wait_seconds)
    if not isinstance(page, dict) or not page.get("id"):
        raise PilotError("일일 업무 페이지를 확인하지 못했습니다.")
    database = ensure_database(mcp, state, settings, sleeper=sleeper, pages=(page,))
    fresh = mcp.call("fetch", {"id": page["id"]})
    if isinstance(fresh, dict) and isinstance(fresh.get("text"), str):
        page = {**page, "text": fresh["text"], "truncated": fresh.get("truncated")}
    hidden = hidden_between(_read_rows(sheet, HIDE_TAB), start, end)
    pending = period_pending(_read_rows(sheet, REVIEW_TAB), start, end)
    added = add_pending(mcp, database, pending)
    fill_written_dates(sheet, mcp, database)
    outcome = _place_toggle(mcp, page, day, hidden, len(pending), database, sleeper)
    if outcome in {"inserted", "unchanged"}:
        remember_coverage(state, end)
    _log("notion_summary_" + outcome, day=day, hidden=hidden["total"], pending=len(pending),
         start=start, end=end)
    return {"status": outcome, "day": day, "page_id": page["id"], "hidden": hidden["total"],
            "pending": len(pending), "added": len(added), "database": database, "start": start, "end": end}


def _open_locked(settings, state, attempts=4, wait_seconds=15, sleeper=time.sleep):
    """Refresh Notion on its own lock. A busy lock is retried and does not wait on daily recover."""
    from .cloud_state import NOTION_OAUTH_LEASE, CloudError, lease_retry
    if not hasattr(state, "lease"):
        return open_notion(settings, state)
    try:
        with lease_retry(state, 60, NOTION_OAUTH_LEASE, attempts=attempts, wait_seconds=wait_seconds, sleeper=sleeper):
            return open_notion(settings, state)
    except CloudError as error:
        _log("notion_decision_deferred", reason=str(error), attempts=attempts)
        return None


def open_notion(settings, state):
    from .mcp import McpClient
    from .notion_mcp import NotionMcpClient
    from .notion_oauth import cloud_credentials
    secret = os.environ.get("SNS_NOTION_OAUTH_SECRET", "")
    source = (settings.get("notion") or {}).get("daily_data_source_id")
    if not secret or not source:
        raise PilotError("Notion 연결 설정을 확인해주세요.")
    auth = cloud_credentials(state.transport, secret)
    auth.refresh()
    client = McpClient(auth)
    reader = NotionMcpClient(client, source)
    return client, reader


def prepare_notion_decisions(settings, state, sheet):
    """Read Notion choices before the sheet hide/keep pass. Failures stay non-fatal."""
    if not notion_decisions_enabled() or sheet is None:
        return None
    try:
        opened = _open_locked(settings, state)
        if opened is None:
            return None
        mcp, _reader = opened
        database = ensure_database(mcp, state, settings)
        pulled = pull_decisions(sheet, mcp, database, state=state)
        return {"mcp": mcp, "database": database, "pulled": pulled}
    except PilotError as error:
        _log("notion_decision_unavailable", error=str(error))
        return None


def complete_notion_decisions(context, sheet, now=None):
    """Write sheet results back and add comments that started waiting during the day."""
    if not context or sheet is None:
        return None
    try:
        pushed = push_results(sheet, context["mcp"], context["database"], now=now)
        added = add_pending(context["mcp"], context["database"], pending_reviews(_read_rows(sheet, REVIEW_TAB)))
        filled = fill_written_dates(sheet, context["mcp"], context["database"])
        return {"pushed": pushed, "added": len(added), "filled": len(filled)}
    except PilotError as error:
        _log("notion_decision_unavailable", error=str(error))
        return None


def run_notion_summary(settings, state, now=None, attempts=None, wait_seconds=None, sleeper=time.sleep):
    if not notion_decisions_enabled():
        _log("notion_summary_off")
        return {"status": "off"}
    if is_weekend(now):
        _log("notion_summary_weekend_skip", message="weekend skip", day=_today(now).isoformat())
        return {"status": "weekend_skip", "day": _today(now).isoformat()}
    from .moderation_job import open_sheet
    sheet = open_sheet(settings, state)
    if sheet is None:
        _log("notion_summary_unavailable", error="기준표 시트를 열 수 없습니다.")
        return {"status": "failed"}
    opened = _open_locked(settings, state)
    if opened is None:
        return {"status": "deferred"}
    mcp, reader = opened

    def finder(day):
        page = reader.daily_page(day)
        return {"id": page["id"], "url": page.get("url"), "text": page.get("text") or body(page),
                "truncated": page.get("truncated")}

    return publish_daily_summary(settings, state, sheet, mcp, finder, now=now,
                                 attempts=attempts, wait_seconds=wait_seconds, sleeper=sleeper)
