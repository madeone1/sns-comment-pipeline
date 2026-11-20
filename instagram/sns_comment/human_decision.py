"""Apply a person's 숨김/유지 choice from the rulebook '사람 판단' tab.

The live sheet has no separate 처리상태 column. Completion is written in 메모,
which is how the 2026-11-11 rows already record '숨김 완료'. Rows that were
finished by hand have no job marker, so they are left untouched. Hide runs only
when SNS_HUMAN_DECISION_HIDE=1. Nothing is deleted and nothing is un-hidden.
"""

import datetime as dt
import json
import os
import re

from .api import GraphError, PilotError
from .moderation_sheet import REVIEW_COLUMNS, REVIEW_SHEET_ID, REVIEW_TAB, _read_rows, _text, sheet_section
from .sheets import cell


KST = dt.timezone(dt.timedelta(hours=9))
DECISION_COLUMN = 11
MEMO_COLUMN = 12
COMMENT_COLUMN = 6
FIRST_DATA_ROW = 2
MAX_ATTEMPTS = 3
PENDING_MARKERS = ("자동 감지", "사람 판단 대기", "숨김 예정(시험)")
ATTEMPT = re.compile(r"숨김 실패 \((\d+)/(\d+)\)")


def human_hide_enabled():
    return os.environ.get("SNS_HUMAN_DECISION_HIDE", "").strip() == "1"


def _log(event, **values):
    print(json.dumps({"event": event, **values}, ensure_ascii=False), flush=True)


def _metadata(client):
    value = client.request(query={"fields": "sheets.properties"})
    if not isinstance(value, dict):
        raise PilotError("시트 목록 응답이 올바르지 않습니다.")
    return value


def _review_sheet(metadata):
    for sheet in metadata.get("sheets") or []:
        prop = sheet.get("properties") if isinstance(sheet, dict) else None
        if isinstance(prop, dict) and prop.get("title") == REVIEW_TAB and prop.get("sheetId") == REVIEW_SHEET_ID:
            return prop
    raise PilotError("'사람 판단' 탭을 찾지 못했습니다.")


def ensure_decision_dropdown(client):
    """Put a 숨김/유지 dropdown on column L. Existing cell values are not written."""
    prop = _review_sheet(_metadata(client))
    row_count = int((prop.get("gridProperties") or {}).get("rowCount") or 1000)
    if row_count <= FIRST_DATA_ROW:
        return False
    client.request(":batchUpdate", body={"requests": [{"setDataValidation": {"range": {
        "sheetId": REVIEW_SHEET_ID, "startRowIndex": FIRST_DATA_ROW, "endRowIndex": row_count,
        "startColumnIndex": DECISION_COLUMN, "endColumnIndex": DECISION_COLUMN + 1},
        "rule": {"condition": {"type": "ONE_OF_LIST", "values": [
            {"userEnteredValue": "숨김"}, {"userEnteredValue": "유지"}]},
            "showCustomUi": True, "strict": True}}}]})
    return True


def _memo_state(memo):
    if "숨김 완료" in memo or "유지 확정" in memo or "숨김 실패 (중단)" in memo:
        return "done"
    match = ATTEMPT.search(memo)
    if match:
        return "done" if int(match.group(1)) >= int(match.group(2)) else "pending"
    if any(marker in memo for marker in PENDING_MARKERS):
        return "pending"
    return "manual"


def _actionable(decision, memo):
    if decision not in {"숨김", "유지"}:
        return False
    return _memo_state(memo) == "pending"


def _permanent(error):
    return isinstance(error, GraphError) and (error.status in {400, 404} and error.code in {100, 803} or error.status == 404)


