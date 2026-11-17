"""Insert moderation rows in comment-time order. Never rewrite existing rows.

Row 1 is a note and row 2 is the header. Data rows stay newest-first by column C,
댓글 작성일(KST). A new row is inserted where that Instagram time belongs, and only
when the time is known. A missing time is not invented from the moment we noticed
the comment. Rows already on the sheet with an empty column C stay after every
dated row until --fill-missing-dates writes the real time. The same written time
keeps the existing row above the new one. 번호 keeps increasing and is not the
sort key. Comment IDs already in column G are left untouched.
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
    else:
        text = (when + " KST 자동 감지").strip()
    ref = record.get("policy_ref") if isinstance(record.get("policy_ref"), str) else ""
    if ref.strip():
        text += " 기준 " + ref.strip()
    return text


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


def _one_cell(sheet_id, row_index, column, text):
    """Replace one data cell. Neighboring columns, validation, and the row number stay put."""
    if row_index < FIRST_DATA_ROW:
        raise PilotError("기준표의 안내·머리글 행은 바꾸지 않습니다.")
    return {"updateCells": {"range": {"sheetId": sheet_id, "startRowIndex": row_index, "endRowIndex": row_index + 1,
            "startColumnIndex": column, "endColumnIndex": column + 1},
            "rows": [{"values": [cell(text)]}], "fields": "userEnteredValue"}}


def _row_request(sheet_id, row_index, values):
    if row_index < FIRST_DATA_ROW:
        raise PilotError("기준표의 안내·머리글 행은 바꾸지 않습니다.")
    return {"updateCells": {"range": {"sheetId": sheet_id, "startRowIndex": row_index, "endRowIndex": row_index + 1,
            "startColumnIndex": 0, "endColumnIndex": len(values)},
            "rows": [{"values": [cell(value) for value in values]}], "fields": "userEnteredValue"}}


def _insert_at(sheet_id, index, count=1):
    """Insert data rows. The top slot copies the data row below, not the header."""
    if count < 1 or index < FIRST_DATA_ROW:
        raise PilotError("데이터 행만 삽입할 수 있습니다.")
    return {"insertDimension": {"range": {"sheetId": sheet_id, "dimension": "ROWS",
            "startIndex": index, "endIndex": index + count},
            "inheritFromBefore": index > FIRST_DATA_ROW}}


def _written_text(row):
    return _text(row[2]) if len(row) > 2 else ""


def _time_key(value):
    """YYYY-MM-DD HH:MM, or a Sheets date serial shown in that same form.

    Text order matches time order for the zero-padded form the sheet uses.
    An empty cell has no key and sorts after every dated row.
    """
    text = _text(value).strip()
    if not text:
        return ""
    try:
        number = float(text)
    except ValueError:
        return text
    if not 20000 < number < 100000:
        return text
    moment = dt.datetime(1899, 12, 30) + dt.timedelta(days=number)
    return moment.strftime("%Y-%m-%d %H:%M")


def _insertion_index(rows, written_text):
    """Where a comment time belongs. Empty times go after every dated row."""
    target = _time_key(written_text)
    for index, row in enumerate(rows):
        if index < FIRST_DATA_ROW:
            continue
        current = _time_key(_written_text(row))
        if not target:
            if not current:
                return index
            continue
        if not current or current < target:
            return index
    return max(len(rows), FIRST_DATA_ROW)


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
    located = {}
    for index, row in enumerate(rows):
        if index < FIRST_DATA_ROW:
            continue
        comment_id = _text(row[COMMENT_COLUMN]) if len(row) > COMMENT_COLUMN else ""
        if comment_id and comment_id not in located:
            located[comment_id] = index
    fresh = []
    reflected = []
    status_requests = []
    for record in _newest_first(group):
        comment_id = str(record.get("comment_id") or "")
        if not comment_id or comment_id in located:
            index = located.get(comment_id)
            if isinstance(index, int) and title == HIDE_TAB:
                row = rows[index]
                current = _text(row[11]) if len(row) > 11 else ""
                wanted = display_status(record) or ""
                if wanted and current != wanted and current != "숨김 완료":
                    status_requests.append(_one_cell(sheet_id, index, 11, wanted))
                    status_requests.append(_one_cell(sheet_id, index, 12, _note(record, True)))
            if comment_id:
                reflected.append(record)
            continue
        if not _clock(record.get("written_at")):
            continue
        located[comment_id] = None
        fresh.append(record)
    if not fresh:
        if status_requests:
            client.request(":batchUpdate", body={"requests": status_requests})
        return reflected
    highest = _max_number(rows)
    numbered = []
    for offset, record in enumerate(fresh):
        numbered.append((record, highest + len(fresh) - offset))
    shadow = [list(row) for row in rows]
    requests = []
    for record, number in reversed(numbered):
        values = row_values(record, number)
        index = _insertion_index(shadow, values[2])
        requests.append(_insert_at(sheet_id, index))
        requests.append(_row_request(sheet_id, index, values))
        shadow.insert(index, values)
        reflected.append(record)
    if title == REVIEW_TAB:
        requests.append(_decision_validation(max(row_count, len(shadow))))
    client.request(":batchUpdate", body={"requests": status_requests + requests})
    return reflected


def _dated_then_blank(rows):
    """Newest comment time first. Empty column C stays after dated rows, in its old order."""
    data = list(enumerate(rows[FIRST_DATA_ROW:]))
    dated = [item for item in data if _time_key(_written_text(item[1]))]
    blank = [item for item in data if not _time_key(_written_text(item[1]))]
    dated.sort(key=lambda item: _time_key(_written_text(item[1])), reverse=True)
    return dated + blank


def sort_plan(rows):
    ordered = _dated_then_blank(rows)
    current = rows[FIRST_DATA_ROW:]
    moves = []
    for new_index, (old_index, row) in enumerate(ordered):
        if old_index != new_index:
            moves.append({"from": FIRST_DATA_ROW + old_index + 1, "to": FIRST_DATA_ROW + new_index + 1,
                          "number": _text(row[0]) if row else "", "written": _written_text(row),
                          "comment_id": _text(row[COMMENT_COLUMN]) if len(row) > COMMENT_COLUMN else ""})
    return {"rows": len(current), "moves": moves, "ordered": ordered}


def sort_requests(sheet_id, rows):
    """Copy each data row into time order, then delete the old block. Header stays put."""
    ordered = _dated_then_blank(rows)
    current = rows[FIRST_DATA_ROW:]
    if [index for index, _row in ordered] == list(range(len(current))):
        return []
    count = len(current)
    width = max([len(row) for row in rows] + [13])
    requests = [_insert_at(sheet_id, FIRST_DATA_ROW, count)]
    for new_index, (old_index, _row) in enumerate(ordered):
        source = FIRST_DATA_ROW + count + old_index
        dest = FIRST_DATA_ROW + new_index
        span = {"sheetId": sheet_id, "startRowIndex": source, "endRowIndex": source + 1,
                "startColumnIndex": 0, "endColumnIndex": width}
        requests.append({"copyPaste": {"source": span,
            "destination": {**span, "startRowIndex": dest, "endRowIndex": dest + 1},
            "pasteType": "PASTE_NORMAL", "pasteOrientation": "NORMAL"}})
    requests.append({"deleteDimension": {"range": {"sheetId": sheet_id, "dimension": "ROWS",
        "startIndex": FIRST_DATA_ROW + count, "endIndex": FIRST_DATA_ROW + count + count}}})
    return requests


def sort_tabs(client, state, apply=False):
    """Reorder both rulebook tabs by column C. Dry-run unless apply is true."""
    metadata = _metadata(client)
    results = []
    for title in (HIDE_TAB, REVIEW_TAB):
        with sheet_section(state, title):
            rows = _read_rows(client, title)
            header = [_text(value) for value in rows[HEADER_ROW]] if len(rows) > HEADER_ROW else []
            if header[:len(_columns(title))] != _columns(title):
                raise PilotError("'" + title + "' 머리글이 기준표와 다릅니다.")
            plan = sort_plan(rows)
            sheet_id, _row_count = _resolve(metadata, title)
            requests = sort_requests(sheet_id, rows) if apply else []
            if apply and requests:
                client.request(":batchUpdate", body={"requests": requests})
            results.append({"tab": title, "rows": plan["rows"], "moves": len(plan["moves"]),
                            "applied": bool(apply and requests), "preview": plan["moves"][:20]})
    return results


def _date_request(sheet_id, row_index, text):
    """Write column C only. The rest of the row, including validation, stays put."""
    if row_index < FIRST_DATA_ROW:
        raise PilotError("기준표의 안내·머리글 행은 바꾸지 않습니다.")
    return {"updateCells": {"range": {"sheetId": sheet_id, "startRowIndex": row_index,
            "endRowIndex": row_index + 1, "startColumnIndex": 2, "endColumnIndex": 3},
            "rows": [{"values": [cell(text)]}], "fields": "userEnteredValue"}}


def missing_dates(rows):
    """Data rows whose 댓글 작성일 cell is empty and whose 댓글 ID is present."""
    found = []
    for index, row in enumerate(rows):
        if index < FIRST_DATA_ROW or _time_key(_written_text(row)):
            continue
        comment_id = _text(row[COMMENT_COLUMN]) if len(row) > COMMENT_COLUMN else ""
        if not comment_id:
            continue
        found.append({"row": index, "number": _text(row[0]) if row else "",
                      "account": _text(row[1]) if len(row) > 1 else "",
                      "author": _text(row[3]) if len(row) > 3 else "",
                      "comment_id": comment_id})
    return found


def _preview_moves(rows):
    places = {}
    for new_index, (old_index, row) in enumerate(sort_plan(rows)["ordered"]):
        comment_id = _text(row[COMMENT_COLUMN]) if len(row) > COMMENT_COLUMN else ""
        if comment_id:
            places[comment_id] = FIRST_DATA_ROW + new_index + 1
    return places


def fill_missing_dates(client, state, lookup, apply=False):
    """Read Instagram timestamps for blank column C. Write and re-sort only when apply is true.

    lookup(account_label, comment_id) returns an ISO timestamp or raises PilotError.
    The read is allowed during a dry-run. Sheet cells change only when apply is true.
    """
    blanks = []
    snapshots = {}
    for title in (HIDE_TAB, REVIEW_TAB):
        with sheet_section(state, title):
            rows = [list(row) for row in _read_rows(client, title)]
            snapshots[title] = rows
            for item in missing_dates(rows):
                blanks.append({**item, "tab": title})
    resolved = []
    stopped = False
    for item in blanks:
        if stopped:
            resolved.append({**item, "written": "", "error": "API 호출 제한"})
            continue
        try:
            written = lookup(item["account"], item["comment_id"])
        except PilotError as error:
            resolved.append({**item, "written": "", "error": str(error)})
            if "호출 제한" in str(error):
                stopped = True
            continue
        clock = _clock(written)
        if not clock:
            resolved.append({**item, "written": "", "error": "댓글 작성 시각을 읽지 못했습니다."})
            continue
        resolved.append({**item, "written": clock, "error": ""})
    for title, rows in snapshots.items():
        shadow = [list(row) for row in rows]
        clocks = {item["comment_id"]: item["written"] for item in resolved if item["tab"] == title and item["written"]}
        for row in shadow[FIRST_DATA_ROW:]:
            comment_id = _text(row[COMMENT_COLUMN]) if len(row) > COMMENT_COLUMN else ""
            if comment_id in clocks and not _time_key(_written_text(row)):
                while len(row) < 3:
                    row.append("")
                row[2] = clocks[comment_id]
        places = _preview_moves(shadow)
        for item in resolved:
            if item["tab"] == title and item["comment_id"] in places:
                item["sheet_row"] = places[item["comment_id"]]
    if not apply:
        return {"applied": False, "rows": resolved}
    for title in (HIDE_TAB, REVIEW_TAB):
        wanted = {item["comment_id"]: item["written"] for item in resolved if item["tab"] == title and item["written"]}
        if not wanted:
            continue
        with sheet_section(state, title):
            metadata = _metadata(client)
            sheet_id, _row_count = _resolve(metadata, title)
            rows = [list(row) for row in _read_rows(client, title)]
            header = [_text(value) for value in rows[HEADER_ROW]] if len(rows) > HEADER_ROW else []
            if header[:len(_columns(title))] != _columns(title):
                raise PilotError("'" + title + "' 머리글이 기준표와 다릅니다.")
            requests = []
            for index, row in enumerate(rows):
                if index < FIRST_DATA_ROW:
                    continue
                comment_id = _text(row[COMMENT_COLUMN]) if len(row) > COMMENT_COLUMN else ""
                clock = wanted.get(comment_id)
                if not clock or _time_key(_written_text(row)):
                    continue
                while len(row) < 3:
                    row.append("")
                row[2] = clock
                requests.append(_date_request(sheet_id, index, clock))
            if not requests:
                continue
            requests.extend(sort_requests(sheet_id, rows))
            client.request(":batchUpdate", body={"requests": requests})
            places = _preview_moves(rows)
            for item in resolved:
                if item["tab"] == title and item["comment_id"] in places:
                    item["sheet_row"] = places[item["comment_id"]]
    return {"applied": True, "rows": resolved}


def sync_sheet(client, records, state=None):
    """Insert each new comment where its 댓글 작성일 belongs. Existing rows are not rewritten."""
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
