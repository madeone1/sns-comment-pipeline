"""Cloud candidate dry run: validate account paths and preserve Sheet/Notion data.

Run in an isolated Cloud Run job via scripts/token_connection_release.py verify.
Only private verification artifacts (and the OAuth refresh credential) are written;
production registry, Sheets and Notion content are not modified.
"""

import copy
import datetime as dt
import json
import os
import re

from sns_comment import token_connections as core,workflow
from sns_comment.cloud_state import CloudState
from sns_comment.connection_registry import REGISTRY_OBJECT,runtime_configs
from sns_comment.google_access import access_token
from sns_comment.mcp import McpClient
from sns_comment.notion_mcp import NotionMcpClient,body,normalized_markdown
from sns_comment.notion_oauth import cloud_credentials
from sns_comment.notion_sections import locate
from sns_comment.profiles import read_settings

class MemoryMcp:
    def __init__(self,page,access,spec):self.page=copy.deepcopy(page);self.access=access;self.spec=spec
    def call(self,name,args):
        if name=='get-tool-access':return self.access
        if name=='fetch':return self.spec if args['id'].startswith('notion://') else copy.deepcopy(self.page)
        if name=='update-page':
            for change in args['content_updates']:
                assert self.page['text'].count(change['old_str'])==1
                self.page['text']=self.page['text'].replace(change['old_str'],change['new_str'],1)
            return {'page_id':self.page['id']}
        raise AssertionError(name)


def main():
    DAY=os.environ['SNS_VERIFY_DAY'];PAGE=os.environ['SNS_VERIFY_PAGE']
    dt.date.fromisoformat(DAY)
    if not re.fullmatch(r'[a-fA-F0-9-]{32,36}',PAGE):raise ValueError('Invalid verification page ID')
    shared=CloudState(os.environ['SNS_STATE_BUCKET']);settings=read_settings()
    stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ');proof='verification/token-connections/'+stamp


    with shared.lease():
        before,generation=shared.json_get(REGISTRY_OBJECT)
        registry,plan=core.reconcile(settings,shared,apply=False)
        configs=runtime_configs(settings,registry)
        shared.json_put(proof+'/registry-before.json',before,generation=0)
        shared.json_put(proof+'/candidate.json',{'registry':registry,'verification':plan},generation=0)
        assert 'SNS_IG_ACCESS_TOKEN' not in os.environ
        assert {a['id'] for a in before['accounts'].values() if a['active']}=={c['account_id'] for c in configs}
        for config in configs:
            assert config['_cloud_prefix']==before['accounts'][config['account_id']]['source_prefix']
            assert config['_credential_secret_version']==registry['token_control']['source_version']
        projections=[];sources=[];sections=[]
        for config in configs:
            state=CloudState(shared.bucket,shared.transport,prefix=config['_cloud_prefix'])
            source,_=state.json_get('runs/'+DAY+'/source.json')
            analysis,_=state.json_get('runs/'+DAY+'/daily-analysis.json')
            documents,_=state.json_get('state/reports.json')
            projections.append(workflow.make_account(config,source,analysis['annotations'],{'connection':'현재 토큰 검증 완료'}))
            sources.append({'id':config['account_id'],'username':config['expected_username'],'label':config['region_label'],
                'counts':next(v['counts'] for v in source['views'] if v['period']['key']=='daily')})
            sections.append(documents[DAY]['sections']['daily'])
        projection=workflow.merge(projections);target=settings['workflow']['google_sheets']
        sheets=workflow.Client(access_token(target,shared.transport),target['spreadsheet_id'],headers=projection['headers'])
        metadata,rows=sheets.snapshot();shared.json_put(proof+'/sheets-before.json',{'metadata':metadata,'tabs':rows},generation=0)
        sheet_plan=workflow.sync(projection,sheets,dry_run=True,account_roster=configs[0]['_sheet_roster'])
        auth=cloud_credentials(shared.transport,os.environ['SNS_NOTION_OAUTH_SECRET']);auth.refresh();mcp=McpClient(auth)
        notion=NotionMcpClient(mcp,settings['notion']['daily_data_source_id'],regional=True)
        page=notion.fetch(PAGE);page['id']=PAGE;old=body(page);old_area=locate(old)
        shared.json_put(proof+'/notion-before.json',page,generation=0)
        memory=MemoryMcp(page,mcp.call('get-tool-access',{}),mcp.call('fetch',{'id':'notion://docs/notion-flavored-markdown-spec'}))
        for config,content in zip(configs,sections):
            writer=NotionMcpClient(memory,settings['notion']['daily_data_source_id'],username=config['expected_username'],regional=True,
                region_label=config['region_label'],account_roster=config['_report_roster'],connection_revision=config['_connection_revision'])
            writer.daily_page=lambda day,through_day=None:{**copy.deepcopy(memory.page),'_daily_day':DAY}
            writer.publish(DAY,content)
        final=body(memory.page);area=locate(final)
        assert area['kind']=='heading'
        ids=lambda value:set(re.findall(r'댓글 ID\s+(\d+)',normalized_markdown(value)))
        assert ids(old)<=ids(final)
        assert normalized_markdown(old[:old_area['start']])==normalized_markdown(final[:area['start']])
        assert normalized_markdown(old[old_area['end']:])==normalized_markdown(final[area['end']:])
        current=core.read_report_state(memory,PAGE,DAY,configs,configs[0]['_connection_revision'])
        for source in sources:assert current[source['username']]['count']==source['counts']['comments_and_replies']
        shared.json_put(proof+'/notion-simulation.json',memory.page,generation=0)
        unchanged,unchanged_generation=shared.json_get(REGISTRY_OBJECT)
        assert unchanged==before and unchanged_generation==generation
        result={'status':'verified','image':os.environ['SNS_RELEASE_IMAGE'],'proof':proof,'accounts':sources,
            'token_read_from_secret':True,'raw_token_environment_present':False,'fixed_version_verified':True,
            'same_account_paths_preserved':True,'sheets_dry_run':sheet_plan,'notion_active_roster_verified':True,
            'evidence_ids_preserved':len(ids(old)),'notion_writes':0,'sheets_writes':0,'registry_writes':0}
        shared.json_put(proof+'/candidate-result.json',result,generation=0)
        shared.json_put('verification/token-connections/candidates/'+os.environ['SNS_RELEASE_IMAGE'].rsplit(':',1)[-1]+'.json',result,generation=0)
        print(json.dumps(result,ensure_ascii=False),flush=True)


if __name__ == "__main__":
    main()
