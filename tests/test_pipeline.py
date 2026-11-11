import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
import urllib.error
import urllib.parse
from unittest import mock

from sns_comment.api import GraphClient, GraphError, PilotError, https_context, next_cursor, page_data
from sns_comment.collector import Collector, canonical_url, post_key
from sns_comment.demo import DemoGraph, comment
from sns_comment.storage import Store, run_lock, timestamp
from sns_comment.__main__ import main


POST = "https://www.instagram.com/reel/DfCKxEYlZVv/"


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "comments.sqlite3"
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def collect(self, graph=None):
        return Collector(graph or DemoGraph(), self.store).collect(POST)

    def test_all_parent_reply_pages_and_repeat_run(self):
        first = self.collect()
        before = self.store.db.execute("SELECT first_seen_at FROM comments WHERE id='300'").fetchone()[0]
        self.store.close()
        self.store = Store(self.path)
        second = self.collect()
        self.assertEqual(first["status"], "complete")
        self.assertEqual(second["stored"]["stored_comments_and_replies"], 5)
        self.assertEqual(second["stored"]["replies"], 2)
        self.assertEqual(self.store.db.execute("SELECT first_seen_at FROM comments WHERE id='300'").fetchone()[0], before)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 5)
        self.assertEqual(self.store.db.execute("SELECT parent_id FROM comments WHERE id='401'").fetchone()[0], "300")
        # Same text, distinct IDs must both survive.
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM comments WHERE text='[합성] 같은 문구'").fetchone()[0], 2)

    def test_partial_reply_failure_keeps_raw_and_prior_completion(self):
        self.collect()
        before = self.store.db.execute("SELECT last_completed_at FROM media").fetchone()[0]
        graph = DemoGraph()
        graph.responses[("300/replies", "reply2")] = GraphError(403, 10)
        with self.assertRaises(PilotError):
            self.collect(graph)
        run = self.store.runs()[0]
        self.assertEqual(run["status"], "partial")
        self.assertEqual(run["stage"], "replies:300")
        self.assertEqual(self.store.db.execute("SELECT last_completed_at FROM media").fetchone()[0], before)
        self.assertEqual(self.store.summary()["stored_comments_and_replies"], 5)
        self.assertEqual(self.collect()["status"], "complete")

    def test_page_and_checkpoint_roll_back_together(self):
        self.collect()
        old_checkpoint = dict(self.store.db.execute("SELECT * FROM checkpoints WHERE edge='comments'").fetchone())
        items = [comment("800", "valid", "2026-11-04T00:00:00Z"),
                 comment("801", "invalid time", "not-a-timestamp")]
        with self.assertRaises(PilotError):
            self.store.save_page("test-run", "100", "200", "comments", items, "cursor3", "waterbomb_official")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM comments WHERE id IN ('800','801')").fetchone()[0], 0)
        self.assertEqual(dict(self.store.db.execute("SELECT * FROM checkpoints WHERE edge='comments'").fetchone()), old_checkpoint)

    def test_original_observation_preserved_on_edit(self):
        self.collect()
        graph = DemoGraph()
        graph.responses[("200/comments", "page2")]["data"][0]["text"] = "[합성] 변경된 원문"
        self.collect(graph)
        originals = self.store.db.execute("SELECT raw_json FROM observations WHERE comment_id='301'").fetchall()
        self.assertEqual(len(originals), 2)
        self.assertIn("[합성] 같은 문구", {json.loads(row[0])["text"] for row in originals})
        self.assertEqual(self.store.db.execute("SELECT text FROM comments WHERE id='301'").fetchone()[0], "[합성] 변경된 원문")

    def test_missing_and_null_text_preserve_raw_and_differ_from_empty(self):
        graph = DemoGraph()
        del graph.responses[("200/comments", None)]["data"][0]["text"]
        graph.responses[("200/comments", "page2")]["data"][0]["text"] = None
        graph.responses[("300/replies", "reply2")]["data"][0]["text"] = ""
        result = self.collect(graph)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["stored"]["text_unavailable"], 2)
        self.assertEqual(result["stored"]["text_available"], 3)
        rows = {row["id"]: row for row in self.store.db.execute("SELECT id,text,raw_json FROM comments")}
        self.assertIsNone(rows["300"]["text"])
        self.assertNotIn("text", json.loads(rows["300"]["raw_json"]))
        self.assertIsNone(json.loads(rows["301"]["raw_json"])["text"])
        self.assertEqual(rows["401"]["text"], "")

    def test_text_returning_later_keeps_original_unavailable_observation(self):
        graph = DemoGraph()
        del graph.responses[("200/comments", None)]["data"][0]["text"]
        self.collect(graph)
        first_seen = self.store.db.execute("SELECT first_seen_at FROM comments WHERE id='300'").fetchone()[0]
        result = self.collect()
        self.assertEqual(result["stored"]["stored_comments_and_replies"], 5)
        self.assertEqual(result["stored"]["text_unavailable"], 0)
        row = self.store.db.execute("SELECT text,first_seen_at FROM comments WHERE id='300'").fetchone()
        self.assertEqual(row["text"], "[합성] 오래된 부모 댓글")
        self.assertEqual(row["first_seen_at"], first_seen)
        observations = [json.loads(row[0]) for row in self.store.db.execute("SELECT raw_json FROM observations WHERE comment_id='300'")]
        self.assertEqual(len(observations), 2)
        self.assertEqual(sum("text" not in raw for raw in observations), 1)

    def test_legacy_required_text_database_preserves_existing_history(self):
        self.collect()
        path = Path(self.temp.name) / "legacy.sqlite3"
        with contextlib.closing(sqlite3.connect(path)) as legacy, legacy:
            legacy.execute("""CREATE TABLE comments (
              account_id TEXT NOT NULL, id TEXT NOT NULL, media_id TEXT NOT NULL,
              parent_id TEXT, author_id TEXT, username TEXT, is_owner INTEGER,
              text TEXT NOT NULL, written_at TEXT NOT NULL,
              first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
              payload_hash TEXT NOT NULL, raw_json TEXT NOT NULL,
              PRIMARY KEY(account_id,id),
              FOREIGN KEY(account_id,media_id) REFERENCES media(account_id,id)
            )""")
            for table in ("media", "runs", "observations", "checkpoints", "comments"):
                if table != "comments":
                    schema = self.store.db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
                    legacy.execute(schema)
                rows = [tuple(row) for row in self.store.db.execute(f"SELECT * FROM {table}")]
                if rows:
                    placeholders = ",".join("?" for _ in rows[0])
                    legacy.executemany(f"INSERT INTO {table} VALUES({placeholders})", rows)
            expected_rows = legacy.execute("SELECT * FROM comments ORDER BY id").fetchall()
            expected_observations = legacy.execute("SELECT * FROM observations ORDER BY comment_id").fetchall()
        reopened = Store(path)
        try:
            self.assertEqual([tuple(row) for row in reopened.db.execute("SELECT * FROM comments ORDER BY id")], expected_rows)
            self.assertEqual([tuple(row) for row in reopened.db.execute("SELECT * FROM observations ORDER BY comment_id")], expected_observations)
            graph = DemoGraph()
            del graph.responses[("200/comments", None)]["data"][0]["text"]
            result = Collector(graph, reopened).collect(POST)
            self.assertEqual(result["stored"]["text_unavailable"], 1)
            self.assertEqual(reopened.db.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            reopened.close()

    def test_written_time_window_not_discovery_time(self):
        self.collect()
        counts = self.store.summary(start=timestamp("2026-11-03T08:00:00+09:00"),
                                    end=timestamp("2026-11-04T08:00:00+09:00"))
        self.assertEqual(counts["stored_comments_and_replies"], 0)
        counts = self.store.summary(start=timestamp("2026-11-04T08:00:00+09:00"),
                                    end=timestamp("2026-11-05T08:00:00+09:00"))
        self.assertEqual(counts["stored_comments_and_replies"], 4)
        self.assertEqual(counts["official_account"], 1)
        self.assertEqual(counts["known_audience"], 3)

    def test_account_mismatch_stops_before_reading_comments(self):
        graph = DemoGraph()
        graph.responses[("100", None)]["username"] = "different_account"
        with self.assertRaises(PilotError):
            self.collect(graph)
        self.assertEqual(graph.request_count, 2)
        self.assertEqual(self.store.runs()[0]["status"], "failed")
        self.assertEqual(self.store.summary()["stored_comments_and_replies"], 0)

    def test_explicit_account_id_works_without_discovery(self):
        graph = DemoGraph()
        graph.responses[("me/assigned_instagram_accounts", None)] = {"data": []}
        result = Collector(graph, self.store).collect(POST, "100")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["stored"]["stored_comments_and_replies"], 5)
        self.assertEqual(graph.request_count, 5)

    def test_explicit_account_id_still_checks_username(self):
        graph = DemoGraph()
        graph.responses[("100", None)]["username"] = "different_account"
        with self.assertRaises(PilotError):
            Collector(graph, self.store).collect(POST, "100")
        self.assertEqual(graph.request_count, 1)
        self.assertEqual(self.store.summary()["stored_comments_and_replies"], 0)

    def test_media_must_be_in_verified_account_list(self):
        graph = DemoGraph()
        graph.responses[("100/media", None)]["data"][0]["permalink"] = "https://www.instagram.com/p/Another/"
        with self.assertRaises(PilotError):
            self.collect(graph)
        self.assertEqual(graph.request_count, 3)
        self.assertEqual(self.store.runs()[0]["status"], "failed")

    def test_missing_expansion_fetches_replies_and_missing_identity_is_unknown(self):
        graph = DemoGraph()
        parent = graph.responses[("200/comments", "page2")]["data"][0]
        del parent["replies"]
        del parent["username"]
        del parent["from"]
        graph.responses[("301/replies", None)] = {"data": []}
        result = self.collect(graph)
        self.assertEqual(result["stored"]["owner_unknown"], 1)
        self.assertEqual(result["api_requests"], 7)

    def test_missing_data_is_failure_not_successful_zero(self):
        graph = DemoGraph()
        graph.responses[("200/comments", None)] = {"unexpected": []}
        with self.assertRaises(PilotError):
            self.collect(graph)
        self.assertEqual(self.store.runs()[0]["status"], "failed")
        self.assertIsNone(self.store.db.execute("SELECT last_completed_at FROM media").fetchone()[0])

    def test_cursor_loop_is_partial_failure(self):
        graph = DemoGraph()
        graph.responses[("200/comments", "page2")]["paging"] = {"next": "https://graph.facebook.com/x?after=page2"}
        with self.assertRaises(PilotError):
            self.collect(graph)
        self.assertEqual(self.store.runs()[0]["status"], "partial")
        self.assertIsNone(self.store.db.execute("SELECT last_completed_at FROM media").fetchone()[0])

    def test_dead_run_marked_interrupted_on_restart(self):
        run_id = self.store.start_run(POST)
        self.collect()
        row = self.store.db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        self.assertEqual(row[0], "interrupted")

    def test_file_lock_rejects_second_collector(self):
        with run_lock(self.path):
            with self.assertRaises(PilotError):
                with run_lock(self.path):
                    self.fail("second lock must not be acquired")


