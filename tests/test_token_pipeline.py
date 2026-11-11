import contextlib
import io
import os
import unittest
from unittest import mock

from sns_comment import cloud,workflow,token_connections as core
from sns_comment.api import PilotError
from sns_comment.connection_registry import initial_registry,runtime_configs
from sns_comment.notion_mcp import NotionMcpClient
from sns_comment.regional_notion import containers,ARCHIVE
from account_fixtures import fixture
import test_account_tabs as tabs
import test_notion_mcp as notion
from test_token_connections import Graph
from test_workflow import source,row


class TokenPipelineTests(unittest.TestCase):
    def test_a_new_token_restarts_collection_but_the_same_token_can_resume(self):
        import datetime as dt
        reference='projects/synthetic/secrets/sns-meta-token/versions/2'
        config={'expected_username':'waterbomb_official','account_id':'100','api_version':'v26.0',
            '_credential_secret_version':reference,'notion':{'rules_page_id':'parent'},
            'collection':{},'cloud':{'refresh_known_tracking':False}}
        for previous,restart in [('projects/synthetic/secrets/sns-meta-token/versions/1',True),(reference,False)]:
            state=mock.MagicMock();state.restore_database.return_value=0
            state.json_get.side_effect=lambda path,default=None:({'secret_version':previous},1) if path=='state/collection-credential.json' else ({},0)
            notion=mock.Mock();notion.rules.return_value={'version':'v2.0','last_edited_at':'2026-11-09T00:00:00Z','text':'rules'}
            notion.publish.return_value={'page_id':'page'}
            analyzer=mock.Mock();analyzer.classify.return_value={};analyzer.report.return_value={'lines':['새 댓글 없음'],'insights':[],'actions':[]}
            primary={'status':'complete','media_discovered':0,'api_requests':2,'last_scope_completed_at':'2026-11-08T23:00:10+00:00'}
            with mock.patch.object(cloud.token_connections,'read_token',return_value=('synthetic-token',reference)), \
                    mock.patch.object(cloud,'GraphClient',return_value=mock.Mock(request_count=0)), \
                    mock.patch.object(cloud,'collect_continuously',return_value=primary) as collect,contextlib.redirect_stdout(io.StringIO()):
                cloud.execute(config,state,notion,analyzer,instant=dt.datetime(2026,11,9,0,tzinfo=dt.timezone.utc),lease_held=True)
            self.assertEqual(collect.call_args.kwargs['restart'],restart)
            saved=[c for c in state.json_put.call_args_list if c.args[0]=='state/collection-credential.json']
            self.assertEqual(bool(saved),restart)

    def setUp(self):
        self.settings,_=fixture();self.settings['cloud']['project_id']='synthetic'
        self.settings['notion']={'auth_mode':'mcp_oauth','daily_data_source_id':'data','regional_report':True}
        self.settings['connections']={'enabled':True}
        self.registry=initial_registry(self.settings)
        self.ref='projects/synthetic/secrets/sns-meta-token/versions/2'

    def test_worker_revalidates_the_token_before_it_chooses_collection_and_report_targets(self):
        plan=core.inspect_token(self.settings,self.registry,'ade',labels={'10004':'대구','10005':'제주'},graph=Graph)
        updated=core.snapshot(self.registry,plan,self.ref,'operator','now',core.source_secret(self.settings))
        shared=mock.MagicMock();shared.json_get.return_value=(self.registry,1);seen=[]
        def execute(config,*args,**kwargs):
            seen.append(config);return {'status':'complete','primary':{},'report_status':'published'}
        with mock.patch.dict(os.environ,{'GOOGLE_CLOUD_PROJECT':'synthetic','SNS_STATE_BUCKET':'state','SNS_NOTION_OAUTH_SECRET':'secret'},clear=True), \
                mock.patch.object(cloud,'read_settings',return_value=self.settings),mock.patch.object(cloud,'CloudState',return_value=shared), \
                mock.patch.object(cloud.token_connections,'reconcile',return_value=(updated,{'status':'applied','revision':'new'})) as reconcile, \
                mock.patch.object(cloud,'merge_daily_pages',return_value={'status':'disabled'}),mock.patch.object(cloud,'cloud_credentials'), \
                mock.patch.object(cloud,'McpClient'),mock.patch.object(cloud,'NotionMcpClient'),mock.patch.object(cloud,'execute',side_effect=execute), \
                mock.patch.object(cloud.token_connections,'verify_delivery',return_value={'status':'synced'}) as verified,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cloud.main(['--all-accounts']),0)
        reconcile.assert_called_once()
        self.assertEqual({c['account_id'] for c in seen},{'10001','10004','10005'})
        self.assertTrue(all(c['_credential_secret_version']==self.ref for c in seen))
        self.assertTrue(all({a['id'] for a in c['_report_roster'] if a['active']}=={'10001','10004','10005'} for c in seen))
        verified.assert_called_once()

    def test_invalid_latest_token_stops_before_collection_sheets_and_notion(self):
        shared=mock.MagicMock();shared.json_get.return_value=(self.registry,1)
        with mock.patch.dict(os.environ,{'GOOGLE_CLOUD_PROJECT':'synthetic','SNS_STATE_BUCKET':'state'},clear=True), \
                mock.patch.object(cloud,'read_settings',return_value=self.settings),mock.patch.object(cloud,'CloudState',return_value=shared), \
                mock.patch.object(cloud.token_connections,'reconcile',side_effect=PilotError('토큰 검증 실패')), \
                mock.patch.object(cloud,'execute') as execute,mock.patch.object(cloud,'cloud_credentials') as oauth, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cloud.main(['--all-accounts']),1)
        execute.assert_not_called();oauth.assert_not_called()
        self.assertTrue(shared.json_put.call_args.args[1]['previous_connection_preserved'])

    def test_one_verified_token_snapshot_drives_sheet_tabs_and_notion_regions_end_to_end(self):
        book=tabs.Workbook();api=notion.FakeMcp();api.date='2026-11-09';helper=notion.McpReportTests()
        before_configs=runtime_configs(self.settings,self.registry)
        old=[]
        for config in before_configs:
            old.append(workflow.make_account(config,source([row(1,'영상 요청')]),{}))
            NotionMcpClient(api,notion.SOURCE,username=config['expected_username'],regional=True).publish(api.date,
                helper.regional_sections(config['expected_username'],1,'old '+config['expected_username']))
        before=workflow.sync(workflow.merge(old),book,account_roster=before_configs[0]['_sheet_roster'])
        b=before['account_tabs']['10002'];j=workflow.REGIONAL_HEADERS.index('답변 내용');book.rows[b][1][j]='사람 답변 보존'
        old_b=[r[:] for r in book.rows[b]]
        original_a=book.sheet(before['account_tabs']['10001'])['properties']['sheetId']
        plan=core.inspect_token(self.settings,self.registry,'ade',labels={'10004':'대구','10005':'제주'},graph=Graph)
        updated=core.snapshot(self.registry,plan,self.ref,'operator','now',core.source_secret(self.settings))
        configs=runtime_configs(self.settings,updated)
        for config in configs:
            sheet=workflow.sync(workflow.make_account(config,source([row(2,'관객 반응')]),{}),book,account_roster=config['_sheet_roster'])
            NotionMcpClient(api,notion.SOURCE,username=config['expected_username'],regional=True,region_label=config['region_label'],
                account_roster=config['_report_roster'],connection_revision=config['_connection_revision']).publish(api.date,
                    helper.regional_sections(config['expected_username'],1,'new '+config['expected_username']))
        self.assertEqual(book.sheet(sheet['account_tabs']['10001'])['properties']['sheetId'],original_a)
        self.assertTrue(book.sheet(b)['properties']['hidden']);self.assertEqual(book.rows[b],old_b)
        self.assertIn('10004',sheet['account_tabs']);self.assertIn('10005',sheet['account_tabs'])
        overview={r[0]:r for r in book.rows['한눈에 보기'][1:]}
        self.assertEqual(overview['10002'][workflow.HEADERS['한눈에 보기'].index('수집 상태')],'대상 제외 · 이력 보존')
        report=containers(api.content,'callout')[0][3];regions=containers(report,'toggle')
        current=[x[2] for x in regions if x[2] not in (ARCHIVE,'공통 수집 기록')]
        self.assertEqual(set(current),{'대표(공식) · 새 댓글 1건','대구 · 새 댓글 1건','제주 · 새 댓글 1건'})
        archived=next(x[3] for x in regions if x[2]==ARCHIVE)
        self.assertIn('부산 · 새 댓글 1건',archived);self.assertIn('속초 · 새 댓글 1건',archived)
        self.assertNotIn('old waterbomb',report.split('<toggle>',1)[0])
        self.assertIn('### SNS Comments',api.content)


if __name__=='__main__':unittest.main()
