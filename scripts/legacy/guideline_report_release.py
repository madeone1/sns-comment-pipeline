# Historical release tooling. See scripts/legacy/README.md before use.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
"""Release the existing worker with current-guideline and heading checks."""

import argparse
import copy
import datetime as dt
import hashlib
import json
import shutil

from cloud_common import PROJECT, REGION, ROOT, gcloud

RELEASE=ROOT/'.local/guideline-report-release'


def save(name,value):
    (RELEASE/(name+'.json')).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')


def prepare():
    RELEASE.mkdir(parents=True,exist_ok=True)
    baseline=json.loads((ROOT/'.local/account-tabs-release/context/config.json').read_text())
    settings=copy.deepcopy(baseline)
    settings['notion']['verify_report_guidelines']=True
    assert settings['notion']['regional_report'] is True
    assert settings['notion']['report_instructions_page_id']=='4130761a-512d-8150-9362-ffc4b4a8de68'
    assert settings['workflow']['google_sheets']['enabled'] is True
    assert settings['workflow']['enabled'] is False
    folder=RELEASE/'context';folder.mkdir(exist_ok=True)
    shutil.copytree(ROOT/'sns_comment',folder/'sns_comment',dirs_exist_ok=True,
        ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    for name in ('Dockerfile','.dockerignore','.gcloudignore'):
        shutil.copy2(ROOT/name,folder/name)
    (folder/'config.json').write_text(json.dumps(settings,ensure_ascii=False,indent=2)+'\n')
    tag=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S')+'-guideline-report'
    image=f'{REGION}-docker.pkg.dev/{PROJECT}/containers/sns-comments:{tag}'
    save('release',{'image':image,'config_change':'notion.verify_report_guidelines=true',
        'files':{str(p.relative_to(folder)):hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.rglob('*') if p.is_file()}})
    save('cloudbuild',{'steps':[{'name':'gcr.io/cloud-builders/docker','args':['build','-t',image,'.']}],
        'images':[image],'options':{'logging':'GCS_ONLY'}})
    print(json.dumps({'prepared':True,'image':image}))


def build():
    info=json.loads(gcloud(['builds','submit',str(RELEASE/'context'),'--config='+str(RELEASE/'cloudbuild.json'),
        '--service-account=projects/'+PROJECT+'/serviceAccounts/sns-build@'+PROJECT+'.iam.gserviceaccount.com',
        '--gcs-log-dir=gs://'+PROJECT+'-build/logs','--gcs-source-staging-dir=gs://'+PROJECT+'-build/source',
        '--async','--format=json']).stdout)
    save('build',info);print(json.dumps({'id':info['id'],'status':info['status']}))


def deploy():
    image=json.loads((RELEASE/'release.json').read_text())['image']
    build_id=json.loads((RELEASE/'build.json').read_text())['id']
    assert gcloud(['builds','describe',build_id,'--format=value(status)']).stdout.strip()=='SUCCESS'
    before=json.loads(gcloud(['run','jobs','describe','sns-comments-daily','--region='+REGION,'--format=json']).stdout)
    save('job-before',before)
    gcloud(['run','jobs','update','sns-comments-daily','--region='+REGION,'--image='+image])
    after=json.loads(gcloud(['run','jobs','describe','sns-comments-daily','--region='+REGION,'--format=json']).stdout)
    expected=copy.deepcopy(before['spec']['template']['spec']['template']['spec'])
    expected['containers'][0]['image']=image
    assert expected==after['spec']['template']['spec']['template']['spec']
    save('job-after',after);print(json.dumps({'deployed':True,'image_only_verified':True,'image':image}))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','build','deploy'])
    globals()[p.parse_args().action]()
