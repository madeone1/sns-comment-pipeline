import contextlib
import datetime as dt
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import os

from sns_comment import cloud
from sns_comment.comment_windows import baseline, projections
from sns_comment.storage import Store
from sns_comment.account import previous_month_start, AccountCollector, collect_continuously
from sns_comment.demo import comment
from sns_comment.api import PilotError
from test_account import AccountGraph, START, END


class WrittenCommentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name)/'raw.sqlite3')

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def seed(self, identifier, written, parent=None):
        self.store.save_media('100', {'id':'200','permalink':'https://www.instagram.com/p/Old/', 'timestamp':'2020-01-01T00:00:00Z'})
        self.store.save_page('seed','100','200','comments',[{'id':identifier,'text':'synthetic original','timestamp':written}],None,'waterbomb_official',parent_id=parent)

    def test_comment_boundaries_ignore_old_post_age_and_preserve_replies(self):
        for key,at,parent in [('1','2026-11-09T08:00:00+09:00',None),
                              ('2','2026-11-10T07:59:59+09:00','99'),
                              ('3','2026-11-10T08:00:00+09:00',None),
                              ('4','2026-11-03T08:00:00+09:00',None),
                              ('5','2026-11-03T07:59:59+09:00',None)]:
            self.seed(key,at,parent)
        views,week=projections(self.store.db,'100',dt.datetime.fromisoformat('2026-11-10T08:00:00+09:00'))
        self.assertEqual({r['id'] for r in views[0]['comments']},{'1','2'})
        self.assertEqual({r['id'] for r in week['comments']},{'1','2','4'})
        self.assertEqual(views[0]['counts']['replies'],1)
        self.assertEqual(week['period']['basis'],'comment_written_at')

    def test_baseline_discovers_old_media_and_resumes_after_reporting_day_changes(self):
        config={'account_id':'100','expected_username':'waterbomb_official','collection':{'workers':1},'cloud':{'historical_baseline_requests':5}}
        first=baseline(config,self.store,AccountGraph(),START,END,lambda:None)
        self.assertEqual(first['status'],'pending')
        config['cloud']['historical_baseline_requests']=50
        graph=AccountGraph()
        second=baseline(config,self.store,graph,'2026-10-29T23:00:00+00:00','2026-11-05T23:00:00+00:00',lambda:None)
        self.assertEqual(second['status'],'complete')
        self.assertEqual(first['scope_id'],second['scope_id'])
        self.assertNotIn(('100/media',None),graph.calls)
        self.assertIn(('202/comments',None),graph.calls)
        third_graph=AccountGraph()
        third=baseline(config,self.store,third_graph,START,END,lambda:None)
        self.assertEqual(third['status'],'complete')
        self.assertEqual(third_graph.calls,[])

    def test_previous_calendar_month_boundary_and_year_rollover(self):
        self.assertEqual(previous_month_start('2026-11-10T08:00:00+09:00'),'2026-09-30T15:00:00+00:00')
        self.assertEqual(previous_month_start('2027-01-01T08:00:00+09:00'),'2026-11-30T15:00:00+00:00')

    def test_comment_frontier_includes_old_posts_and_new_replies_without_old_parent_archive(self):
        for workers in (1,3):
            with self.subTest(workers=workers),tempfile.TemporaryDirectory() as folder:
                store=Store(Path(folder)/'frontier.sqlite3');graph=AccountGraph()
                graph.responses[('100/media',None)]['data'][0]['timestamp']='2020-01-01T00:00:00Z'
                graph.responses[('200/comments',None)]={'data':[
                    comment('90001','new comment','2026-11-09T00:00:00Z',replies={'data':[]}),
                    comment('90002','month boundary','2026-09-30T15:00:00Z',replies={'data':[]}),
                    comment('80000','old parent','2026-09-01T00:00:00Z',replies={'data':[
                        comment('90003','new reply on old parent','2026-11-08T00:00:00Z')]}),
                    comment('70000','old comment without reply field','2026-08-01T00:00:00Z')],
                    'paging':{'next':'https://graph.facebook.com/200/comments?after=must-not-read'}}
                graph.responses[('201/comments',None)]={'data':[]}
                result=collect_continuously(graph,store,'waterbomb_official','2026-09-30T15:00:00+00:00',
                    '2026-11-09T23:00:00+00:00',account_id='100',media_policy='recent_comments',workers=workers)
                self.assertEqual(result['status'],'complete')
                self.assertEqual(result['unobserved_old_post_activity'],'historical_parent_replies_require_webhook')
                self.assertNotIn(('200/comments','must-not-read'),graph.calls)
                self.assertNotIn(('70000/replies',None),graph.calls)
                self.assertEqual({r[0] for r in store.db.execute('SELECT id FROM comments')},{'90001','90002','90003','80000'})
                views,week=projections(store.db,'100',dt.datetime.fromisoformat('2026-11-10T08:00:00+09:00'))
                self.assertEqual({r['id'] for r in week['comments']},{'90001','90003'})
                store.close()

    def test_comment_frontier_rejects_non_chronological_pages(self):
        collector=AccountCollector(AccountGraph(),self.store,media_policy='recent_comments')
        scope=self.store.collection_scope('100','waterbomb_official','2026-09-30T15:00:00+00:00',END,
            media_policy='recent_comments')
        with self.assertRaisesRegex(PilotError,'최신순'):
            collector.window_page(scope['id'],[comment('1','old','2026-09-01T00:00:00Z'),
                comment('2','new','2026-11-01T00:00:00Z')],'cursor')

    def test_selected_publication_period_limits_comments_without_deleting_raw(self):
        self.seed('1','2026-11-09T10:00:00Z')
        self.store.save_media('100',{'id':'201','permalink':'https://www.instagram.com/p/Recent/',
            'timestamp':'2026-09-30T15:00:00+0000'})
        self.store.save_page('seed','100','201','comments',[{'id':'2','text':'boundary post',
            'timestamp':'2026-11-09T11:00:00Z'}],None,'waterbomb_official')
        views,week=projections(self.store.db,'100',dt.datetime.fromisoformat('2026-11-10T08:00:00+09:00'),
            media_since=previous_month_start('2026-11-10T08:00:00+09:00'))
        self.assertEqual([r['id'] for r in views[0]['comments']],['2'])
        self.assertEqual([r['id'] for r in week['comments']],['2'])
        self.assertEqual([r['id'] for r in week['media']],['201'])
        self.assertEqual(self.store.summary('100')['stored_comments_and_replies'],2)

    def test_collector_checks_recent_posts_on_later_catalog_pages_and_skips_old_comments(self):
        graph=AccountGraph()
        graph.responses[('100/media',None)]['data'][0]['timestamp']='2026-09-30T15:00:00Z'
        result=AccountCollector(graph,self.store,media_policy='previous_month_posts').collect_account(START,END,'100')
        self.assertEqual(result['status'],'complete')
        self.assertEqual(result['media_discovered'],2)
        self.assertTrue(result['discovery_exhaustive'])
        self.assertIn(('100/media','media2'),graph.calls)
        self.assertIn(('200/comments',None),graph.calls)
        self.assertIn(('201/comments',None),graph.calls)
        self.assertNotIn(('202/comments',None),graph.calls)

    def test_narrowing_reuses_completed_evidence_only_for_same_comment_interval(self):
        graph=AccountGraph()
        AccountCollector(graph,self.store).collect_account(START,END,'100')
        same=AccountGraph()
        result=AccountCollector(same,self.store,media_policy='previous_month_posts').collect_account(START,END,'100')
        self.assertEqual(result['status'],'complete')
        self.assertNotIn(('200/comments',None),same.calls)
        self.assertNotIn(('201/comments',None),same.calls)
        self.assertTrue(any(row[0].startswith('covered_by_scope:') for row in self.store.db.execute(
            'SELECT selection_reason FROM scope_media WHERE scope_id=?',(result['scope_id'],))))
        next_day=AccountGraph()
        AccountCollector(next_day,self.store,media_policy='previous_month_posts').collect_account(
            '2026-10-29T23:00:00+00:00','2026-11-05T23:00:00+00:00','100')
        self.assertIn(('201/comments',None),next_day.calls)

    def test_pipeline_collects_old_activity_before_daily_and_seven_day_reports(self):
        config={'expected_username':'waterbomb_official','account_id':'100','api_version':'v26.0',
            'notion':{'rules_page_id':'rules'},'collection':{},'cloud':{'comment_written_windows':True}}
        state=mock.MagicMock();state.restore_database.return_value=0;state.json_get.return_value=({},0)
        notion=mock.Mock();notion.rules.return_value={'text':'rules','version':'v1','last_edited_at':'2026-11-10T00:00:00Z'}
        notion.publish.return_value={'page_id':'page'}
        analyzer=mock.Mock();analyzer.classify.return_value={key:{'id':key,'category':'기타','intent':'판단 보류',
            'sentiment':'중립','tags':[],'reason':'synthetic test'} for key in ('1','2')}
        analyzer.report.return_value={'lines':['확인된 댓글'],'insights':[],'actions':[]}
        def collect(client,store,owner,start,end,**kwargs):
            old=kwargs['media_policy']=='tracked_activity'
            media='201' if old else '200'
            store.save_media('100',{'id':media,'permalink':'https://www.instagram.com/p/Source/',
                'timestamp':'2020-01-01T00:00:00Z' if old else '2026-11-08T00:00:00Z'})
            store.save_page('seed','100',media,'comments',[{'id':'2' if old else '1','text':'synthetic',
                'timestamp':'2026-11-09T10:00:00Z'}],None,owner)
            return {'status':'complete','media_discovered':1,'api_requests':2,'last_scope_completed_at':'2026-11-10T00:00:00Z'}
        with mock.patch.dict(os.environ,{'SNS_IG_ACCESS_TOKEN':'synthetic'},clear=True), \
             mock.patch.object(cloud,'GraphClient',return_value=mock.Mock(request_count=0)), \
             mock.patch.object(cloud,'collect_continuously',side_effect=collect), \
             mock.patch('sns_comment.comment_windows.baseline',return_value={'status':'pending'}), \
             contextlib.redirect_stdout(io.StringIO()):
            result=cloud.execute(config,state,notion,analyzer,instant=dt.datetime.fromisoformat('2026-11-10T08:00:00+09:00'),lease_held=True)
        source=next(c.args[1] for c in state.json_put.call_args_list if c.args[0]=='runs/2026-11-10/source.json')
        self.assertEqual({r['id'] for r in source['views'][0]['comments']},{'1','2'})
        self.assertEqual({r['id'] for r in source['recent_seven_days']['comments']},{'1','2'})
        self.assertEqual(source['collection']['status'],'partial')
        self.assertEqual(result['status'],'pending')
        self.assertEqual(notion.publish.call_count,1)


if __name__=='__main__': unittest.main()
