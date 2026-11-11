# Historical release tooling. See scripts/legacy/README.md before use.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
"""Narrow the post window while preserving collected evidence and reviews."""

import argparse
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from comment_windows_release import UserTransport
import notion_regional_release as release
from sns_comment.account import collection_window, previous_month_start
from sns_comment.cloud_state import CloudState
from sns_comment.comment_windows import projections
from sns_comment.profiles import select
from sns_comment.storage import timestamp
from cloud_common import PROJECT, ROOT


HERE=ROOT/'.local/post-window-release'
DAY='2026-11-10'


def reproject(apply=False):
    os.umask(0o077)
    HERE.mkdir(parents=True,exist_ok=True)
    settings=json.loads((ROOT/'.local/web-release/operational-config.json').read_text())
    instant=dt.datetime.fromisoformat(DAY+'T08:00:00+09:00')
    start,end=collection_window(instant=instant)
    floor=previous_month_start(end)
    transport=UserTransport()
    shared=CloudState(PROJECT+'-state',transport)
    results=[]
    with (shared.lease() if apply else contextlib.nullcontext()):
        for name in settings['cloud']['scheduled_accounts']:
            config=select(settings,name)
            state=CloudState(shared.bucket,transport,prefix=config['_cloud_prefix'])
            old,generation=state.json_get(f'runs/{DAY}/source.json')
            raw,db_generation=state.get('state/comments.sqlite3')
            analysis,_=state.json_get(f'runs/{DAY}/daily-analysis.json')
            with tempfile.TemporaryDirectory(prefix='sns-post-window-') as temp:
                path=Path(temp)/'snapshot.sqlite3';path.write_bytes(raw)
                with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
                    db.row_factory=sqlite3.Row
                    db.execute('PRAGMA query_only=ON')
                    targets=[r['id'] for r in db.execute('SELECT id,written_at FROM media WHERE account_id=?',(config['account_id'],))
                        if floor<=timestamp(r['written_at'])<end]
                    catalog=db.execute('''SELECT id FROM collection_scopes WHERE account_id=? AND catalog_complete=1
                        AND media_policy='all_accessible' AND window_end>=? ORDER BY started_at DESC LIMIT 1''',
                        (config['account_id'],end)).fetchone()
                    missing=[]
                    for media in targets:
                        covered=db.execute('''SELECT 1 FROM scope_media sm JOIN collection_scopes s ON s.id=sm.scope_id
                            WHERE sm.account_id=? AND sm.media_id=? AND sm.status='complete' AND s.window_start<=?
                            AND s.window_end>=? LIMIT 1''',(config['account_id'],media,start,end)).fetchone()
                        if not covered:missing.append(media)
                    complete=bool(catalog) and not missing
                    views,week=projections(db,config['account_id'],instant,media_since=floor)
            proof={'status':'complete' if complete else 'pending','basis':'existing_completed_comment_edges',
                'catalog_scope_id':catalog['id'] if catalog else None,'media_discovered':len(targets),
                'media_completed':len(targets)-len(missing),'media_pending':len(missing),'missing_media_ids':missing,
                'database_generation':db_generation,'media_since':floor,'raw_preserved':True}
            for view in [*views,week]:
                view['period']['coverage_complete']=complete
            collection={**old['collection'],'status':proof['status'],'media_age_restricted':True,'media_since':floor,
                'media_discovered':len(targets),'media_completed':len(targets)-len(missing),'media_pending':len(missing),
                'stored_in_window':{'stored_comments_and_replies':len(week['comments'])},
                'coverage':{'status':proof['status'],'selected_post_window':proof,
                    'historical_baseline':{'status':'not_required','reason':'user_selected_previous_month_posts'}},
                'last_checked_at':dt.datetime.now(dt.timezone.utc).isoformat()}
            source={**old,'views':views,'recent_seven_days':week,'collection':collection,
                'media_age_restricted':True,'media_since':floor,'collection_coverage':collection['coverage'],
                'generated_at':dt.datetime.now(dt.timezone.utc).isoformat(),
                'reprojection':{'reason':'user_selected_previous_calendar_month','previous_generation':generation}}
            (HERE/(name+'-source-before.json')).write_text(json.dumps(old,ensure_ascii=False,indent=2))
            (HERE/(name+'-source.json')).write_text(json.dumps(source,ensure_ascii=False,indent=2))
            (HERE/(name+'-analysis-before.json')).write_text(json.dumps(analysis,ensure_ascii=False,indent=2))
            result={'account':name,'posts':len(targets),'daily':len(views[0]['comments']),'week':len(week['comments']),
                'proof':proof,'applied':False}
            if apply:
                if not complete:
                    raise SystemExit('선택 기간의 미수집 게시물을 먼저 확인해야 합니다: '+name)
                stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
                state.json_put(f'runs/{DAY}/revisions/post-window-{stamp}/source.json',old,0)
                state.json_put(f'runs/{DAY}/source.json',source,generation)
                observed,_=state.json_get(f'runs/{DAY}/source.json')
                assert observed==source
                result['applied']=True
            results.append(result)
            print(json.dumps(result,ensure_ascii=False),flush=True)
    (HERE/('applied.json' if apply else 'audit.json')).write_text(json.dumps(results,ensure_ascii=False,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['audit','apply','prepare','build','deploy'])
    args=parser.parse_args()
    if args.action in {'audit','apply'}:
        reproject(args.action=='apply')
    else:
        release.RELEASE=HERE
        getattr(release,args.action)()
