"""Append moderation decisions to the existing operations spreadsheet.

The daily job already owns that file. This writer only adds two tabs and never
touches region tabs. A sheet error is the caller's to catch; it must not stop
classification. Rows are keyed by comment ID.
"""

import datetime as dt
import urllib.parse

from .api import PilotError
from .sheets import cell


KST = dt.timezone(dt.timedelta(hours=9))
HIDE_TAB = "숨김 기록"
REVIEW_TAB = "사람 판단 대기"
COLUMNS = ["처리 시각(KST)", "계정", "게시물 링크", "댓글 ID", "작성자", "댓글 원문", "규칙 ID", "판정 이유", "신뢰도", "상태"]
COMMENT_COLUMN = 3
STATUS_COLUMN = 9


def display_status(record):
    status = record.get("status")
    if status == "would_hide":
        return "숨겼을 것(시험)"
    if status == "hidden":
        return "숨김 완료"
    if status == "hide_failed":
        error = record.get("error")
        if isinstance(error, str) and error.strip():
            return "숨김 실패 (" + " ".join(error.split())[:80] + ")"
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


def _when(record):
    raw = record.get("updated_at") or record.get("classified_at")
    if not isinstance(raw, str) or not raw:
        return ""
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return parsed.astimezone(KST).strftime("%Y-%m-%d %H:%M")


def row_values(record):
    confidence = {"high": "높음", "low": "낮음"}.get(record.get("confidence"), record.get("confidence") or "")
    account = record.get("region") or record.get("account_username") or ""
    author = record.get("author_username") or ""
    return [_when(record), account, record.get("permalink") or "", str(record.get("comment_id") or ""),
            author, record.get("text") or "", record.get("rule_id") or "", record.get("reason") or "",
            confidence, display_status(record) or ""]


def _ids(metadata):
    found = {}
    for sheet in metadata.get("sheets") or []:
        prop = sheet.get("properties") if isinstance(sheet, dict) else None
        if isinstance(prop, dict) and isinstance(prop.get("title"), str) and isinstance(prop.get("sheetId"), int):
            found[prop["title"]] = prop["sheetId"]
    return found


def _metadata(client):
    value = client.request(query={"fields": "sheets.properties"})
    if not isinstance(value, dict):
        raise PilotError("시트 목록 응답이 올바르지 않습니다.")
    return value


def _header_request(sheet_id):
    return {"updateCells": {"range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1,
            "startColumnIndex": 0, "endColumnIndex": len(COLUMNS)},
            "rows": [{"values": [cell(name) for name in COLUMNS]}], "fields": "userEnteredValue"}}


def _row_request(sheet_id, row_index, values):
    return {"updateCells": {"range": {"sheetId": sheet_id, "startRowIndex": row_index, "endRowIndex": row_index + 1,
            "startColumnIndex": 0, "endColumnIndex": len(COLUMNS)},
            "rows": [{"values": [cell(value) for value in values]}], "fields": "userEnteredValue"}}


def _range(title):
    a1 = "'" + title.replace("'", "''") + "'!A:J"
    return "/values/" + urllib.parse.quote(a1, safe="")


def _read_rows(client, title):
    data = client.request(_range(title), query={"valueRenderOption": "UNFORMATTED_VALUE", "majorDimension": "ROWS"})
    values = data.get("values") if isinstance(data, dict) else None
    if values is None:
        values = []
    if not isinstance(values, list) or any(not isinstance(row, list) for row in values):
        raise PilotError("시트 행 응답 형식이 올바르지 않습니다.")
    return values


def _text(value):
    if value is None:
        return ""
    return str(value)


def ensure_tabs(client, titles):
    """Create a missing tab and its header. An existing header is left as-is."""
    existing = _ids(_metadata(client))
    requests, made = [], []
    for offset, title in enumerate(titles):
        if title in existing:
            continue
        sheet_id = 880000 + offset
        while sheet_id in existing.values() or sheet_id in made:
            sheet_id += 1
        made.append(sheet_id)
        requests.append({"addSheet": {"properties": {"sheetId": sheet_id, "title": title,
            "gridProperties": {"rowCount": 5000, "columnCount": len(COLUMNS), "frozenRowCount": 1}}}})
        requests.append(_header_request(sheet_id))
        existing[title] = sheet_id
    if requests:
        client.request(":batchUpdate", body={"requests": requests})
    return existing


def sync_sheet(client, records):
    """Insert one row per new comment ID, or update the status cell's row when it changed.

    Returns the records now reflected on the sheet. Raises PilotError on failure.
    """
    grouped = {}
    for record in records:
        title = tab_for(record)
        if title and needs_sheet(record):
            grouped.setdefault(title, []).append(record)
    if not grouped:
        return [record for record in records if not needs_sheet(record)]
    ids = ensure_tabs(client, list(grouped))
    reflected, requests = [], []
    for title, group in grouped.items():
        sheet_id = ids.get(title)
        if not isinstance(sheet_id, int):
            ids = _ids(_metadata(client))
            sheet_id = ids.get(title)
        if not isinstance(sheet_id, int):
            raise PilotError("시트 탭을 만들지 못했습니다.")
        rows = _read_rows(client, title)
        header = [_text(value) for value in rows[0]] if rows else []
        if header != COLUMNS:
            if rows and any(_text(value) for value in rows[0]):
                raise PilotError("'" + title + "' 첫 행이 기대하는 머리글과 다릅니다.")
            requests.append(_header_request(sheet_id))
            rows = [list(COLUMNS)]
        found = {}
        for index, row in enumerate(rows[1:], start=1):
            comment_id = _text(row[COMMENT_COLUMN]) if len(row) > COMMENT_COLUMN else ""
            if comment_id and comment_id not in found:
                found[comment_id] = index
        next_row = len(rows)
        for record in group:
            comment_id = str(record.get("comment_id") or "")
            values = row_values(record)
            current = found.get(comment_id)
            if current is None:
                requests.append(_row_request(sheet_id, next_row, values))
                found[comment_id] = next_row
                next_row += 1
            elif len(rows[current]) <= STATUS_COLUMN or _text(rows[current][STATUS_COLUMN]) != values[STATUS_COLUMN]:
                requests.append(_row_request(sheet_id, current, values))
                rows[current] = values
            reflected.append(record)
    if requests:
        client.request(":batchUpdate", body={"requests": requests})
    return reflected
