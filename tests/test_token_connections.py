import base64
import copy
import unittest
from unittest import mock

from sns_comment.api import GraphError,PilotError
from sns_comment.connection_registry import initial_registry,runtime_configs,REGISTRY_OBJECT
from sns_comment import token_connections as core
from sns_comment.token_cli import apply_token
from account_fixtures import fixture


class Graph:
    def __init__(self,token,version,max_requests=150):
        if token=='invalid':raise GraphError(400,190)
        self.ids=['10001','10002','10003'] if token=='abc' else ['10001','10004','10005']
        self.request_count=0;self.api_host='graph.facebook.com'
    def account(self,key):
        names={'10001':'waterbomb_official','10002':'waterbomb_busan_official','10003':'waterbomb_sokcho_official','10004':'new_d','10005':'new_e'}
        return {'id':key,'username':names[key]}
    def get(self,path,**params):
        self.request_count+=1
        if path=='me/assigned_instagram_accounts':return {'data':[self.account(i) for i in self.ids]}
        if path=='me/accounts':return {'data':[]}
        if path.endswith('/media'):return {'data':[{'id':'90001'}]}
        if path.endswith('/comments'):return {'data':[]}
        if path not in self.ids:raise GraphError(400,100)
        return self.account(path)


class State:
    bucket='example';transport=None
    def __init__(self,registry):self.objects={REGISTRY_OBJECT:copy.deepcopy(registry)};self.writes=[]
    def json_get(self,path,default=None):return copy.deepcopy(self.objects.get(path,default)),1 if path in self.objects else 0
    def json_put(self,path,value,generation=None):
        self.writes.append(path);self.objects[path]=copy.deepcopy(value);return 2


