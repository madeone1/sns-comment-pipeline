# Historical operation. See scripts/legacy/README.md before use.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
"""Replay saved reports after a presentation-only readback correction."""

import datetime as dt
import json
import os

from comment_windows_release import UserTransport
from sns_comment.cloud_state import CloudState
from sns_comment.mcp import McpClient
from sns_comment.notion_mcp import NotionMcpClient
from sns_comment.notion_oauth import cloud_credentials
from sns_comment.notion import block
from sns_comment.profiles import select
from cloud_common import ROOT, PROJECT


DAY='2026-11-10'
os.umask(0o077)
settings=json.loads((ROOT/'.local/web-release/operational-config.json').read_text(encoding="utf-8"))
shared=CloudState(PROJECT+'-state',UserTransport())
recovered=[]
with shared.lease():
    auth=cloud_credentials(shared.transport,'projects/'+PROJECT+'/secrets/sns-notion-oauth')
    auth.refresh()
    mcp=McpClient(auth)
    for name in settings['cloud']['scheduled_accounts']:
        config=select(settings,name)
        state=CloudState(shared.bucket,shared.transport,prefix=config['_cloud_prefix'])
        docs,generation=state.json_get('state/reports.json',{})
        doc=docs.get(DAY,{})
        if not doc.get('sections'):
            continue
        client=NotionMcpClient(mcp,config['notion']['daily_data_source_id'],username=name,regional=True)
        sections=list(doc['sections'].get('daily',[]))
        extra=[]
        for key in ('weekly','monthly'):
            extra.extend(doc['sections'].get(key,[]))
        if extra:sections.append(block('toggle','주간·월간 보고',children=extra))
        result=client.publish(DAY,sections,through_day=DAY)
        # The unchanged branch compares a freshly fetched saved report with
        # the generated report. It is itself a successful readback.
        assert result.get('readback_verified') or result.get('unchanged')
        result['readback_verified']=True
        doc.update(status='published',result=result)
        doc.pop('last_error',None)
        state.json_put('state/reports.json',docs,generation)
        evidence={'status':'published','result':result,'recovered_at':dt.datetime.now(dt.timezone.utc).isoformat(),
            'reason':'notion_implicit_domain_autolink_equivalence','instagram_requests':0,'ai_requests':0}
        state.json_put(f'runs/{DAY}/report-recovery.json',evidence)
        recovered.append(name)
        print(json.dumps({'account':name,**evidence},ensure_ascii=False),flush=True)
    pipeline,generation=shared.json_get(f'pipeline/{DAY}/status.json')
    if pipeline:
        for row in pipeline['accounts']:
            if row['account'] not in recovered:continue
            config=select(settings,row['account'])
            state=CloudState(shared.bucket,shared.transport,prefix=config['_cloud_prefix'])
            source,_=state.json_get(f'runs/{DAY}/source.json')
            row.update(status='complete' if source['collection']['status']=='complete' else 'pending',
                report_status='published',pending_report_dates=[],counts=source['collection']['stored_in_window'])
            row.pop('error',None)
        pipeline['status']='failed' if any(r['status']=='failed' for r in pipeline['accounts']) else 'pending' if any(r['status']=='pending' for r in pipeline['accounts']) else 'complete'
        pipeline['notion_recovery']={'accounts':recovered,'verified_at':dt.datetime.now(dt.timezone.utc).isoformat()}
        shared.json_put(f'pipeline/{DAY}/status.json',pipeline,generation)
folder=ROOT/'.local/notion-autolink-release';folder.mkdir(parents=True,exist_ok=True)
(folder/'recovery.json').write_text(json.dumps({'recovered':recovered,'no_collection_or_ai':True},ensure_ascii=False,indent=2), encoding="utf-8")