class FakeResponse:
    def __init__(self, payload):
        self.raw = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit):
        return self.raw[:limit]


class FakeOpener:
    def __init__(self, payload=None, error=None):
        self.payload = payload or {"data": []}
        self.error = error
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return FakeResponse(self.payload)


class TransportTests(unittest.TestCase):
    def test_empty_mac_ca_store_uses_verified_system_bundle(self):
        context = mock.Mock()
        context.get_ca_certs.return_value = []
        with mock.patch("sns_comment.api.ssl.create_default_context", return_value=context), \
                mock.patch("sns_comment.api.sys.platform", "darwin"), \
                mock.patch.dict(os.environ, {}, clear=True):
            self.assertIs(https_context(), context)
        context.load_verify_locations.assert_called_once_with(cafile="/etc/ssl/cert.pem")

    def test_existing_or_custom_ca_configuration_is_preserved(self):
        for certs, environment in (([{}], {}), ([], {"SSL_CERT_FILE": "/custom/ca.pem"}),
                                   ([], {"SSL_CERT_DIR": "/custom/certs"})):
            with self.subTest(certs=certs, environment=environment):
                context = mock.Mock()
                context.get_ca_certs.return_value = certs
                with mock.patch("sns_comment.api.ssl.create_default_context", return_value=context), \
                        mock.patch("sns_comment.api.sys.platform", "darwin"), \
                        mock.patch.dict(os.environ, environment, clear=True):
                    self.assertIs(https_context(), context)
                context.load_verify_locations.assert_not_called()

    def test_get_only_and_no_token_in_query(self):
        opener = FakeOpener()
        client = GraphClient("synthetic-secret", "v26.0", opener=opener)
        client.get("200/comments", fields="id,text", limit=50)
        request = opener.requests[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-secret")
        self.assertNotIn("synthetic-secret", request.full_url)
        with self.assertRaises(PilotError):
            client.get("200/comments", method="POST")
        with self.assertRaises(PilotError):
            client.get("https://evil.test/200/comments")
        with self.assertRaises(PilotError):
            client.get("200/comments", access_token="synthetic-secret")
        self.assertEqual(len(opener.requests), 1)

    def test_graph_error_does_not_leak_secret_or_error_message(self):
        body = io.BytesIO(json.dumps({"error": {"code": 190, "message": "leaked synthetic-secret"}}).encode())
        opener = FakeOpener(error=urllib.error.HTTPError("https://example/?access_token=synthetic-secret", 400, "synthetic-secret", {}, body))
        client = GraphClient("synthetic-secret", "v26.0", opener=opener)
        with self.assertRaises(GraphError) as result:
            client.get("100", fields="id,username")
        self.assertNotIn("synthetic-secret", str(result.exception))
        self.assertIn("190", str(result.exception))

    def test_next_urls_are_not_followed_and_budget_is_bounded(self):
        payload = {"data": [], "paging": {"next": "https://evil.test/?access_token=synthetic-secret&after=safe-cursor"}}
        opener = FakeOpener(payload)
        client = GraphClient("synthetic-secret", "v26.0", max_requests=2, opener=opener)
        iterator = Collector(client, None).pages("200/comments", "id,text")
        next(iterator)
        next(iterator)
        self.assertTrue(all(urllib.parse.urlsplit(r.full_url).hostname == "graph.facebook.com" for r in opener.requests))
        self.assertNotIn("synthetic-secret", opener.requests[1].full_url)
        with self.assertRaises(PilotError):
            next(iterator)
        with self.assertRaises(PilotError):
            client.get("200/comments", fields="id")
        self.assertEqual(client.request_count, 2)

    def test_tokens_redacted_from_returned_objects(self):
        opener = FakeOpener({"data": [{"id": "300", "text": "synthetic-secret", "access_token": "synthetic-secret"}]})
        result = GraphClient("synthetic-secret", "v26.0", opener=opener).get("200/comments", fields="id,text")
        self.assertNotIn("synthetic-secret", json.dumps(result))
        self.assertNotIn("access_token", result["data"][0])

    def test_bad_pagination_not_treated_as_complete(self):
        with self.assertRaises(PilotError):
            next_cursor({"data": [], "paging": {"next": "https://example/?access_token=not-stored"}})
        with self.assertRaises(PilotError):
            page_data({})
        self.assertIsNone(next_cursor({"data": [], "paging": {"cursors": {"after": "unused"}}}))

    def test_only_post_urls_accepted_and_tracking_removed(self):
        self.assertEqual(post_key(POST + "?utm_source=test"), "DfCKxEYlZVv")
        self.assertEqual(canonical_url(POST + "?utm_source=test"), POST)
        self.assertEqual(canonical_url("https://www.instagram.com/p/AbC/?q=1"), "https://www.instagram.com/p/AbC/")
        for url in ("http://www.instagram.com/reel/abc/", "https://evil.test/reel/abc/", "https://www.instagram.com/waterbomb_official/"):
            with self.assertRaises(PilotError):
                post_key(url)

    def test_offline_cli_has_separate_db_and_no_network(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "demo.sqlite3"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(main(["demo", "--db", str(path)]), 0)
            result = json.loads(stdout.getvalue())
            self.assertEqual(result["mode"], "offline_synthetic_demo")
            self.assertFalse(result["live_instagram_called"])
            self.assertEqual(result["stored"]["stored_comments_and_replies"], 5)
            self.assertNotIn("api_requests", result)


if __name__ == "__main__":
    unittest.main()
