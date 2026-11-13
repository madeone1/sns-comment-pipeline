"""Release token-led collection without a hosted management dependency."""
import argparse
import copy
import datetime as dt
import hashlib
import json
import shutil
from cloud_common import ROOT,PROJECT,REGION,gcloud

RELEASE=ROOT/'.local/token-connection-release'


def save(name,value):
    (RELEASE/(name+'.json')).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n', encoding="utf-8")


def prepare(config=ROOT/'config.json'):
    RELEASE.mkdir(parents=True,exist_ok=True)
    settings=json.loads(config.read_text(encoding="utf-8"))
    assert settings['workflow']['enabled'] is False and settings['workflow']['google_sheets']['enabled'] is True
    assert settings['notion']['verify_report_guidelines'] is True
    settings['connections']={'enabled':True,'token_secret':'projects/'+PROJECT+'/secrets/sns-meta-token'}
    folder=RELEASE/'context'
    if folder.exists():shutil.rmtree(folder)
    folder.mkdir()
    shutil.copytree(ROOT/'sns_comment',folder/'sns_comment',dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    for name in ('Dockerfile','.dockerignore','.gcloudignore'):shutil.copy2(ROOT/name,folder/name)
    (folder/'config.json').write_text(json.dumps(settings,ensure_ascii=False,indent=2)+'\n', encoding="utf-8")
    tag=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S')+'-token-connections'
    image=f'{REGION}-docker.pkg.dev/{PROJECT}/containers/sns-comments:{tag}'
    save('release',{'image':image,'source_hashes':{str(p.relative_to(folder)):hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.rglob('*') if p.is_file()},
        'changes':['worker image','connections.enabled=true','remove raw Meta token environment injection'],
        'schedule_preserved':True,'hosted_management_required':False})
    save('cloudbuild',{'steps':[{'name':'gcr.io/cloud-builders/docker','args':['build','-t',image,'.']}],
        'images':[image],'options':{'logging':'GCS_ONLY'}})
    print(json.dumps({'prepared':True,'image':image}))


def build():
    info=json.loads(gcloud(['builds','submit',str(RELEASE/'context'),'--config='+str(RELEASE/'cloudbuild.json'),
        '--service-account=projects/'+PROJECT+'/serviceAccounts/sns-build@'+PROJECT+'.iam.gserviceaccount.com',
        '--gcs-log-dir=gs://'+PROJECT+'-build/logs','--gcs-source-staging-dir=gs://'+PROJECT+'-build/source',
        '--async','--format=json']).stdout)
    save('build',info);print(json.dumps({'id':info['id'],'status':info['status']}))


def verify(day,page):
    if not day or not page:raise SystemExit('--day와 --page로 기존 운영 보고를 지정해주세요.')
    dt.date.fromisoformat(day)
    import re
    if not re.fullmatch(r'[a-fA-F0-9-]{32,36}',page):raise SystemExit('Notion 페이지 ID를 확인해주세요.')
    image=json.loads((RELEASE/'release.json').read_text(encoding="utf-8"))['image']
    build_id=json.loads((RELEASE/'build.json').read_text(encoding="utf-8"))['id']
    if gcloud(['builds','describe',build_id,'--format=value(status)']).stdout.strip()!='SUCCESS':
        raise SystemExit('빌드가 완료된 뒤 검증해주세요.')
    job='sns-token-check-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d%H%M%S')
    gcloud(['run','jobs','create',job,'--region='+REGION,'--image='+image,
        '--service-account=sns-pipeline@'+PROJECT+'.iam.gserviceaccount.com',
        '--command=python','--args=-m,sns_comment.release_check','--max-retries=0',
        '--task-timeout=1800s','--memory=512Mi','--cpu=1',
        '--set-env-vars=GOOGLE_CLOUD_PROJECT='+PROJECT+',SNS_STATE_BUCKET='+PROJECT+
        '-state,SNS_NOTION_OAUTH_SECRET=projects/'+PROJECT+'/secrets/sns-notion-oauth'+
        ',SNS_RELEASE_IMAGE='+image+',SNS_VERIFY_DAY='+day+',SNS_VERIFY_PAGE='+page])
    try:
        gcloud(['run','jobs','execute',job,'--region='+REGION,'--wait'])
        key='verification/token-connections/candidates/'+image.rsplit(':',1)[-1]+'.json'
        result=json.loads(gcloud(['storage','cat','gs://'+PROJECT+'-state/'+key]).stdout)
        if result.get('status')!='verified' or result.get('image')!=image:
            raise SystemExit('후보 이미지 검증 결과가 일치하지 않습니다.')
        save('candidate-result',result)
        print(json.dumps({'status':'verified','image':image,'proof':result['proof']}))
    finally:
        gcloud(['run','jobs','delete',job,'--region='+REGION],optional=True)


