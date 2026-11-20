import copy
import contextlib
import datetime as dt
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from sns_comment.api import PilotError
from sns_comment.collector import Collector
from sns_comment.demo import DemoGraph
from sns_comment.sheets import HEADERS, SheetsClient, build_plan, cell, local_time, read_source, request_batches, sync
from sns_comment.storage import Store
from sns_comment.__main__ import main


POST = "https://www.instagram.com/reel/DfCKxEYlZVv/"
AS_OF = dt.datetime.fromisoformat("2026-11-05T09:00:00+09:00")
CONFIG = {"expected_username": "waterbomb_official", "account_id": "100", "post_url": POST, "api_version": "v26.0"}


class MemorySheet:
    def __init__(self, source, fail_after_write=False):
        self.metadata = {"sheets": [{"properties": {"title": name, "sheetId": i,
            "gridProperties": {"rowCount": 1000, "columnCount": len(HEADERS[name])}}}
            for i, name in enumerate(HEADERS)]}
        self.rows = {name: [header[:]] for name, header in HEADERS.items()}
        self.fail_after_write = fail_after_write
        self.writes = 0
        self.bodies = []
        self.fail_on_write = None

    def snapshot(self):
        return copy.deepcopy(self.metadata), copy.deepcopy(self.rows)

    def request(self, suffix, body):
        self.writes += 1
        self.bodies.append(copy.deepcopy(body))
        for request in body["requests"]:
            if "appendDimension" in request:
                op = request["appendDimension"]
                self.metadata["sheets"][op["sheetId"]]["properties"]["gridProperties"]["rowCount"] += op["length"]
            if "updateTable" in request:
                op = request["updateTable"]["table"]
                for sheet in self.metadata["sheets"]:
                    for table in sheet.get("tables", []):
                        if table["tableId"] == op["tableId"]:
                            table["range"] = copy.deepcopy(op["range"])
            if "updateCells" not in request:
                continue
            update = request["updateCells"]
            area = update["range"]
            name = list(HEADERS)[area["sheetId"]]
            target = self.rows[name]
            for i, data in enumerate(update["rows"], area["startRowIndex"]):
                while len(target) <= i:
                    target.append([])
                while len(target[i]) < len(HEADERS[name]):
                    target[i].append("")
                for j, entry in enumerate(data["values"], area["startColumnIndex"]):
                    value = entry.get("userEnteredValue", {})
                    target[i][j] = next(iter(value.values()), "")
        if self.fail_after_write or self.fail_on_write == self.writes:
            self.fail_after_write = False
            raise PilotError("응답 유실 재현")
        return {}


class SheetsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "comments.sqlite3"
        self.store = Store(self.path)
        Collector(DemoGraph(), self.store).collect(POST)
        self.source = read_source(self.path, CONFIG, as_of=AS_OF)
        self.remote = MemorySheet(self.source)
        self.state = Path(self.temp.name) / "sheets-state.json"

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_source_is_read_only_complete_and_preserves_ids(self):
        before = self.path.read_bytes()
        source = read_source(self.path, CONFIG, as_of=AS_OF)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(source["tabs"]), list(HEADERS))
        self.assertEqual(len(source["tabs"]["댓글 원본"]), 6)
        self.assertIsInstance(source["tabs"]["댓글 원본"][1][2], str)
        self.assertEqual(len(source["tabs"]["수집 로그"]), 2)

    def test_repeat_sync_does_not_duplicate_rows_or_write_again(self):
        sync(self.source, self.remote, self.state)
        second = sync(self.source, self.remote, self.state)
        self.assertEqual(self.remote.writes, 1)
        self.assertTrue(all(v == 0 for v in second["changed_rows"].values()))
        self.assertEqual(len(self.remote.rows["댓글 원본"]), 6)

    def test_human_review_survives_sort_and_raw_change(self):
        sync(self.source, self.remote, self.state)
        review = self.remote.rows["분류와 처리"][1]
        key = review[0]
        review[6], review[11], review[14], review[27] = "콘텐츠 반응", "검토 완료", "직접 작성한 답변", "검수 당시 원본"
        self.remote.rows["분류와 처리"][1:] = reversed(self.remote.rows["분류와 처리"][1:])
        self.remote.rows["댓글 원본"][1:] = reversed(self.remote.rows["댓글 원본"][1:])
        changed = copy.deepcopy(self.source)
        raw = next(r for r in changed["tabs"]["댓글 원본"][1:] if r[0] == key)
        raw[8], raw[14] = "변경된 원문", "new-hash"
        next(r for r in changed["tabs"]["분류와 처리"][1:] if r[0] == key)[3] = "new-hash"
        sync(changed, self.remote, self.state)
        final = next(r for r in self.remote.rows["분류와 처리"][1:] if r[0] == key)
        self.assertEqual(final[3], "new-hash")
        self.assertEqual([final[i] for i in (6, 11, 14, 27)], ["콘텐츠 반응", "검토 완료", "직접 작성한 답변", "검수 당시 원본"])
        self.assertEqual(next(r for r in self.remote.rows["댓글 원본"][1:] if r[0] == key)[8], "변경된 원문")

    def test_missing_text_is_not_confused_with_empty_string(self):
        self.store.db.execute("UPDATE comments SET text=NULL WHERE id='300'")
        self.store.db.execute("UPDATE comments SET text='' WHERE id='301'")
        self.store.db.commit()
        source = read_source(self.path, CONFIG, as_of=AS_OF)
        rows = {r[2]: r for r in source["tabs"]["댓글 원본"][1:]}
        self.assertEqual(rows["300"][9], "미제공")
        self.assertEqual(rows["301"][9], "제공")

    def test_formula_like_text_is_a_literal_and_long_id_stays_a_string(self):
        self.assertEqual(cell('=HYPERLINK("https://example.invalid")')["userEnteredValue"].keys(), {"stringValue"})
        self.assertEqual(cell("18137736370572410"), {"userEnteredValue": {"stringValue": "18137736370572410"}})

    def test_duplicate_or_missing_keys_stop_before_write(self):
        sync(self.source, self.remote, self.state)
        for key in (self.remote.rows["댓글 원본"][1][0], "", 123):
            with self.subTest(key=key):
                rows = copy.deepcopy(self.remote.rows)
                rows["댓글 원본"].append([key, "other value"])
                with self.assertRaises(PilotError):
                    build_plan(self.source, self.remote.metadata, rows)

    def test_changed_schema_stops_without_overwrite(self):
        self.remote.rows["댓글 원본"][0][0] = "다른 문서"
        with self.assertRaises(PilotError):
            sync(self.source, self.remote, self.state)
        self.assertEqual(self.remote.writes, 0)

    def test_lost_response_is_reconciled_without_duplicate_write(self):
        self.remote.fail_after_write = True
        with self.assertRaises(PilotError):
            sync(self.source, self.remote, self.state)
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8"))["status"], "pending")
        result = sync(self.source, self.remote, self.state)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(self.remote.writes, 1)
        self.assertEqual(len(self.remote.rows["댓글 원본"]), 6)

    def test_failed_sync_preserves_last_success(self):
        successful = sync(self.source, self.remote, self.state)
        self.remote.rows["댓글 원본"][0] = ["invalid"]
        with self.assertRaises(PilotError):
            sync(self.source, self.remote, self.state)
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8"))["last_success_at"], successful["last_success_at"])

    def test_dry_run_does_not_write_remote_or_create_state(self):
        result = sync(self.source, self.remote, self.state, dry_run=True)
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(self.remote.writes, 0)
        self.assertFalse(self.state.exists())

    def test_period_counts_use_written_time_and_exclusive_end(self):
        data = read_source(self.path, CONFIG, as_of=AS_OF)
        counts = {row[3]: row[5] for row in data["tabs"]["기간별 요약"][1:]}
        self.assertEqual(counts[local_time("2026-11-04T08:00:00+09:00")], 4)
        self.assertTrue(all("잠정" in row[11] for row in data["tabs"]["기간별 요약"][1:]))

    def test_summary_conclusions_and_recovery_notes_are_preserved(self):
        sync(self.source, self.remote, self.state)
        self.remote.rows["기간별 요약"][1][18] = "운영팀 실행안"
        self.remote.rows["수집 로그"][1][13] = "담당자 확인"
        changed = copy.deepcopy(self.source)
        changed["tabs"]["기간별 요약"][1][5] += 1
        changed["tabs"]["수집 로그"][1][8] += 1
        sync(changed, self.remote, self.state)
        self.assertEqual(self.remote.rows["기간별 요약"][1][18], "운영팀 실행안")
        self.assertEqual(self.remote.rows["수집 로그"][1][13], "담당자 확인")

    def test_read_only_export_does_not_create_missing_db(self):
        missing = Path(self.temp.name) / "missing.sqlite3"
        with self.assertRaises(PilotError):
            read_source(missing, CONFIG)
        self.assertFalse(missing.exists())

    def test_token_never_in_url_or_payload(self):
        from test_pipeline import FakeOpener
        opener = FakeOpener(payload={"spreadsheetId": "sample"})
        client = SheetsClient("PRIVATE_GOOGLE_TOKEN", "verified_sheet_id_123456", opener=opener)
        client.request(query={"fields": "spreadsheetId"})
        self.assertNotIn("PRIVATE_GOOGLE_TOKEN", opener.requests[0].full_url)
        self.assertEqual(opener.requests[0].get_header("Authorization"), "Bearer PRIVATE_GOOGLE_TOKEN")

    def run_pipeline(self, remote):
        config = dict(CONFIG, google_sheets={"owner_email": "syan@madeone.kr", "spreadsheet_id": "verified_sheet_id_123456"})
        output = io.StringIO()
        with mock.patch("sns_comment.__main__.read_config", return_value=config), \
                mock.patch("sns_comment.__main__.ROOT", Path(self.temp.name)), \
                mock.patch("sns_comment.__main__.GraphClient", return_value=DemoGraph()), \
                mock.patch("sns_comment.__main__.SheetsClient", return_value=remote), \
                mock.patch.dict("os.environ", {"SNS_IG_ACCESS_TOKEN": "meta-test-token", "SNS_GOOGLE_ACCESS_TOKEN": "google-test-token"}), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            code = main(["run", "--sync-sheets", "--db", str(self.path)])
        return code, json.loads(output.getvalue())

    def test_live_pipeline_runs_sheet_stage_after_collection(self):
        code, result = self.run_pipeline(self.remote)
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["sheets_sync"]["status"], "complete")
        self.assertEqual(len(self.remote.rows["댓글 원본"]), 6)

    def test_sheet_failure_keeps_collection_and_reports_separate_retry(self):
        self.remote.fail_after_write = True
        code, result = self.run_pipeline(self.remote)
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["sheets_sync"]["status"], "pending")
        self.assertTrue(result["sheets_sync"]["collection_preserved"])
        self.assertEqual(self.store.summary()["stored_comments_and_replies"], 5)

    def test_table_range_and_grid_expand_for_new_records(self):
        sync(self.source, self.remote, self.state)
        changed = copy.deepcopy(self.source)
        for name in ("댓글 원본", "분류와 처리"):
            row = changed["tabs"][name][1][:]
            row[0], row[2] = "100:999", "999"
            changed["tabs"][name].append(row)
        table = self.remote.metadata["sheets"][1]
        table["properties"]["gridProperties"]["rowCount"] = 6
        table["tables"] = [{"tableId": "raw-table", "range": {"sheetId": 1, "startRowIndex": 0,
            "endRowIndex": 6, "startColumnIndex": 0, "endColumnIndex": len(HEADERS["댓글 원본"])}}]
        plan = build_plan(changed, self.remote.metadata, self.remote.rows)
        self.assertIn({"appendDimension": {"sheetId": 1, "dimension": "ROWS", "length": 1}}, plan["requests"])
        self.assertEqual(next(r["updateTable"]["table"]["range"]["endRowIndex"] for r in plan["requests"] if "updateTable" in r), 7)

    def expanded_source(self, count, text_size=10):
        result = copy.deepcopy(self.source)
        for name in ("댓글 원본", "분류와 처리"):
            seed = result["tabs"][name][1]
            rows = []
            for i in range(count):
                row = seed[:]
                row[0], row[2] = f"100:{10000+i}", str(10000+i)
                if name == "댓글 원본":
                    row[8] = "한" * text_size
                rows.append(row)
            result["tabs"][name] = [HEADERS[name][:]] + rows
        result["coverage"]["stored_comments_and_replies"] = count
        result["coverage"]["raw_rows_targeted"] = count
        result["source_digest"] = f"synthetic-{count}-{text_size}"
        return result

    def test_large_source_uses_bounded_utf8_batches_without_truncation(self):
        source = self.expanded_source(301, text_size=5000)
        result = sync(source, self.remote, self.state)
        self.assertEqual(result["status"], "complete")
        self.assertGreater(self.remote.writes, 3)
        for body in self.remote.bodies:
            self.assertLessEqual(len(json.dumps(body, ensure_ascii=False).encode()), 1_500_000)
            self.assertLessEqual(sum(len(r.get("updateCells", {}).get("rows", [])) for r in body["requests"]), 500)
        self.assertEqual(len(self.remote.rows["댓글 원본"]), 302)
        self.assertEqual(self.remote.rows["댓글 원본"][-1][8], "한" * 5000)

    def test_batch_budget_and_rerun_keep_pending_rows_and_keys(self):
        source = self.expanded_source(1201)
        pending = sync(source, self.remote, self.state, max_batches=1)
        self.assertEqual(pending["status"], "pending")
        self.assertIsNone(pending["last_success_at"])
        self.assertGreater(sum(pending["remaining_rows"].values()), 0)
        final = sync(source, self.remote, self.state)
        self.assertEqual(final["status"], "complete")
        self.assertEqual(len(self.remote.rows["댓글 원본"]), 1202)
        self.assertEqual(len({r[0] for r in self.remote.rows["댓글 원본"][1:]}), 1201)
        self.assertTrue(all(v == 0 for v in final["remaining_rows"].values()))

    def test_mid_batch_response_loss_can_resume(self):
        source = self.expanded_source(1001)
        self.remote.fail_on_write = 2
        with self.assertRaises(PilotError):
            sync(source, self.remote, self.state)
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state["batches_completed"], 1)
        self.assertEqual(state["status"], "pending")
        result = sync(source, self.remote, self.state)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(self.remote.rows["댓글 원본"]), 1002)

    def test_last_seen_only_change_does_not_resend_raw_text(self):
        sync(self.source, self.remote, self.state)
        changed = copy.deepcopy(self.source)
        changed["tabs"]["댓글 원본"][1][13] += 1
        plan = build_plan(changed, self.remote.metadata, self.remote.rows)
        raw_updates = [r["updateCells"] for r in plan["requests"] if "updateCells" in r and r["updateCells"]["range"]["sheetId"] == 1]
        self.assertEqual(len(raw_updates), 1)
        self.assertEqual(raw_updates[0]["range"]["startColumnIndex"], 13)
        self.assertNotIn(changed["tabs"]["댓글 원본"][1][8], json.dumps(raw_updates, ensure_ascii=False))

    def test_capacity_warning_does_not_remove_or_select_away_originals(self):
        source = self.expanded_source(10001)
        plan = build_plan(source, self.remote.metadata, self.remote.rows)
        self.assertTrue(plan["warnings"])
        self.assertEqual(plan["coverage"]["stored_comments_and_replies"], 10001)
        self.assertEqual(plan["capacity"]["댓글 원본"]["projected_rows"], 10001)
        self.assertFalse(any("deleteDimension" in r or "deleteSheet" in r for r in plan["requests"]))

    def test_full_database_period_counts_survive_more_than_ten_thousand_records(self):
        from sns_comment.demo import comment
        items = [comment(str(20000 + i), "[합성] 규모 검증", "2026-11-04T01:00:00Z") for i in range(10001)]
        self.store.save_page("scale-check", "100", "200", "comments", items, None, "waterbomb_official")
        source = read_source(self.path, CONFIG, as_of=AS_OF)
        self.assertEqual(len(source["tabs"]["댓글 원본"]) - 1, 10006)
        self.assertEqual(source["coverage"]["stored_comments_and_replies"], 10006)
        self.assertEqual(sum(r[5] for r in source["tabs"]["기간별 요약"][1:]), 10006)
        self.assertEqual(self.store.summary()["stored_comments_and_replies"], 10006)

    def test_period_coverage_uses_oldest_target_completion(self):
        with self.store.db:
            self.store.db.execute("INSERT INTO media(account_id,id,permalink,raw_json) VALUES('100','201','https://www.instagram.com/p/Other/','{}')")
        source = read_source(self.path, CONFIG, as_of=AS_OF)
        self.assertTrue(all(r[13] == "" for r in source["tabs"]["기간별 요약"][1:]))
        self.assertTrue(all("수집 미확인" in r[11] for r in source["tabs"]["기간별 요약"][1:]))

    def test_initial_empty_review_context_is_filled_once_and_manual_clear_survives(self):
        sync(self.source, self.remote, self.state)
        self.remote.rows["분류와 처리"][1][20] = ""
        sync(self.source, self.remote, self.state)
        self.assertEqual(self.remote.rows["분류와 처리"][1][20], "")
        legacy_state = json.loads(self.state.read_text(encoding="utf-8"))
        legacy_state.pop("review_context_initialized")
        self.state.write_text(json.dumps(legacy_state), encoding="utf-8")
        sync(self.source, self.remote, self.state)
        self.assertTrue(self.remote.rows["분류와 처리"][1][20].startswith("분류 전 원문 발췌"))

    def test_single_oversized_row_fails_without_silent_text_truncation(self):
        request = {"updateCells": {"range": {"sheetId": 1, "startRowIndex": 1, "endRowIndex": 2,
            "startColumnIndex": 0, "endColumnIndex": 1}, "rows": [{"values": [cell("한" * 2000)]}], "fields": "userEnteredValue"}}
        with self.assertRaises(PilotError):
            list(request_batches([request], max_bytes=2000))

    def test_date_format_requests_precede_first_new_data_batch(self):
        plan = build_plan(self.source, self.remote.metadata, self.remote.rows)
        raw = [r for r in plan["requests"] if next(iter(r.values())).get("sheetId", next(iter(r.values())).get("range", {}).get("sheetId")) == 1]
        first_data = next(i for i, r in enumerate(raw) if "updateCells" in r)
        self.assertTrue(any("repeatCell" in r for r in raw[:first_data]))


if __name__ == "__main__":
    unittest.main()
