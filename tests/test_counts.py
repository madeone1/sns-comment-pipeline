import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from sns_comment.api import GraphError, RequestBudgetExceeded
from sns_comment.counts import count_media, main
from sns_comment.demo import DemoGraph


class CounterGraph(DemoGraph):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.responses[("100/media", None)] = {"data": [
            {"id": "200", "comments_count": 100}, {"id": "201", "comments_count": 0}],
            "paging": {"next": "https://graph.facebook.com/100/media?after=page2"}}
        self.responses[("100/media", "page2")] = {"data": [{"id": "202", "comments_count": 7}]}

    def get(self, path, **params):
        self.calls.append((path, params.copy()))
        return super().get(path, **params)


class CounterTests(unittest.TestCase):
    def test_counts_all_media_pages_without_comment_or_reply_requests(self):
        graph = CounterGraph()
        result = count_media(graph, "waterbomb_official", "100")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["total_comments_count"], 107)
        self.assertEqual(result["media_discovered"], 3)
        self.assertEqual(result["api_requests"], 3)
        self.assertIsNone(result["recent_seven_day_count"])
        self.assertFalse(result["comment_bodies_requested"])
        self.assertEqual([c[0] for c in graph.calls], ["100", "100/media", "100/media"])
        for path, params in graph.calls[1:]:
            self.assertEqual(params["fields"], "id,comments_count,timestamp")

    def test_missing_invalid_counters_are_unknown_and_not_normal_zero(self):
        graph = CounterGraph()
        graph.responses[("100/media", "page2")] = {"data": [
            {"id": "202"}, {"id": "203", "comments_count": -1}, {"id": "204", "comments_count": True}]}
        result = count_media(graph, "waterbomb_official", "100")
        self.assertEqual(result["status"], "partial")
        self.assertIsNone(result["total_comments_count"])
        self.assertEqual(result["known_counter_sum"], 100)
        self.assertEqual(result["media_with_counter"], 2)
        self.assertEqual(result["media_without_counter"], ["202", "203", "204"])

    def test_duplicate_media_is_not_added_twice(self):
        graph = CounterGraph()
        graph.responses[("100/media", "page2")]["data"].append({"id": "200", "comments_count": 103})
        result = count_media(graph, "waterbomb_official", "100")
        self.assertEqual(result["total_comments_count"], 110)
        self.assertEqual(result["media_discovered"], 3)

    def test_listing_failure_retains_known_sum_without_complete_total(self):
        graph = CounterGraph()
        graph.responses[("100/media", "page2")] = GraphError(403, 200)
        result = count_media(graph, "waterbomb_official", "100")
        self.assertFalse(result["catalog_complete"])
        self.assertEqual(result["known_counter_sum"], 100)
        self.assertIsNone(result["total_comments_count"])
        self.assertTrue(result["error"])

    def test_genuinely_empty_account_can_return_zero(self):
        graph = CounterGraph()
        graph.responses[("100/media", None)] = {"data": []}
        result = count_media(graph, "waterbomb_official", "100")
        self.assertEqual((result["status"], result["total_comments_count"]), ("complete", 0))

    def test_wrong_account_stops_before_media_listing(self):
        graph = CounterGraph()
        graph.responses[("100", None)]["username"] = "someone_else"
        result = count_media(graph, "waterbomb_official", "100")
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["total_comments_count"])
        self.assertEqual(len(graph.calls), 1)

    def test_missing_data_is_not_a_zero_counter(self):
        graph = CounterGraph()
        graph.responses[("100/media", None)] = {"unexpected": []}
        result = count_media(graph, "waterbomb_official", "100")
        self.assertEqual(result["status"], "partial")
        self.assertIsNone(result["total_comments_count"])
        self.assertFalse(result["catalog_complete"])

    def test_cli_writes_only_metadata_and_never_credentials(self):
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch("sns_comment.counts.GraphClient", return_value=CounterGraph()), \
                mock.patch("sns_comment.counts.read_config", return_value={"expected_username": "waterbomb_official", "account_id": "100", "api_version": "v26.0"}), \
                mock.patch.dict(os.environ, {"SNS_IG_ACCESS_TOKEN": "synthetic-secret"}), \
                mock.patch("sys.stdin.isatty", return_value=False), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            path = Path(folder) / "counts.json"
            self.assertEqual(main(["--output", str(path)]), 0)
            result = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(result["total_comments_count"], 107)
            self.assertNotIn("synthetic-secret", path.read_text(encoding="utf-8") + output.getvalue())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
