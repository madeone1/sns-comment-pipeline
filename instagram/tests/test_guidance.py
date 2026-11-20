import copy
import unittest
from unittest import mock

from sns_comment import guidance, cloud
from sns_comment.api import PilotError
from sns_comment.reporting import Analyzer
from test_workflow import row, source


RULES={'page_id':'guide','version':'v2.0','last_edited_at':'2026-11-11T01:00:00Z',
       'report_text':'필요한 행동이 없으면 생략해주세요. 요약은 짧게 작성해주세요.',
       'text':'필요한 행동이 없으면 생략해주세요. 요약은 짧게 작성해주세요.',
       'workflow_enabled':True,'enforce_guidelines':True}


def verdict(passed=True):
    clause='필요한 행동이 없으면 생략해주세요.'
    return {'compliant':passed,'checks':[{'instruction':clause,'passed':passed,'reason':'실제 행동 필요 여부 대조'}],
        'violations':[] if passed else [{'instruction':clause,'reason':'필요 없는 모니터링 문구','fix':'행동과 해당 요약 줄 생략'}]}


class GuidanceTests(unittest.TestCase):
    def test_new_region_mapping_is_used_by_both_report_writer_and_reviewer(self):
        context={'account_id':'10004','username':'new_d','region':'대구'}
        analyzer=Analyzer(None,'p');view=source([row(1,'영상 좋아요')])['views'][0]
        analyzer.generate=mock.Mock(side_effect=[{'lines':['영상 호감 반응 1건'],'insights':[],'actions':[],'rule_improvements':[]},verdict()])
        result=analyzer.report(view,view,{}, {**RULES,'account_context':context})
        self.assertTrue(result['guideline_review']['compliant'])
        self.assertEqual(analyzer.generate.call_args_list[0].args[1]['account_context'],context)
        self.assertEqual(analyzer.generate.call_args_list[1].args[1]['evidence']['account_context'],context)
    def test_pending_report_is_regenerated_from_its_own_saved_source(self):
        saved=source([row(1,'환불 문의')]);target=saved['views'][0]['period']['target_day']
        state=mock.Mock();state.json_get.return_value=(saved,7)
        document={'sections':{'daily':['stale']}}
        analyzer=mock.Mock();analyzer.classify.return_value={'1':{}}
        report={'guideline_review':verdict()}
        with mock.patch.object(guidance,'generate_current',return_value=(report,RULES)) as generate, \
                mock.patch.object(cloud,'render',return_value=['fresh']):
            cloud.refresh_document({'expected_username':'account'},state,document,target,analyzer,
                mock.Mock(),{},lambda:RULES,lambda:None)
        state.json_get.assert_called_once_with('runs/'+target+'/source.json')
        self.assertEqual(generate.call_args.args[1],saved['views'][0])
        self.assertEqual(document['sections']['daily'][-1],'fresh')
        self.assertEqual(document['guidance']['daily']['rules_identity'],guidance.identity(RULES))

    def test_missing_saved_source_cannot_publish_stale_pending_report(self):
        state=mock.Mock();state.json_get.return_value=(None,0)
        document={'sections':{'daily':['stale']}}
        with self.assertRaisesRegex(PilotError,'저장 원본'):
            cloud.refresh_document({},state,document,'2026-11-09',mock.Mock(),mock.Mock(),{},lambda:RULES,lambda:None)
        self.assertEqual(document['sections']['daily'],['stale'])

    def test_failed_publication_review_rewrites_a_cached_draft_with_its_feedback(self):
        saved=source([row(1,'귀여워요')]);target=saved['views'][0]['period']['target_day']
        state=mock.Mock();state.json_get.return_value=(saved,1)
        from sns_comment.reporting import REPORT_TEMPLATE_VERSION
        failed={**verdict(False),'rules_identity':guidance.identity(RULES)}
        document={'sections':{'daily':['cached']},'publication_review':failed,
            'guidance':{'daily':{'rules_identity':guidance.identity(RULES),'template_version':REPORT_TEMPLATE_VERSION}}}
        analyzer=mock.Mock();store=mock.Mock();captured=[]
        def generate(analyzer,view,context,annotations,loader):
            current=loader();captured.extend(current['_guideline_feedback'])
            return {'guideline_review':verdict()},current
        with mock.patch.object(guidance,'generate_current',side_effect=generate),mock.patch.object(cloud,'render',return_value=['repaired']):
            cloud.refresh_document({'expected_username':'a'},state,document,target,analyzer,store,{},lambda:RULES,lambda:None)
        self.assertEqual(captured,failed['violations']);self.assertEqual(document['sections']['daily'][-1],'repaired')

    def test_final_publication_feedback_reaches_the_first_regenerated_draft(self):
        analyzer=Analyzer(None,'p');view=source([row(1,'귀여워요')])['views'][0]
        analyzer._report=mock.Mock(return_value={'lines':['긍정 반응'],'insights':[],'actions':[]})
        analyzer.generate=mock.Mock(return_value=verdict())
        feedback=verdict(False)['violations']
        analyzer.report(view,view,{}, {**RULES,'_guideline_feedback':feedback})
        self.assertEqual(analyzer._report.call_args.args[3]['_guideline_feedback'],feedback)

    def test_review_uses_current_instructions_and_records_exact_identity(self):
        analyzer=Analyzer(None,'p');analyzer.generate=mock.Mock(return_value=verdict())
        result=guidance.review(analyzer,RULES,{'lines':['긍정 반응'],'actions':[]})
        payload=analyzer.generate.call_args.args[1]
        self.assertEqual(payload['latest_instructions'],RULES['report_text'])
        self.assertEqual(result['rules_identity'],guidance.identity(RULES))
        self.assertEqual(result['last_edited_at'],RULES['last_edited_at'])

    def test_empty_or_simple_only_reports_still_use_current_writing_instructions(self):
        for rows in [[],[row(1)]]:
            analyzer=Analyzer(None,'p');view=source(rows)['views'][0]
            analyzer.generate=mock.Mock(side_effect=[
                {'lines':['새 댓글 없음' if not rows else '단순 반응 1건'],'insights':[],'actions':[],'rule_improvements':[]},verdict()])
            result=analyzer.report(view,view,{},RULES)
            self.assertTrue(result['guideline_review']['compliant'])
            self.assertEqual(analyzer.generate.call_count,2)
            self.assertEqual(analyzer.generate.call_args_list[0].args[1]['rules'],RULES['report_text'])

    def test_a_changed_body_is_detected_even_without_a_version_bump(self):
        changed={**RULES,'report_text':RULES['report_text']+' 환불 문의를 먼저 설명해주세요.'}
        self.assertNotEqual(guidance.identity(RULES),guidance.identity(changed))

    def test_review_cannot_claim_pass_with_failed_checks_or_invented_instructions(self):
        bad=verdict(False);bad['compliant']=True
        invented=verdict();invented['checks'][0]['instruction']='없는 지침'
        for result in [bad,invented,{'compliant':True,'checks':[],'violations':[]}]:
            analyzer=Analyzer(None,'p');analyzer.generate=mock.Mock(return_value=result)
            with self.subTest(result=result),self.assertRaises(PilotError):
                guidance.review(analyzer,RULES,{'lines':['초안']})

    def test_violation_regenerates_the_draft_with_feedback(self):
        analyzer=Analyzer(None,'p');view=source([row(1,'귀여워요')])['views'][0]
        bad={'lines':['긍정 반응','계속 모니터링 필요'],'insights':[],'actions':[{'text':'모니터링','comment_ids':['1']}]}
        good={'lines':['긍정 반응'],'insights':[],'actions':[]}
        analyzer._report=mock.Mock(side_effect=[bad,good]);analyzer.generate=mock.Mock(side_effect=[verdict(False),verdict()])
        result=analyzer.report(view,view,{},RULES)
        self.assertEqual(result['actions'],[])
        self.assertTrue(result['guideline_review']['compliant'])
        self.assertTrue(analyzer._report.call_args_list[1].args[3]['_guideline_feedback'])

    def test_failed_review_never_returns_an_unchecked_counts_fallback(self):
        analyzer=Analyzer(None,'p');view=source([row(1)])['views'][0]
        analyzer._report=mock.Mock(return_value={'lines':['유지 필요'],'insights':[],'actions':[]})
        analyzer.generate=mock.Mock(return_value=verdict(False))
        with self.assertRaisesRegex(PilotError,'기존 보고'):
            analyzer.report(view,view,{},RULES)
        self.assertEqual(analyzer._report.call_count,3)

    def test_change_during_generation_regenerates_with_the_new_document(self):
        later={**RULES,'last_edited_at':'2026-11-11T02:00:00Z','report_text':'새 지침'}
        latest=mock.Mock(side_effect=[RULES,later,later,later]);analyzer=mock.Mock()
        report,rules=guidance.generate_current(analyzer,{}, {}, {},latest)
        self.assertEqual(analyzer.report.call_count,2)
        self.assertEqual(rules['report_text'],'새 지침')

    def test_changed_instructions_before_or_during_publication_block_the_write(self):
        later={**RULES,'report_text':'수정한 지침'}
        analyzer=Analyzer(None,'p');analyzer.generate=mock.Mock(return_value=verdict())
        for sequence in [[later],[RULES,later]]:
            validate=guidance.publication_validator(analyzer,RULES,mock.Mock(side_effect=sequence))
            with self.subTest(sequence=sequence),self.assertRaisesRegex(PilotError,'변경'):
                validate('### SNS Comments\n본문',{'account':'a'})

    def test_final_layout_review_is_mandatory(self):
        analyzer=Analyzer(None,'p');analyzer.generate=mock.Mock(return_value=verdict(False))
        checked=[]
        validate=guidance.publication_validator(analyzer,RULES,lambda:RULES,checked.append)
        with self.assertRaisesRegex(PilotError,'최종 출력'):
            validate('### SNS Comments\n본문',{'account':'a'})
        self.assertEqual(checked[0]['scope'],'publication')

    def test_combined_summary_uses_current_instructions_and_preserves_regions_and_warning(self):
        analyzer=Analyzer(None,'p');analyzer.generate=mock.Mock(return_value={'lines':['장소 문의 확인 필요']})
        validate=guidance.publication_validator(analyzer,RULES,lambda:RULES)
        regions='<toggle>\n대표(공식) · 새 댓글 1건\n\n근거 보존\n</toggle>\n</callout>'
        report='<callout>\n보고 제목\n\n- 예전 고정 요약\n\n수집 범위 확인 중: 잠정.\n\n'+regions
        output=validate.prepare(report,{'account':'a'})
        self.assertIn('- 장소 문의 확인 필요',output);self.assertNotIn('예전 고정 요약',output)
        self.assertIn('수집 범위 확인 중: 잠정.',output);self.assertTrue(output.endswith(regions))
        self.assertEqual(analyzer.generate.call_args.args[1]['latest_instructions'],RULES['report_text'])


if __name__=='__main__':unittest.main()
