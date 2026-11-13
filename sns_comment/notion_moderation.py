"""Daily Notion summary and Notion choices for comments that need a person.

The 09:00 job appends one toggle to today's 일일 업무 page. It does not create
that page. The 10-minute job, only when SNS_NOTION_DECISIONS=1, copies a Notion
숨김/유지 choice into the rulebook sheet when that cell is still empty, then
the existing sheet logic hides or confirms 유지. The sheet stays the source of
truth. Nothing is deleted and nothing is un-hidden.
"""

import datetime as dt
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
ATTR = re.compile(r'([\w:-]+)="([^"]*)"')


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


def hidden_yesterday(rows, yesterday):
    """Count 숨김 완료 rows whose note date is yesterday. Other statuses stay out."""
    counts = {name: 0 for name in ACCOUNTS}
    for row in rows[FIRST_DATA_ROW:]:
        if _cell(row, 11) != "숨김 완료":
            continue
        match = NOTE_DAY.match(_cell(row, 12))
        if not match or match.group(1) != yesterday:
            continue
        name = account_name(_cell(row, 1))
        if name:
            counts[name] += 1
    return {"total": sum(counts.values()), "by_account": counts}


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
    lines = ["<toggle>", "💬 <b>" + toggle_title(day) + "</b>", "",
             "어제 숨긴 댓글 " + str(hidden["total"]) + "건", ""]
    for name in ACCOUNTS:
        lines.append("- " + name + " " + str(hidden["by_account"][name]) + "건")
    lines.extend(["", "[숨김 대상 시트](" + HIDE_URL + ")", "",
                  "사람 판단 필요 " + str(pending_count) + "건",
                  "아래 표에서 숨김 또는 유지를 고릅니다. 시트에 이미 결정이 있으면 시트를 유지합니다.",
                  "", "[사람 판단 시트](" + REVIEW_URL + ")"])
    if database:
        lines.extend(["", '<database url="' + database["url"] + '" data-source-url="' + database["data_source_url"]
                      + '" inline="true">' + DATABASE_TITLE + "</database>"])
    lines.append("</toggle>")
    return "\n".join(lines)


def _attrs(text):
    return {key: value for key, value in ATTR.findall(text)}


def parse_database(result):
    text = result.get("text") if isinstance(result, dict) else ""
    matches = []
    for attrs, title in DATABASE_TAG.findall(text or ""):
        values = _attrs(attrs)
        if values.get("url") and values.get("data-source-url"):
            matches.append((title.strip(), {"url": values["url"], "data_source_url": values["data-source-url"]}))
    titled = [item for item in matches if item[0] == DATABASE_TITLE]
    if len(titled) == 1:
        return titled[0][1]
    if len(matches) == 1:
        return matches[0][1]
    raise PilotError("사람 판단 데이터베이스 주소를 확인하지 못했습니다.")


def _page_text(page):
    if not isinstance(page, dict):
        raise PilotError("Notion 페이지 응답을 확인하지 못했습니다.")
    return page.get("text") or ""


def find_database_on_page(page):
    for attrs, title in DATABASE_TAG.findall(_page_text(page)):
        if title.strip() != DATABASE_TITLE:
            continue
        values = _attrs(attrs)
        if values.get("url") and values.get("data-source-url"):
            return {"url": values["url"], "data_source_url": values["data-source-url"]}
    return None


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


def ensure_database(mcp, state, settings, sleeper=time.sleep):
    from .cloud_state import NOTION_MODERATION_LEASE, lease_retry
    def create():
        current, generation = state.json_get(STATE_NAME)
        if isinstance(current, dict) and current.get("url") and current.get("data_source_url"):
            return current
        parent = parent_page_id(settings)
        found = find_database_on_page(mcp.call("fetch", {"id": parent}))
        created = found or parse_database(_finish_async(mcp, mcp.call("create-database", {
            "parent": {"type": "page_id", "page_id": parent},
            "title": DATABASE_TITLE,
            "schema": SCHEMA,
        }), sleeper))
        state.json_put(STATE_NAME, created, generation=generation)
        return created
    if not hasattr(state, "lease"):
        return create()
    with lease_retry(state, 120, NOTION_MODERATION_LEASE, attempts=4, wait_seconds=2, sleeper=sleeper):
        return create()


def _source_id(database):
    return database["data_source_url"].replace("collection://", "")


def _query(mcp, database, where="", params=None):
    source = database["data_source_url"]
    sql = 'SELECT url, "댓글 ID", "결정", "상태" FROM "' + source + '"'
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
            "decision": _plain(row.get("결정")), "status": _plain(row.get("상태"))}


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


def _set_status(mcp, page_id, status):
    result = _finish_async(mcp, mcp.call("update-page", {
        "page_id": page_id, "command": "update_properties",
        "properties": {"상태": status}, "allow_async": False,
    }), time.sleep)
    if isinstance(result, dict) and result.get("warnings"):
        raise PilotError("Notion 상태 기록에 경고가 있어 완료를 보류합니다.")


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


def _append_toggle(mcp, page, day, hidden, pending_count, database, sleeper):
    if page.get("truncated") and toggle_title(day) not in _page_text(page):
        raise PilotError("일일 업무 페이지가 잘려 오늘 토글이 있는지 확인하지 못했습니다.")
    if toggle_title(day) in _page_text(page):
        return "unchanged"
    result = _finish_async(mcp, mcp.call("update-page", {
        "page_id": page["id"], "command": "insert_content", "position": {"type": "end"},
        "content": toggle_markdown(day, hidden, pending_count, database), "allow_async": False,
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
    day = _today(now).isoformat()
    yesterday = (_today(now) - dt.timedelta(days=1)).isoformat()
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
    database = ensure_database(mcp, state, settings, sleeper=sleeper)
    hidden = hidden_yesterday(_read_rows(sheet, HIDE_TAB), yesterday)
    pending = pending_reviews(_read_rows(sheet, REVIEW_TAB))
    added = add_pending(mcp, database, pending)
    outcome = _append_toggle(mcp, page, day, hidden, len(pending), database, sleeper)
    _log("notion_summary_" + outcome, day=day, hidden=hidden["total"], pending=len(pending))
    return {"status": outcome, "day": day, "page_id": page["id"], "hidden": hidden["total"],
            "pending": len(pending), "added": len(added), "database": database}


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
        return {"pushed": pushed, "added": len(added)}
    except PilotError as error:
        _log("notion_decision_unavailable", error=str(error))
        return None


def run_notion_summary(settings, state, now=None, attempts=None, wait_seconds=None, sleeper=time.sleep):
    if not notion_decisions_enabled():
        _log("notion_summary_off")
        return {"status": "off"}
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
