import copy
import io
import json
import unittest
from unittest import mock

from sns_comment.api import PilotError
from sns_comment.notion import block
from sns_comment.notion_mcp import NoDailyPage, NotionMcpClient, markdown_blocks


PAGE="40b0761a512d81e49f63dde6a629ec3a"
SOURCE="74e519dd-0028-4fc0-8dad-f3169636b767"


class FakeMcp:
    def __init__(self):
        self.calls=[]
        self.date="2026-11-05"
        self.content='### Other\n\nKeep this note.\n\n### SNS Comments\n\n- <b>SNS Comments:</b>\n\n---\n\n### Notices\n\n<page url="https://www.notion.so/11111111111111111111111111111111">Keep child</page>'
        self.warnings=[]
        self.async_update=False

    def call(self,name,args):
        self.calls.append((name,copy.deepcopy(args)))
        if name=="get-tool-access":return {"current_tool_access":{key:{"status":"available"} for key in ("fetch","query_data_sources","update_page")}}
        if name=="fetch":
            if args["id"].startswith("collection://"):return {"text":'url TEXT, "유형" TEXT, "date:이벤트 시간:start" TEXT'}
            if args["id"].startswith("notion://"):return {"text":"markdown specification"}
            props={"유형":"오늘 업무","date:이벤트 시간:start":self.date}
            return {"url":"https://app.notion.com/p/"+PAGE,"truncated":True,"unknown_block_count":1,
                "text":"<properties>\n"+json.dumps(props,ensure_ascii=False)+"\n</properties>\n<content>\n"+self.content+"\n</content>"}
        if name=="query-data-sources":return {"results":[{"url":"https://app.notion.com/"+PAGE}]}
        if name=="update-page":
            for u in args["content_updates"]:
                if self.content.count(u["old_str"])!=1:raise AssertionError("unsafe match")
                self.content=self.content.replace(u["old_str"],u["new_str"])
            if self.async_update:return {"type":"async_task","task_id":"task1"}
            return {"page_id":PAGE,"warnings":self.warnings}
        if name=="get-async-task":return {"status":"succeeded","result":{"page_id":PAGE}}
        raise AssertionError(name)


