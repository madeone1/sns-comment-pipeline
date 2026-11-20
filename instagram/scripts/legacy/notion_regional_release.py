# Historical release tooling. See scripts/legacy/README.md before use.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
"""Release only the existing worker's adopted Notion report layout."""

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import shutil

from cloud_common import PROJECT, REGION, ROOT, gcloud


RELEASE = ROOT/'.local/notion-regional-release'


def prepare():
    settings = json.loads((ROOT/'.local/web-release/operational-config.json').read_text(encoding="utf-8"))
    assert settings['notion']['regional_report'] is True
    assert settings['notion']['report_instructions_page_id']=='4130761a-512d-8150-9362-ffc4b4a8de68'
    assert settings['cloud']['comment_written_windows'] is True
    assert not settings.get('workflow',{}).get('enabled',False)
    folder = RELEASE/'context'
    folder.mkdir(parents=True,exist_ok=True)
    shutil.copytree(ROOT/'sns_comment',folder/'sns_comment',dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    for name in ('Dockerfile','.dockerignore','.gcloudignore'):
        shutil.copy2(ROOT/name,folder/name)
    (folder/'config.json').write_text(json.dumps(settings,ensure_ascii=False,indent=2)+'\n', encoding="utf-8")
    tag=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S')+'-regional-notion'
    image=f'{REGION}-docker.pkg.dev/{PROJECT}/containers/sns-comments:{tag}'
    build={'steps':[{'name':'gcr.io/cloud-builders/docker','args':['build','-t',image,'.']}],
           'images':[image],'options':{'logging':'GCS_ONLY'}}
    (RELEASE/'cloudbuild.json').write_text(json.dumps(build,indent=2), encoding="utf-8")
    (RELEASE/'release.json').write_text(json.dumps({'image':image,'files':{
        str(p.relative_to(folder)):hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.rglob('*') if p.is_file()}},indent=2), encoding="utf-8")
    print(json.dumps({'image':image,'sheets_enabled':False,'existing_schedule_preserved':True}))


def build():
    info=json.loads(gcloud(['builds','submit',str(RELEASE/'context'),'--config='+str(RELEASE/'cloudbuild.json'),
        '--service-account=projects/'+PROJECT+'/serviceAccounts/sns-build@'+PROJECT+'.iam.gserviceaccount.com',
        '--gcs-log-dir=gs://'+PROJECT+'-build/logs','--gcs-source-staging-dir=gs://'+PROJECT+'-build/source',
        '--async','--format=json']).stdout)
    (RELEASE/'build.json').write_text(json.dumps(info,indent=2), encoding="utf-8")
    print(json.dumps({'id':info['id'],'status':info['status']}))


def deploy():
    image=json.loads((RELEASE/'release.json').read_text(encoding="utf-8"))['image']
    build_id=json.loads((RELEASE/'build.json').read_text(encoding="utf-8"))['id']
    if gcloud(['builds','describe',build_id,'--format=value(status)']).stdout.strip()!='SUCCESS':
        raise SystemExit('빌드 성공 확인 후 배포해주세요.')
    before=json.loads(gcloud(['run','jobs','describe','sns-comments-daily','--region='+REGION,'--format=json']).stdout)
    (RELEASE/'job-before.json').write_text(json.dumps(before,indent=2), encoding="utf-8")
    gcloud(['run','jobs','update','sns-comments-daily','--region='+REGION,'--image='+image])
    after=json.loads(gcloud(['run','jobs','describe','sns-comments-daily','--region='+REGION,'--format=json']).stdout)
    expected=before['spec']['template']['spec']['template']['spec']
    expected['containers'][0]['image']=image
    if expected!=after['spec']['template']['spec']['template']['spec']:
        raise SystemExit('이미지 외 설정 변경을 확인해야 합니다.')
    (RELEASE/'job-after.json').write_text(json.dumps(after,indent=2), encoding="utf-8")
    print(json.dumps({'deployed':True,'image_only_verified':True,'image':image}))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['prepare','build','deploy'])
    args=parser.parse_args()
    globals()[args.action]()
