"""Append moderation rows to the rulebook spreadsheet. Never edit existing rows.

Row 1 is a note, row 2 is the header, and data starts on row 3. Comment IDs
already in column G are left untouched. A sheet error must not stop classification.
"""

import datetime as dt
import urllib.parse

from .api import PilotError
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
    for sheet in metadata.get("sheets") or []:
        prop = sheet.get("properties") if isinstance(sheet, dict) else None
        if not isinstance(prop, dict):
            continue
        if prop.get("title") == title and isinstance(prop.get("sheetId"), int):
            found = prop["sheetId"]
            break
    if found is None:
        raise PilotError("'" + title + "' 탭이 기준표 시트에 없습니다.")
    if found != expected:
        raise PilotError("'" + title + "' 탭의 시트 ID가 기준표와 다릅니다.")
    return found


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


def sync_sheet(client, records):
    """Append rows whose comment ID is not already in column G. Existing rows stay as they are."""
    grouped = {}
    for record in records:
        title = tab_for(record)
        if title and display_status(record):
            grouped.setdefault(title, []).append(record)
    if not grouped:
        return []
    metadata = _metadata(client)
    reflected, requests = [], []
    for title, group in grouped.items():
        sheet_id = _resolve(metadata, title)
        columns = _columns(title)
        rows = _read_rows(client, title)
        header = [_text(value) for value in rows[HEADER_ROW]] if len(rows) > HEADER_ROW else []
        if header[:len(columns)] != columns:
            raise PilotError("'" + title + "' 머리글이 기준표와 다릅니다.")
        present = set()
        last_number = 0
        for row in rows[FIRST_DATA_ROW:]:
            comment_id = _text(row[COMMENT_COLUMN]) if len(row) > COMMENT_COLUMN else ""
            if comment_id:
                present.add(comment_id)
            number = _number(row[0] if row else "")
            if number is not None:
                last_number = number
        next_row = max(len(rows), FIRST_DATA_ROW)
        for record in group:
            comment_id = str(record.get("comment_id") or "")
            if not comment_id or comment_id in present:
                reflected.append(record)
                continue
            last_number += 1
            requests.append(_row_request(sheet_id, next_row, row_values(record, last_number)))
            present.add(comment_id)
            next_row += 1
            reflected.append(record)
    if requests:
        client.request(":batchUpdate", body={"requests": requests})
    return reflected