SCHEDULER = 'serviceAccount:sns-scheduler@' + PROJECT + '.iam.gserviceaccount.com'
INVOKER_JOBS = ('sns-comments-daily', 'sns-comments-moderate', 'sns-comments-notion-summary')


def scheduler_invoker_bound(policy, member=SCHEDULER):
    """True when the scheduler identity already has roles/run.invoker. Does not replace the policy."""
    for binding in (policy or {}).get('bindings') or []:
        if binding.get('role') == 'roles/run.invoker' and member in (binding.get('members') or []):
            return True
    return False


def ensure_scheduler_invoker(job):
    """Restore a missing invoker binding. Never call set-iam-policy; that would wipe other members."""
    described = gcloud(['run', 'jobs', 'describe', job, '--region=' + REGION, '--format=value(metadata.name)'], optional=True)
    if described.returncode:
        return False
    policy = json.loads(gcloud(['run', 'jobs', 'get-iam-policy', job, '--region=' + REGION, '--format=json']).stdout or '{}')
    if scheduler_invoker_bound(policy):
        print(json.dumps({'job': job, 'scheduler_invoker': True}))
        return True
    gcloud(['run', 'jobs', 'add-iam-policy-binding', job, '--region=' + REGION,
            '--member=' + SCHEDULER, '--role=roles/run.invoker'])
    checked = json.loads(gcloud(['run', 'jobs', 'get-iam-policy', job, '--region=' + REGION, '--format=json']).stdout or '{}')
    if not scheduler_invoker_bound(checked):
        raise SystemExit(job + ' 작업에 스케줄러 실행 권한이 없습니다.')
    print(json.dumps({'job': job, 'scheduler_invoker': True, 'binding_restored': True}))
    return True


def deploy():
    image=json.loads((RELEASE/'release.json').read_text(encoding="utf-8"))['image']
    build_id=json.loads((RELEASE/'build.json').read_text(encoding="utf-8"))['id']
    if gcloud(['builds','describe',build_id,'--format=value(status)']).stdout.strip()!='SUCCESS':
        raise SystemExit('성공한 빌드가 필요합니다.')
    verified=json.loads((RELEASE/'candidate-result.json').read_text(encoding="utf-8"))
    if verified.get('status')!='verified' or verified.get('image')!=image:
        raise SystemExit('같은 후보 이미지의 성공한 검증 증빙이 필요합니다.')
    before=json.loads(gcloud(['run','jobs','describe','sns-comments-daily','--region='+REGION,'--format=json']).stdout)
    save('job-before-deploy',before)
    gcloud(['run','jobs','update','sns-comments-daily','--region='+REGION,'--image='+image,'--remove-secrets=SNS_IG_ACCESS_TOKEN'])
    after=json.loads(gcloud(['run','jobs','describe','sns-comments-daily','--region='+REGION,'--format=json']).stdout)
    expected=copy.deepcopy(before['spec']['template']['spec']['template']['spec'])
    expected['containers'][0]['image']=image
    expected['containers'][0]['env']=[e for e in expected['containers'][0].get('env',[]) if e['name']!='SNS_IG_ACCESS_TOKEN']
    assert expected==after['spec']['template']['spec']['template']['spec']
    save('job-after',after);print(json.dumps({'deployed':True,'image':image,'only_planned_changes':True}))
    # Image only. Do not pass --set-env-vars: that replaces SNS_MODERATION_HIDE and the other flags.
    # This repo never calls set-iam-policy. A missing scheduler binding is added back, not rewritten.
    for job in ('sns-comments-moderate', 'sns-comments-notion-summary'):
        described = gcloud(['run', 'jobs', 'describe', job, '--region=' + REGION, '--format=value(metadata.name)'], optional=True)
        if described.returncode:
            print(json.dumps({'job': job, 'image_update': 'absent'}))
            continue
        gcloud(['run', 'jobs', 'update', job, '--region=' + REGION, '--image=' + image])
        print(json.dumps({'job': job, 'image': image, 'env_unchanged': True}))
    for job in INVOKER_JOBS:
        ensure_scheduler_invoker(job)


if __name__=='__main__':
    from pathlib import Path
    p=argparse.ArgumentParser()
    p.add_argument('action',choices=['prepare','build','verify','deploy'])
    p.add_argument('--config',type=Path,default=ROOT/'config.json')
    p.add_argument('--day',help='검증할 기존 보고 날짜 YYYY-MM-DD')
    p.add_argument('--page',help='해당 날짜의 기존 Notion 페이지 ID')
    args=p.parse_args()
    if args.action=='prepare':prepare(args.config)
    elif args.action=='verify':verify(args.day,args.page)
    else:globals()[args.action]()