class McpReportTests(unittest.TestCase):
    def test_concurrent_other_work_is_retained_without_failing_the_sns_update(self):
        api=FakeMcp();original=api.call
        def call(name,args):
            result=original(name,args)
            if name=='update-page':api.content+='\n\nHuman concurrent work'
            return result
        api.call=call
        result=NotionMcpClient(api,SOURCE).publish('2026-11-05',[block('paragraph','new SNS report')])
        self.assertTrue(result['readback_verified']);self.assertTrue(result['other_work_changed_concurrently'])
        self.assertTrue(api.content.endswith('Human concurrent work'))

    def test_concurrent_sns_change_is_not_reported_as_a_complete_update(self):
        api=FakeMcp();original=api.call
        def call(name,args):
            result=original(name,args)
            if name=='update-page':api.content=api.content.replace('\n---','\nConcurrent SNS note\n---',1)
            return result
        api.call=call
        with self.assertRaisesRegex(PilotError,'동시 변경'):
            NotionMcpClient(api,SOURCE).publish('2026-11-05',[block('paragraph','new SNS report')])

    def test_regeneration_preserves_previous_cited_evidence_without_stale_summary(self):
        api=FakeMcp();client=NotionMcpClient(api,SOURCE,regional=True)
        original=self.regional_sections('waterbomb_official',1,'옛 요약')
        original[-1]['toggle']['children']=[block('quote','이전 관객 원문'),block('paragraph','댓글 ID 987 · 원문 링크')]
        client.publish('2026-11-05',original)
        client.publish('2026-11-05',self.regional_sections('waterbomb_official',2,'새 요약'))
        self.assertIn('댓글 ID 987',api.content);self.assertIn('이전 관객 원문',api.content)
        self.assertIn('이전 보고에서 보존한 근거 댓글',api.content)
        self.assertNotIn('옛 요약',api.content)
        self.assertTrue(client.publish('2026-11-05',self.regional_sections('waterbomb_official',2,'새 요약'))['unchanged'])

    def test_readback_accepts_notion_generated_domain_links_without_ignoring_changed_links(self):
        api=FakeMcp();original=api.call
        def call(name,args):
            result=original(name,args)
            if name=='fetch':result['text']=result['text'].replace('D.va','[D.va](http://D.va)')
            return result
        api.call=call
        self.assertTrue(NotionMcpClient(api,SOURCE).publish('2026-11-05',[block('quote','Me lembrou a D.va do Overwatch')])['readback_verified'])
        from sns_comment.notion_mcp import normalized_markdown
        self.assertNotEqual(normalized_markdown('D.va'),normalized_markdown('[D.va](http://different.example)'))

    def regional_sections(self, name, count=1, text='확인된 반응'):
        return [block('paragraph',f'수집 계정: @{name} · 댓글 작성일 기준'),
                block('paragraph',f'일일 · 댓글·답글 {count}건'),
                block('bulleted_list_item',text),
                block('toggle','일일 상세 분석·근거 댓글',children=[
                    block('paragraph','댓글 ID 123 · 과거 게시물 대조 pending'),
                    block('quote','관객 원문')])]

    def test_regional_migration_preserves_evidence_other_dates_and_manual_content(self):
        api=FakeMcp()
        names=('waterbomb_official','waterbomb_busan_official','waterbomb_sokcho_official')
        for name in names:
            NotionMcpClient(api,SOURCE,username=name).publish('2026-11-05',self.regional_sections(name))
        # Previous dates on the same daily page remain separately owned.
        api.content=api.content.replace('\n---', '\n<callout>\n인스타그램 댓글 자동 보고 · 2026-11-04 08시 · @waterbomb\\_official\n\nOlder report\n</callout>\n\nHuman SNS note.\n---',1)
        client=NotionMcpClient(api,SOURCE,username=names[0],regional=True)
        result=client.publish('2026-11-05',self.regional_sections(names[0],11,'새 요약'))
        self.assertTrue(result['readback_verified'])
        self.assertIn('대표(공식) · 새 댓글 11건',api.content)
        self.assertIn('부산 · 새 댓글 1건',api.content)
        self.assertIn('속초 · 새 댓글 1건',api.content)
        self.assertEqual(api.content.count('공통 수집 기록'),1)
        self.assertEqual(api.content.count('관객 원문'),3)
        for kept in ('댓글 ID 123','Human SNS note.','Older report','Keep this note.','Keep child</page>'):
            self.assertIn(kept,api.content)
        self.assertIn('수집 범위 확인 중: 대표(공식)·부산·속초',api.content)
        self.assertTrue(client.publish('2026-11-05',self.regional_sections(names[0],11,'새 요약'))['unchanged'])

    def test_regional_rerun_only_updates_one_account_and_keeps_summary_outside_toggles(self):
        api=FakeMcp()
        official=NotionMcpClient(api,SOURCE,regional=True)
        busan=NotionMcpClient(api,SOURCE,username='waterbomb_busan_official',regional=True)
        official.publish('2026-11-05',self.regional_sections('waterbomb_official',11,'원호 출연 희망'))
        busan.publish('2026-11-05',self.regional_sections('waterbomb_busan_official',0,'이전 빈 보고'))
        before=api.content.split('대표(공식) · 새 댓글 11건',1)[1].split('</callout>',1)[0].split('<toggle>\n부산',1)[0]
        busan.publish('2026-11-05',self.regional_sections('waterbomb_busan_official',1,'부산 일정 문의'))
        after=api.content.split('대표(공식) · 새 댓글 11건',1)[1].split('</callout>',1)[0].split('<toggle>\n부산',1)[0]
        self.assertEqual(before,after)
        outer=api.content.split('<callout',1)[1].split('<toggle>',1)[0]
        self.assertIn('원호 출연 희망',outer)
        self.assertIn('부산 일정 문의',outer)
        self.assertLessEqual(outer.count('\n- '),3)
        self.assertEqual(api.content.count('<callout icon="📊"'),1)

    def test_regional_title_uses_marketer_managed_label(self):
        api=FakeMcp()
        client=NotionMcpClient(api,SOURCE,username='waterbomb_new_official',regional=True,region_label='새 지역')
        client.publish('2026-11-05',self.regional_sections('waterbomb_new_official',1,'새 지역 반응'))
        self.assertIn('새 지역 · 새 댓글 1건',api.content)
        self.assertTrue(client.publish('2026-11-05',self.regional_sections('waterbomb_new_official',1,'새 지역 반응'))['unchanged'])

    def test_outer_sns_toggle_is_read_and_repaired_to_an_ordinary_heading(self):
        api=FakeMcp()
        before='### Other\n\nKeep this note.\n\n'
        after='\n\n---\n\n### Notices\n\nKeep next work.'
        api.content=before+'<toggle heading="h2" color="gray_bg">\nSNS Comments\n\nHuman SNS note.\n\n- <b>SNS Comments:</b>\n\n<toggle>\n수동 근거\n\n```html\n<toggle>\n```\n\nKeep nested notes.\n</toggle>\n</toggle>'+after
        client=NotionMcpClient(api,SOURCE,regional=True)
        result=client.publish('2026-11-05',self.regional_sections('waterbomb_official',1,'관객 반응'))
        self.assertTrue(result['readback_verified'])
        self.assertTrue(api.content.startswith(before+'### SNS Comments\n'))
        self.assertTrue(api.content.endswith(after))
        for kept in ['Human SNS note.','수동 근거','Keep nested notes.']:
            self.assertIn(kept,api.content)
        self.assertNotIn('<toggle heading="h2" color="gray_bg">\nSNS Comments',api.content)
        self.assertIn('<toggle>\n대표(공식)',api.content)
        self.assertTrue(client.publish('2026-11-05',self.regional_sections('waterbomb_official',1,'관객 반응'))['unchanged'])

    def test_both_heading_levels_and_next_heading_toggle_are_bounded(self):
        for heading in ['## SNS Comments','### SNS Comments','<h2>SNS Comments</h2>', '<h3>\nSNS Comments\n</h3>']:
            with self.subTest(heading=heading):
                api=FakeMcp();suffix='\n<toggle heading="h2">\nNext work\n\nKeep exactly.\n</toggle>'
                api.content=heading+'\n\n- <b>SNS Comments:</b>'+suffix
                NotionMcpClient(api,SOURCE).publish('2026-11-05',[block('paragraph','new report')])
                self.assertTrue(api.content.startswith(heading))
                self.assertTrue(api.content.endswith(suffix))

    def test_duplicate_heading_and_toggle_are_never_selected(self):
        api=FakeMcp();api.content+='\n<toggle>\nSNS Comments\n\nDuplicate\n</toggle>'
        with self.assertRaisesRegex(PilotError,'중복'):
            NotionMcpClient(api,SOURCE).publish('2026-11-05',[block('paragraph','new report')])
        self.assertFalse(any(name=='update-page' for name,_ in api.calls))

    def test_publication_guideline_failure_prevents_remote_write(self):
        api=FakeMcp()
        def reject(markdown, context):
            self.assertTrue(markdown.startswith('### SNS Comments'))
            self.assertEqual(context['account'],'waterbomb_official')
            raise PilotError('최신 지침 위반')
        with self.assertRaisesRegex(PilotError,'지침 위반'):
            NotionMcpClient(api,SOURCE,regional=True).publish('2026-11-05',
                self.regional_sections('waterbomb_official'),guideline_validator=reject)
        self.assertFalse(any(name=='update-page' for name,_ in api.calls))

    def test_zero_daily_keeps_nonzero_week_visible_before_detail(self):
        api=FakeMcp()
        sections=self.regional_sections('waterbomb_sokcho_official',0,'새 댓글 없음')
        sections.insert(2,block('paragraph','반응 분석 범위: 최근 7일 · 댓글·답글 10건'))
        NotionMcpClient(api,SOURCE,username='waterbomb_sokcho_official',regional=True).publish('2026-11-05',sections)
        self.assertIn('속초 · 새 댓글 0건',api.content)
        self.assertIn('7일 참고 · 댓글·답글 10건',api.content.split('상세·근거 댓글',1)[0])

    def test_readback_accepts_equivalent_quote_and_linebreak_encoding(self):
        api=FakeMcp();original=api.call
        def call(name,args):
            result=original(name,args)
            if name=='fetch':
                result['text']=result['text'].replace('<quote>\n','> ').replace('\n</quote>','').replace('first\nsecond','first<br />second')
            return result
        api.call=call
        result=NotionMcpClient(api,SOURCE).publish('2026-11-05',[block('quote','원문'),block('paragraph','first\nsecond')])
        self.assertTrue(result['readback_verified'])

    def test_readback_rejects_changed_comment_id_or_link(self):
        for before,after in [('댓글 ID 123','댓글 ID 456'),('https://www.instagram.com/p/a/','https://www.instagram.com/p/b/')]:
            with self.subTest(before=before):
                api=FakeMcp();original=api.call
                def call(name,args):
                    result=original(name,args)
                    if name=='fetch':result['text']=result['text'].replace(before,after)
                    return result
                api.call=call
                report=[block('paragraph','댓글 ID 123'),{'object':'block','type':'paragraph','paragraph':{'rich_text':[{'type':'text','text':{'content':'원문','link':{'url':'https://www.instagram.com/p/a/'}}}]}}]
                with self.assertRaisesRegex(PilotError,'다시 읽은 결과'):
                    NotionMcpClient(api,SOURCE).publish('2026-11-05',report)

    def test_successful_write_response_without_report_readback_is_pending(self):
        api=FakeMcp();original=api.call
        def call(name,args):
            if name=='update-page':return {'page_id':PAGE}
            return original(name,args)
        api.call=call
        with self.assertRaisesRegex(PilotError,'다시 읽은 결과'):
            NotionMcpClient(api,SOURCE).publish('2026-11-05',[block('paragraph','missing report')])

    def test_merge_appendix_headings_do_not_shadow_the_daily_sns_section(self):
        api=FakeMcp()
        appendix='\n\n<toggle>\n자동 생성 일일업무 병합 원본 · 2026-11-05 · source\n\n### SNS Comments\n\nKeep archived source report.\n\n```json\n{"example":"<toggle>"}\n```\n</toggle>'
        api.content += appendix
        NotionMcpClient(api,SOURCE).publish("2026-11-05",[block("paragraph","new active report")])
        self.assertIn("new active report",api.content)
        self.assertTrue(api.content.endswith(appendix))

    def test_timestamp_date_is_normalized_and_url_shaped_fetch_id_is_accepted(self):
        api=FakeMcp();api.date="2026-11-05T00:00:00+09:00"
        original=api.call
        def call(name,args):
            result=original(name,args)
            if name=="fetch" and not args["id"].startswith(("collection://","notion://")):
                result["id"]="https://app.notion.com/p/"+PAGE
            return result
        api.call=call
        self.assertEqual(NotionMcpClient(api,SOURCE).daily_page("2026-11-05")["id"],PAGE)
        query=next(args for name,args in api.calls if name=="query-data-sources")
        self.assertIn('substr("date:이벤트 시간:start",1,10)',query["data"]["query"])

    def test_empty_sql_rechecks_with_structured_date_filter(self):
        api=FakeMcp();original=api.call
        def call(name,args):
            if name=="query-data-sources" and args["data"]["mode"]=="sql":
                api.calls.append((name,copy.deepcopy(args)))
                return {"results":[],"has_more":False}
            return original(name,args)
        api.call=call
        self.assertEqual(NotionMcpClient(api,SOURCE).daily_page("2026-11-05")["id"],PAGE)
        self.assertEqual([args["data"]["mode"] for name,args in api.calls if name=="query-data-sources"],["sql","rows"])

    def test_missing_page_is_deferred_and_malformed_result_is_an_error(self):
        for result,error in [({"results":[]},NoDailyPage),({"text":"unexpected response"},PilotError)]:
            api=FakeMcp();original=api.call
            api.call=lambda name,args:result if name=="query-data-sources" else original(name,args)
            with self.subTest(result=result),self.assertRaises(error):
                NotionMcpClient(api,SOURCE).publish("2026-11-05",[])
            self.assertFalse(any(name=="update-page" for name,_ in api.calls))

    def test_weekend_report_goes_to_next_existing_page_and_is_idempotent(self):
        api=FakeMcp();api.date="2026-11-09"
        client=NotionMcpClient(api,SOURCE)
        first=client.publish("2026-11-07",[block("paragraph","Saturday evidence")],through_day="2026-11-09")
        second=client.publish("2026-11-07",[block("paragraph","Saturday evidence")],through_day="2026-11-09")
        self.assertEqual(first["day"],"2026-11-07")
        self.assertEqual(first["page_day"],"2026-11-09")
        self.assertTrue(second["unchanged"])
        self.assertIn('date start="2026-11-09"',api.content)
        self.assertIn("2026-11-07 08시",api.content)
        self.assertEqual(sum(name=="update-page" for name,_ in api.calls),1)

    def test_duplicate_daily_pages_are_never_chosen(self):
        api=FakeMcp();original=api.call
        api.call=lambda name,args:{"results":[{"url":"https://app.notion.com/"+PAGE}]*2} if name=="query-data-sources" else original(name,args)
        with self.assertRaisesRegex(PilotError,"여러 개"):
            NotionMcpClient(api,SOURCE).publish("2026-11-05",[])
        self.assertFalse(any(name=="update-page" for name,_ in api.calls))

    def test_same_day_duplicate_uses_only_the_auto_created_page(self):
        auto = "4190761a512d8189b670cd9d65d6ee6f"
        moved = "40e0761a512d8006a198d6becd86bc8d"
        pages = {
            auto: {"이름": '<date start="2026-11-16" />', "유형": "오늘 업무",
                   "date:이벤트 시간:start": "2026-11-16",
                   "date:생성 일시:start": "2026-11-15T16:05:10.000Z"},
            moved: {"이름": '<p><date start="2026-11-16" /> </p>', "유형": "오늘 업무",
                    "date:이벤트 시간:start": "2026-11-16",
                    "date:생성 일시:start": "2026-11-04T02:11:00.000Z"},
        }
        api = FakeMcp()
        original = api.call

        def call(name, args):
            if name == "query-data-sources":
                api.calls.append((name, copy.deepcopy(args)))
                return {"results": [{"url": "https://app.notion.com/p/" + moved},
                                     {"url": "https://app.notion.com/p/" + auto}], "has_more": False}
            if name == "fetch" and not args["id"].startswith(("collection://", "notion://")):
                api.calls.append((name, copy.deepcopy(args)))
                compact = args["id"].replace("-", "")
                page_id = auto if auto in compact else moved
                props = pages[page_id]
                return {"id": page_id, "url": "https://app.notion.com/p/" + page_id,
                        "text": "<properties>\n" + json.dumps(props, ensure_ascii=False) + "\n</properties>\n<content>\n</content>"}
            return original(name, args)

        api.call = call
        stream = io.StringIO()
        with mock.patch("sys.stdout", stream):
            page = NotionMcpClient(api, SOURCE).daily_page("2026-11-16")
        self.assertEqual(page["id"], auto)
        logged = json.loads(stream.getvalue())
        self.assertEqual(logged["event"], "notion_daily_page_duplicate_resolved")
        self.assertEqual(logged["severity"], "WARNING")
        self.assertEqual(set(logged["pages"]), {auto, moved})
        self.assertEqual(logged["chosen"], auto)
        query = next(args for name, args in api.calls if name == "query-data-sources")
        self.assertIn("LIMIT 10", query["data"]["query"])

    def test_two_auto_created_pages_for_one_day_are_still_rejected(self):
        first = "4190761a512d8189b670cd9d65d6ee6f"
        second = "4190761a512d8189b670cd9d65d6ee6e"
        props = {"이름": '<date start="2026-11-16" />', "유형": "오늘 업무",
                 "date:이벤트 시간:start": "2026-11-16",
                 "date:생성 일시:start": "2026-11-15T16:05:10.000Z"}
        api = FakeMcp()
        original = api.call

        def call(name, args):
            if name == "query-data-sources":
                return {"results": [{"url": "https://app.notion.com/p/" + first},
                                     {"url": "https://app.notion.com/p/" + second}]}
            if name == "fetch" and not args["id"].startswith(("collection://", "notion://")):
                page_id = second if second in args["id"].replace("-", "") else first
                return {"id": page_id, "text": "<properties>\n" + json.dumps(props) + "\n</properties>\n<content>\n</content>"}
            return original(name, args)

        api.call = call
        with self.assertRaisesRegex(PilotError, "여러 개"):
            NotionMcpClient(api, SOURCE).daily_page("2026-11-16")

    def test_earliest_day_in_a_range_is_kept_when_a_later_day_is_duplicated(self):
        friday = "4120761a512d817fae8ff53edda31b9e"
        api = FakeMcp()
        original = api.call

        def call(name, args):
            if name == "query-data-sources":
                return {"results": [
                    {"url": "https://app.notion.com/p/" + friday},
                    {"url": "https://app.notion.com/p/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
                    {"url": "https://app.notion.com/p/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},
                ]}
            if name == "fetch" and not args["id"].startswith(("collection://", "notion://")):
                compact = args["id"].replace("-", "")
                day = "2026-11-13" if friday in compact else "2026-11-14"
                props = {"유형": "오늘 업무", "date:이벤트 시간:start": day, "이름": "other"}
                page_id = friday if friday in compact else compact[-32:]
                return {"id": page_id, "text": "<properties>\n" + json.dumps(props) + "\n</properties>\n<content>\n</content>"}
            return original(name, args)

        api.call = call
        page = NotionMcpClient(api, SOURCE).daily_page("2026-11-13", through_day="2026-11-15")
        self.assertEqual(page["id"], friday)

    def test_three_account_reports_coexist_and_rerun_updates_only_selected_account(self):
        api = FakeMcp()
        names = ("waterbomb_official", "waterbomb_busan_official", "waterbomb_sokcho_official")
        clients = [NotionMcpClient(api, SOURCE, username=name) for name in names]
        for client, name in zip(clients, names):
            client.publish("2026-11-05", [block("paragraph", name + " original report")])
        clients[1].publish("2026-11-05", [block("paragraph", "updated Busan report")])
        self.assertEqual(api.content.count('<callout icon="📊"'), 3)
        self.assertIn("updated Busan report", api.content)
        self.assertNotIn("busan\\_official original report", api.content)
        self.assertIn("waterbomb\\_official original report", api.content)
        self.assertIn("waterbomb\\_sokcho\\_official original report", api.content)
        self.assertIn("Keep this note.", api.content)
        self.assertIn("Keep child</page>", api.content)

    def test_api_response_without_id_uses_verified_system_url(self):
        api=FakeMcp()
        self.assertEqual(NotionMcpClient(api,SOURCE).daily_page("2026-11-05")["id"],PAGE)

    def test_rerun_has_one_owned_report_and_preserves_human_notes_and_child_page(self):
        api=FakeMcp()
        client=NotionMcpClient(api,SOURCE)
        report=[block("bulleted_list_item","actual reaction"),block("toggle","detail",children=[block("quote","original comment")])]
        client.publish("2026-11-05",report)
        second=client.publish("2026-11-05",report)
        self.assertTrue(second["unchanged"])
        self.assertEqual(api.content.count('<callout icon="📊"'),1)
        self.assertIn("Keep this note.",api.content)
        self.assertIn("Keep child</page>",api.content)
        self.assertEqual(sum(name=="update-page" for name,_ in api.calls),1)

    def test_date_mismatch_never_writes(self):
        api=FakeMcp();api.date="2026-11-04"
        with self.assertRaises(PilotError):NotionMcpClient(api,SOURCE).publish("2026-11-05",[])
        self.assertFalse(any(name=="update-page" for name,_ in api.calls))

    def test_unknown_in_target_section_fails_but_unknown_elsewhere_is_allowed(self):
        api=FakeMcp();api.content=api.content.replace("- <b>SNS Comments:</b>",'<unknown url="x" />')
        with self.assertRaises(PilotError):NotionMcpClient(api,SOURCE).publish("2026-11-05",[])
        self.assertFalse(any(name=="update-page" for name,_ in api.calls))

    def test_comment_markup_is_literal_and_cannot_create_subpages_or_headings(self):
        text=markdown_blocks([block("quote",'<page url="https://evil.example">fake</page>\n### forged heading')])
        self.assertNotIn('<page url=',text)
        self.assertNotIn('\n### forged',text)
        self.assertIn('&lt;page',text)

    def test_owned_report_with_a_manual_subpage_is_not_replaced(self):
        api=FakeMcp();client=NotionMcpClient(api,SOURCE)
        client.publish("2026-11-05",[block("paragraph","old")])
        api.content=api.content.replace("</callout>",'<page url="https://www.notion.so/22222222222222222222222222222222">manual child</page>\n</callout>')
        with self.assertRaises(PilotError):client.publish("2026-11-05",[block("paragraph","new")])
        self.assertIn('manual child',api.content)

    def test_async_write_is_polled_to_completion(self):
        api=FakeMcp();api.async_update=True
        with mock.patch("sns_comment.notion_mcp.time.sleep"):
            result=NotionMcpClient(api,SOURCE).publish("2026-11-05",[block("paragraph","actual")])
        self.assertEqual(result["page_id"],PAGE)
        self.assertTrue(any(name=="get-async-task" for name,_ in api.calls))

    def test_warning_is_not_treated_as_success(self):
        api=FakeMcp();api.warnings=["not matched"]
        with self.assertRaises(PilotError):NotionMcpClient(api,SOURCE).publish("2026-11-05",[block("paragraph","actual")])


if __name__=="__main__":unittest.main()
