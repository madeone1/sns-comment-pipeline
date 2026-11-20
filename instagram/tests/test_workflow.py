import copy
import io
import json
import sqlite3
import unittest
from unittest import mock
import zipfile

from sns_comment import screening, workflow
from sns_comment.api import PilotError
from sns_comment.drive_archive import bundle, Client as DriveClient
from sns_comment.google_access import access_token
from sns_comment.notion_mcp import NotionMcpClient
from sns_comment.reporting import Analyzer, counts


def row(key,text='❤️',owner=0,author='a'):
    return {'id':str(key),'text':text,'is_owner':owner,'author_id':author,'media_id':'m','parent_id':None,
            'written_at':'2026-11-08T12:00:00+09:00','payload_hash':str(key), 'caption':'영상','permalink':'https://www.instagram.com/p/example/'}


def source(rows):
    period={'key':'daily','label':'일일','start':'2026-11-08T08:00:00+09:00','end':'2026-11-09T08:00:00+09:00','target_day':'2026-11-09'}
    view={'period':period,'comments':rows,'counts':counts(rows),'media':[{'id':'m'}]}
    return {'views':[view],'recent_seven_days':{**view,'period':{**period,'key':'recent_seven_days'}},'collection':{'status':'complete','run_id':'run'}}


class WorkflowTests(unittest.TestCase):
    config={'account_id':'100','expected_username':'waterbomb_official','workflow':{'review_sample':20}}

    def test_1200_reactions_preserve_counts_without_1200_tasks_or_ai_calls(self):
        rows=[row(i,author='a'+str(i%30)) for i in range(1200)]
        analyzer=Analyzer(None,'project')
        with sqlite3.connect(':memory:') as db,mock.patch.object(analyzer,'generate') as generate:
            annotations=analyzer.classify(db,rows,{'text':'규정','workflow_enabled':True})
            generate.assert_not_called()
            selection=screening.persist(db,'100',rows)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM comment_screenings').fetchone()[0],1200)
            blank=analyzer.classify(db,[row('blank',' ')],{'text':'규정','workflow_enabled':True})
            self.assertEqual(blank['blank']['processing_group'],'hold');generate.assert_not_called()
        result=workflow.make_account(self.config,source(rows),annotations)
        self.assertEqual(selection['counts']['simple'],1200)
        self.assertEqual(selection['reaction_groups'][0]['known_authors'],30)
        self.assertEqual(len(result['tabs']['분류와 처리'])-1,20)
        self.assertEqual(len(result['tabs']['댓글 원본'])-1,20)
        self.assertEqual(len(annotations),1200)

    def test_unknown_mixed_text_missing_and_official_are_separate(self):
        rows=[row(1,'환불은요? ❤️'),row(2,None),row(3,'☠️'),row(4,'❤️',owner=1)]
        self.assertEqual([screening.classify(r)['group'] for r in rows],['analysis','hold','analysis','official'])
        with self.assertRaises(ValueError):screening.summarize([rows[0],rows[0]])

    def test_no_model_call_and_no_empty_filler_for_simple_reactions(self):
        rows=[row(1),row(2,None)];view=source(rows)['views'][0];analyzer=Analyzer(None,'project')
        with mock.patch.object(analyzer,'generate') as generate:
            result=analyzer.report(view,view,{}, {'text':'규정','workflow_enabled':True})
            generate.assert_not_called()
        self.assertEqual(len(result['lines']),2)
        self.assertIn('판단 보류',result['lines'][1])

    def test_style_edits_do_not_reclassify_content(self):
        analyzer=Analyzer(None,'p');rows=[row(1,'환불 문의')]
        annotation={'id':'1','category':'티켓과 결제','intent':'문의','sentiment':'중립','tags':[],'reason':'질문'}
        rules={'text':'first','classification_text':'same','workflow_enabled':True}
        with sqlite3.connect(':memory:') as db,mock.patch.object(analyzer,'generate',return_value=[annotation]) as generate:
            analyzer.classify(db,rows,rules);analyzer.classify(db,rows,{**rules,'text':'new writing style'})
            self.assertEqual(generate.call_count,1)

    def test_plan_preserves_human_cells_and_other_account(self):
        data=workflow.make_account(self.config,source([row(1,'환불 문의')]),{})
        name='분류와 처리';header=workflow.HEADERS[name]
        old=copy.deepcopy(data['tabs']);old[name][1][header.index('담당')]='실제 담당';old[name][1][header.index('답변 내용')]='사람 답변'
        old[name].append(workflow.values(name,{'연결 키':'200:other','목록 상태':'검토 대상','담당':'다른 계정'}))
        meta={'sheets':[{'properties':{'title':n,'sheetId':i,'gridProperties':{'rowCount':1000}}} for i,n in enumerate(data['tabs'])]}
        plan=workflow.plan(data,meta,old)
        # Only human edits differ, so the machine must leave them alone.
        self.assertEqual(plan['requests'],[])
        data2=workflow.make_account(self.config,source([]),{})
        plan=workflow.plan(data2,meta,old)
        edits=[r['updateCells'] for r in plan['requests'] if 'updateCells' in r and r['updateCells']['range']['sheetId']==1]
        self.assertTrue(any(e['range']['startRowIndex']==1 for e in edits))
        self.assertFalse(any(e['range']['startRowIndex']==2 for e in edits))

    def test_hash_ignores_retry_timestamps_but_tracks_payload(self):
        a=source([row(1)]);b=copy.deepcopy(a);b['collection']['run_id']='retry';b['collection']['elapsed']=99
        self.assertEqual(workflow.make_account(self.config,a,{})['source_hash'],workflow.make_account(self.config,b,{})['source_hash'])
        b['views'][0]['comments'][0]['payload_hash']='edited'
        self.assertNotEqual(workflow.make_account(self.config,a,{})['source_hash'],workflow.make_account(self.config,b,{})['source_hash'])

    def test_ambiguous_sheet_response_recovers_without_duplicate_rows(self):
        data=workflow.make_account(self.config,source([row(1,'환불 문의')]),{})
        class Client:
            def __init__(self):
                self.rows={name:[header[:]] for name,header in data['headers'].items()};self.lose_response=True
                self.meta={'sheets':[{'properties':{'title':name,'sheetId':i,'gridProperties':{'rowCount':1000}}} for i,name in enumerate(data['tabs'])]}
            def snapshot(self):return copy.deepcopy(self.meta),copy.deepcopy(self.rows)
            def request(self,suffix,body):
                names=list(data['tabs'])
                for request in body['requests']:
                    if 'createDeveloperMetadata' in request:
                        item=request['createDeveloperMetadata']['developerMetadata']
                        self.meta['sheets'][item['location']['sheetId']].setdefault('developerMetadata',[]).append(copy.deepcopy(item))
                    if 'updateCells' not in request:continue
                    update=request['updateCells'];area=update['range'];table=self.rows[names[area['sheetId']]]
                    while len(table)<area['endRowIndex']:table.append(['']*len(table[0]))
                    for j,cell in enumerate(update['rows'][0]['values'],area['startColumnIndex']):
                        table[area['startRowIndex']][j]=next(iter(cell.get('userEnteredValue',{}).values()),'')
                if self.lose_response:
                    self.lose_response=False;raise PilotError('응답 유실')
        client=Client()
        with self.assertRaisesRegex(PilotError,'유실'):workflow.sync(data,client)
        result=workflow.sync(data,client)
        self.assertEqual(result['status'],'complete')
        self.assertEqual(len(client.rows['분류와 처리']),len(data['tabs']['분류와 처리']))
        self.assertEqual(workflow.sync(data,client)['batches'],0)

    def test_regional_originals_include_reactions_replies_missing_and_official(self):
        comments=[row(1),row(2,None),row(3,'공식 답변',owner=1),dict(row(4,'답글'),parent_id='1')]
        result=workflow.make_account(self.config,source(comments),{})
        rows=result['tabs']['대표(공식)'];header=rows[0]
        self.assertEqual(len(rows)-1,4)
        self.assertEqual({r[0] for r in rows[1:]},{'100:'+str(i) for i in range(1,5)})
        self.assertEqual(next(r for r in rows[1:] if r[0]=='100:2')[header.index('본문 제공 여부')],'미제공')
        self.assertEqual(next(r for r in rows[1:] if r[0]=='100:4')[header.index('상위 댓글 ID')],'1')

    def test_regions_stay_separate_and_preserve_manual_review_on_refresh(self):
        first=workflow.make_account(self.config,source([row(1)]),{})
        other_config={**self.config,'account_id':'200','expected_username':'waterbomb_busan_official'}
        second=workflow.make_account(other_config,source([row(1)]),{})
        combined=workflow.merge([first,second]);old=copy.deepcopy(combined['tabs'])
        header=workflow.REGIONAL_HEADERS
        old['대표(공식)'][1][header.index('답변 내용')]='직접 작성한 답변'
        old['대표(공식)'][1][header.index('검토 여부')]='검토 완료'
        old['대표(공식)'][1][header.index('작성 계정')]='saved_author'
        meta={'sheets':[{'properties':{'title':n,'sheetId':i,'gridProperties':{'rowCount':1000}}} for i,n in enumerate(old)]}
        self.assertEqual(workflow.plan(combined,meta,old)['requests'],[])
        updated=workflow.make_account(self.config,source([]),{})
        plan=workflow.plan(updated,meta,old)
        regional_id=list(old).index('대표(공식)');other_id=list(old).index('부산')
        edits=[r['updateCells'] for r in plan['requests'] if 'updateCells' in r]
        self.assertTrue(any(e['range']['sheetId']==regional_id and e['range']['startColumnIndex']==header.index('조회 상태') for e in edits))
        self.assertFalse(any(e['range']['sheetId']==other_id for e in edits))
        bad=copy.deepcopy(combined);bad['tabs']['부산'][1][0]='100:wrong'
        with self.assertRaisesRegex(PilotError,'다른 계정'):workflow.plan(bad,meta,old)

    def test_regional_history_marks_old_rows_and_rejects_duplicates(self):
        rows=workflow.regional_rows(self.config,[row(1),row(2)],{},current_ids={'2'})
        statuses={r[0]:r[workflow.REGIONAL_HEADERS.index('조회 상태')] for r in rows[1:]}
        self.assertEqual(statuses,{'100:2':'현재 조회','100:1':'이전 자료'})
        with self.assertRaisesRegex(PilotError,'중복'):workflow.regional_rows(self.config,[row(1),row(1)],{})

    def test_rule_reader_excludes_examples_and_history(self):
        client=object.__new__(NotionMcpClient)
        child='## 사람이 수정하는 항목별 작성 지침\n### 데이터 선별·축적\n선별\n### 전체 요약\n간결하게\n<toggle>\n운영 기준·파이프라인 기본 지침\n## 7. 보관\n원본 유지\n</toggle>\n<toggle>\n상세 작성법\n과거 숫자 999\n통합본 v1.0.7 / 날짜\n</toggle>'
        with mock.patch.object(client,'rules',side_effect=[{'text':'parent','version':'v1.1'}, {'text':child,'version':'v1.0.7'}]):
            rules=client.workflow_rules('parent','child')
        self.assertIn('간결하게',rules['report_text']);self.assertNotIn('간결하게',rules['classification_text'])
        self.assertNotIn('999',rules['text'])
        self.assertEqual(rules['version'],'v1.0.7')

    def test_archive_is_deterministic_and_recoverable(self):
        a=bundle({'source.json':{'a':1},'comments.sqlite3':b'db'})
        self.assertEqual(a,bundle({'comments.sqlite3':b'db','source.json':{'a':1}}))
        with zipfile.ZipFile(io.BytesIO(a)) as archive:
            self.assertEqual(archive.read('comments.sqlite3'),b'db')
            self.assertIn('sha256',json.loads(archive.read('manifest.json'))['source.json'])

    def test_missing_google_auth_does_not_use_browser_or_cloud_token(self):
        with mock.patch.dict('os.environ',{},clear=True):
            with self.assertRaisesRegex(PilotError,'별도'):access_token({})

    def test_sheet_service_identity_uses_only_sheets_scope_without_private_keys(self):
        transport=mock.Mock();transport.json.return_value={'accessToken':'synthetic-scoped-token'}
        identity='sns-pipeline@waterbomb-sns-pipeline.iam.gserviceaccount.com'
        result=access_token({'auth_mode':'service_account','service_account':identity},transport)
        self.assertEqual(result,'synthetic-scoped-token')
        method,url,body=transport.json.call_args.args
        self.assertEqual(method,'POST')
        self.assertTrue(url.endswith(identity+':generateAccessToken'))
        self.assertEqual(body['scope'],['https://www.googleapis.com/auth/spreadsheets'])
        self.assertEqual(body['lifetime'],'3600s')
        with self.assertRaisesRegex(PilotError,'서비스 계정'):
            access_token({'auth_mode':'service_account','service_account':identity})

    def test_archive_retry_reconciles_existing_object_without_upload(self):
        import hashlib
        client=DriveClient('synthetic','folder_123456')
        raw=b'archive';existing={'files':[{'id':'file','size':str(len(raw)),'md5Checksum':hashlib.md5(raw).hexdigest()}]}
        with mock.patch.object(client,'request',return_value=existing) as request:
            result=client.archive('account/day',raw)
        self.assertEqual(request.call_count,1);self.assertEqual(result['status'],'complete')


if __name__=='__main__':unittest.main()
