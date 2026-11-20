import contextlib
import io
import os
import unittest
from unittest import mock
from account_fixtures import fixture
from sns_comment.connection_registry import REGISTRY_OBJECT, initial_registry


class CloudRegistryTests(unittest.TestCase):
    def test_worker_snapshots_accounts_and_credential_versions_once(self):
        from sns_comment import cloud
        settings,_=fixture();settings['notion']={'auth_mode':'mcp_oauth','daily_data_source_id':'daily'}
        registry=initial_registry(settings)
        # The verified roster is authoritative even if the old baked-in
        # schedule no longer names the current collection targets.
        settings['cloud']['scheduled_accounts']=[]
        registry['groups']['primary'].update(legacy=False,secret_version='projects/synthetic/secrets/sns-web-meta-connections/versions/7')
        states=[mock.MagicMock() for _ in range(3)]
        states[0].json_get.return_value=(registry,1)
        seen=[]
        def execute(config,*args,**kwargs):
            seen.append(config)
            registry['accounts']['10002']['active']=False
            return {'status':'complete','report_status':'published'}
        environment={'GOOGLE_CLOUD_PROJECT':'synthetic','SNS_STATE_BUCKET':'bucket','SNS_NOTION_OAUTH_SECRET':'existing'}
        with mock.patch.dict(os.environ,environment,clear=True),mock.patch.object(cloud,'read_settings',return_value=settings),\
                mock.patch.object(cloud,'CloudState',side_effect=states),mock.patch.object(cloud,'cloud_credentials'),\
                mock.patch.object(cloud,'McpClient'),mock.patch.object(cloud,'NotionMcpClient'),mock.patch.object(cloud,'Analyzer'),\
                mock.patch.object(cloud,'merge_daily_pages',return_value={'status':'complete'}),\
                mock.patch.object(cloud,'execute',side_effect=execute),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cloud.main(['--all-accounts']),0)
        self.assertEqual([c['account_id'] for c in seen],['10001','10002','10003'])
        self.assertEqual({c['_web_secret_version'] for c in seen},{'projects/synthetic/secrets/sns-web-meta-connections/versions/7'})
        states[0].json_get.assert_called_once_with(REGISTRY_OBJECT)