def _stamp(now):
    moment = now or dt.datetime.now(dt.timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(KST).strftime("%Y-%m-%d %H:%M")


def _attempts(memo):
    match = ATTEMPT.search(memo)
    if not match:
        return 0
    return int(match.group(1))


def find_comment_row(rows, comment_id):
    """Row index of a comment ID in column G. Header and note rows are never returned."""
    for index, row in enumerate(rows):
        if index < FIRST_DATA_ROW:
            continue
        found = _text(row[COMMENT_COLUMN]) if len(row) > COMMENT_COLUMN else ""
        if found == comment_id:
            return index
    return None


def set_decision_if_blank(client, comment_id, decision, state=None):
    """Write 담당자 결정 only when that cell is empty. The row is found again inside the lock."""
    if decision not in {"숨김", "유지"}:
        return {"result": "skip", "sheet_decision": ""}
    with sheet_section(state, REVIEW_TAB):
        rows = _read_rows(client, REVIEW_TAB)
        index = find_comment_row(rows, comment_id)
        if index is None:
            return {"result": "missing", "sheet_decision": ""}
        current = _text(rows[index][DECISION_COLUMN]) if len(rows[index]) > DECISION_COLUMN else ""
        if current:
            return {"result": "same" if current == decision else "conflict", "sheet_decision": current}
        client.request(":batchUpdate", body={"requests": [_cell(REVIEW_SHEET_ID, index, DECISION_COLUMN, decision)]})
        return {"result": "written", "sheet_decision": decision}


def _cell(sheet_id, row_index, column, value):
    if row_index < FIRST_DATA_ROW:
        raise PilotError("사람 판단 안내·머리글 행은 바꾸지 않습니다.")
    return {"updateCells": {"range": {"sheetId": sheet_id, "startRowIndex": row_index, "endRowIndex": row_index + 1,
            "startColumnIndex": column, "endColumnIndex": column + 1},
            "rows": [{"values": [cell(value)]}], "fields": "userEnteredValue"}}


def _confirm_hidden(graph, comment_id):
    if graph is None or not hasattr(graph, "get"):
        return False
    checked = graph.get(comment_id, fields="hidden")
    return isinstance(checked, dict) and checked.get("hidden") is True


def _failure_memo(now, memo, reason, permanent):
    if permanent:
        return _stamp(now) + " KST 숨김 실패 (중단): " + reason
    count = _attempts(memo) + 1
    return _stamp(now) + " KST 숨김 실패 (" + str(count) + "/" + str(MAX_ATTEMPTS) + "): " + reason


def _write_memos(client, updates, state):
    """Find each comment ID again immediately before writing. Do not reuse an earlier index."""
    if not updates:
        return 0
    with sheet_section(state, REVIEW_TAB):
        fresh = _read_rows(client, REVIEW_TAB)
        requests = []
        for comment_id, memo in updates:
            index = find_comment_row(fresh, comment_id)
            if index is None:
                continue
            requests.append(_cell(REVIEW_SHEET_ID, index, MEMO_COLUMN, memo))
        if requests:
            client.request(":batchUpdate", body={"requests": requests})
        return len(requests)


def apply_human_decisions(client, graph, now=None, hide_enabled=None, state=None):
    """Scan 사람 판단. Blank decisions and already finished manual rows are skipped."""
    if hide_enabled is None:
        hide_enabled = human_hide_enabled()
    try:
        ensure_decision_dropdown(client)
    except PilotError as error:
        _log("human_decision_dropdown_failed", error=str(error))
    rows = _read_rows(client, REVIEW_TAB)
    header = [_text(value) for value in rows[1]] if len(rows) > 1 else []
    if header[:len(REVIEW_COLUMNS)] != REVIEW_COLUMNS:
        raise PilotError("'사람 판단' 머리글이 기준표와 다릅니다.")
    plans, updates, hidden_ids = [], [], set()
    for row in rows[FIRST_DATA_ROW:]:
        decision = _text(row[DECISION_COLUMN]) if len(row) > DECISION_COLUMN else ""
        memo = _text(row[MEMO_COLUMN]) if len(row) > MEMO_COLUMN else ""
        comment_id = _text(row[COMMENT_COLUMN]) if len(row) > COMMENT_COLUMN else ""
        if not _actionable(decision, memo):
            continue
        if find_comment_row(rows, comment_id) is None or not re.fullmatch(r"[0-9]{1,30}", comment_id) or comment_id in hidden_ids:
            plans.append({"comment_id": comment_id, "decision": decision, "action": "skip"})
            continue
        if decision == "유지":
            updates.append((comment_id, _stamp(now) + " KST 유지 확정"))
            plans.append({"comment_id": comment_id, "decision": decision, "action": "keep"})
            _log("human_decision", comment_id=comment_id, decision=decision, action="keep")
            continue
        if not hide_enabled:
            if "숨김 예정(시험)" not in memo:
                updates.append((comment_id, _stamp(now) + " KST 숨김 예정(시험)"))
            plans.append({"comment_id": comment_id, "decision": decision, "action": "would_hide"})
            _log("human_decision", message="would hide", comment_id=comment_id, decision=decision, action="would_hide")
            continue
        try:
            graph.hide_comment(comment_id)
            if not _confirm_hidden(graph, comment_id):
                raise PilotError("숨김 후 hidden=true를 확인하지 못했습니다.")
        except (GraphError, PilotError) as error:
            permanent = _permanent(error)
            reason = "댓글을 찾지 못했습니다" if permanent else "숨김 요청 실패"
            if isinstance(error, GraphError) and error.status == 429:
                reason = "API 호출 제한"
            updates.append((comment_id, _failure_memo(now, memo, reason, permanent)))
            plans.append({"comment_id": comment_id, "decision": decision, "action": "failed", "permanent": permanent})
            _log("human_decision_failed", comment_id=comment_id, error=reason, permanent=permanent)
            continue
        updates.append((comment_id, _stamp(now) + " KST 숨김 완료"))
        hidden_ids.add(comment_id)
        plans.append({"comment_id": comment_id, "decision": decision, "action": "hidden"})
        _log("human_decision", comment_id=comment_id, decision=decision, action="hidden")
    return {"plans": plans, "updates": _write_memos(client, updates, state)}
