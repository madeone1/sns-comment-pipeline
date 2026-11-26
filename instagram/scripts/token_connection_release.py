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
    from sns_comment.moderation_release_check import check_settings
    check_settings(settings)
    settings['connections']={'enabled':True,'token_secret':'projects/'+PROJECT+'/secrets/sns-meta-token'}
    folder=RELEASE/'context'
    if folder.exists():shutil.rmtree(folder)
    folder.mkdir()
    shutil.copytree(ROOT/'sns_comment',folder/'sns_comment',dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    for name in ('Dockerfile','.dockerignore','.gcloudignore'):shutil.copy2(ROOT/name,folder/name)
    (folder/'config.json').write_text(json.dumps(settings,ensure_ascii=False,indent=2)+'\n', encoding="utf-8")
    tag=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S')+'-moderation-coverage'
    image=f'{REGION}-docker.pkg.dev/{PROJECT}/containers/sns-comments:{tag}'
    save('release',{'image':image,'source_hashes':{str(p.relative_to(folder)):hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.rglob('*') if p.is_file()},
        'changes':['moderation and summary images','durable 60-minute polling','legacy daily retired'],
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


def verify(day=None,page=None):
    # day/page are accepted for older local launchers, but no daily report is read.
    image=json.loads((RELEASE/'release.json').read_text(encoding="utf-8"))['image']
    build_id=json.loads((RELEASE/'build.json').read_text(encoding="utf-8"))['id']
    if gcloud(['builds','describe',build_id,'--format=value(status)']).stdout.strip()!='SUCCESS':
        raise SystemExit('빌드가 완료된 뒤 검증해주세요.')
    job='sns-token-check-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d%H%M%S')
    gcloud(['run','jobs','create',job,'--region='+REGION,'--image='+image,
        '--service-account=sns-pipeline@'+PROJECT+'.iam.gserviceaccount.com',
        '--command=python','--args=-m,sns_comment.moderation_release_check','--max-retries=0',
        '--task-timeout=1800s','--memory=512Mi','--cpu=1',
        '--update-env-vars=GOOGLE_CLOUD_PROJECT='+PROJECT+',SNS_STATE_BUCKET='+PROJECT+
        '-state,SNS_NOTION_OAUTH_SECRET=projects/'+PROJECT+'/secrets/sns-notion-oauth'+
        ',SNS_RELEASE_IMAGE='+image])
    try:
        gcloud(['run','jobs','execute',job,'--region='+REGION,'--wait'])
        key='verification/moderation/candidates/'+image.rsplit(':',1)[-1]+'.json'
        result=json.loads(gcloud(['storage','cat','gs://'+PROJECT+'-state/'+key]).stdout)
        if result.get('status')!='verified' or result.get('image')!=image:
            raise SystemExit('후보 이미지 검증 결과가 일치하지 않습니다.')
        save('candidate-result',result)
        print(json.dumps({'status':'verified','image':image,'accounts':len(result['accounts'])}))
    finally:
        gcloud(['run','jobs','delete',job,'--region='+REGION],optional=True)


SCHEDULER = 'serviceAccount:sns-scheduler@' + PROJECT + '.iam.gserviceaccount.com'
INVOKER_JOBS = ('sns-comments-moderate', 'sns-comments-notion-summary')
SWEEP_JOB = 'sns-comments-past-sweep'
MONTHLY_COST_JOB = 'sns-comments-monthly-cost'
CAPTION_POLICY_JOB = 'sns-comments-caption-policy'


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
    for job in INVOKER_JOBS:
        args=['run','jobs','update',job,'--region='+REGION,'--image='+image]
        if job=='sns-comments-moderate':args.append('--task-timeout=1500s')
        # --image and timeout only: preserve every existing environment flag.
        gcloud(args)
        print(json.dumps({'job':job,'image':image,'env_unchanged':True}))
    for job in INVOKER_JOBS:
        ensure_scheduler_invoker(job)
    update_past_sweep_image(image)
    update_monthly_cost_image(image)
    update_caption_policy_image(image)


def update_past_sweep_image(image):
    """Point the on-demand sweep at the new image. Skip it until an admin creates the job.

    Image only. Environment variables, arguments, and the task timeout stay as created.
    """
    described = gcloud(['run', 'jobs', 'describe', SWEEP_JOB, '--region=' + REGION,
                        '--format=value(metadata.name)'], optional=True)
    if getattr(described, 'returncode', 1):
        print(json.dumps({'job': SWEEP_JOB, 'skipped': True, 'reason': 'not_created'}))
        return
    gcloud(['run', 'jobs', 'update', SWEEP_JOB, '--region=' + REGION, '--image=' + image])
    print(json.dumps({'job': SWEEP_JOB, 'image': image, 'env_unchanged': True}))


def update_monthly_cost_image(image):
    """Point the monthly cost job at the new image. Skip it until an admin creates the job.

    Image only. Environment variables, arguments, and the task timeout stay as created.
    """
    described = gcloud(['run', 'jobs', 'describe', MONTHLY_COST_JOB, '--region=' + REGION,
                        '--format=value(metadata.name)'], optional=True)
    if getattr(described, 'returncode', 1):
        print(json.dumps({'job': MONTHLY_COST_JOB, 'skipped': True, 'reason': 'not_created'}))
        return
    gcloud(['run', 'jobs', 'update', MONTHLY_COST_JOB, '--region=' + REGION, '--image=' + image])
    print(json.dumps({'job': MONTHLY_COST_JOB, 'image': image, 'env_unchanged': True}))


def update_caption_policy_image(image):
    """Point the caption rulebook job at the new image. Skip it until an admin creates the job.

    Image only. Environment variables, arguments, the schedule, and the task timeout stay as created.
    This does not create the job or the scheduler, and it does not touch the moderation jobs' settings.
    """
    described = gcloud(['run', 'jobs', 'describe', CAPTION_POLICY_JOB, '--region=' + REGION,
                        '--format=value(metadata.name)'], optional=True)
    if getattr(described, 'returncode', 1):
        print(json.dumps({'job': CAPTION_POLICY_JOB, 'skipped': True, 'reason': 'not_created'}))
        return
    gcloud(['run', 'jobs', 'update', CAPTION_POLICY_JOB, '--region=' + REGION, '--image=' + image])
    print(json.dumps({'job': CAPTION_POLICY_JOB, 'image': image, 'env_unchanged': True}))


def retire_daily():
    """Remove only the retired Instagram daily resources; raw state stays intact."""
    scheduler=gcloud(['scheduler','jobs','describe','sns-comments-daily','--location='+REGION,
                      '--format=value(state)'],optional=True)
    if scheduler.returncode==0:
        gcloud(['scheduler','jobs','delete','sns-comments-daily','--location='+REGION])
    described=gcloud(['run','jobs','describe','sns-comments-daily','--region='+REGION,
                     '--format=json(metadata.name,status.latestCreatedExecution)'],optional=True)
    if described.returncode==0:
        snapshot=json.loads(described.stdout)
        save('retired-daily',snapshot)
        latest=snapshot.get('status',{}).get('latestCreatedExecution',{})
        if latest.get('completionStatus')=='EXECUTION_RUNNING':
            gcloud(['run','jobs','executions','cancel',latest['name'],'--region='+REGION])
        gcloud(['run','jobs','delete','sns-comments-daily','--region='+REGION])
    print(json.dumps({'daily_retired':True,'raw_state_preserved':True}))


if __name__=='__main__':
    from pathlib import Path
    p=argparse.ArgumentParser()
    p.add_argument('action',choices=['prepare','build','verify','deploy','retire-daily'])
    p.add_argument('--config',type=Path,default=ROOT/'config.json')
    p.add_argument('--day',help='검증할 기존 보고 날짜 YYYY-MM-DD')
    p.add_argument('--page',help='해당 날짜의 기존 Notion 페이지 ID')
    args=p.parse_args()
    if args.action=='prepare':prepare(args.config)
    elif args.action=='verify':verify(args.day,args.page)
    elif args.action=='retire-daily':retire_daily()
    else:globals()[args.action]()
