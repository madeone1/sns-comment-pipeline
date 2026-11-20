"""Create and deploy only the dedicated YouTube resources; no legacy mutations."""

import argparse
from contextlib import closing
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess

from .cloud_baseline import snapshot
from .google_cloud import PROJECT, REGION, RUNTIME, BUCKET, SECRET, JOB
from .storage import STATE, private_dir, read_json, write_json

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT/'.local/youtube-pipeline-release'
SCHEDULER = 'youtube-scheduler@' + PROJECT + '.iam.gserviceaccount.com'
ACCOUNT = 'syan@madeone.kr'


def gcloud(args, optional=False, value=None):
    process = subprocess.run(['gcloud', *args, '--project='+PROJECT, '--account='+ACCOUNT, '--quiet'],
        input=value, capture_output=True, text=True, timeout=180)
    if process.returncode and not optional:
        # All credential payloads are on stdin, never in arguments or logs.
        raise RuntimeError('클라우드 단계 실패: ' + ' '.join(args[:3]) + '\n' + process.stderr[-1600:])
    return process


def provision():
    private_dir(RELEASE)
    before = RELEASE/'baseline-before.json'
    if not before.exists():
        write_json(before,snapshot())
    for email, name in ((RUNTIME,'YouTube comment pipeline'), (SCHEDULER,'YouTube pipeline scheduler')):
        exists=gcloud(['iam','service-accounts','describe',email,'--format=json'],optional=True)
        if exists.returncode:
            gcloud(['iam','service-accounts','create',email.split('@')[0],'--display-name='+name])
        elif json.loads(exists.stdout).get('displayName') != name:
            raise RuntimeError('다른 용도의 기존 서비스 계정입니다: '+email)
    for member in ('serviceAccount:'+RUNTIME, 'user:'+ACCOUNT):
        gcloud(['iam','service-accounts','add-iam-policy-binding',RUNTIME,'--member='+member,
                '--role=roles/iam.serviceAccountTokenCreator'])
    bucket=gcloud(['storage','buckets','describe','gs://'+BUCKET,'--format=json'],optional=True)
    if bucket.returncode:
        gcloud(['storage','buckets','create','gs://'+BUCKET,'--location='+REGION,
                '--uniform-bucket-level-access','--public-access-prevention'])
    elif json.loads(bucket.stdout).get('labels',{}).get('app') != 'youtube-comments':
        raise RuntimeError('기존 버킷의 용도가 일치하지 않습니다.')
    gcloud(['storage','buckets','update','gs://'+BUCKET,'--update-labels=app=youtube-comments'])
    gcloud(['storage','buckets','add-iam-policy-binding','gs://'+BUCKET,
            '--member=serviceAccount:'+RUNTIME,'--role=roles/storage.objectAdmin'])
    secret=gcloud(['secrets','describe',SECRET,'--format=json'],optional=True)
    if secret.returncode:
        gcloud(['secrets','create',SECRET,'--replication-policy=automatic','--labels=app=youtube-comments'])
    elif json.loads(secret.stdout).get('labels',{}).get('app') != 'youtube-comments':
        raise RuntimeError('기존 Secret의 용도가 일치하지 않습니다.')
    gcloud(['secrets','add-iam-policy-binding',SECRET,'--member=serviceAccount:'+RUNTIME,
            '--role=roles/secretmanager.secretAccessor'])
    credentials()
    print(json.dumps({'runtime':RUNTIME,'scheduler':SCHEDULER,'bucket':BUCKET,'secret':SECRET}))


def credentials():
    private_dir(RELEASE)
    data=read_json(STATE/'authorized-user.json')
    # Access tokens are short-lived and need not be copied into the cloud secret.
    credential={key:data[key] for key in ('client_id','client_secret','refresh_token','token_uri','scopes')}
    payload=json.dumps(credential,ensure_ascii=False)
    fingerprint=hashlib.sha256(payload.encode()).hexdigest()
    saved=read_json(RELEASE/'secret-version.json') if (RELEASE/'secret-version.json').exists() else {}
    if saved.get('sha256') != fingerprint:
        name=gcloud(['secrets','versions','add',SECRET,'--data-file=-','--format=value(name)'],value=payload).stdout.strip()
        saved={'version':name.rsplit('/',1)[-1],'sha256':fingerprint}
        write_json(RELEASE/'secret-version.json',saved)
    return saved['version']


