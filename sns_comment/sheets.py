"""Google Sheets projection of the raw DB. Never creates a remote spreadsheet.

Stable keys are looked up on every sync; row numbers are not persisted. Existing
review cells are never rewritten. A failed export can be rerun without collecting
Instagram again. All comment text is sent as a literal string, never a formula.
"""

import datetime as dt
import bisect
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

from .api import NoRedirect, PilotError, https_context
from .storage import now, run_lock, timestamp
from .account import collection_window


KST = dt.timezone(dt.timedelta(hours=9))
RULE_URL = "https://app.notion.com/p/4050761a512d81cd95a0c42269a38744"
SCHEMA_VERSION = "sns-sheets-v1"
HEADERS = {
    "설정": ["항목", "값", "설명"],
    "댓글 원본": ["연결 키", "계정 ID", "댓글 ID", "상위 댓글 ID", "게시물 ID", "게시물 링크",
              "작성자 ID", "작성 계정", "원문", "본문 제공 여부", "공식 계정 여부",
              "작성 시각 (KST)", "최초 수집 시각 (KST)", "최종 수집 시각 (KST)", "원본 버전"],
    "분류와 처리": ["연결 키", "계정 ID", "댓글 ID", "현재 원본 버전", "분류 상태", "분류 버전",
                "주 카테고리", "보조 태그", "의도", "감정", "긴급도", "검토 여부", "담당",
                "답변 상태", "답변 내용", "답변 시각", "검토 유형", "계정 링크", "게시물 링크",
                "반복 횟수", "근거", "권장 조치", "검수자", "처리 상태", "처리 시각",
                "분석 집계 제외 여부", "제외 사유", "검수 원본 버전"],
    "기간별 요약": ["보고 키", "계정 ID", "보고 종류", "시작 시각 (KST, 포함)", "종료 시각 (KST, 미포함)",
               "저장된 댓글·답글", "본문 제공", "본문 미제공", "공식 계정 작성", "작성자 미확인",
               "확인된 관객 작성자 수", "데이터 상태", "수집 범위", "마지막 수집 완료 (KST)",
               "분류별 비중", "반복 문의", "미해결 건수", "근거", "실행안", "담당", "적용 규정"],
    "수집 로그": ["실행 ID", "계정 ID", "게시물 ID", "게시물 링크", "시작 시각 (KST)", "종료 시각 (KST)",
              "수집 상태", "처리 단계", "API 요청 수", "오류", "마지막 성공 시각 (KST)",
              "종료까지 최초 저장된 고유 댓글·답글", "누락 상태", "복구 결과"],
}
CATEGORIES = ["티켓과 결제", "일정과 장소", "아티스트와 라인업", "VVIP", "현장 운영", "콘텐츠 반응", "기타"]
DROPDOWNS = {"분류와 처리": {
    4: ["미분류", "자동 분류", "분류 실패", "수동 분류"],
    6: CATEGORIES, 8: ["문의", "불만", "칭찬", "요청", "스팸", "판단 보류"],
    9: ["긍정", "중립", "부정", "판단 보류"], 10: ["일반", "긴급", "판단 보류"],
    11: ["검토 전", "검토 중", "검토 완료"],
    13: ["미확인", "답변 필요", "답변 불필요", "답변 완료"],
    23: ["미확인", "검토 중", "조치 필요", "처리 완료", "오탐"],
    25: ["미판정", "포함", "제외"],
}}
DATE_COLUMNS = {"댓글 원본": [11, 12, 13], "기간별 요약": [3, 4, 13], "수집 로그": [4, 5, 10]}
MAX_BATCH_ROWS = 500
MAX_BATCH_BYTES = 1_500_000
MAX_BATCHES_PER_RUN = 20
RAW_ROW_WARNING = 10_000


def local_time(value):
    if not value:
        return ""
    local = dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(KST).replace(tzinfo=None)
    return (local - dt.datetime(1899, 12, 30)).total_seconds() / 86400


