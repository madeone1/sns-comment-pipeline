import datetime as dt
import json
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from sns_comment.api import PilotError
from sns_comment.cloud import coverage, retry
from sns_comment.notion import NotionClient, NotionError, block
from sns_comment.notion_oauth import OAuthCredentials
from sns_comment.reporting import Analyzer, clean_narrative, counts, digest, intent_counts, repeats, render, snapshot, validate_annotations, windows
from sns_comment.notion_mcp import markdown_blocks
from sns_comment.storage import Store



def instant(value):
    return dt.datetime.fromisoformat(value)


def row(key,text="좋아요",author="a",at="2026-11-04T01:00:00+00:00"):
    return {"id":key,"text":text,"author_id":author,"is_owner":0,"written_at":at,"parent_id":None}


class ReportingTests(unittest.TestCase):
    def test_invalid_ai_evidence_retries_and_falls_back_without_invented_claims(self):
        source={**row('1'),'caption':'영상'}
        view={'period':{'key':'daily'},'comments':[source],'counts':counts([source])}
        annotations={'1':{'category':'콘텐츠 반응','intent':'칭찬','reason':'호감'}}
        broken={'lines':['반응','현재 조치','다음 행동'],'insights':[{'text':'확인 안 된 주장','comment_ids':['invented']}],'actions':[],'rule_improvements':[]}
        analyzer=Analyzer(None,'project')
        with mock.patch.object(analyzer,'generate',return_value=broken) as generate:
            result=analyzer.report(view,view,annotations,{'text':'규정'})
        self.assertEqual(generate.call_count,2)
        self.assertEqual(result['narrative_status'],'validation_failed_counts_only')
        self.assertEqual(result['insights'],[])
        self.assertNotIn('확인 안 된 주장',json.dumps(result,ensure_ascii=False))

    def test_invalid_first_draft_can_recover_with_valid_evidence(self):
        source={**row('1'),'caption':'영상'}
        view={'period':{'key':'daily'},'comments':[source],'counts':counts([source])}
        annotations={'1':{'category':'콘텐츠 반응','intent':'칭찬','reason':'호감'}}
        valid={'lines':['반응','현재 조치','다음 행동'],'insights':[{'text':'호감 반응','comment_ids':[1]}],'actions':[],'rule_improvements':[]}
        analyzer=Analyzer(None,'project')
        with mock.patch.object(analyzer,'generate',side_effect=[{},valid]):
            result=analyzer.report(view,view,annotations,{'text':'규정'})
        self.assertEqual(result['validation_retries'],1)
        self.assertEqual(result['insights'][0]['comment_ids'],['1'])

    def test_daily_unreadable_comments_do_not_become_no_unresolved_issues(self):
        rows=[{**row('1',None),'caption':'영상'}]
        view={'period':{'key':'daily'},'comments':rows,'counts':counts(rows)}
        report={'lines':['본문 없음','미해결 없음','다음 수집'],'insights':[],'actions':[],'rule_improvements':[]}
        analyzer=Analyzer(None,'project')
        with mock.patch.object(analyzer,'generate',return_value=report):
            result=analyzer.report(view,view,{}, {'text':'규정'})
        self.assertIn('본문 확인 불가 1건은 미판정',result['lines'][1])
        self.assertIn('실제 답변·처리 이력은 미확인',result['lines'][1])
        self.assertNotIn('미해결 없음',result['lines'][1])

    def test_daily_and_context_counts_and_every_followup_are_visible(self):
        period=windows(instant('2026-11-09T08:00:00+09:00'))[0]
        def source(key,text,at):return {**row(key,text,at=at),'permalink':'https://www.instagram.com/p/example/'}
        daily=[source('today','좋아요','2026-11-08T12:00:00+09:00'),source('missing',None,'2026-11-08T13:00:00+09:00')]
        old=[source('old'+str(n),'일정은 언제인가요?','2026-11-07T12:00:00+09:00') for n in range(5)]
        annotations={'today':{'category':'콘텐츠 반응','intent':'칭찬','reason':'호감'}}
        annotations.update({r['id']:{'category':'일정과 장소','intent':'문의','reason':'일정 정보 질문'} for r in old})
        view={'period':period,'comments':daily,'counts':counts(daily),'media':[]}
        context={'period':{**period,'start':period['media_start']},'comments':old+daily,'counts':counts(old+daily),'media':[]}
        report={'lines':['반응','문의 없음','다음 점검'],'insights':[],'actions':[],'rule_improvements':[]}
        rules={'version':'v1.1','last_edited_at':'2026-11-07T00:00:00Z','text':'rule'}
        text=markdown_blocks(render(view,context,report,annotations,rules,'완료'))
        self.assertRegex(text,r'일일 의도별 반응: .*정보 문의 0건')
        self.assertRegex(text,r'최근 7일 참고 의도별 반응: .*정보 문의 5건')
        self.assertIn('전체 2건 = 관객 본문 분류 1건 + 관객 본문 확인 불가 1건',text)
        for r in old+daily[1:]:self.assertIn('댓글 ID '+r['id'],text)
        self.assertIn('2026-11-10 08:00',text)
        with self.assertRaisesRegex(PilotError,'분류가 누락'):
            render(view,context,report,{},rules,'완료')

    def test_changed_live_rules_invalidate_classification_cache(self):
        connection=sqlite3.connect(':memory:');self.addCleanup(connection.close)
        source={**row('1'),'caption':'영상','payload_hash':'raw'}
        draft={'id':'1','category':'콘텐츠 반응','intent':'칭찬','sentiment':'긍정','tags':[],'reason':'호감'}
        analyzer=Analyzer(None,'project','model')
        rules={'version':'v1.1','last_edited_at':'2026-11-07T00:00:00Z','text':'before'}
        with mock.patch.object(analyzer,'generate',return_value=[draft]) as generate:
            analyzer.classify(connection,[source],rules)
            analyzer.classify(connection,[source],rules)
            analyzer.classify(connection,[source],{**rules,'text':'changed rule'})
            self.assertEqual(generate.call_count,2)

    def test_daily_cutoff_before_and_after_8_kst(self):
        before = windows(instant("2026-11-05T07:59:59+09:00"))[0]
        after = windows(instant("2026-11-05T08:00:00+09:00"))[0]
        self.assertEqual(before["target_day"],"2026-11-04")
        self.assertEqual(after["target_day"],"2026-11-05")
        self.assertEqual(after["start"],"2026-11-04T08:00:00+09:00")
        self.assertEqual(after["media_start"],"2026-10-29T08:00:00+09:00")

    def test_friday_weekly_exact_friday_8_interval(self):
        periods = windows(instant("2026-11-06T08:00:00+09:00"))
        self.assertEqual([r["key"] for r in periods],["daily","weekly"])
        self.assertEqual(periods[1]["start"],"2026-10-30T08:00:00+09:00")

    def test_month_end_overlap_with_friday_and_next_month_final_target(self):
        periods = windows(instant("2027-12-31T08:00:00+09:00"))
        self.assertEqual([r["key"] for r in periods],["daily","weekly","monthly"])
        final = windows(instant("2028-01-01T08:00:00+09:00"))[-1]
        self.assertEqual(final["key"],"monthly_final")
        self.assertEqual(final["target_day"],"2027-12-31")
        self.assertEqual(final["end"],"2028-01-01T00:00:00+09:00")

    def test_repeated_emoji_is_candidate_not_spam_and_missing_authors_not_merged(self):
        values = [row("1","❤️","a","2026-11-04T00:00:00+00:00"),row("2","❤️","a","2026-11-04T00:00:02+00:00"),row("3","❤️","a","2026-11-04T00:00:04+00:00")]
        result = repeats(values+[row("4","❤️",None),row("5","❤️",None),row("6","❤️",None)])
        self.assertEqual(result["accounts"],1)
        self.assertEqual(result["comment_ids"],["1","2","3"])
        self.assertFalse(result["confirmed_spam"])
        self.assertIsNone(result["actual_actions"])

    def test_repetition_window_does_not_merge_requests_many_days_apart(self):
        self.assertEqual(repeats([row("1",at="2026-11-01T00:00:00+00:00"),row("2",at="2026-11-03T00:00:00+00:00"),row("3",at="2026-11-05T00:00:00+00:00")])["accounts"],0)

    def test_annotations_must_cover_readable_audience_ids_without_invented_ids(self):
        source = [row("1"),row("2",None)]
        draft = [{"id":"1","category":"콘텐츠 반응","intent":"칭찬","sentiment":"긍정","tags":[],"reason":"호감 표현"}]
        self.assertEqual(set(validate_annotations(source,draft)),{"1"})
        for broken in [[],draft+draft,[{**draft[0],"id":"fake"}],[{**draft[0],"category":"new"}]]:
            with self.assertRaises(PilotError):validate_annotations(source,broken)

    def test_internal_author_identity_is_not_displayed_in_report_narrative(self):
        self.assertEqual(clean_narrative("작성자 1명(author_key: abcdef0123456789)이 요청했습니다."),"작성자 1명이 요청했습니다.")
        self.assertEqual(clean_narrative("최근 7일 참고 데이터 기준 호주 개최 요청이 있었습니다."),"호주 개최 요청이 있었습니다.")
        self.assertEqual(clean_narrative("최근 7일 참고 기준 영상 공개 요청 1건."),"영상 공개 요청 1건.")
        self.assertEqual(clean_narrative("최근 7일 참고 데이터에서 티켓 예매를 언급했습니다."),"티켓 예매를 언급했습니다.")

    def test_wants_questions_and_action_requests_have_separate_counts(self):
        rows=[row("1","호주에서도 해주세요"),row("2","호주 꼭 와주세요"),
              row("3","티켓은 언제 열리나요?","b"),row("4","태민 풀영상 올려주세요","c"),
              row("5","OO 나왔으면",None),{**row("6"),"is_owner":1},row("7",None)]
        annotations={key:{"intent":intent} for key,intent in [
            ("1","희망·선호"),("2","희망·선호"),("3","문의"),("4","구체적 행동 요청"),
            ("5","희망·선호"),("6","희망·선호")]}
        result=intent_counts(rows,annotations)
        self.assertEqual(result["희망·선호"],{"comments":3,"unique_known_authors":1,"unknown_author_comments":1})
        self.assertEqual(result["문의"]["comments"],1)
        self.assertEqual(result["구체적 행동 요청"]["comments"],1)

    def test_intent_policy_change_reclassifies_legacy_request_cache_once(self):
        source={**row("1","호주에서도 해주세요"),"caption":"행사 영상","payload_hash":"raw-hash"}
        rules={"text":"규정","version":"v1.1"}
        legacy={"id":"1","category":"일정과 장소","intent":"요청","sentiment":"긍정","tags":[],"reason":"포괄 요청"}
        connection=sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE comment_analyses(comment_id TEXT,payload_hash TEXT,rule_hash TEXT,model TEXT,result_json TEXT,PRIMARY KEY(comment_id,payload_hash,rule_hash,model))")
        connection.execute("INSERT INTO comment_analyses VALUES(?,?,?,?,?)",("1","raw-hash",digest(rules),"model",json.dumps(legacy)))
        draft={**legacy,"intent":"희망·선호","reason":"정보를 묻기보다 개최를 희망함"}
        analyzer=Analyzer(None,"project","model")
        with mock.patch.object(analyzer,"generate",return_value=[draft]) as generate:
            result=analyzer.classify(connection,[source],rules)
            self.assertEqual(result["1"]["intent"],"희망·선호")
            analyzer.classify(connection,[source],rules)
            generate.assert_called_once()
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM comment_analyses").fetchone()[0],2)

    def test_final_month_report_does_not_use_new_month_context(self):
        analyzer=Analyzer(None,"project")
        period=windows(instant("2026-12-01T08:00:00+09:00"))[-1]
        view={"period":period,"comments":[],"counts":{"comments_and_replies":0}}
        context={"period":{"key":"context"},"comments":[row("december",at="2026-12-01T01:00:00+09:00")],"counts":{"comments_and_replies":1}}
        with mock.patch.object(analyzer,"generate") as generate:
            result=analyzer.report(view,context,{}, {"text":"rules"})
            generate.assert_not_called()
        self.assertEqual(result["insights"],[])
        self.assertIn("새 댓글·답글이 확인되지 않음",result["lines"][0])

    def test_monthly_totals_exclude_separate_old_post_tracking(self):
        with tempfile.TemporaryDirectory() as temp:
            store=Store(Path(temp)/"monthly.sqlite3")
            try:
                for key,at in [("100","2026-11-01T00:00:00Z"),("101","2026-11-20T00:00:00Z")]:
                    store.save_media("a",{"id":key,"permalink":"https://www.instagram.com/p/A/","timestamp":at})
                for key,media,at in [("1","100","2026-11-03T00:00:00Z"),("2","100","2026-11-21T00:00:00Z"),("3","101","2026-11-21T00:00:00Z")]:
                    store.save_page("seed","a",media,"comments",[{"id":key,"text":"합성 반응","timestamp":at,"from":{"id":"viewer","username":"viewer"}}],None,"waterbomb_official")
                for begin,end in [("2026-10-28T23:00:00+00:00","2026-11-04T23:00:00+00:00"),("2026-11-15T23:00:00+00:00","2026-11-22T23:00:00+00:00")]:
                    scope=store.collection_scope("a","waterbomb_official",begin,end,media_policy="recent_posts")
                    store.prepare_recent_scope(scope["id"])
                    with store.db:
                        store.db.execute("UPDATE scope_edges SET complete=1 WHERE scope_id=?",(scope["id"],))
                    for media in store.scope_media_ids(scope["id"]):
                        store.finish_scope_media(scope["id"],media,"complete")
                    store.finish_scope(scope["id"],0)
                period=windows(instant("2026-11-30T08:00:00+09:00"))[-1]
                result=snapshot(store.db,"a",period)
                self.assertEqual([r["id"] for r in result["comments"]],["1","3"])
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM comments").fetchone()[0],3)
            finally:store.close()

    def test_snapshot_filters_both_post_age_and_comment_time(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp)/"source.sqlite3")
            try:
                for key,date in [("100","2026-10-29T00:00:00+00:00"),("101","2026-10-01T00:00:00+00:00")]:
                    store.save_media("a",{"id":key,"permalink":"https://www.instagram.com/p/A/","timestamp":date})
                for key,media,at in [("1","100","2026-11-03T23:00:00+00:00"),("2","100","2026-11-04T23:00:00+00:00"),("3","101","2026-11-04T00:00:00+00:00")]:
                    store.save_page("seed","a",media,"comments",[{"id":key,"text":"안녕","timestamp":at,"from":{"id":"viewer","username":"viewer"}}],None,"waterbomb_official")
                view = snapshot(store.db,"a",windows(instant("2026-11-05T08:00:00+09:00"))[0])
                self.assertEqual([r["id"] for r in view["comments"]],["1"])
            finally:store.close()

    def test_retry_only_transient_failures_and_fixed_5_minute_interval(self):
        calls,sleeps = [],[]
        def failure():
            calls.append(1)
            raise PilotError("API HTTP 503")
        with self.assertRaises(PilotError):retry(failure,sleep=sleeps.append)
        self.assertEqual(len(calls),4)
        self.assertEqual(sleeps,[300,300,300])
        with mock.patch("sns_comment.cloud.time.sleep") as sleep:
            with self.assertRaises(PilotError):retry(lambda:(_ for _ in ()).throw(PilotError("인증 HTTP 401")),sleep=sleep)
            sleep.assert_not_called()

    def test_oauth_rotation_is_saved_before_return_and_save_failure_is_not_ignored(self):
        current = {"access_token":"old","refresh_token":"one-use","client_id":"client","token_endpoint":"https://mcp.notion.com/token"}
        stored=[]
        credential = OAuthCredentials(lambda:dict(current),stored.append)
        with mock.patch("sns_comment.notion_oauth.oauth_http",return_value={"access_token":"new","refresh_token":"rotated","expires_in":3600}):
            self.assertEqual(credential.refresh(),"new")
        self.assertEqual(stored[0]["refresh_token"],"rotated")
        credential = OAuthCredentials(lambda:dict(current),lambda value:(_ for _ in ()).throw(PilotError("save failed")))
        with mock.patch("sns_comment.notion_oauth.oauth_http",return_value={"access_token":"new","refresh_token":"rotated"}):
            with self.assertRaises(PilotError):credential.refresh()


