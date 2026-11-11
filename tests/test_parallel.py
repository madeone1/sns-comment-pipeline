import contextlib
import copy
import datetime as dt
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from sns_comment.__main__ import main
from sns_comment.account import AccountCollector, is_global_failure
from sns_comment.api import GraphClient, GraphError, PilotError, RequestBudgetExceeded
from sns_comment.demo import DemoGraph, comment
from sns_comment.parallel import ParallelAccountCollector
from sns_comment.storage import Store


START,END="2026-10-28T23:00:00+00:00","2026-11-04T23:00:00+00:00"
CONFIG={"expected_username":"waterbomb_official","account_id":"100","api_version":"v26.0",
        "post_url":"https://www.instagram.com/p/ParallelA/",
        "collection":{"mode":"account","media_scope":"recent_posts","workers":3}}


class DelayedGraph(DemoGraph):
    def __init__(self,delay=0.01,barrier=False,expanded=False):
        super().__init__()
        self.delay=delay
        self.lock=threading.Lock()
        self.active=self.peak=0
        self.calls=[]
        self.barrier=threading.Barrier(3) if barrier else None
        self.responses={("100",None):{"id":"100","username":"waterbomb_official"},
            ("100/media",None):{"data":[
                {"id":"200","permalink":"https://www.instagram.com/p/ParallelA/","timestamp":START},
                {"id":"201","permalink":"https://www.instagram.com/p/ParallelB/","timestamp":START}]}}
        for media in ("200","201"):
            parents=[]
            for i in range(6):
                cid=str(int(media)*100+i)
                parents.append(comment(cid,"[합성] parent",START,replies={"data":[]} if expanded else None))
                self.responses[(cid+"/replies",None)]={"data":[] if i else [comment(cid+"9","[합성] reply",START)]}
            self.responses[(media+"/comments",None)]={"data":parents}

    def get(self,path,**params):
        with self.lock:
            self.request_count+=1
            self.active+=1
            self.peak=max(self.peak,self.active)
            self.calls.append((path,params.get("after")))
        try:
            if self.barrier and path.endswith("/replies"):
                try:self.barrier.wait(timeout=2)
                except threading.BrokenBarrierError:pass
            time.sleep(self.delay)
            value=self.responses[(path,params.get("after"))]
            if isinstance(value,Exception):raise value
            return copy.deepcopy(value)
        finally:
            with self.lock:self.active-=1


class ParallelTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/"parallel.sqlite3"
        self.store=Store(self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def collect(self,graph=None,**kwargs):
        return ParallelAccountCollector(graph or DelayedGraph(),self.store,media_policy="recent_posts",**kwargs).collect_account(START,END,"100")

    def test_gets_overlap_with_bound_and_database_writes_stay_on_caller(self):
        graph=DelayedGraph(barrier=True)
        owner=threading.get_ident();writers=[]
        save=self.store.save_page
        def write(*args,**kwargs):
            writers.append(threading.get_ident())
            return save(*args,**kwargs)
        with mock.patch.object(self.store,"save_page",side_effect=write):
            result=self.collect(graph,workers=3)
        self.assertEqual(result["status"],"complete")
        self.assertEqual(result["network_workers"],3)
        self.assertEqual(graph.peak,3)
        self.assertEqual(set(writers),{owner})
        self.assertEqual(result["stored_in_window"]["stored_comments_and_replies"],14)

    def test_parallel_and_sequential_return_identical_ids_parents_and_raw_versions(self):
        self.collect(DelayedGraph(delay=0))
        expected=[tuple(r) for r in self.store.db.execute("select id,parent_id,text,payload_hash from comments order by id")]
        other=Store(Path(self.tmp.name)/"serial.sqlite3")
        try:
            result=AccountCollector(DelayedGraph(delay=0),other,media_policy="recent_posts").collect_account(START,END,"100")
            actual=[tuple(r) for r in other.db.execute("select id,parent_id,text,payload_hash from comments order by id")]
            self.assertEqual(actual,expected)
            self.assertEqual(result["status"],"complete")
        finally:other.close()

    def test_shared_budget_is_never_exceeded_and_saved_edges_resume(self):
        graph=DelayedGraph()
        first=self.collect(graph,max_requests=6)
        self.assertEqual(first["status"],"partial")
        self.assertLessEqual(graph.request_count,6)
        self.assertIsNone(first["last_scope_completed_at"])
        next_graph=DelayedGraph()
        second=self.collect(next_graph)
        self.assertEqual(second["scope_id"],first["scope_id"])
        self.assertEqual(second["status"],"complete")
        self.assertNotIn(("100/media",None),next_graph.calls)
        self.assertEqual(self.store.summary()["stored_comments_and_replies"],14)

    def test_explicit_empty_reply_expansions_need_no_individual_get(self):
        graph=DelayedGraph(expanded=True)
        result=self.collect(graph)
        self.assertEqual(result["status"],"complete")
        self.assertFalse(any(path.endswith("/replies") for path,_ in graph.calls))
        self.assertEqual(result["api_requests"],4)

    def test_one_failed_post_does_not_hide_another_post_completion(self):
        graph=DelayedGraph();graph.responses[("201/comments",None)]=GraphError(400,100)
        result=self.collect(graph)
        self.assertEqual(result["status"],"partial")
        self.assertEqual(result["media_completed"],1)
        self.assertEqual(result["media_pending"],1)
        self.assertEqual(result["media_errors"][0]["media_id"],"201")

    def test_rate_limit_stops_scheduling_new_gets_and_never_completes_scope(self):
        graph=DelayedGraph();graph.responses[("200/comments",None)]=GraphError(429,4)
        # Let the collector observe the rate limit before the other admitted
        # parent request finishes; thread completion order is otherwise random.
        stopped=threading.Event();get=graph.get
        def controlled_get(path,**params):
            if path=="201/comments":
                if not stopped.wait(timeout=2):raise AssertionError("rate limit was not observed")
            return get(path,**params)
        def observe(error):
            result=is_global_failure(error)
            if result:stopped.set()
            return result
        with mock.patch.object(graph,"get",side_effect=controlled_get),mock.patch("sns_comment.parallel.is_global_failure",side_effect=observe):
            with self.assertRaises(PilotError):self.collect(graph)
        self.assertFalse(any(path.endswith("/replies") for path,_ in graph.calls))
        self.assertEqual(self.store.account_scopes()[0]["status"],"partial")

    def test_repeating_cursor_cannot_turn_into_success(self):
        graph=DelayedGraph(expanded=True)
        graph.responses[("200/comments",None)]["paging"]={"next":"https://graph.facebook.com/200/comments?after=same"}
        graph.responses[("200/comments","same")]=graph.responses[("200/comments",None)]
        result=self.collect(graph)
        self.assertEqual(result["status"],"partial")
        self.assertIsNone(result["last_scope_completed_at"])

    def test_crash_after_last_page_commit_can_finish_without_refetch(self):
        first=self.collect()
        with self.store.db:
            self.store.db.execute("update collection_scopes set status='partial',completed_at=null where id=?",(first["scope_id"],))
            self.store.db.execute("update scope_media set status='running',completed_at=null where scope_id=?",(first["scope_id"],))
        graph=DelayedGraph()
        result=self.collect(graph)
        self.assertEqual(result["status"],"complete")
        self.assertEqual(graph.calls,[("100",None)])

    def test_cli_uses_configured_parallel_workers(self):
        graph=DelayedGraph();output=io.StringIO()
        with mock.patch("sns_comment.__main__.read_config",return_value=CONFIG), \
                mock.patch("sns_comment.__main__.GraphClient",return_value=graph), \
                mock.patch.dict("os.environ",{"SNS_IG_ACCESS_TOKEN":"synthetic-token"}), \
                contextlib.redirect_stdout(output),contextlib.redirect_stderr(io.StringIO()):
            code=main(["run","--db",str(self.path),"--start",START,"--end",END,"--until-complete"])
        self.assertEqual(code,0)
        result=json.loads(output.getvalue())
        self.assertEqual(result["network_workers"],3)
        self.assertGreater(graph.peak,1)

    def test_transport_budget_is_atomic_and_measures_actual_get_overlap(self):
        from concurrent.futures import ThreadPoolExecutor
        class Transport:
            def __init__(self):
                self.requests=[]
                self.lock=threading.Lock()
            def open(self,request,timeout):
                with self.lock:self.requests.append(request)
                time.sleep(0.02)
                return io.BytesIO(b'{"id":"100"}')
        transport=Transport()
        client=GraphClient("synthetic-secret","v26.0",max_requests=7,opener=transport)
        def get(_):
            try:return client.get("100",fields="id")
            except RequestBudgetExceeded:return None
        with ThreadPoolExecutor(max_workers=3) as pool:
            values=list(pool.map(get,range(20)))
        self.assertEqual(sum(v is not None for v in values),7)
        self.assertEqual(client.request_count,7)
        self.assertEqual(client.active_requests,0)
        self.assertGreater(client.metrics()["peak_concurrent_requests"],1)
        self.assertLessEqual(client.metrics()["peak_concurrent_requests"],3)
        self.assertEqual(client.metrics()["by_endpoint"]["account_or_media"]["requests"],7)
        self.assertEqual(len(transport.requests),7)
        self.assertTrue(all(r.get_method()=="GET" for r in transport.requests))
        self.assertTrue(all("synthetic-secret" not in r.full_url for r in transport.requests))

    def test_duplicate_parent_expansion_cannot_complete_an_inflight_reply(self):
        class OverlappingGraph(DelayedGraph):
            def get(self,path,**params):
                if path=="20000/replies":
                    time.sleep(0.1)
                return super().get(path,**params)
        graph=OverlappingGraph(delay=0)
        graph.responses[("100/media",None)]["data"]=graph.responses[("100/media",None)]["data"][:1]
        graph.responses[("200/comments",None)]={"data":[comment("20000","[합성] duplicated",START)],
            "paging":{"next":"https://graph.facebook.com/200/comments?after=page2"}}
        graph.responses[("200/comments","page2")]={"data":[comment("20000","[합성] duplicated",START,replies={"data":[]})]}
        graph.responses[("20000/replies",None)]=GraphError(429,4)
        with self.assertRaises(PilotError):self.collect(graph)
        self.assertIsNone(self.store.db.execute("select last_completed_at from media where id='200'").fetchone()[0])
        self.assertEqual(self.store.scope_edge(self.store.account_scopes()[0]["id"],"200","replies:20000")["complete"],0)


if __name__=="__main__":unittest.main()
