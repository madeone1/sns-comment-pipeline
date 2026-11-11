import contextlib
import hashlib
import hmac
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from sns_comment.__main__ import main
from sns_comment.account import AccountCollector
from sns_comment.api import PilotError
from sns_comment.demo import AccountDemoGraph, comment
from sns_comment.sheets import read_source
from sns_comment.storage import Store
from sns_comment.tracking import accept_comment_webhook


START, END = "2026-10-28T23:00:00+00:00", "2026-11-04T23:00:00+00:00"
CONFIG = {"expected_username":"waterbomb_official","account_id":"100","api_version":"v26.0",
          "post_url":"https://www.instagram.com/reel/DfCKxEYlZVv/",
          "collection":{"mode":"account","media_scope":"recent_posts","lookback_days":7,"discovery_pages":1}}


class RecentGraph(AccountDemoGraph):
    def __init__(self):
        super().__init__()
        self.calls = []
        old = self.responses[("100/media",None)]["data"][0]
        recent = self.responses[("100/media","media2")]["data"][0]
        self.responses[("100/media",None)]["data"] = [recent,old]
        self.responses[("100/media","media2")]["data"] = [self.responses[("100/media","media2")]["data"][1]]

    def get(self,path,**params):
        self.calls.append((path,params.get("after")))
        return super().get(path,**params)


class RecentScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/"comments.sqlite3"
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def collect(self,graph=None,policy="recent_posts",**kwargs):
        return AccountCollector(graph or RecentGraph(),self.store,media_policy=policy,**kwargs).collect_account(START,END,"100")

    def seed_old_activity(self,at=START):
        media = RecentGraph().responses[("100/media",None)]["data"][1]
        self.store.save_media("100",media)
        self.store.save_page("seed","100","200","comments",[comment("900","[합성] 확인된 최근 댓글",at)],None,"waterbomb_official")

    def test_recent_posts_only_and_one_catalog_page_not_historic_comment_sweep(self):
        graph=RecentGraph();result=self.collect(graph)
        self.assertEqual(result["status"],"complete")
        self.assertEqual(result["scope"],"recent_posts")
        self.assertEqual(result["media_discovered"],1)
        self.assertEqual(result["api_requests"],3)
        self.assertEqual(result["stored_in_window"]["stored_comments_and_replies"],2)
        self.assertNotIn(("100/media","media2"),graph.calls)
        self.assertNotIn(("200/comments",None),graph.calls)
        self.assertFalse(result["discovery_exhaustive"])

    def test_cached_recent_media_is_used_without_remote_historical_catalog_walk(self):
        self.store.save_media("100",{"id":"203","permalink":"https://www.instagram.com/p/CachedNew/","timestamp":"2026-11-04T02:00:00Z"})
        graph=RecentGraph();graph.responses[("203/comments",None)]={"data":[]}
        result=self.collect(graph)
        self.assertEqual(result["media_discovered"],2)
        self.assertIn(("203/comments",None),graph.calls)
        self.assertNotIn(("100/media","media2"),graph.calls)

    def test_old_activity_is_separate_and_unrelated_old_posts_are_never_queried(self):
        self.seed_old_activity()
        primary_graph=RecentGraph();primary=self.collect(primary_graph)
        self.assertEqual(primary["stored_in_window"]["stored_comments_and_replies"],2)
        self.assertNotIn(("200/comments",None),primary_graph.calls)
        watch_graph=RecentGraph();watch=self.collect(watch_graph,policy="tracked_activity")
        self.assertEqual(watch["scope"],"tracked_activity")
        self.assertEqual(watch["media_discovered"],1)
        self.assertIn(("200/comments",None),watch_graph.calls)
        self.assertNotIn(("100/media",None),watch_graph.calls)
        self.assertNotIn(("201/comments",None),watch_graph.calls)
        self.assertNotIn(("202/comments",None),watch_graph.calls)
        self.assertNotEqual(primary["scope_id"],watch["scope_id"])

    def test_old_recent_comment_not_yet_observed_does_not_trigger_a_full_search(self):
        self.collect()
        watch_graph=RecentGraph();watch=self.collect(watch_graph,policy="tracked_activity")
        self.assertEqual(watch["media_discovered"],0)
        self.assertEqual(watch_graph.calls,[("100",None)])
        self.assertEqual(watch["unobserved_old_post_activity"],"not_monitored_without_event_delivery")

    def test_expired_old_activity_is_not_polled_forever(self):
        self.seed_old_activity("2026-10-28T22:59:59Z")
        self.store.track_media("100","200","2026-10-28T22:59:59Z")
        watch=self.collect(policy="tracked_activity")
        self.assertEqual(watch["media_discovered"],0)

    def test_prior_full_scope_is_not_reused_for_recent_mode(self):
        old=self.store.collection_scope("100","waterbomb_official",START,END)
        recent=self.collect()
        self.assertNotEqual(old["id"],recent["scope_id"])
        with self.assertRaises(PilotError):
            AccountCollector(RecentGraph(),self.store,media_policy="recent_posts").collect_account(START,END,"100",resume_scope_id=old["id"])

    def test_recent_scope_resumes_without_recataloguing_or_unrelated_old_work(self):
        first=self.collect(max_requests=2)
        self.assertEqual(first["status"],"partial")
        graph=RecentGraph();second=self.collect(graph)
        self.assertEqual(second["status"],"complete")
        self.assertEqual(first["scope_id"],second["scope_id"])
        self.assertEqual(graph.calls,[("100",None),("201/comments",None)])

    def test_export_separates_primary_raw_and_old_activity_summary(self):
        self.seed_old_activity();recent=self.collect();self.collect(policy="tracked_activity")
        source=read_source(self.path,CONFIG,collection_period=(START,END))
        self.assertEqual(source["coverage"]["raw_rows_targeted"],2)
        self.assertEqual(source["coverage"]["recent_posts"],1)
        self.assertEqual(source["coverage"]["account_scope_id"],recent["scope_id"])
        self.assertEqual(source["coverage"]["tracked_comments_and_replies"],5)
        self.assertEqual({r[4] for r in source["tabs"]["댓글 원본"][1:]},{"201"})
        self.assertTrue(any(r[2]=="이전 게시물 추적" for r in source["tabs"]["기간별 요약"][1:]))

    def test_cli_continuous_mode_obeys_recent_policy_from_config(self):
        graph=RecentGraph();output=io.StringIO()
        with mock.patch("sns_comment.__main__.read_config",return_value=CONFIG), \
                mock.patch("sns_comment.__main__.GraphClient",return_value=graph), \
                mock.patch.dict("os.environ",{"SNS_IG_ACCESS_TOKEN":"synthetic-token"}), \
                contextlib.redirect_stdout(output),contextlib.redirect_stderr(io.StringIO()):
            code=main(["run","--db",str(self.path),"--start",START,"--end",END,"--until-complete"])
        self.assertEqual(code,0)
        self.assertEqual(json.loads(output.getvalue())["scope"],"recent_posts")
        self.assertNotIn(("200/comments",None),graph.calls)

    def test_missing_policy_does_not_restore_full_historical_scan(self):
        config={**CONFIG,"collection":{"mode":"account","lookback_days":7}}
        graph=RecentGraph();output=io.StringIO()
        with mock.patch("sns_comment.__main__.read_config",return_value=config), \
                mock.patch("sns_comment.__main__.GraphClient",return_value=graph), \
                mock.patch.dict("os.environ",{"SNS_IG_ACCESS_TOKEN":"synthetic-token"}), \
                contextlib.redirect_stdout(output),contextlib.redirect_stderr(io.StringIO()):
            code=main(["run","--db",str(self.path),"--start",START,"--end",END])
        self.assertEqual(code,0)
        self.assertEqual(json.loads(output.getvalue())["scope"],"recent_posts")
        self.assertNotIn(("200/comments",None),graph.calls)

    def test_only_verified_account_comment_events_activate_watchlist(self):
        self.seed_old_activity("2026-10-28T22:59:59Z")
        payload={"object":"instagram","entry":[{"id":"100","changes":[{"field":"comments","value":{"media":{"id":"200"},"text":"do not store this body"}}]}]}
        body=json.dumps(payload).encode();secret="synthetic-app-secret"
        with self.assertRaises(PilotError):
            accept_comment_webhook(body,"sha256="+"0"*64,secret,"100",self.store)
        self.assertEqual(self.store.tracked_media("100"),[])
        signature="sha256="+hmac.new(secret.encode(),body,hashlib.sha256).hexdigest()
        result=accept_comment_webhook(body,signature,secret,"other-account",self.store)
        self.assertEqual(result["tracked_media"],[])
        result=accept_comment_webhook(body,signature,secret,"100",self.store)
        self.assertEqual(result["tracked_media"],["200"])
        self.assertNotIn("do not store",str(self.store.tracked_media("100")))
        watch=self.collect(policy="tracked_activity")
        self.assertEqual(watch["media_discovered"],1)

    def test_post_publication_boundaries_are_start_inclusive_end_exclusive(self):
        graph=RecentGraph()
        graph.responses[("100/media",None)]["data"].extend([
            {"id":"204","permalink":"https://www.instagram.com/p/AtStart/","timestamp":START},
            {"id":"205","permalink":"https://www.instagram.com/p/AtEnd/","timestamp":END},
            {"id":"206","permalink":"https://www.instagram.com/p/BeforeStart/","timestamp":"2026-10-28T22:59:59Z"}])
        graph.responses[("204/comments",None)]={"data":[]}
        result=self.collect(graph)
        self.assertEqual(result["media_discovered"],2)
        self.assertIn(("204/comments",None),graph.calls)
        self.assertNotIn(("205/comments",None),graph.calls)
        self.assertNotIn(("206/comments",None),graph.calls)

    def test_unknown_signalled_media_must_verify_account_before_comments(self):
        self.store.track_media("100","999",START,"verified_webhook")
        graph=RecentGraph()
        item={"id":"999","permalink":"https://www.instagram.com/p/SignalledOld/",
              "timestamp":"2020-01-01T00:00:00Z","owner":{"id":"wrong-account"}}
        graph.responses[("999",None)]=item
        with self.assertRaises(PilotError):
            self.collect(graph,policy="tracked_activity")
        self.assertNotIn(("999/comments",None),graph.calls)
        item["owner"]={"id":"100"}
        graph.responses[("999/comments",None)]={"data":[]}
        result=self.collect(graph,policy="tracked_activity")
        self.assertEqual(result["media_discovered"],1)
        self.assertEqual(result["status"],"complete")

    def test_recent_counter_needs_no_token_or_network_and_keeps_old_counts_separate(self):
        self.seed_old_activity();self.collect()
        from sns_comment.counts import main as count_main
        output=io.StringIO()
        with mock.patch("sns_comment.counts.read_config",return_value=CONFIG), \
                mock.patch("sns_comment.counts.GraphClient",side_effect=AssertionError("network")), \
                mock.patch("sys.stdin.isatty",return_value=False), \
                mock.patch("sns_comment.sheets.collection_window",return_value=(START,END)), \
                contextlib.redirect_stdout(output):
            code=count_main(["--recent","--db",str(self.path),"--output",str(Path(self.temp.name)/"count.json")])
        self.assertEqual(code,0)
        result=json.loads(output.getvalue())
        self.assertEqual(result["coverage"]["raw_rows_targeted"],2)
        self.assertEqual(result["separate_old_post_tracking"]["comments_and_replies"],1)


if __name__=="__main__":
    unittest.main()
