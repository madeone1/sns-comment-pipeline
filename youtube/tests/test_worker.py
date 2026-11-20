from contextlib import nullcontext, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from youtube_validation.api import ValidationError
from youtube_validation.worker import main


class WorkerRecoveryTests(unittest.TestCase):
    def test_revoked_grant_stops_collection_and_queues_stored_copy_deletion(self):
        with tempfile.TemporaryDirectory() as temporary:
            cloud=MagicMock();cloud.lease.side_effect=lambda:nullcontext()
            cloud.get.return_value=(None,0);cloud.list_objects.return_value=[];cloud.restore.return_value=0;cloud.save.return_value=1
            def read(path):
                return {'spreadsheet_id':'dedicated-sheet'} if path.name=='pipeline_config.json' else {'refresh_token':'synthetic-test-token'}
            with patch('youtube_validation.worker.STATE',Path(temporary)),patch('youtube_validation.worker.CloudState',return_value=cloud), \
                 patch('youtube_validation.worker.read_json',side_effect=read),patch('youtube_validation.worker.load_credentials'), \
                 patch('youtube_validation.worker.YouTube'),patch('youtube_validation.worker.privacy.clear_processing_history',return_value=0), \
                 patch('youtube_validation.worker.run',return_value={'state':'error','error':'oauth_authorization_revoked','api_requests':1}), \
                 patch('youtube_validation.worker.privacy.revoke_connection',return_value='revoked'), \
                 patch('youtube_validation.worker.sync',return_value={}) as sync,redirect_stdout(io.StringIO()):
                self.assertEqual(main(),0)
                saved=[json.loads(call.args[1]) for call in cloud.put.call_args_list if call.args[0]=='privacy/control.json'][0]
                self.assertFalse(saved['collection_enabled'])
                self.assertEqual(saved['requests'][0]['kind'],'all')
                self.assertEqual(sync.call_args.kwargs['result']['state'],'disconnected')
                self.assertNotIn('synthetic-test-token',json.dumps(saved))

    def test_sheet_failure_retries_even_after_collection_is_already_complete(self):
        with tempfile.TemporaryDirectory() as temporary:
            cloud = MagicMock()
            cloud.lease.side_effect = lambda:nullcontext()
            cloud.restore.return_value = 0
            cloud.get.return_value = (None,0)
            cloud.list_objects.return_value = []
            cloud.save.return_value = 1
            def read(path):
                return {'spreadsheet_id':'dedicated-sheet'} if path.name=='pipeline_config.json' else {}
            with patch('youtube_validation.worker.STATE',Path(temporary)), \
                 patch('youtube_validation.worker.CloudState',return_value=cloud), \
                 patch('youtube_validation.worker.read_json',side_effect=read), \
                 patch('youtube_validation.worker.load_credentials'), \
                 patch('youtube_validation.worker.YouTube'), \
                 patch('youtube_validation.worker.run',return_value={'state':'idle','api_requests':0}), \
                 patch('youtube_validation.worker.sync',side_effect=[
                     ValidationError('google_transport_failed'),{'updated_rows':0}]) as sync, \
                 redirect_stdout(io.StringIO()):
                with self.assertRaises(ValidationError):
                    main()
                self.assertEqual(main(),0)
                self.assertEqual(sync.call_count,2)
