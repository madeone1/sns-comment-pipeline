import contextlib
import copy
import datetime as dt
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from sns_comment.account import AccountCollector, collect_continuously, collection_window
from sns_comment.api import GraphError, PilotError
from sns_comment.collector import Collector
from sns_comment.demo import DemoGraph, comment
from sns_comment.sheets import read_source
from sns_comment.storage import Store
from sns_comment.__main__ import main


START, END = "2026-10-28T23:00:00+00:00", "2026-11-04T23:00:00+00:00"
POST = "https://www.instagram.com/reel/DfCKxEYlZVv/"
CONFIG = {"expected_username": "waterbomb_official", "account_id": "100", "post_url": POST,
          "api_version": "v26.0", "collection": {"mode": "account", "lookback_days": 7,"media_scope":"all_accessible"}}


class AccountGraph(DemoGraph):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.responses[("100/media", None)]["paging"] = {"next": "https://graph.facebook.com/100/media?after=media2"}
        self.responses[("100/media", "media2")] = {"data": [
            {"id": "201", "permalink": "https://www.instagram.com/p/NewPost/", "timestamp": "2026-11-03T00:00:00Z"},
            {"id": "202", "permalink": "https://www.instagram.com/p/VeryOld/", "timestamp": "2024-01-01T00:00:00Z"},
        ]}
        self.responses[("201/comments", None)] = {"data": [comment("500", "[합성] 두 번째 게시물", "2026-11-04T01:00:00Z",
            replies={"data": [comment("501", "[합성] 시작 포함", START), comment("502", "[합성] 끝 미포함", END)]})]}
        self.responses[("202/comments", None)] = {"data": []}

    def get(self, path, **params):
        self.calls.append((path, params.get("after")))
        return super().get(path, **params)


class AccountCollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "comments.sqlite3"
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def collect(self, graph=None, **kwargs):
        restart = kwargs.pop("restart", False)
        return AccountCollector(graph or AccountGraph(), self.store, **kwargs).collect_account(START, END, "100", restart=restart)

    def test_entire_account_catalog_old_posts_and_recent_replies(self):
        graph = AccountGraph()
        result = self.collect(graph)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["media_discovered"], 3)
        self.assertEqual(result["media_completed"], 3)
        self.assertTrue(result["catalog_complete"])
        self.assertIn(("100/media", "media2"), graph.calls)
        self.assertIn(("200/comments", None), graph.calls)
        self.assertIn(("202/comments", None), graph.calls)
        self.assertEqual(result["stored_in_window"]["stored_comments_and_replies"], 6)
        self.assertEqual(self.store.summary()["stored_comments_and_replies"], 8)
        self.assertEqual(self.store.db.execute("SELECT parent_id FROM comments WHERE id='400'").fetchone()[0], "300")

    def test_written_window_includes_start_and_excludes_end(self):
        result = self.collect()
        self.assertEqual(result["stored_in_window"], self.store.summary("100", start=START, end=END))
        source = read_source(self.path, CONFIG, collection_period=(START, END))
        ids = {r[2] for r in source["tabs"]["댓글 원본"][1:]}
        self.assertIn("501", ids)
        self.assertNotIn("502", ids)
        self.assertNotIn("300", ids)
        self.assertEqual(source["coverage"]["raw_rows_targeted"], 6)
        self.assertEqual(source["coverage"]["stored_comments_and_replies"], 8)
        self.assertEqual(source["coverage"]["account_scope_status"], "complete")

    def test_request_budget_restarts_at_saved_reply_and_parent_pages(self):
        first = self.collect(max_requests=5)
        self.assertEqual(first["status"], "partial")
        self.assertEqual(first["reason"], "request_budget_reached")
        checkpoint = self.store.scope_edge(first["scope_id"], "200", "replies:300")
        self.assertEqual(checkpoint["next_cursor"], "reply2")
        self.store.close()
        self.store = Store(self.path)
        graph = AccountGraph()
        second = self.collect(graph)
        self.assertEqual(second["scope_id"], first["scope_id"])
        self.assertEqual(second["status"], "complete")
        self.assertNotIn(("100/media", None), graph.calls)
        self.assertNotIn(("200/comments", None), graph.calls)
        self.assertIn(("300/replies", "reply2"), graph.calls)
        self.assertIn(("200/comments", "page2"), graph.calls)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 8)

    def test_catalog_page_budget_is_not_account_completion(self):
        first = self.collect(max_media_pages=1)
        self.assertEqual(first["status"], "partial")
        self.assertFalse(first["media_total_known"])
        self.assertFalse(first["catalog_complete"])
        self.assertEqual(first["media_completed"], 1)
        graph = AccountGraph()
        second = self.collect(graph)
        self.assertEqual(second["status"], "complete")
        self.assertNotIn(("200/comments", None), graph.calls)
        self.assertNotIn(("100/media", None), graph.calls)
        self.assertIn(("100/media", "media2"), graph.calls)

    def test_completed_scope_is_reused_and_explicit_restart_reconciles_without_duplicates(self):
        first = self.collect()
        graph = AccountGraph()
        reused = self.collect(graph)
        self.assertTrue(reused["reused_completed_scope"])
        self.assertEqual(graph.calls, [("100", None)])
        restarted = self.collect(restart=True)
        self.assertNotEqual(restarted["scope_id"], first["scope_id"])
        self.assertEqual(restarted["status"], "complete")
        self.assertEqual(self.store.summary()["stored_comments_and_replies"], 8)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 8)

    def test_catalog_invalid_cursor_has_one_first_page_recovery(self):
        first = self.collect(max_media_pages=1)
        graph = AccountGraph()
        graph.responses[("100/media", "media2")] = GraphError(400, 100)
        graph.responses[("100/media", None)]["paging"]["next"] = "https://graph.facebook.com/100/media?after=fresh"
        graph.responses[("100/media", "fresh")] = AccountGraph().responses[("100/media", "media2")]
        result = self.collect(graph)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["scope_id"], first["scope_id"])
        self.assertEqual(result["cursor_restarts"], 1)
        self.assertEqual(result["media_discovered"], 3)

    def test_reply_invalid_cursor_replays_that_edge_only(self):
        self.collect(max_requests=5)
        graph = AccountGraph()
        graph.responses[("300/replies", "reply2")] = GraphError(400, 100)
        graph.responses[("300/replies", None)] = {"data": [comment("400", "[합성] 새 답글", "2026-11-04T00:10:00Z")],
            "paging": {"next": "https://graph.facebook.com/300/replies?after=fresh-reply"}}
        graph.responses[("300/replies", "fresh-reply")] = AccountGraph().responses[("300/replies", "reply2")]
        result = self.collect(graph)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["cursor_restarts"], 1)
        self.assertNotIn(("200/comments", None), graph.calls)
        self.assertEqual(self.store.summary()["stored_comments_and_replies"], 8)

    def test_one_failed_media_does_not_hide_other_media_or_advance_failed_completion(self):
        Collector(DemoGraph(), self.store).collect(POST)
        before = self.store.db.execute("SELECT last_completed_at FROM media WHERE id='200'").fetchone()[0]
        graph = AccountGraph()
        graph.responses[("200/comments", "page2")]["paging"] = {"next": "https://graph.facebook.com/200/comments?after=page2"}
        result = self.collect(graph)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["media_completed"], 2)
        self.assertEqual(result["media_pending"], 1)
        self.assertEqual(self.store.db.execute("SELECT last_completed_at FROM media WHERE id='200'").fetchone()[0], before)
        self.assertIsNone(result["last_scope_completed_at"])

    def test_missing_api_data_is_failure_and_not_successful_zero(self):
        graph = AccountGraph()
        graph.responses[("100/media", None)] = {"unexpected": []}
        with self.assertRaises(PilotError):
            self.collect(graph)
        scope = self.store.account_scopes()[0]
        self.assertFalse(scope["catalog_complete"])
        self.assertEqual(scope["status"], "partial")

    def test_normal_empty_account_can_complete(self):
        graph = AccountGraph()
        graph.responses[("100/media", None)] = {"data": []}
        result = self.collect(graph)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["media_discovered"], 0)
        self.assertEqual(result["stored_in_window"]["stored_comments_and_replies"], 0)

    def test_exact_resume_id_does_not_switch_to_a_newer_scope(self):
        first = self.collect(max_requests=4)
        newer = self.collect(restart=True, max_requests=4)
        self.assertNotEqual(first["scope_id"], newer["scope_id"])
        result = AccountCollector(AccountGraph(), self.store).collect_account(
            START, END, account_id="100", resume_scope_id=first["scope_id"])
        self.assertEqual(result["scope_id"], first["scope_id"])
        self.assertEqual(result["status"], "complete")
        self.assertEqual(self.store.scope(newer["scope_id"])["status"], "partial")

    def test_account_log_counts_all_stored_history_and_not_last_media(self):
        self.collect()
        source = read_source(self.path, CONFIG, collection_period=(START, END))
        log = source["tabs"]["수집 로그"][-1]
        self.assertEqual(log[2], "")
        self.assertEqual(log[11], 8)
        self.assertEqual(source["coverage"]["raw_rows_targeted"], 6)

    def test_rate_limit_stops_further_media_and_keeps_scope_incomplete(self):
        graph = AccountGraph()
        graph.responses[("201/comments", None)] = GraphError(429, 4)
        with self.assertRaises(PilotError):
            self.collect(graph)
        self.assertNotIn(("200/comments", None), graph.calls)
        self.assertNotIn(("202/comments", None), graph.calls)
        self.assertEqual(self.store.account_scopes()[0]["status"], "partial")

    def test_continuous_collection_resumes_local_budgets_with_one_client(self):
        graph, progress = AccountGraph(), []
        result = collect_continuously(graph, self.store, "waterbomb_official", START, END,
            account_id="100", max_requests=4, max_media_pages=1, max_total_requests=40, progress=progress.append)
        self.assertEqual(result["status"], "complete")
        self.assertGreater(result["passes"], 1)
        self.assertEqual(result["api_requests"], graph.request_count)
        self.assertEqual(len(self.store.account_scopes()), 1)
        self.assertEqual(result["stored_in_window"]["stored_comments_and_replies"], 6)
        self.assertEqual(progress[-1]["status"], "complete")

    def test_continuous_collection_stops_at_total_budget_without_success(self):
        graph = AccountGraph()
        result = collect_continuously(graph, self.store, "waterbomb_official", START, END,
            account_id="100", max_requests=4, max_total_requests=6)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["reason"], "total_request_budget_reached")
        self.assertLessEqual(graph.request_count, 6)
        self.assertIsNone(result["last_scope_completed_at"])

    def test_continuous_cli_reads_hidden_token_only_once_across_passes(self):
        output = io.StringIO()
        with mock.patch("sns_comment.__main__.read_config", return_value=CONFIG), \
                mock.patch("sns_comment.__main__.GraphClient", return_value=AccountGraph()) as client, \
                mock.patch.dict("os.environ", {}, clear=True), \
                mock.patch("sys.stdin.isatty", return_value=True), \
                mock.patch("getpass.getpass", return_value="synthetic-token") as prompt, \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            code = main(["run", "--db", str(self.path), "--end", END,
                         "--until-complete", "--max-requests", "4", "--max-total-requests", "40"])
        result = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(prompt.call_count, 1)
        self.assertEqual(client.call_count, 1)
        self.assertGreater(result["passes"], 1)
        self.assertNotIn("synthetic-token", output.getvalue())

    def test_recent_media_is_processed_first_without_omitting_old_posts(self):
        graph = AccountGraph()
        self.collect(graph)
        self.assertLess(graph.calls.index(("201/comments", None)), graph.calls.index(("200/comments", None)))
        self.assertIn(("202/comments", None), graph.calls)

    def test_account_demo_cli_can_resume_without_network_or_credentials(self):
        output = io.StringIO()
        with mock.patch("sns_comment.__main__.GraphClient", side_effect=AssertionError("network")), \
                mock.patch.dict("os.environ", {}, clear=True), contextlib.redirect_stdout(output):
            first_code = main(["account-demo", "--db", str(self.path), "--end", END, "--max-requests", "4"])
            first = json.loads(output.getvalue())
            output.seek(0)
            output.truncate()
            second_code = main(["account-demo", "--db", str(self.path), "--resume-scope", first["scope_id"]])
            second = json.loads(output.getvalue())
        self.assertEqual((first_code, second_code), (1, 0))
        self.assertFalse(second["live_instagram_called"])
        self.assertEqual(first["scope_id"], second["scope_id"])
        self.assertIn("account-demo", first["resume_command"])
        self.assertIn(str(self.path), first["resume_command"])
        self.assertEqual(second["stored_in_window"]["stored_comments_and_replies"], 6)

    def test_account_mismatch_stops_before_catalog(self):
        graph = AccountGraph()
        graph.responses[("100", None)]["username"] = "wrong_account"
        with self.assertRaises(PilotError):
            self.collect(graph)
        self.assertEqual(graph.calls, [("100", None)])
        self.assertEqual(self.store.account_scopes(), [])

    def test_page_and_scope_cursor_and_reply_tasks_roll_back_together(self):
        scope = self.store.collection_scope("100", "waterbomb_official", START, END)
        self.store.save_catalog_page(scope["id"], "100", [{"id": "200", "permalink": POST}], None)
        with self.assertRaises(PilotError):
            self.store.save_page("test", "100", "200", "comments", [comment("700", "ok", START), comment("701", "bad", "invalid")],
                                 "next", "waterbomb_official", scope_id=scope["id"])
        self.assertEqual(self.store.summary()["stored_comments_and_replies"], 0)
        self.assertIsNone(self.store.scope_edge(scope["id"], "200", "comments")["next_cursor"])
        self.assertIsNone(self.store.pending_scope_reply(scope["id"], "200"))

    def test_raw_and_queue_do_not_persist_expansion_next_urls(self):
        self.collect()
        raw = " ".join(r[0] for r in self.store.db.execute("SELECT raw_json FROM observations"))
        self.assertNotIn("graph.facebook.com", raw)
        self.assertNotIn("paging", raw)
        self.assertNotIn("next_cursor", raw)

    def test_cli_account_mode_returns_partial_and_resume_command(self):
        output = io.StringIO()
        with mock.patch("sns_comment.__main__.read_config", return_value=CONFIG), \
                mock.patch("sns_comment.__main__.GraphClient", return_value=AccountGraph()), \
                mock.patch.dict("os.environ", {"SNS_IG_ACCESS_TOKEN": "synthetic-token"}), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            status = main(["run", "--db", str(self.path), "--start", START, "--end", END, "--max-requests", "4"])
        result = json.loads(output.getvalue())
        self.assertEqual(status, 1)
        self.assertEqual(result["mode"], "live_api_account_window")
        self.assertEqual(result["status"], "partial")
        self.assertIn("--resume-scope", result["resume_command"])


class WindowTests(unittest.TestCase):
    def test_default_is_seven_days_ending_at_latest_korean_0800(self):
        self.assertEqual(collection_window(instant=dt.datetime.fromisoformat("2026-11-05T10:00:00+09:00")), (START, END))

    def test_before_0800_uses_preceding_cutoff(self):
        start, end = collection_window(instant=dt.datetime.fromisoformat("2026-11-05T07:59:59+09:00"))
        self.assertEqual(end, "2026-11-03T23:00:00+00:00")
        self.assertEqual(dt.datetime.fromisoformat(end) - dt.datetime.fromisoformat(start), dt.timedelta(days=7))

    def test_invalid_or_naive_windows_are_rejected(self):
        for kwargs in ({"days": 0}, {"start": START}, {"start": END, "end": START},
                       {"end": "2026-11-05T08:00:00"}, {"instant": dt.datetime(2026, 11, 5)}):
            with self.subTest(kwargs=kwargs), self.assertRaises(PilotError):
                collection_window(**kwargs)


if __name__ == "__main__":
    unittest.main()