def prepare(spreadsheet_id):
    if not spreadsheet_id or '/' in spreadsheet_id:
        raise RuntimeError('새 스프레드시트의 ID를 지정해주세요.')
    private_dir(RELEASE)
    config={'spreadsheet_id':spreadsheet_id,'owner_email':ACCOUNT,'max_requests':2000,
        'daily_budget':8000,'seconds':2400,'oauth_mode':'production','schedule':'30 * * * *',
        'time_zone':'Asia/Seoul','daily_start':'08:30'}
    write_json(ROOT/'youtube_validation/pipeline_config.json',config)
    context=RELEASE/'context'
    if context.exists():
        shutil.rmtree(context)
    package=private_dir(context/'youtube_validation')
    for source in (ROOT/'youtube_validation').glob('*.py'):
        if source.name != 'release.py':
            shutil.copy2(source,package/source.name)
    shutil.copy2(ROOT/'youtube_validation/pipeline_config.json',package/'pipeline_config.json')
    shutil.copy2(ROOT/'requirements.lock.txt',context/'requirements.txt')
    (context/'Dockerfile').write_text('''FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY youtube_validation ./youtube_validation
ENTRYPOINT ["python", "-m", "youtube_validation.worker"]
''')
    tag=datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    image=f'{REGION}-docker.pkg.dev/{PROJECT}/containers/youtube-comments:{tag}'
    write_json(RELEASE/'release.json',{'image':image,'spreadsheet_id':spreadsheet_id})
    write_json(RELEASE/'cloudbuild.json',{'steps':[{'name':'gcr.io/cloud-builders/docker',
        'args':['build','-t',image,'.']}],'images':[image],'options':{'logging':'GCS_ONLY'}})
    print(json.dumps({'prepared':True,'image':image,'job':JOB,'spreadsheet_id':spreadsheet_id}))


def build():
    info=json.loads(gcloud(['builds','submit',str(RELEASE/'context'),'--config='+str(RELEASE/'cloudbuild.json'),
        '--service-account=projects/'+PROJECT+'/serviceAccounts/sns-build@'+PROJECT+'.iam.gserviceaccount.com',
        '--gcs-log-dir=gs://'+PROJECT+'-build/logs','--gcs-source-staging-dir=gs://'+PROJECT+'-build/source',
        '--async','--format=json']).stdout)
    write_json(RELEASE/'build.json',info)
    print(json.dumps({'build_id':info['id'],'status':info['status']}))


def seed():
    source=STATE/'comments.sqlite3'
    target=RELEASE/'seed.sqlite3'
    with closing(sqlite3.connect(source)) as db, closing(sqlite3.connect(target)) as destination:
        db.backup(destination)
    compressed=RELEASE/'seed.sqlite3.gz'
    compressed.write_bytes(gzip.compress(target.read_bytes()))
    compressed.chmod(0o600)
    target.unlink()
    # Initial publication only; never overwrite an existing cloud checkpoint.
    exists=gcloud(['storage','objects','describe','gs://'+BUCKET+'/state/comments.sqlite3.gz','--format=value(name)'],optional=True)
    if exists.returncode==0:
        raise RuntimeError('클라우드 상태가 이미 있습니다. 초기 DB로 덮어쓰지 않습니다.')
    gcloud(['storage','cp',str(compressed),'gs://'+BUCKET+'/state/comments.sqlite3.gz','--if-generation-match=0'])
    print(json.dumps({'seeded':True,'bucket':BUCKET}))