class FakeNotion(NotionClient):
    def __init__(self,rows):
        self.rows=rows
        self.writes=[]
        self.source_id="source"
        self.day="2026-11-05"
        self.callout_children={}

    def request(self,method,path,payload=None):
        if path.endswith("/query"):
            return {"results":[{"id":"page","url":"https://www.notion.so/page","properties":{"이벤트 시간":{"date":{"start":self.day}}}}]}
        self.writes.append((method,path,payload))
        if path=="blocks/page/children":
            created={**payload["children"][0],"id":"auto"}
            self.rows.insert(1,created)
            return {"results":[created]}
        if path=="blocks/auto/children":self.callout_children["auto"]=[{**b,"id":"generated"+str(i)} for i,b in enumerate(payload["children"])]
        return {}

    def children(self,parent):
        return self.rows if parent=="page" else self.callout_children.get(parent,[])


class NotionScopeTests(unittest.TestCase):
    def test_rerun_reuses_owned_callout_and_preserves_other_sections_and_subpages(self):
        rows=[{**block("heading_3","SNS Comments"),"id":"heading"},
              {**block("paragraph","manual note"),"id":"human"},
              {**block("heading_3","Notices"),"id":"next"},
              {"id":"child","type":"child_page","child_page":{"title":"keep"}}]
        client=FakeNotion(rows)
        for _ in range(2):client.publish("2026-11-05",[block("paragraph","report")])
        self.assertEqual(sum(r["type"]=="callout" for r in rows),1)
        archived={path for method,path,value in client.writes if value.get("archived")}
        self.assertNotIn("blocks/human",archived)
        self.assertNotIn("blocks/child",archived)
        self.assertNotIn("blocks/next",archived)

    def test_date_mismatch_or_missing_section_never_writes(self):
        client=FakeNotion([])
        with self.assertRaises(NotionError):client.publish("2026-11-05",[])
        self.assertEqual(client.writes,[])
        client.day="2026-11-04"
        with self.assertRaises(NotionError):client.publish("2026-11-05",[])
        self.assertEqual(client.writes,[])


if __name__=="__main__":unittest.main()
