# Historical operation. See scripts/legacy/README.md before use.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
"""Small read-only API sample, separating post age from comment timestamps."""

import datetime as dt
import json
import os

from cloud_common import ROOT
from webhook_connect import secret, graph


os.umask(0o077)
folder=ROOT/'.local/post-window-release/regional-check'
folder.mkdir(parents=True,exist_ok=True)
settings=json.loads((ROOT/'.local/web-release/operational-config.json').read_text(encoding="utf-8"))
token=secret('sns-meta-token')
end=dt.datetime.fromisoformat('2026-11-10T08:00:00+09:00')
for name in ('waterbomb_busan_official','waterbomb_sokcho_official'):
    account=settings['accounts'][name]['account_id']
    identity=graph(account,token,{'fields':'id,username,media_count'})
    assert identity['username']==name and identity['id']==account
    catalog=graph(account+'/media',token,{'fields':'id,permalink,timestamp,comments_count','limit':20})
    previous=folder/(name+'.json')
    cached={x['post']['id']:x for x in json.loads(previous.read_text(encoding="utf-8"))['posts']} if previous.exists() else {}
    evidence={'identity':identity,'checked_at':dt.datetime.now(dt.timezone.utc).isoformat(),'posts':[]}
    for post in catalog.get('data',[]):
        if post['id'] in cached:
            evidence['posts'].append(cached[post['id']])
            continue
        comments=graph(post['id']+'/comments',token,{'fields':'id,text,timestamp','limit':100})
        rows=comments.get('data',[])
        times=[dt.datetime.fromisoformat(r['timestamp'].replace('Z','+00:00')) for r in rows]
        result={'post_id':post['id'],'post_written_at':post['timestamp'],'permalink':post['permalink'],
            'total_comments_api':post.get('comments_count'),'sampled_top_level':len(rows),
            'more_pages':bool(comments.get('paging',{}).get('next')),
            'earliest_sample':min(times).isoformat() if times else None,'latest_sample':max(times).isoformat() if times else None,
            'daily_sample':sum(end-dt.timedelta(days=1)<=t<end for t in times),
            'week_sample':sum(end-dt.timedelta(days=7)<=t<end for t in times),
            'after_cutoff_sample':sum(t>=end for t in times)}
        evidence['posts'].append({'post':post,'comments':rows,'summary':result})
        if result['week_sample'] or result['after_cutoff_sample']:
            print(json.dumps({'account':name,**result},ensure_ascii=False),flush=True)
    (folder/(name+'.json')).write_text(json.dumps(evidence,ensure_ascii=False,indent=2), encoding="utf-8")
    print(json.dumps({'account':name,'sampled_posts':len(evidence['posts']),
        'daily_at_least':sum(x['summary']['daily_sample'] for x in evidence['posts']),
        'week_at_least':sum(x['summary']['week_sample'] for x in evidence['posts'])},ensure_ascii=False),flush=True)