def deploy():
    image=read_json(RELEASE/'release.json')['image']
    build_id=read_json(RELEASE/'build.json')['id']
    if gcloud(['builds','describe',build_id,'--format=value(status)']).stdout.strip()!='SUCCESS':
        raise RuntimeError('성공한 컨테이너 빌드가 필요합니다.')
    existing=gcloud(['run','jobs','describe',JOB,'--region='+REGION,'--format=json'],optional=True)
    if existing.returncode==0 and json.loads(existing.stdout).get('metadata',{}).get('labels',{}).get('app')!='youtube-comments':
        raise RuntimeError('기존 작업의 용도가 다릅니다. 덮어쓰지 않습니다.')
    version=credentials()
    action='create' if existing.returncode else 'update'
    # Replace the dedicated job's mounts instead of accumulating unused secret
    # volumes on each update. This job has no other application volumes.
    mounts=[] if action=='create' else ['--clear-volumes','--clear-volume-mounts']
    gcloud(['run','jobs',action,JOB,*mounts,'--region='+REGION,'--image='+image,'--service-account='+RUNTIME,
        '--tasks=1','--parallelism=1','--max-retries=1','--task-timeout=2700s','--memory=1Gi','--cpu=1',
        '--labels=app=youtube-comments','--set-env-vars=YOUTUBE_AUTH_FILE=/secrets/youtube/oauth.json',
        '--set-secrets=/secrets/youtube/oauth.json='+SECRET+':'+version])
    gcloud(['run','jobs','add-iam-policy-binding',JOB,'--region='+REGION,
            '--member=serviceAccount:'+SCHEDULER,'--role=roles/run.invoker'])
    print(json.dumps({'deployed':True,'job':JOB,'image':image,'secret_version':version}))


def execute(force_cycle=False):
    args=['run','jobs','execute',JOB,'--region='+REGION,'--async','--format=json']
    if force_cycle:
        args.append('--update-env-vars=YOUTUBE_FORCE_CYCLE=true')
    info=json.loads(gcloud(args).stdout)
    write_json(RELEASE/'execution.json',info)
    print(json.dumps(info))


def schedule():
    url=f'https://run.googleapis.com/v2/projects/{PROJECT}/locations/{REGION}/jobs/{JOB}:run'
    existing=gcloud(['scheduler','jobs','describe',JOB,'--location='+REGION,'--format=json'],optional=True)
    if existing.returncode==0:
        data=json.loads(existing.stdout)
        if data.get('httpTarget',{}).get('uri')!=url:
            raise RuntimeError('기존 예약의 대상이 다릅니다.')
    action='create' if existing.returncode else 'update'
    gcloud(['scheduler','jobs',action,'http',JOB,'--location='+REGION,'--schedule=30 * * * *',
        '--time-zone=Asia/Seoul','--uri='+url,'--http-method=POST','--message-body={}',
        '--headers=Content-Type=application/json','--oauth-service-account-email='+SCHEDULER,
        '--oauth-token-scope=https://www.googleapis.com/auth/cloud-platform'])
    print(json.dumps({'scheduled':True,'job':JOB,'time_zone':'Asia/Seoul','schedule':'30 * * * *'}))


def verify_legacy():
    before=read_json(RELEASE/'baseline-before.json')
    after=snapshot()
    after['unchanged']=all(before.get(key)==after.get(key) for key in ('jobs','schedulers'))
    write_json(RELEASE/'baseline-after.json',after)
    print(json.dumps({'legacy_unchanged':after['unchanged']}))
    if not after['unchanged']:
        raise RuntimeError('기존 작업·예약 설정 대조가 일치하지 않습니다.')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['provision','credentials','prepare','build','seed','deploy','execute','schedule','verify-legacy'])
    parser.add_argument('--spreadsheet-id')
    parser.add_argument('--force-cycle',action='store_true')
    args=parser.parse_args()
    if args.action=='prepare':
        prepare(args.spreadsheet_id)
    elif args.action=='verify-legacy':
        verify_legacy()
    elif args.action=='execute':
        execute(args.force_cycle)
    else:
        globals()[args.action]()
