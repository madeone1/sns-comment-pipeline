# Historical release tooling. See scripts/legacy/README.md before use.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
"""Approved correction of real daily evidence and comment-time worker release."""

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from sns_comment.cloud_state import CloudState, GoogleTransport
from sns_comment.comment_windows import projections
from sns_comment.profiles import select
from sns_comment.storage import Store
from cloud_common import PROJECT, REGION, gcloud

RELEASE = ROOT/'.local/comment-windows-release'


class UserTransport(GoogleTransport):
    def token(self):
        if self._token is None:
            p = subprocess.run(['gcloud', 'auth', 'print-access-token', '--account=syan@madeone.kr'], capture_output=True, text=True)
            if p.returncode:
                raise SystemExit('클라우드 인증 확인 실패. 비밀 값은 출력하지 않았습니다.')
            self._token = p.stdout.strip()
        return self._token


def backfill(day, apply=False):
    os.umask(0o077)
    RELEASE.mkdir(parents=True, exist_ok=True)
    settings = json.loads((ROOT/'.local/web-release/operational-config.json').read_text())
    shared = CloudState(PROJECT+'-state', UserTransport())
    instant = dt.datetime.fromisoformat(day+'T08:00:00+09:00')
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    results = []
    with shared.lease():
        for name in settings['cloud']['scheduled_accounts']:
            config = select(settings, name)
            state = CloudState(shared.bucket, shared.transport, prefix=config['_cloud_prefix'])
            old, generation = state.json_get('runs/'+day+'/source.json')
            if old is None:
                raise SystemExit('기존 일일 원본이 없습니다: '+name)
            with tempfile.TemporaryDirectory(prefix='sns-reproject-') as temp:
                path = Path(temp)/'snapshot.sqlite3'
                state.restore_database(path)
                # Read the DB only; this operation changes dated list objects.
                with sqlite3.connect(path.as_uri()+'?mode=ro', uri=True) as db:
                    db.row_factory = sqlite3.Row
                    db.execute('PRAGMA query_only=ON')
                    views, week = projections(db, config['account_id'], instant)
                prior = next(v for v in old['views'] if v['period']['key']=='daily')
                daily = views[0]
                before_ids = {r['id'] for r in prior['comments']}
                after_ids = {r['id'] for r in daily['comments']}
                if not before_ids <= after_ids:
                    raise SystemExit('기존 댓글이 빠져 재집계를 적용하지 않습니다: '+name)
                collection = {**old['collection'], 'basis': 'comment_written_at', 'scope': 'comment_written_at',
                    'status': 'partial', 'media_age_restricted': False, 'last_checked_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                    'coverage': {'status': 'pending', 'basis': 'existing_verified_database', 'historical_baseline': {'status':'not_started'}}}
                value = {**old, 'views': views, 'recent_seven_days': week, 'collection': collection,
                    'basis': 'comment_written_at', 'media_age_restricted': False,
                    'generated_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'analysis_state': 'rebuild_required',
                    'reprojection': {'reason': 'user_requested_comment_written_windows', 'previous_generation': generation}}
                (RELEASE/(name+'-source-before.json')).write_text(json.dumps(old, ensure_ascii=False, indent=2))
                (RELEASE/(name+'-source-after.json')).write_text(json.dumps(value, ensure_ascii=False, indent=2))
                result = {'account':name,'daily_before':len(before_ids),'daily_after':len(after_ids),
                    'added_ids':sorted(after_ids-before_ids),'rolling_week':len(week['comments']), 'applied':False}
                if apply:
                    state.json_put(f'runs/{day}/revisions/comment-written-at-{stamp}/source.json', old, 0)
                    new_generation = state.json_put(f'runs/{day}/source.json', value, generation)
                    observed, current = state.json_get(f'runs/{day}/source.json')
                    if current != new_generation or observed != value:
                        raise SystemExit('목록 저장 후 대조 실패: '+name)
                    result.update(applied=True,generation=new_generation)
                results.append(result)
    (RELEASE/('backfill-applied.json' if apply else 'backfill-preview.json')).write_text(json.dumps(results,ensure_ascii=False,indent=2))
    print(json.dumps(results,ensure_ascii=False,indent=2))


def prepare():
    RELEASE.mkdir(parents=True, exist_ok=True)
    settings = json.loads((ROOT/'.local/web-release/operational-config.json').read_text())
    settings.setdefault('cloud', {}).update(webhook_enabled=True, comment_written_windows=True, historical_baseline_requests=1000)
    folder=RELEASE/'context';folder.mkdir(parents=True,exist_ok=True)
    shutil.copytree(ROOT/'sns_comment',folder/'sns_comment',dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    for name in ['Dockerfile','Dockerfile.web','web-requirements.txt','.dockerignore','.gcloudignore']:
        shutil.copy2(ROOT/name,folder/name)
    (folder/'config.json').write_text(json.dumps(settings,ensure_ascii=False,indent=2)+'\n')
    tag=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S')+'-comment-windows'
    base=REGION+'-docker.pkg.dev/'+PROJECT+'/containers/'
    images={'worker':base+'sns-comments:'+tag,'web':base+'sns-comments-web:'+tag}
    build={'steps':[{'name':'gcr.io/cloud-builders/docker','args':['build','-f',f,'-t',images[k],'.']}
        for k,f in [('worker','Dockerfile'),('web','Dockerfile.web')]],'images':list(images.values()),'options':{'logging':'GCS_ONLY'}}
    (RELEASE/'cloudbuild.json').write_text(json.dumps(build,indent=2))
    (RELEASE/'release.json').write_text(json.dumps({'images':images,'files':{str(p.relative_to(folder)):hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.rglob('*') if p.is_file()}},indent=2))
    print(json.dumps({'images':images,'collection_basis':'comment_written_at','schedule_preserved':True}))


def build():
    info=json.loads(gcloud(['builds','submit',str(RELEASE/'context'),'--config='+str(RELEASE/'cloudbuild.json'),
        '--service-account=projects/'+PROJECT+'/serviceAccounts/sns-build@'+PROJECT+'.iam.gserviceaccount.com',
        '--gcs-log-dir=gs://'+PROJECT+'-build/logs','--gcs-source-staging-dir=gs://'+PROJECT+'-build/source','--async','--format=json']).stdout)
    (RELEASE/'build.json').write_text(json.dumps(info,indent=2));print(json.dumps({'id':info['id'],'status':info['status']}))


def deploy():
    release=json.loads((RELEASE/'release.json').read_text())
    build_id=json.loads((RELEASE/'build.json').read_text())['id']
    if gcloud(['builds','describe',build_id,'--format=value(status)']).stdout.strip()!='SUCCESS':
        raise SystemExit('검증된 빌드 성공 후 배포해주세요.')
    before=json.loads(gcloud(['run','jobs','describe','sns-comments-daily','--region='+REGION,'--format=json']).stdout)
    (RELEASE/'job-before.json').write_text(json.dumps(before,indent=2))
    gcloud(['run','jobs','update','sns-comments-daily','--region='+REGION,'--image='+release['images']['worker']])
    after=json.loads(gcloud(['run','jobs','describe','sns-comments-daily','--region='+REGION,'--format=json']).stdout)
    expected=before['spec']['template']['spec']['template']['spec']
    expected['containers'][0]['image']=release['images']['worker']
    if expected!=after['spec']['template']['spec']['template']['spec']:
        raise SystemExit('이미지 외 설정 변경을 확인해야 합니다.')
    (RELEASE/'job-after.json').write_text(json.dumps(after,indent=2))
    print(json.dumps({'deployed':True,'image_only_verified':True,'image':release['images']['worker']}))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['backfill','prepare','build','deploy'])
    parser.add_argument('--day',default='2026-11-10')
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    {'backfill':lambda:backfill(args.day,args.apply),'prepare':prepare,'build':build,'deploy':deploy}[args.action]()
