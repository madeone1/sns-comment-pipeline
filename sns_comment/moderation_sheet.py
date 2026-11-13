"""Insert moderation rows under the rulebook header. Never edit existing rows.

Row 1 is a note, row 2 is the header, and the newest comment is inserted at row 3.
Older rows keep their order and their 번호. Comment IDs already in column G are
left untouched. A sheet error must not stop classification.
"""

import contextlib
import datetime as dt
import urllib.parse

from .api import PilotError
from .cloud_state import SHEET_HIDE_LEASE, SHEET_REVIEW_LEASE, lease_retry
from .sheets import cell


KST = dt.timezone(dt.timedelta(hours=9))
HIDE_TAB = "숨김 대상"
REVIEW_TAB = "사람 판단"
HIDE_SHEET_ID = 123926565
REVIEW_SHEET_ID = 715862237
HIDE_COLUMNS = ["번호", "계정/탭", "댓글 작성일(KST)", "작성자", "댓글 원문", "게시물 URL", "댓글 ID",
                "원댓글 ID", "규칙 ID", "판정 이유", "확신도", "처리상태", "비고"]
REVIEW_COLUMNS = ["번호", "계정/탭", "댓글 작성일(KST)", "작성자", "댓글 원문", "게시물 URL", "댓글 ID",
                  "원댓글 ID", "규칙 ID", "판정 이유", "확신도", "담당자 결정", "메모"]
COMMENT_COLUMN = 6
HEADER_ROW = 1
FIRST_DATA_ROW = 2
ACCOUNT_TABS = {
    "서울": "서울 댓글",
    "대표(공식)": "서울 댓글",
    "대표": "서울 댓글",
    "부산": "부산 댓글",
    "속초": "속초 댓글",
    "토론토": "토론토 댓글",
    "waterbomb_official": "서울 댓글",
    "waterbomb_busan_official": "부산 댓글",
    "waterbomb_sokcho_official": "속초 댓글",
    "waterbomb_toronto_official": "토론토 댓글",
}


def display_status(record):
    status = record.get("status")
    if status == "would_hide":
        return "숨겼을 것(시험)"
    if status == "hidden":
        return "숨김 완료"
    if status == "hide_failed":
        return "숨김 실패"
    if status == "human_review":
        return "사람판단"
    return None


def tab_for(record):
    if record.get("status") in {"would_hide", "hidden", "hide_failed"}:
        return HIDE_TAB
    if record.get("status") == "human_review":
        return REVIEW_TAB
    return None


def needs_sheet(record):
    shown = display_status(record)
    return bool(tab_for(record)) and shown is not None and record.get("sheet_synced_status") != shown


def _columns(title):
    if title == HIDE_TAB:
        return HIDE_COLUMNS
    if title == REVIEW_TAB:
        return REVIEW_COLUMNS
    raise PilotError("알 수 없는 기준표 탭입니다.")


def _sheet_id(title):
    return HIDE_SHEET_ID if title == HIDE_TAB else REVIEW_SHEET_ID