def read_source(path, config, as_of=None, collection_period=None):
    """Consistent read-only snapshot, including *all* runs rather than status's last 10."""
    path = Path(path).resolve()
    if not path.is_file():
        raise PilotError("저장된 DB가 없습니다. 댓글 수집을 먼저 실행해주세요.")
    instant = as_of or dt.datetime.now(dt.timezone.utc)
    if instant.tzinfo is None:
        raise PilotError("집계 기준 시각에는 시간대가 필요합니다.")
    account_mode = collection_period is not None or config.get("collection", {}).get("mode") == "account"
    recent_mode = account_mode and config.get("collection", {}).get("media_scope") == "recent_posts"
    period = collection_period or (collection_window(config.get("collection", {}).get("lookback_days", 7), instant=instant) if account_mode else None)
    if period:
        if len(period) != 2:
            raise PilotError("집계 시작·종료 시각을 함께 지정해야 합니다.")
        period = tuple(timestamp(v) for v in period)
        if period[0] >= period[1]:
            raise PilotError("집계 종료 시각은 시작 시각보다 나중이어야 합니다.")
        instant = dt.datetime.fromisoformat(period[1])
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN")
        clauses, params = [], []
        if account_mode and config.get("account_id"):
            clauses.append("c.account_id=?")
            params.append(config["account_id"])
        count_where = " WHERE " + " AND ".join(clauses) if clauses else ""
        total_stored = connection.execute("SELECT COUNT(*) FROM comments c" + count_where, params).fetchone()[0]
        if period:
            clauses.extend(["c.written_at>=?", "c.written_at<?"])
            params.extend(period)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        comments = [dict(r) for r in connection.execute("""SELECT c.account_id,c.id,c.media_id,c.parent_id,
            c.author_id,c.username,c.is_owner,c.text,c.written_at,c.first_seen_at,c.last_seen_at,
            c.payload_hash,m.permalink,m.last_completed_at,m.written_at AS media_written_at
            FROM comments c JOIN media m ON c.account_id=m.account_id AND c.media_id=m.id
            """ + where + " ORDER BY c.written_at,c.account_id,c.id", params)]
        runs = [dict(r) for r in connection.execute("SELECT * FROM runs ORDER BY started_at,id")]
        media = [dict(r) for r in connection.execute("SELECT account_id,id,permalink,written_at,last_completed_at FROM media ORDER BY account_id,id")]
        # Log counts must use the whole stored history, not the selected seven
        # days. Account attempts cannot use their last visited media as scope.
        first_seen_records = connection.execute("SELECT account_id,media_id,first_seen_at FROM comments").fetchall()
        if account_mode and config.get("account_id"):
            runs = [r for r in runs if r["account_id"] in (None, config["account_id"])]
            media = [m for m in media if m["account_id"] == config["account_id"]]
        period_scope = None
        has_scopes = connection.execute("SELECT 1 FROM sqlite_master WHERE name='collection_scopes'").fetchone()
        if period and has_scopes:
            policy_filter = " AND media_policy='recent_posts'" if recent_mode else ""
            row = connection.execute("""SELECT * FROM collection_scopes WHERE account_id=? AND owner=? AND window_start=? AND window_end=?
                """ + policy_filter + " ORDER BY started_at DESC,id DESC LIMIT 1", (config.get("account_id"), config["expected_username"].lstrip("@"), *period)).fetchone()
            period_scope = dict(row) if row else None
    finally:
        connection.close()
    tracked_comments, tracked_media = [], []
    if recent_mode:
        tracked_comments = [c for c in comments if timestamp(c["media_written_at"]) < period[0]]
        comments = [c for c in comments if period[0] <= timestamp(c["media_written_at"]) < period[1]]
        tracked_ids = {(c["account_id"],c["media_id"]) for c in tracked_comments}
        tracked_media = [m for m in media if (m["account_id"],m["id"]) in tracked_ids]
        media = [m for m in media if period[0] <= timestamp(m["written_at"]) < period[1]]
    tabs = {name: [header[:]] for name, header in HEADERS.items()}
    settings = [
        ["시트 형식", SCHEMA_VERSION, "연결 키와 열 이름을 유지합니다."],
        ["공식 계정", config["expected_username"], "연결된 공식 계정"],
        ["계정 ID", config.get("account_id", ""), "공개 식별자. 인증 정보가 아닙니다."],
        ["마스터 소유 계정", config.get("google_sheets", {}).get("owner_email", "미확정"), "시트 생성 전에 연결 계정을 확인합니다."],
        ["수집 범위", "최근 7일 게시물의 기간 내 댓글·답글" if recent_mode else "공식 계정의 접근 가능한 전체 게시물·댓글·답글" if account_mode else config["post_url"],
         "이전 게시물 활동은 별도 추적. 과거 전체 댓글은 순회하지 않습니다." if recent_mode else "댓글 작성 기간 기준" if account_mode else "게시물 1개 진단"],
        ["최초 저장 댓글 작성 시각 (KST)", local_time(comments[0]["written_at"]) if comments else "", "API가 반환해 저장된 범위"],
        ["최초 수집 시작일", "기간 수집 이력 참조" if account_mode else "미확정", "계정 기간 수집과 게시물 1개 파일럿을 구분합니다."],
        ["담당", "원수현 매니저", "운영 규정 v1.1"],
        ["규정 실행 시각", "매일 08:00 Asia/Seoul", "예약 실행은 아직 가동하지 않았습니다."],
        ["분류 버전", "미구현", "AI 분류·정확도 검증 전"],
        ["적용 규정", "v1.1", RULE_URL],
        ["수집 상태", (period_scope["status"] if period_scope else "계정 기간 수집 미검증") if account_mode else "수동 수집", "API 반환 범위. Instagram 화면 전체와 대조 전"],
        ["노션 보고", "미연결", "요약 보고 자동 반영 전"],
        ["Drive 원본 백업", "미연결", "날짜별 원본 백업 자동 반영 전"],
        ["인증 정보", "시트 저장 금지", "Meta·Google 토큰과 비밀키를 입력하지 않습니다."],
        ["전체 저장 댓글·답글", total_stored, "DB 기준. 계정 기간 집계·시트 표시와 구분합니다."],
        ["이번 원본 반영 대상", len(comments), "계정 모드는 작성 기간 내 댓글·답글. 기간 밖 DB·시트 기존 행은 보존합니다." if account_mode else "현재는 전건 반영."],
        ["원본 행 수 경보", RAW_ROW_WARNING, "표시·보관 정책 점검 경보이며 자동 삭제·잘라내기 기준이 아닙니다."],
    ]
    if period:
        settings.extend([["재조회 시작 (KST, 포함)", local_time(period[0]), "댓글·답글 작성 시각 기준"],
                         ["재조회 종료 (KST, 미포함)", local_time(period[1]), "기본은 가장 최근 08시. 주간 보고와 구분"],
                         ["기간 수집 ID", period_scope["id"] if period_scope else "미검증", "목록·댓글·답글의 미완료 위치와 완료 상태 연결"]])
    if recent_mode:
        settings.extend([["기본 대상 게시물", len(media), "최근 7일 작성 게시물. 누적 게시물 수와 구분"],
                         ["이전 게시물 별도 추적", len(tracked_media), "저장 자료에서 최근 댓글이 확인된 이전 게시물"],
                         ["이전 게시물 신규 알림", "미연결", "알려지지 않은 과거 게시물의 새 댓글은 전체 순회로 찾지 않음"]])
    tabs["설정"].extend(settings)
    for c in comments:
        key = c["account_id"] + ":" + c["id"]
        tabs["댓글 원본"].append([
            key, c["account_id"], c["id"], c["parent_id"] or "", c["media_id"], c["permalink"],
            c["author_id"] or "", c["username"] or "", c["text"] if c["text"] is not None else "",
            "미제공" if c["text"] is None else "제공", {0: "관객", 1: "공식 계정", None: "미확인"}[c["is_owner"]],
            local_time(c["written_at"]), local_time(c["first_seen_at"]), local_time(c["last_seen_at"]), c["payload_hash"],
        ])
        review = [""] * len(HEADERS["분류와 처리"])
        review[:5] = [key, c["account_id"], c["id"], c["payload_hash"], "미분류"]
        review[11:14] = ["검토 전", "원수현 매니저", "미확인"]
        review[17] = "https://www.instagram.com/" + urllib.parse.quote(c["username"], safe="._") + "/" if c["username"] else ""
        review[18] = c["permalink"]
        review[23], review[25] = "미확인", "미판정"
        review[20] = ("분류 전 원문 발췌: " + c["text"][:300] + (" … [발췌]" if len(c["text"]) > 300 else "")
                      if c["text"] is not None else "본문 미제공. 댓글 원본 탭에서 ID·부모 관계를 확인합니다.")
        tabs["분류와 처리"].append(review)
    last_success = {}
    first_seen_by_media = {}
    for c in first_seen_records:
        first_seen_by_media.setdefault((c["account_id"], c["media_id"]), []).append(c["first_seen_at"])
        first_seen_by_media.setdefault((c["account_id"], None), []).append(c["first_seen_at"])
    for seen in first_seen_by_media.values():
        seen.sort()
    for r in runs:
        account_run = bool(r.get("scope_id"))
        scope = (r["account_id"], None if account_run else r["media_id"])
        if r["status"] == "complete":
            last_success[scope] = r["ended_at"]
        observed = (bisect.bisect_right(first_seen_by_media.get(scope, []), r["ended_at"])
                    if r["ended_at"] else "미확인")
        tabs["수집 로그"].append([
            r["id"], r["account_id"] or "", "" if account_run else r["media_id"] or "", r["post_url"],
            local_time(r["started_at"]), local_time(r["ended_at"]), r["status"], r["stage"], r["requests"],
            r["error"] or "", local_time(last_success.get(scope)), observed,
            "API 반환 범위 순회 완료" if r["status"] == "complete" else "수집 미완료·누락 범위 미확정", "",
        ])
    # Completed 08:00 reporting windows only. Historic stored days and today's
    # daily report are explicit partial snapshots, never proof of full coverage.
    cutoff = instant.astimezone(KST).replace(hour=8, minute=0, second=0, microsecond=0)
    if cutoff > instant:
        cutoff -= dt.timedelta(days=1)
    by_period, by_account = {}, {}
    for m in media:
        by_account.setdefault(m["account_id"], []).append(m)
    if recent_mode and config.get("account_id"):
        by_account.setdefault(config["account_id"], [])
    for c in comments:
        written = dt.datetime.fromisoformat(c["written_at"]).astimezone(KST)
        end = written.replace(hour=8, minute=0, second=0, microsecond=0)
        if written >= end:
            end += dt.timedelta(days=1)
        if end <= cutoff:
            by_period.setdefault((c["account_id"], end), []).append(c)
    for account, account_media in sorted(by_account.items()):
        ends = {cutoff} | {end for acc, end in by_period if acc == account}
        # Every target must have completed. The newest successful target cannot
        # stand in for another target's older or missing completion.
        latest = min((m["last_completed_at"] or "" for m in account_media), default="")
        scope = " / ".join(m["permalink"] for m in account_media)
        for end in sorted(ends):
            start = end - dt.timedelta(days=1)
            selected = by_period.get((account, end), [])
            count = lambda predicate: sum(bool(predicate(c)) for c in selected)
            state = ("잠정·계정 기간 수집 " + (period_scope["status"] if period_scope else "미검증")) if account_mode else (
                "잠정·저장된 범위만 집계" if latest and dt.datetime.fromisoformat(latest) >= end else "잠정·종료 이후 수집 미확인")
            tabs["기간별 요약"].append([
                f"{account}:daily:{start.isoformat()}:{end.isoformat()}", account, "일일", local_time(start.isoformat()), local_time(end.isoformat()),
                len(selected), count(lambda c: c["text"] is not None), count(lambda c: c["text"] is None),
                count(lambda c: c["is_owner"] == 1), count(lambda c: c["author_id"] is None),
                len({c["author_id"] for c in selected if c["is_owner"] == 0 and c["author_id"]}),
                state,
                scope, local_time(latest), "미구현·분류 완료율 미확인", "", "미확인", "", "", "원수현 매니저", "v1.1",
            ])
    if recent_mode and tracked_comments:
        by_day = {}
        for c in tracked_comments:
            written = dt.datetime.fromisoformat(c["written_at"]).astimezone(KST)
            end = written.replace(hour=8,minute=0,second=0,microsecond=0)
            if written >= end:
                end += dt.timedelta(days=1)
            by_day.setdefault((c["account_id"],end),[]).append(c)
        for (account,end), selected in sorted(by_day.items()):
            start = end - dt.timedelta(days=1)
            count = lambda predicate:sum(bool(predicate(c)) for c in selected)
            links = " / ".join(sorted({c["permalink"] for c in selected}))
            tabs["기간별 요약"].append([
                f"{account}:tracked:daily:{start.isoformat()}:{end.isoformat()}",account,"이전 게시물 추적",local_time(start.isoformat()),local_time(end.isoformat()),
                len(selected),count(lambda c:c["text"] is not None),count(lambda c:c["text"] is None),count(lambda c:c["is_owner"]==1),count(lambda c:c["author_id"] is None),
                len({c["author_id"] for c in selected if c["is_owner"]==0 and c["author_id"]}),
                "별도 추적·기존 저장 자료·신규 알림 미연결",links,"","미구현·분류 완료율 미확인","","미확인","","","원수현 매니저","v1.1"])
    body = {"schema": SCHEMA_VERSION, "rule_url": RULE_URL, "tabs": tabs,
            "coverage": {"stored_comments_and_replies": total_stored, "raw_rows_targeted": len(comments),
                         "classification": "not_implemented", "selection_policy": "recent_posts_and_comment_window" if recent_mode else "written_time_window" if period else "all_stored_no_truncation",
                         "collection_window": period, "account_scope_status": period_scope["status"] if period_scope else "not_verified",
                         "account_scope_id": period_scope["id"] if period_scope else None}}
    if recent_mode:
        body["coverage"].update(recent_posts=len(media),tracked_old_posts=len(tracked_media),tracked_comments_and_replies=len(tracked_comments))
        body["tracking"] = {"status":"cached_activity_only","event_delivery_connected":False,
            "posts":[{"id":m["id"],"permalink":m["permalink"]} for m in tracked_media],
            "comments_and_replies":len(tracked_comments)}
    body["source_digest"] = hashlib.sha256(json.dumps(tabs, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return body


def column_name(index):
    label = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        label = chr(65 + remainder) + label
    return label


def cell(value):
    if value == "" or value is None:
        return {}
    if isinstance(value, bool):
        return {"userEnteredValue": {"boolValue": value}}
    return {"userEnteredValue": {"numberValue" if isinstance(value, (int, float)) else "stringValue": value}}


def build_plan(source, metadata, snapshot, seed_review_context=False):
    """Machine-owned data may change. Existing human-owned fields never do."""
    properties = {s["properties"]["title"]: s["properties"] for s in metadata["sheets"]}
    tables = {s["properties"]["title"]: s.get("tables", []) for s in metadata["sheets"]}
    requests, counts, capacity = [], {}, {}
    for name, wanted in source["tabs"].items():
        if name not in properties or name not in snapshot:
            raise PilotError(f"마스터 시트에 '{name}' 탭이 없습니다. 지정한 시트와 형식을 확인해주세요.")
        prop, rows = properties[name], snapshot[name]
        width = len(HEADERS[name])
        if not rows or rows[0] != HEADERS[name]:
            raise PilotError(f"'{name}' 탭의 열 이름이 다릅니다. 기존 내용을 덮어쓰지 않습니다.")
        if prop["gridProperties"]["columnCount"] < width:
            raise PilotError(f"'{name}' 탭의 열 수가 부족합니다.")
        existing = {}
        for index, row in enumerate(rows[1:], 1):
            if not any(value != "" and value is not None for value in row):
                continue
            if not row or not isinstance(row[0], str) or not row[0] or row[0].startswith("=") or row[0] in existing:
                raise PilotError(f"'{name}' 탭의 연결 키가 비어 있거나 중복·수식입니다. 수정 후 재실행해주세요.")
            existing[row[0]] = (index, row + [""] * max(0, width - len(row)))
        changes, next_row = [], len(rows)
        for row in wanted[1:]:
            old = existing.get(row[0])
            if old is None:
                changes.append((next_row, 0, row))
                next_row += 1
            elif name == "설정":
                if row[0] in {"수집 범위", "수집 상태", "전체 저장 댓글·답글", "이번 원본 반영 대상", "재조회 시작 (KST, 포함)",
                              "재조회 종료 (KST, 미포함)", "기간 수집 ID", "기본 대상 게시물", "이전 게시물 별도 추적", "이전 게시물 신규 알림"} and old[1][1:3] != row[1:3]:
                    changes.append((old[0], 1, row[1:3]))
            elif name == "분류와 처리":
                if old[1][1:3] != row[1:3]:
                    raise PilotError("분류 탭의 계정·댓글 ID가 연결 키와 다릅니다.")
                if old[1][3] != row[3]:
                    changes.append((old[0], 3, [row[3]]))
                if seed_review_context and not old[1][20] and row[20]:
                    changes.append((old[0], 20, [row[20]]))
            else:
                # Summary conclusions and log recovery notes belong to people.
                owned = 14 if name == "기간별 요약" else 13 if name == "수집 로그" else width
                # Last-seen-only refreshes must not resend every long comment.
                start = None
                for col in range(owned + 1):
                    different = col < owned and old[1][col] != row[col]
                    if different and start is None:
                        start = col
                    if not different and start is not None:
                        changes.append((old[0], start, row[start:col]))
                        start = None
        if next_row > prop["gridProperties"]["rowCount"]:
            requests.append({"appendDimension": {"sheetId": prop["sheetId"], "dimension": "ROWS",
                                                 "length": next_row - prop["gridProperties"]["rowCount"]}})
        added = [i for i, col, _ in changes if col == 0 and i >= len(rows)]
        if added:
            # Format the future owned rows before writing the first data batch.
            # Replanning after a partial run must not forget earlier date cells.
            for col in DATE_COLUMNS.get(name, []):
                requests.append({"repeatCell": {"range": {"sheetId": prop["sheetId"],
                    "startRowIndex": min(added), "endRowIndex": max(added) + 1,
                    "startColumnIndex": col, "endColumnIndex": col + 1},
                    "cell": {"userEnteredFormat": {"numberFormat": {"type": "DATE_TIME", "pattern": "yyyy-mm-dd hh:mm:ss"}}},
                    "fields": "userEnteredFormat.numberFormat"}})
        # Group contiguous equal-width blocks. No append API means uncertain
        # network results are reconciled by key lookup on the next invocation.
        blocks = []
        for index, col, values in sorted(changes):
            if blocks and blocks[-1][0] + len(blocks[-1][2]) == index and blocks[-1][1] == col and len(blocks[-1][2][0]) == len(values):
                blocks[-1][2].append(values)
            else:
                blocks.append([index, col, [values]])
        for index, col, values in blocks:
            requests.append({"updateCells": {"range": {
                "sheetId": prop["sheetId"], "startRowIndex": index, "endRowIndex": index + len(values),
                "startColumnIndex": col, "endColumnIndex": col + len(values[0])},
                "rows": [{"values": [cell(v) for v in row]} for row in values], "fields": "userEnteredValue"}})
        for table in tables[name]:
            area = table["range"]
            if area.get("startRowIndex", 0) == 0 and area.get("startColumnIndex", 0) == 0 and area["endColumnIndex"] == width and area["endRowIndex"] < next_row:
                requests.append({"updateTable": {"table": {"tableId": table["tableId"],
                    "range": dict(area, endRowIndex=next_row)}, "fields": "range"}})
        counts[name] = len({index for index, _, _ in changes})
        capacity[name] = {"source_rows": len(wanted) - 1, "sheet_rows_before": len(existing),
                          "projected_rows": len(existing) + sum(r[0] not in existing for r in wanted[1:])}
    warnings = []
    if capacity["댓글 원본"]["projected_rows"] > RAW_ROW_WARNING:
        warnings.append(f"원본 시트가 {RAW_ROW_WARNING:,}행 경보를 초과합니다. 조회·보관 정책을 점검해야 합니다. 자동 잘라내기는 하지 않습니다.")
    return {"requests": requests, "changed_rows": counts, "source_digest": source["source_digest"],
            "coverage": source.get("coverage", {}), "capacity": capacity, "warnings": warnings}


def request_batches(requests, max_rows=MAX_BATCH_ROWS, max_bytes=MAX_BATCH_BYTES):
    """Bound each actual request by row count AND UTF-8 JSON bytes, preserving order."""
    if max_rows < 1 or max_bytes < 1024:
        raise PilotError("시트 배치 크기 설정을 확인해주세요.")
    def size(items):
        return len(json.dumps({"requests": items}, ensure_ascii=False).encode())
    def pieces(request):
        update = request.get("updateCells")
        if update is None:
            yield request
            return
        for start in range(0, len(update["rows"]), max_rows):
            pending = [(start, update["rows"][start:start + max_rows])]
            while pending:
                offset, rows = pending.pop(0)
                part = dict(update, rows=rows, range=dict(update["range"],
                    startRowIndex=update["range"]["startRowIndex"] + offset,
                    endRowIndex=update["range"]["startRowIndex"] + offset + len(rows)))
                item = {"updateCells": part}
                if size([item]) <= max_bytes:
                    yield item
                elif len(rows) > 1:
                    middle = len(rows) // 2
                    pending[:0] = [(offset, rows[:middle]), (offset + middle, rows[middle:])]
                else:
                    raise PilotError("댓글 한 행이 시트 전송 상한을 초과했습니다. 원본은 보존하며 생략 없이 반영 대기로 남깁니다.")
    batch, row_count = [], 0
    for request in requests:
        for item in pieces(request):
            count = len(item.get("updateCells", {}).get("rows", []))
            if batch and (row_count + count > max_rows or size(batch + [item]) > max_bytes):
                yield batch
                batch, row_count = [], 0
            if size([item]) > max_bytes:
                raise PilotError("시트 구조 변경 요청이 배치 전송 상한을 초과했습니다.")
            batch.append(item)
            row_count += count
    if batch:
        yield batch


class SheetsClient:
    def __init__(self, token, spreadsheet_id, opener=None):
        if not re.fullmatch(r"[A-Za-z0-9_-]{15,200}", spreadsheet_id or ""):
            raise PilotError("연결할 마스터 시트의 정확한 ID를 지정해주세요.")
        if not token or any(c.isspace() for c in token):
            raise PilotError("Google 액세스 토큰이 필요합니다. 토큰은 로컬에서만 입력해주세요.")
        self.token = token
        self.base = "https://sheets.googleapis.com/v4/spreadsheets/" + spreadsheet_id
        self.opener = opener or urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=https_context()))
        self._last_request_at = None

    def request(self, suffix="", query=None, body=None):
        url = self.base + suffix
        if query:
            url += "?" + urllib.parse.urlencode(query)
        req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
                                     headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"},
                                     method="POST" if body is not None else "GET")
        if self._last_request_at is not None:
            delay = 1.05 - (time.monotonic() - self._last_request_at)
            if delay > 0:
                time.sleep(delay)
        self._last_request_at = time.monotonic()
        try:
            with self.opener.open(req, timeout=45) as response:
                raw = response.read(10_000_001)
            if len(raw) > 10_000_000:
                raise PilotError("Google Sheets 응답 크기 상한을 초과했습니다.")
            value = json.loads(raw)
        except urllib.error.HTTPError as error:
            raise PilotError(f"Google Sheets 요청 실패 (HTTP {error.code}). 원본 DB는 유지됩니다. 인증·권한·호출 제한을 확인해주세요.") from None
        except (urllib.error.URLError, OSError, ValueError):
            raise PilotError("Google Sheets 응답을 확인하지 못했습니다. 재실행 시 시트의 연결 키를 다시 읽어 저장 여부를 확인합니다.") from None
        if not isinstance(value, dict) or "error" in value:
            raise PilotError("Google Sheets의 정상 객체 응답이 아닙니다.")
        return value

    def snapshot(self):
        metadata = self.request(query={"fields": "spreadsheetId,sheets(properties,tables)"})
        result = {}
        for sheet in metadata.get("sheets", []):
            prop = sheet["properties"]
            name = prop["title"]
            if name not in HEADERS:
                continue
            size = prop["gridProperties"]["rowCount"]
            if size > 50000:
                raise PilotError("시트가 파일럿 조회 상한 50,000행을 초과했습니다.")
            rows = []
            for start in range(1, size + 1, 500):
                end = min(start + 499, size)
                a1 = "'" + name.replace("'", "''") + f"'!A{start}:{column_name(len(HEADERS[name]))}{end}"
                data = self.request("/values/" + urllib.parse.quote(a1, safe=""),
                                    query={"valueRenderOption": "FORMULA", "majorDimension": "ROWS"})
                values = data.get("values", [])
                if not isinstance(values, list) or any(not isinstance(row, list) for row in values):
                    raise PilotError("시트 행 응답 형식이 올바르지 않습니다.")
                rows.extend(values + [[] for _ in range(end - start + 1 - len(values))])
            while rows and not rows[-1]:
                rows.pop()
            result[name] = rows
        return metadata, result


