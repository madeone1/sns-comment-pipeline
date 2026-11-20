import contextlib
import unittest
from unittest import mock

from sns_comment.api import PilotError
from sns_comment.cloud_state import CloudError, CloudState, lease_retry


class CloudStateTests(unittest.TestCase):
    def test_active_lease_prevents_second_writer_without_mutations(self):
        state=CloudState("private",mock.Mock())
        with mock.patch.object(state,"json_get",return_value=({"expires_at":200},4)),mock.patch.object(state,"json_put") as put,mock.patch("sns_comment.cloud_state.time.time",return_value=100):
            with self.assertRaises(CloudError):
                with state.lease():self.fail("lease must be exclusive")
            put.assert_not_called()
        state.transport.request.assert_not_called()

    def test_expired_lease_uses_generation_compare_and_releases_only_its_own_version(self):
        transport=mock.Mock()
        state=CloudState("private",transport)
        with mock.patch.object(state,"json_get",return_value=({"expires_at":50},4)),mock.patch.object(state,"json_put",return_value=5) as put,mock.patch("sns_comment.cloud_state.time.time",return_value=100):
            with state.lease():pass
            self.assertEqual(put.call_args.args[2],4)
        self.assertEqual(transport.request.call_args.args[0],"DELETE")
        self.assertIn("ifGenerationMatch=5",transport.request.call_args.args[1])

    def test_lease_compare_failure_does_not_enter_or_delete_other_lease(self):
        state=CloudState("private",mock.Mock())
        with mock.patch.object(state,"json_get",return_value=({},0)),mock.patch.object(state,"json_put",side_effect=CloudError("conflict",412)):
            with self.assertRaises(CloudError):
                with state.lease():self.fail("must not enter")
        state.transport.request.assert_not_called()

    def test_database_download_is_pinned_to_the_metadata_generation(self):
        transport=mock.Mock()
        transport.json.return_value={"generation":"11"}
        transport.request.return_value=b"raw checkpoint"
        state=CloudState("private",transport)
        self.assertEqual(state.get("state/comments.sqlite3"),(b"raw checkpoint",11))
        self.assertIn("generation=11",transport.request.call_args.args[1])

    def test_list_page_stops_after_one_page(self):
        transport = mock.Mock()
        transport.json.return_value = {"items": [{"name": "moderation/decisions/1/9.json", "generation": "3"}],
                                        "nextPageToken": "NEXT"}
        state = CloudState("private", transport)
        items, token = state.list_page("moderation/decisions/1/", 40, None)
        self.assertEqual(items, [{"name": "moderation/decisions/1/9.json", "generation": 3}])
        self.assertEqual(token, "NEXT")
        self.assertIn("maxResults=40", transport.json.call_args.args[1])
        self.assertNotIn("pageToken", transport.json.call_args.args[1])
        state.list_page("moderation/decisions/1/", 40, "NEXT")
        self.assertIn("pageToken=NEXT", transport.json.call_args.args[1])
        state.list_page("moderation/decisions/1/", 30, start_offset="moderation/decisions/1/9.json")
        self.assertIn("startOffset=moderation%2Fdecisions%2F1%2F9.json%0A", transport.json.call_args.args[1])
        self.assertNotIn("pageToken", transport.json.call_args.args[1])
        self.assertEqual(transport.json.call_count, 3)


class LeaseRetryTests(unittest.TestCase):
    def test_a_busy_lock_is_retried_and_then_entered(self):
        calls = {"n": 0}
        waits = []

        class State:
            @contextlib.contextmanager
            def lease(self, seconds=3900, name="state/lease.json"):
                calls["n"] += 1
                calls["name"] = name
                if calls["n"] < 3:
                    raise CloudError("다른 클라우드 실행이 진행 중입니다")
                yield

        with lease_retry(State(), 10, "state/notion-oauth-lease.json", attempts=4, wait_seconds=15, sleeper=waits.append):
            calls["entered"] = True
        self.assertTrue(calls["entered"])
        self.assertEqual(calls["name"], "state/notion-oauth-lease.json")
        self.assertEqual(waits, [15, 15])

    def test_an_error_inside_the_lock_is_not_retried(self):
        calls = {"n": 0}

        class State:
            @contextlib.contextmanager
            def lease(self, seconds=3900, name="state/lease.json"):
                calls["n"] += 1
                yield

        with self.assertRaises(PilotError):
            with lease_retry(State(), attempts=4, sleeper=lambda _seconds: None):
                raise PilotError("안쪽 실패")
        self.assertEqual(calls["n"], 1)


if __name__=="__main__":unittest.main()