def _parsed(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(KST)
    except ValueError:
        return None


def _clock(value):
    parsed = _parsed(value)
    return parsed.strftime("%Y-%m-%d %H:%M") if parsed else ""


def account_tab(record):
    region = record.get("region") if isinstance(record.get("region"), str) else ""
    username = record.get("account_username") if isinstance(record.get("account_username"), str) else ""
    if region in ACCOUNT_TABS:
        return ACCOUNT_TABS[region]
    if username in ACCOUNT_TABS:
        return ACCOUNT_TABS[username]
    if region.endswith(" 댓글") or region.endswith("댓글"):
        return region
    if region:
        return region + " 댓글"
    return username


def _confidence(record):
    value = record.get("confidence")
    return {"high": "높음", "medium": "보통", "low": "낮음", "높음": "높음", "보통": "보통", "낮음": "낮음"}.get(value, "보통" if value else "")


def _note(record, hide_tab):
    when = _clock(record.get("updated_at") or record.get("classified_at"))
    if hide_tab:
        text = (when + " KST 자동 감지(10분 작업)").strip()
        if record.get("status") == "hidden" and record.get("hidden_check"):
            text += " " + str(record["hidden_check"])
        if record.get("status") == "hide_failed" and record.get("error"):
            text += " " + " ".join(str(record["error"]).split())[:80]
        return text
    return (when + " KST 자동 감지").strip()


def row_values(record, number):
    hide_tab = tab_for(record) == HIDE_TAB
    # Column C is the Instagram comment time only. Records saved before that field existed stay blank.
    values = [str(number), account_tab(record), _clock(record.get("written_at")), record.get("author_username") or "",
              record.get("text") or "", record.get("permalink") or "", str(record.get("comment_id") or ""),
              record.get("parent_id") or "", record.get("rule_id") or "", record.get("reason") or "",
              _confidence(record)]
    if hide_tab:
        values.extend([display_status(record) or "", _note(record, True)])
    else:
        values.extend(["", _note(record, False)])
    return values


def _text(value):
    if value is None or value == "":
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return str(value)


def _number(value):
    text = _text(value).strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    if number < 0 or not number.is_integer():
        return None
    return int(number)


def _metadata(client):
    value = client.request(query={"fields": "sheets.properties"})
    if not isinstance(value, dict):
        raise PilotError("시트 목록 응답이 올바르지 않습니다.")
    return value


def _resolve(metadata, title):
    expected = _sheet_id(title)
    found = None
    row_count = 0
    for sheet in metadata.get("sheets") or []:
        prop = sheet.get("properties") if isinstance(sheet, dict) else None
        if not isinstance(prop, dict):
            continue
        if prop.get("title") == title and isinstance(prop.get("sheetId"), int):
            found = prop["sheetId"]
            row_count = int((prop.get("gridProperties") or {}).get("rowCount") or 0)
            break
    if found is None:
        raise PilotError("'" + title + "' 탭이 기준표 시트에 없습니다.")
    if found != expected:
        raise PilotError("'" + title + "' 탭의 시트 ID가 기준표와 다릅니다.")
    return found, row_count


def _range(title):
    a1 = "'" + title.replace("'", "''") + "'!A:M"
    return "/values/" + urllib.parse.quote(a1, safe="")


def _read_rows(client, title):
    data = client.request(_range(title), query={"valueRenderOption": "UNFORMATTED_VALUE", "majorDimension": "ROWS"})
    values = data.get("values") if isinstance(data, dict) else None
    if values is None:
        values = []
    if not isinstance(values, list) or any(not isinstance(row, list) for row in values):
        raise PilotError("시트 행 응답 형식이 올바르지 않습니다.")
    return values


def _row_request(sheet_id, row_index, values):
    if row_index < FIRST_DATA_ROW:
        raise PilotError("기준표의 안내·머리글 행은 바꾸지 않습니다.")
    return {"updateCells": {"range": {"sheetId": sheet_id, "startRowIndex": row_index, "endRowIndex": row_index + 1,
            "startColumnIndex": 0, "endColumnIndex": len(values)},
            "rows": [{"values": [cell(value) for value in values]}], "fields": "userEnteredValue"}}


def _insert_under_header(sheet_id, count):
    """Insert at the first data row (0-based index 2), directly under the header.

    inheritFromBefore is false so the new rows copy the data row below them,
    not the header. The note and header stay where they are.
    """
    if count < 1:
        raise PilotError("삽입할 행이 없습니다.")
    return {"insertDimension": {"range": {"sheetId": sheet_id, "dimension": "ROWS",
            "startIndex": FIRST_DATA_ROW, "endIndex": FIRST_DATA_ROW + count},
            "inheritFromBefore": False}}


def _decision_validation(end_row):
    return {"setDataValidation": {"range": {"sheetId": REVIEW_SHEET_ID, "startRowIndex": FIRST_DATA_ROW,
            "endRowIndex": end_row, "startColumnIndex": 11, "endColumnIndex": 12},
            "rule": {"condition": {"type": "ONE_OF_LIST", "values": [
                {"userEnteredValue": "숨김"}, {"userEnteredValue": "유지"}]},
                "showCustomUi": True, "strict": True}}}


def _newest_first(records):
    return sorted(records, key=lambda record: (
        str(record.get("written_at") or ""), str(record.get("comment_id") or "")), reverse=True)


def _max_number(rows):
    highest = 0
    for row in rows[FIRST_DATA_ROW:]:
        number = _number(row[0] if row else "")
        if number is not None and number > highest:
            highest = number
    return highest


def sheet_lease_name(title):
    if title == HIDE_TAB:
        return SHEET_HIDE_LEASE
    if title == REVIEW_TAB:
        return SHEET_REVIEW_LEASE
    raise PilotError("알 수 없는 기준표 탭입니다.")


@contextlib.contextmanager
def sheet_section(state, title):
    """Hold one tab's lock around the read, the number, and the write."""
    if state is None or not hasattr(state, "lease"):
        yield
        return
    with lease_retry(state, 120, sheet_lease_name(title), attempts=5, wait_seconds=1):
        yield


def _sync_tab(client, metadata, title, group):
    sheet_id, row_count = _resolve(metadata, title)
    columns = _columns(title)
    rows = _read_rows(client, title)
    header = [_text(value) for value in rows[HEADER_ROW]] if len(rows) > HEADER_ROW else []
    if header[:len(columns)] != columns:
        raise PilotError("'" + title + "' 머리글이 기준표와 다릅니다.")
    present = set()
    for row in rows[FIRST_DATA_ROW:]:
        comment_id = _text(row[COMMENT_COLUMN]) if len(row) > COMMENT_COLUMN else ""
        if comment_id:
            present.add(comment_id)
    fresh = []
    reflected = []
    for record in _newest_first(group):
        comment_id = str(record.get("comment_id") or "")
        if not comment_id or comment_id in present:
            reflected.append(record)
            continue
        present.add(comment_id)
        fresh.append(record)
    if not fresh:
        return reflected
    highest = _max_number(rows)
    requests = [_insert_under_header(sheet_id, len(fresh))]
    for offset, record in enumerate(fresh):
        number = highest + len(fresh) - offset
        requests.append(_row_request(sheet_id, FIRST_DATA_ROW + offset, row_values(record, number)))
        reflected.append(record)
    if title == REVIEW_TAB:
        requests.append(_decision_validation(max(row_count, len(rows)) + len(fresh)))
    client.request(":batchUpdate", body={"requests": requests})
    return reflected


def sync_sheet(client, records, state=None):
    """Insert new comment IDs under the header, newest first. Existing rows are not rewritten."""
    grouped = {}
    for record in records:
        title = tab_for(record)
        if title and display_status(record):
            grouped.setdefault(title, []).append(record)
    if not grouped:
        return []
    reflected = []
    for title, group in grouped.items():
        with sheet_section(state, title):
            reflected.extend(_sync_tab(client, _metadata(client), title, group))
    return reflected