def sync(source, client, state_path, dry_run=False, max_batches=MAX_BATCHES_PER_RUN):
    state_path = Path(state_path)
    if not isinstance(max_batches, int) or isinstance(max_batches, bool) or max_batches < 1:
        raise PilotError("실행당 시트 배치 수는 1 이상의 정수여야 합니다.")
    with run_lock(state_path):
        previous = json.loads(state_path.read_text()) if state_path.exists() else {}
        seed_context = not previous.get("review_context_initialized", False)
        state = {"status": "pending", "started_at": now(), "source_digest": source["source_digest"],
                 "last_success_at": previous.get("last_success_at"), "coverage": source.get("coverage", {}),
                 "batches_completed": 0, "review_context_initialized": previous.get("review_context_initialized", False)}
        def save():
            state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = state_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
            os.chmod(temporary, 0o600)
            temporary.replace(state_path)
        if not dry_run:
            save()
        try:
            metadata, snapshot = client.snapshot()
            plan = build_plan(source, metadata, snapshot, seed_review_context=seed_context)
            batches = list(request_batches(plan["requests"]))
            if dry_run:
                return {"status": "dry_run", "changed_rows": plan["changed_rows"], "capacity": plan["capacity"],
                        "coverage": plan["coverage"], "warnings": plan["warnings"], "planned_batches": len(batches),
                        "max_batches_per_run": max_batches, "initial_review_context_fill": seed_context}
            state.update(changed_rows=plan["changed_rows"], warnings=plan["warnings"],
                         capacity_before=plan["capacity"], capacity=plan["capacity"])
            while plan["requests"] and state["batches_completed"] < max_batches:
                batch = next(request_batches(plan["requests"]))
                # A fresh read prevents using stale positions after a user sort.
                fresh_meta, fresh_snapshot = client.snapshot()
                if metadata != fresh_meta or snapshot != fresh_snapshot:
                    raise PilotError("조회 도중 시트가 변경됐습니다. 변경 완료 후 다시 실행해주세요.")
                client.request(":batchUpdate", body={"requests": batch})
                metadata, snapshot = client.snapshot()
                remaining = build_plan(source, metadata, snapshot, seed_review_context=seed_context)
                if remaining["requests"] == plan["requests"]:
                    raise PilotError("시트 반영 후 원본 대조가 일치하지 않습니다. 완료 처리하지 않습니다.")
                plan = remaining
                state["batches_completed"] += 1
                state["remaining_rows"] = plan["changed_rows"]
                state["capacity"] = plan["capacity"]
                save()
            if plan["requests"]:
                state.update(status="pending", reason="batch_budget_reached", remaining_rows=plan["changed_rows"],
                             retry_command="python3 -m sns_comment sheets sync")
                save()
                return state
            # A fresh read also verifies a no-op run, which has no write loop.
            final_meta, final_snapshot = client.snapshot()
            if build_plan(source, final_meta, final_snapshot, seed_review_context=seed_context)["requests"]:
                raise PilotError("시트 최종 대조가 일치하지 않습니다. 완료 처리하지 않습니다.")
            state.update(status="complete", last_success_at=now(), remaining_rows=plan["changed_rows"],
                         review_context_initialized=True)
            save()
            return state
        except PilotError as error:
            if not dry_run:
                state.update(status="pending", error=str(error))
                save()
            raise
