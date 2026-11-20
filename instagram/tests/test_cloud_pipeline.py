import contextlib
import datetime as dt
import io
import os
import unittest
from unittest import mock

from sns_comment import cloud
from sns_comment.api import PilotError
from sns_comment.notion_mcp import NoDailyPage


class ReportRecoveryTests(unittest.TestCase):
    def test_configured_guidance_enforces_review_before_the_cloud_publish(self):
        parent={'page_id':'parent','version':'v2.0','last_edited_at':'2026-11-09T00:00:00Z','text':'rules'}
        rules={'page_id':'child','version':'v2.0','last_edited_at':'2026-11-09T00:00:00Z',
            'parent':{k:v for k,v in parent.items() if k!='text'},'text':'필요한 행동이 없으면 생략.',
            'report_text':'필요한 행동이 없으면 생략.','workflow_enabled':True,'enforce_guidelines':True}
        config={'expected_username':'waterbomb_official','account_id':'100','api_version':'v26.0',
            'notion':{'rules_page_id':'parent','report_instructions_page_id':'child'},
            'collection':{},'cloud':{'refresh_known_tracking':False}}
        rules['account_context']={'account_id':'100','username':'waterbomb_official','region':'waterbomb_official'}
        state=mock.MagicMock();state.restore_database.return_value=0;state.json_get.return_value=({},0)
        notion=mock.Mock();notion.rules.return_value=parent;notion.workflow_rules.return_value=rules
        analyzer=mock.Mock();analyzer.classify.return_value={}
        report={'lines':['새 댓글 없음'],'insights':[],'actions':[],'rule_improvements':[],
            'guideline_review':{'compliant':True}}
        def publish(day,sections,through_day=None,guideline_validator=None):
            saved=[c for c in state.json_put.call_args_list if c.args[0]=='state/reports.json']
            self.assertTrue(saved);self.assertIn('guidance',saved[-1].args[1][day])
            self.assertIsNotNone(guideline_validator)
            guideline_validator('### SNS Comments\n出力',{'account':'waterbomb_official'})
            return {'page_id':'page'}
        notion.publish.side_effect=publish
        primary={'status':'complete','media_discovered':0,'api_requests':2,'last_scope_completed_at':'2026-11-08T23:00:10+00:00'}
        with mock.patch.dict(os.environ,{'SNS_IG_ACCESS_TOKEN':'synthetic'},clear=True), \
                mock.patch.object(cloud,'GraphClient',return_value=mock.Mock(request_count=0)), \
                mock.patch.object(cloud,'collect_continuously',return_value=primary), \
                mock.patch.object(cloud.guidance,'generate_current',return_value=(report,rules)), \
                mock.patch.object(cloud.guidance,'review',return_value={'compliant':True}) as checked, \
                contextlib.redirect_stdout(io.StringIO()):
            result=cloud.execute(config,state,notion,analyzer,instant=dt.datetime(2026,11,9,0,tzinfo=dt.timezone.utc),lease_held=True)
        self.assertEqual(result['report_status'],'published')
        self.assertEqual(checked.call_args.kwargs['scope'],'publication')
        self.assertTrue(any(c.args[0].endswith('publication-guidance-2026-11-09.json') for c in state.json_put.call_args_list))

    def test_a_new_account_without_saved_runs_still_writes_its_first_source(self):
        config={'expected_username':'waterbomb_toronto_official','account_id':'17841454752492067',
                'region_label':'토론토','api_version':'v26.0','notion':{'rules_page_id':'parent'},
                'collection':{},'cloud':{'refresh_known_tracking':False,'webhook_enabled':False},
                'workflow':{'enabled':False,'google_sheets':{'enabled':False}}}
        state=mock.MagicMock();state.restore_database.return_value=0;state.json_get.return_value=({},0)
        notion=mock.Mock();notion.rules.return_value={'version':'v2.0','last_edited_at':'2026-11-10T00:00:00Z','text':'rules'}
        notion.publish.return_value={'page_id':'page'}
        analyzer=mock.Mock();analyzer.classify.return_value={};analyzer.report.return_value={'lines':['새 댓글 없음'],'insights':[],'actions':[]}
        primary={'status':'complete','media_discovered':2,'api_requests':4,'last_scope_completed_at':'2026-11-11T23:10:00+00:00'}
        with mock.patch.dict(os.environ,{'SNS_IG_ACCESS_TOKEN':'synthetic'},clear=True), \
                mock.patch.object(cloud,'GraphClient',return_value=mock.Mock(request_count=0)), \
                mock.patch.object(cloud,'collect_continuously',return_value=primary),contextlib.redirect_stdout(io.StringIO()):
            result=cloud.execute(config,state,notion,analyzer,instant=dt.datetime(2026,11,12,0,tzinfo=dt.timezone.utc),lease_held=True)
        self.assertEqual(result['status'],'complete')
        names=[call.args[0] for call in state.json_put.call_args_list]
        self.assertIn('runs/2026-11-12/source.json',names)
        reports=next(call.args[1] for call in state.json_put.call_args_list if call.args[0]=='state/reports.json')
        self.assertIn('2026-11-12',reports)
        self.assertIn('daily',reports['2026-11-12']['sections'])

    def test_previous_month_scope_skips_unbounded_baseline_and_separate_old_activity(self):
        config={'expected_username':'waterbomb_official','account_id':'100','api_version':'v26.0',
                'notion':{'rules_page_id':'parent'},'collection':{},
                'cloud':{'comment_written_windows':True,'post_history':'previous_month'}}
        state=mock.MagicMock();state.restore_database.return_value=0;state.json_get.return_value=({},0)
        notion=mock.Mock();notion.rules.return_value={'version':'v2.0','last_edited_at':'2026-11-10T00:00:00Z','text':'rules'}
        notion.publish.return_value={'page_id':'page'}
        analyzer=mock.Mock();analyzer.classify.return_value={}
        analyzer.report.return_value={'lines':['새 댓글 없음'],'insights':[],'actions':[]}
        primary={'status':'complete','media_discovered':0,'api_requests':2,'last_scope_completed_at':'2026-11-09T23:10:00+00:00'}
        with mock.patch.dict(os.environ,{'SNS_IG_ACCESS_TOKEN':'synthetic'},clear=True), \
                mock.patch.object(cloud,'GraphClient',return_value=mock.Mock(request_count=0)), \
                mock.patch.object(cloud,'collect_continuously',return_value=primary) as collect, \
                mock.patch('sns_comment.comment_windows.baseline') as baseline,contextlib.redirect_stdout(io.StringIO()):
            result=cloud.execute(config,state,notion,analyzer,instant=dt.datetime(2026,11,10,0,tzinfo=dt.timezone.utc),lease_held=True)
        baseline.assert_not_called()
        self.assertEqual([c.kwargs['media_policy'] for c in collect.call_args_list],['previous_month_posts'])
        self.assertEqual(result['status'],'complete')
        source=next(c.args[1] for c in state.json_put.call_args_list if c.args[0]=='runs/2026-11-10/source.json')
        self.assertEqual(source['media_since'],'2026-09-30T15:00:00+00:00')
        self.assertTrue(source['media_age_restricted'])
        self.assertEqual(source['collection']['coverage']['historical_baseline']['status'],'not_required')

    def test_report_instructions_do_not_activate_sheets_or_change_classification_rules(self):
        config={'expected_username':'waterbomb_official','account_id':'100','api_version':'v26.0',
                'notion':{'rules_page_id':'parent','report_instructions_page_id':'child','verify_report_guidelines':False},
                'collection':{},'cloud':{'refresh_known_tracking':False}}
        state=mock.MagicMock();state.restore_database.return_value=0;state.json_get.return_value=({},0)
        notion=mock.Mock()
        parent={'version':'v2.0','last_edited_at':'2026-11-09T00:00:00Z','text':'parent rules'}
        child={**parent,'text':'adopted report instructions','workflow_enabled':True}
        notion.rules.return_value=parent;notion.workflow_rules.return_value=child
        notion.publish.return_value={'page_id':'page'}
        analyzer=mock.Mock();analyzer.classify.return_value={}
        analyzer.report.return_value={'lines':['새 댓글 없음'],'insights':[],'actions':[]}
        primary={'status':'complete','media_discovered':0,'api_requests':2,'last_scope_completed_at':'2026-11-08T23:00:10+00:00'}
        with mock.patch.dict(os.environ,{'SNS_IG_ACCESS_TOKEN':'synthetic'},clear=True), \
                mock.patch.object(cloud,'GraphClient',return_value=mock.Mock(request_count=0)), \
                mock.patch.object(cloud,'collect_continuously',return_value=primary),contextlib.redirect_stdout(io.StringIO()):
            cloud.execute(config,state,notion,analyzer,instant=dt.datetime(2026,11,9,0,tzinfo=dt.timezone.utc),lease_held=True)
        self.assertEqual(analyzer.classify.call_args.args[2],parent)
        self.assertEqual(analyzer.report.call_args.args[3],child)
        notion.workflow_rules.assert_called_once_with('parent','child')
        names=[call.args[0] for call in state.json_put.call_args_list]
        self.assertIn('runs/2026-11-09/report-rules.json',names)
        self.assertNotIn('runs/2026-11-09/workflow.json',names)
        self.assertNotIn('runs/2026-11-09/sheets.json',names)

    def test_workflow_sheets_auth_failure_preserves_notion_and_reports_pending(self):
        config={'expected_username':'waterbomb_official','account_id':'100','api_version':'v26.0',
                'notion':{'rules_page_id':'parent'},'collection':{},'cloud':{'refresh_known_tracking':False},
                'workflow':{'enabled':True,'instructions_page_id':'child','google_sheets':{'enabled':True,'spreadsheet_id':'sheet'}}}
        state=mock.MagicMock();state.restore_database.return_value=0;state.json_get.return_value=({},0)
        notion=mock.Mock();notion.workflow_rules.return_value={'version':'v1.0.7','last_edited_at':'2026-11-09T00:00:00Z','text':'rules','workflow_enabled':True}
        notion.publish.return_value={'page_id':'page'}
        analyzer=mock.Mock();analyzer.classify.return_value={}
        analyzer.report.return_value={'lines':['새 댓글 없음'],'insights':[],'actions':[]}
        primary={'status':'complete','media_discovered':0,'api_requests':2,'last_scope_completed_at':'2026-11-08T23:00:10+00:00'}
        with mock.patch.dict(os.environ,{'SNS_IG_ACCESS_TOKEN':'synthetic'},clear=True), \
                mock.patch.object(cloud,'GraphClient',return_value=mock.Mock(request_count=0)), \
                mock.patch.object(cloud,'collect_continuously',return_value=primary),contextlib.redirect_stdout(io.StringIO()):
            result=cloud.execute(config,state,notion,analyzer,instant=dt.datetime(2026,11,9,0,tzinfo=dt.timezone.utc),lease_held=True)
        self.assertEqual(result['report_status'],'published')
        self.assertEqual(result['status'],'pending','시트 미반영이면 전체 완료로 표시하지 않음')
        self.assertIn('반영 대기',result['workflow_stages']['sheets'])
        notion.publish.assert_called_once();notion.workflow_rules.assert_called_once_with('parent','child')
        names=[call.args[0] for call in state.json_put.call_args_list]
        self.assertLess(names.index('runs/2026-11-09/source.json'),names.index('runs/2026-11-09/workflow.json'))
        self.assertIn('runs/2026-11-09/sheets.json',names)

    def test_live_sheet_stage_receives_the_verified_account_roster(self):
        roster=[{'id':'100','username':'waterbomb_official','label':'대표','active':True},
                {'id':'200','username':'new_account','label':'새 계정','active':True}]
        config={'expected_username':'waterbomb_official','account_id':'100','api_version':'v26.0',
                'notion':{'rules_page_id':'parent'},'collection':{},'cloud':{'refresh_known_tracking':False},
                '_sheet_roster':roster,
                'workflow':{'enabled':True,'instructions_page_id':'child','google_sheets':{'enabled':True,'spreadsheet_id':'sheet'}}}
        state=mock.MagicMock();state.restore_database.return_value=0;state.json_get.return_value=({},0)
        notion=mock.Mock();notion.workflow_rules.return_value={'version':'v2.0','last_edited_at':'2026-11-09T00:00:00Z','text':'rules','workflow_enabled':True}
        notion.publish.return_value={'page_id':'page'}
        analyzer=mock.Mock();analyzer.classify.return_value={}
        analyzer.report.return_value={'lines':['새 댓글 없음'],'insights':[],'actions':[]}
        primary={'status':'complete','media_discovered':0,'api_requests':2,'last_scope_completed_at':'2026-11-08T23:00:10+00:00'}
        with mock.patch.dict(os.environ,{'SNS_IG_ACCESS_TOKEN':'synthetic'},clear=True), \
                mock.patch.object(cloud,'GraphClient',return_value=mock.Mock(request_count=0)), \
                mock.patch.object(cloud,'collect_continuously',return_value=primary), \
                mock.patch.object(cloud,'google_access_token',return_value='synthetic'), \
                mock.patch.object(cloud.workflow,'Client'), \
                mock.patch.object(cloud.workflow,'sync',return_value={'status':'complete'}) as sync, \
                contextlib.redirect_stdout(io.StringIO()):
            result=cloud.execute(config,state,notion,analyzer,instant=dt.datetime(2026,11,9,0,tzinfo=dt.timezone.utc),lease_held=True)
        self.assertEqual(result['status'],'complete')
        self.assertTrue(sync.called)
        self.assertTrue(all(call.kwargs['account_roster']==roster for call in sync.call_args_list))

    def test_sheets_can_run_without_switching_production_classification_rules(self):
        config={'expected_username':'waterbomb_official','account_id':'100','api_version':'v26.0',
            'notion':{'rules_page_id':'parent','report_instructions_page_id':'child','verify_report_guidelines':False},'collection':{},
            'cloud':{'refresh_known_tracking':False},
            'workflow':{'enabled':False,'google_sheets':{'enabled':True,'spreadsheet_id':'sheet'}}}
        parent={'version':'v2.0','last_edited_at':'2026-11-09T00:00:00Z','text':'existing content rules'}
        child={**parent,'text':'existing report instructions','workflow_enabled':True}
        state=mock.MagicMock();state.restore_database.return_value=0;state.json_get.return_value=({},0)
        notion=mock.Mock();notion.rules.return_value=parent;notion.workflow_rules.return_value=child
        notion.publish.return_value={'page_id':'page'}
        analyzer=mock.Mock();analyzer.classify.return_value={}
        analyzer.report.return_value={'lines':['새 댓글 없음'],'insights':[],'actions':[]}
        primary={'status':'complete','media_discovered':0,'api_requests':2,'last_scope_completed_at':'2026-11-08T23:00:10+00:00'}
        with mock.patch.dict(os.environ,{'SNS_IG_ACCESS_TOKEN':'synthetic'},clear=True), \
            mock.patch.object(cloud,'GraphClient',return_value=mock.Mock(request_count=0)), \
            mock.patch.object(cloud,'collect_continuously',return_value=primary), \
            mock.patch.object(cloud,'google_access_token',return_value='synthetic'), \
            mock.patch.object(cloud.workflow,'Client'), \
            mock.patch.object(cloud.workflow,'sync',return_value={'status':'complete'}) as sync, \
            contextlib.redirect_stdout(io.StringIO()):
            result=cloud.execute(config,state,notion,analyzer,instant=dt.datetime(2026,11,9,0,tzinfo=dt.timezone.utc),lease_held=True)
        self.assertEqual(result['status'],'complete')
        self.assertEqual(analyzer.classify.call_args.args[2],parent)
        self.assertEqual(analyzer.report.call_args.args[3],child)
        self.assertTrue(sync.called)

    def test_merge_only_uses_global_lease_and_never_initializes_sns_or_oauth(self):
        state=mock.MagicMock()
        config={"notion":{"auth_mode":"mcp_oauth"}}
        with mock.patch.dict(os.environ,{"GOOGLE_CLOUD_PROJECT":"example","SNS_STATE_BUCKET":"example-state"},clear=True), \
                mock.patch.object(cloud,"load",return_value=config),mock.patch.object(cloud,"require_enabled"), \
                mock.patch.object(cloud,"CloudState",return_value=state), \
                mock.patch.object(cloud,"merge_daily_pages",return_value={"status":"complete","mode":"apply","results":[]}) as merge, \
                mock.patch.object(cloud,"cloud_credentials") as oauth,mock.patch.object(cloud,"execute") as sns, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cloud.main(["--merge-only"]),0)
        merge.assert_called_once();state.lease.assert_called_once();state.json_put.assert_called_once()
        oauth.assert_not_called();sns.assert_not_called()

    def test_merge_only_hold_is_recorded_as_failure_without_running_sns(self):
        state=mock.MagicMock()
        with mock.patch.dict(os.environ,{"GOOGLE_CLOUD_PROJECT":"example","SNS_STATE_BUCKET":"example-state"},clear=True), \
                mock.patch.object(cloud,"load",return_value={"notion":{"auth_mode":"mcp_oauth"}}),mock.patch.object(cloud,"require_enabled"), \
                mock.patch.object(cloud,"CloudState",return_value=state), \
                mock.patch.object(cloud,"merge_daily_pages",return_value={"status":"held","results":[]}), \
                mock.patch.object(cloud,"cloud_credentials") as oauth,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cloud.main(["--merge-only"]),1)
        self.assertEqual(state.json_put.call_args.args[1]["status"],"held")
        oauth.assert_not_called()

    def test_missing_page_defers_report_and_does_not_skip_old_post_tracking(self):
        self.run_report_failure(NoDailyPage("waiting for next daily page"),deferred=True)

    def test_real_report_failure_is_reported_after_old_post_tracking(self):
        self.run_report_failure(PilotError("synthetic write failure"),deferred=False)

    def test_webhook_failure_preserves_primary_report_and_reports_pending(self):
        self.run_report_failure(NoDailyPage('waiting for page'), deferred=True, webhook=True)

    def run_report_failure(self,error,deferred,webhook=False):
        config={"expected_username":"waterbomb_official","account_id":"100","api_version":"v26.0",
                "notion":{"rules_page_id":"rules"},"collection":{},"cloud":{"refresh_known_tracking":True}}
        config['cloud']['webhook_enabled'] = webhook
        state=mock.MagicMock();state.restore_database.return_value=0;state.json_get.return_value=({},0)
        notion=mock.Mock();notion.publish.side_effect=error
        notion.rules.return_value={"version":"v1.1","last_edited_at":"2026-11-05T00:00:00Z","text":"rules"}
        analyzer=mock.Mock();analyzer.classify.return_value={}
        analyzer.report.return_value={"lines":["No comments","No inquiries","No action"],"insights":[],"actions":[]}
        primary={"status":"complete","media_discovered":0,"api_requests":2,"last_scope_completed_at":"2026-11-04T23:00:10+00:00"}
        with mock.patch.dict(os.environ,{"SNS_IG_ACCESS_TOKEN":"synthetic"},clear=True), \
                mock.patch.object(cloud,"GraphClient",return_value=mock.Mock(request_count=0)),mock.patch.object(cloud,"collect_continuously",return_value=primary) as collect, \
                mock.patch('sns_comment.webhook_worker.process',side_effect=PilotError('synthetic inbox outage')) as inbox, \
                contextlib.redirect_stdout(io.StringIO()):
            if deferred:
                result=cloud.execute(config,state,notion,analyzer,instant=dt.datetime(2026,11,5,0,tzinfo=dt.timezone.utc),lease_held=True)
                self.assertEqual(result["report_status"],"pending")
                self.assertEqual(result["pending_report_dates"],["2026-11-05"])
                if webhook:
                    self.assertEqual(result['status'], 'pending')
                    self.assertEqual(result['webhook']['status'], 'pending')
            else:
                with self.assertRaisesRegex(PilotError,"Notion 기록 보류"):
                    cloud.execute(config,state,notion,analyzer,instant=dt.datetime(2026,11,5,0,tzinfo=dt.timezone.utc),lease_held=True)
        self.assertEqual([call.kwargs["media_policy"] for call in collect.call_args_list],['recent_posts'] if webhook else ["recent_posts","tracked_activity"])
        self.assertEqual(inbox.call_count, int(webhook))
        self.assertEqual(notion.publish.call_count,1,'실패 안내로 기존 보고를 덮어쓰면 안 됨')


if __name__=="__main__":unittest.main()
