import unittest
from unittest import mock

from sns_comment.cloud_state import CloudError, CloudState


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


if __name__=="__main__":unittest.main()