class TokenConnectionTests(unittest.TestCase):
    def setUp(self):
        self.settings,_=fixture();self.settings['cloud']['project_id']='synthetic'
        self.registry=initial_registry(self.settings)
        self.ref='projects/synthetic/secrets/sns-meta-token/versions/2'
    def plan(self,token='ade',**kwargs):return core.inspect_token(self.settings,self.registry,token,graph=Graph,**kwargs)

    def test_token_abc_to_ade_changes_the_snapshot_without_reusing_history(self):
        plan=self.plan(labels={'10004':'대구','10005':'제주'})
        updated=core.snapshot(self.registry,plan,self.ref,'operator','now',core.source_secret(self.settings))
        self.assertEqual({a['id'] for a in updated['accounts'].values() if a['active']},{'10001','10004','10005'})
        for key in ['10001','10002','10003']:
            self.assertEqual(updated['accounts'][key]['source_prefix'],self.registry['accounts'][key]['source_prefix'])
        self.assertEqual(updated['accounts']['10004']['source_prefix'],'accounts/ig_10004')
        configs=runtime_configs(self.settings,updated)
        self.assertEqual({c['_credential_secret_version'] for c in configs},{self.ref})
        self.assertEqual({a['id'] for a in configs[0]['_report_roster'] if a['active']},{'10001','10004','10005'})

    def test_same_account_new_token_version_keeps_all_account_paths_and_reviews(self):
        self.registry['accounts']['10001']['manual_review']='keep'
        plan=self.plan('abc');new=core.snapshot(self.registry,plan,self.ref,'operator','now',core.source_secret(self.settings))
        self.assertEqual(new['accounts']['10001']['manual_review'],'keep')
        self.assertEqual(plan['changes']['added'],[]);self.assertEqual(plan['changes']['paused'],[])
        for key in self.registry['accounts']:self.assertEqual(new['accounts'][key]['source_prefix'],self.registry['accounts'][key]['source_prefix'])

    def test_invalid_token_and_unmapped_regions_never_apply_an_empty_snapshot(self):
        before=copy.deepcopy(self.registry)
        with self.assertRaises(GraphError):self.plan('invalid')
        plan=self.plan();self.assertEqual(plan['status'],'mapping_required')
        with self.assertRaises(PilotError):core.snapshot(self.registry,plan,self.ref,'op','now','secret')
        self.assertEqual(self.registry,before)

    def test_incomplete_pagination_cannot_disable_previous_accounts(self):
        class Partial(Graph):
            def get(self,path,**params):
                if path=='me/assigned_instagram_accounts':
                    if params.get('after'):raise GraphError(400,100)
                    return {'data':[self.account('10001')],'paging':{'next':'https://graph.facebook.com/next?after=cursor'}}
                return super().get(path,**params)
        with self.assertRaises(GraphError):core.inspect_token(self.settings,self.registry,'ade',graph=Partial)

    def test_accessible_previous_account_missing_from_listing_is_retained(self):
        class Omitted(Graph):
            def get(self,path,**params):
                if path=='me/assigned_instagram_accounts':return {'data':[self.account('10001')]}
                return super().get(path,**params)
        plan=core.inspect_token(self.settings,self.registry,'abc',graph=Omitted)
        self.assertEqual({a['id'] for a in plan['accounts']},{'10001','10002','10003'})
        self.assertEqual(plan['changes']['paused'],[])

    def test_zero_media_is_a_valid_empty_account_without_fabricated_comment_permission(self):
        class Empty(Graph):
            def get(self,path,**params):
                if path.endswith('/media'):return {'data':[]}
                return super().get(path,**params)
        plan=core.inspect_token(self.settings,self.registry,'abc',graph=Empty)
        self.assertEqual(plan['status'],'verified')
        self.assertTrue(all(c['comments']=='no_media_to_query' for c in plan['checks']))

    def test_token_commit_uses_secret_body_not_logs_and_preserves_prior_generation(self):
        state=State(self.registry);token='replacement-secret-never-in-audit'
        plan=core.inspect_token(self.settings,self.registry,token,labels={'10004':'대구','10005':'제주'},graph=Graph)
        state.transport=mock.Mock();state.transport.json.return_value={'name':self.ref}
        with mock.patch('sns_comment.token_cli.read_token',return_value=('abc','projects/synthetic/secrets/sns-meta-token/versions/1')):
            result=apply_token(self.settings,state,token,plan,'operator')
        self.assertEqual(result['status'],'sync_pending')
        self.assertEqual(base64.b64decode(state.transport.json.call_args.args[2]['payload']['data']).decode(),token)
        self.assertNotIn(token,str(state.objects));self.assertTrue(state.objects[REGISTRY_OBJECT]['token_control']['source_version'].endswith('/2'))

    def test_a_different_token_cannot_reuse_another_tokens_verification(self):
        state=State(self.registry);plan=self.plan('abc');state.transport=mock.Mock()
        with self.assertRaisesRegex(PilotError,'검증한 토큰'):
            apply_token(self.settings,state,'another-token',plan,'operator')
        state.transport.json.assert_not_called();self.assertEqual(state.writes,[])

    def test_a_changed_registry_blocks_token_storage_before_any_write(self):
        plan=self.plan('abc');other=copy.deepcopy(self.registry);other['audit'].append({'action':'concurrent'})
        state=State(other);state.transport=mock.Mock()
        with self.assertRaisesRegex(PilotError,'바뀌었습니다'):apply_token(self.settings,state,'abc',plan,'operator')
        state.transport.json.assert_not_called();self.assertEqual(state.writes,[])

    def test_a_concurrently_replaced_secret_is_not_overwritten(self):
        state=State(self.registry);state.transport=mock.Mock();plan=self.plan('abc')
        plan['source_version_before']='projects/synthetic/secrets/sns-meta-token/versions/1'
        with mock.patch('sns_comment.token_cli.read_token',return_value=('somebody-elses-token',self.ref)),self.assertRaisesRegex(PilotError,'다른 경로'):
            apply_token(self.settings,state,'abc',plan,'operator')
        state.transport.json.assert_not_called();self.assertEqual(state.writes,[])

    def test_reconcile_skips_discovery_for_the_pinned_unchanged_version(self):
        plan=self.plan('abc');registry=core.snapshot(self.registry,plan,self.ref,'operator','now',core.source_secret(self.settings))
        state=State(registry)
        with mock.patch.object(core,'read_token',return_value=('abc',self.ref)),mock.patch.object(core,'inspect_token') as inspect:
            updated,status=core.reconcile(self.settings,state)
        inspect.assert_not_called();self.assertEqual(status['status'],'unchanged');self.assertEqual(state.writes,[])

    def test_web_or_external_registry_changes_cannot_bypass_the_verified_token_source(self):
        plan=self.plan('abc');registry=core.snapshot(self.registry,plan,self.ref,'operator','now',core.source_secret(self.settings))
        registry['accounts']['10002']['active']=False;state=State(registry)
        with mock.patch.object(core,'read_token',return_value=('abc',self.ref)),self.assertRaisesRegex(PilotError,'다른 경로'):
            core.reconcile(self.settings,state)
        self.assertEqual(state.writes,[])

    def test_new_unmapped_latest_version_preserves_the_last_verified_registry(self):
        old=core.snapshot(self.registry,self.plan('abc'),'projects/synthetic/secrets/sns-meta-token/versions/1','op','now',core.source_secret(self.settings))
        state=State(old)
        with mock.patch.object(core,'read_token',return_value=('ade',self.ref)),self.assertRaisesRegex(PilotError,'매핑'):
            core.reconcile(self.settings,state,graph=Graph)
        self.assertEqual(state.objects[REGISTRY_OBJECT],old)
        self.assertEqual(state.objects[core.STATUS_OBJECT]['status'],'mapping_required')

    def test_delivery_requires_new_revision_and_both_output_checkpoints(self):
        plan=self.plan('abc');registry=core.snapshot(self.registry,plan,self.ref,'op','now',core.source_secret(self.settings))
        configs=runtime_configs(self.settings,registry);revision=registry['token_control']['revision'];shared=State(registry)
        stores={}
        for config in configs:
            original={'connection_revision':revision,'views':[]}
            state=State(registry);state.objects.update({'runs/day/source.json':original,
                'runs/day/workflow.json':{'source_hash':'same'},'runs/day/sheets.json':{'status':'complete','source_hash':'same','account_tabs':{c['account_id']:'tab' for c in configs}},
                'state/reports.json':{'day':{'connection_revision':revision,'source_hash':core.digest(original),'status':'published','result':{'readback_verified':True}}}})
            stores[config['_cloud_prefix']]=state
        with mock.patch('sns_comment.cloud_state.CloudState',side_effect=lambda bucket,transport,prefix:stores[prefix]):
            result=core.verify_delivery(shared,configs,'day','pending')
            self.assertEqual(result['status'],'synced');self.assertEqual(result['collection_status'],'pending')
            stores[configs[1]['_cloud_prefix']].objects['state/reports.json']['day']['connection_revision']='old'
            self.assertEqual(core.verify_delivery(shared,configs,'day','pending')['status'],'sync_pending')
            stores[configs[1]['_cloud_prefix']].objects['state/reports.json']['day']['connection_revision']=revision
            failed=[{'account':configs[0]['expected_username'],'status':'failed'}]
            self.assertEqual(core.verify_delivery(shared,configs,'day','pending',failed)['status'],'sync_pending')


if __name__=='__main__':unittest.main()
