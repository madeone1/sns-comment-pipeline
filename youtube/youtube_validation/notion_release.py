"""Deploy only the dedicated YouTube-to-Notion reporting job.

Reuses the existing WSH connection Secret. Collector jobs and Instagram runtime
configuration are never changed by this tool.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

from .google_cloud import PROJECT, REGION, BUCKET
from .notion_report import checked_config
from .storage import private_dir, read_json, write_json


ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / '.local/notion-release'
JOB = 'youtube-comments-notion-summary'
RUNTIME = 'youtube-notion-report@' + PROJECT + '.iam.gserviceaccount.com'
SCHEDULER = 'youtube-scheduler@' + PROJECT + '.iam.gserviceaccount.com'
ACCOUNT = 'syan@madeone.kr'


def gcloud(args, optional=False):
    result = subprocess.run(['gcloud', *args, '--project=' + PROJECT,
                             '--account=' + ACCOUNT, '--quiet'], capture_output=True, text=True, timeout=240)
    if result.returncode and not optional:
        # The calls in this module never carry a credential in argv or stdin.
        raise RuntimeError('클라우드 단계 실패: ' + ' '.join(args[:3]) + '\n' + result.stderr[:1500])
    return result


def prepare():
    config = checked_config(read_json(ROOT / 'youtube_validation/notion_config.json'))
    private_dir(RELEASE)
    context = RELEASE / 'context'
    if context.exists():
        shutil.rmtree(context)
    package = private_dir(context / 'youtube_validation')
    for path in (ROOT / 'youtube_validation').glob('*.py'):
        if path.name not in {'release.py', 'notion_release.py', 'notion_connect.py'}:
            shutil.copy2(path, package / path.name)
    shutil.copy2(ROOT / 'youtube_validation/notion_config.json', package / 'notion_config.json')
    shutil.copy2(ROOT / 'requirements.lock.txt', context / 'requirements.txt')
    (context / 'Dockerfile').write_text('''FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY youtube_validation ./youtube_validation
ENTRYPOINT ["python", "-m", "youtube_validation.notion_report"]
''')
    image = f'{REGION}-docker.pkg.dev/{PROJECT}/containers/youtube-notion-report:' + datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    files = {str(path.relative_to(context)): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in context.rglob('*') if path.is_file()}
    write_json(RELEASE / 'release.json', {'job': JOB, 'image': image, 'source_hashes': files,
        'token_secret': config['token_secret'], 'schedule': config['schedule']})
    write_json(RELEASE / 'cloudbuild.json', {'steps': [{'name': 'gcr.io/cloud-builders/docker',
        'args': ['build', '-t', image, '.']}], 'images': [image], 'options': {'logging': 'GCS_ONLY'}})
    print(json.dumps({'prepared': True, 'files': len(files), 'image': image, 'job': JOB}))


def build():
    value = json.loads(gcloud(['builds', 'submit', str(RELEASE / 'context'),
        '--config=' + str(RELEASE / 'cloudbuild.json'), '--async', '--format=json',
        '--service-account=projects/' + PROJECT + '/serviceAccounts/sns-build@' + PROJECT + '.iam.gserviceaccount.com',
        '--gcs-log-dir=gs://' + PROJECT + '-build/logs',
        '--gcs-source-staging-dir=gs://' + PROJECT + '-build/source']).stdout)
    write_json(RELEASE / 'build.json', value)
    print(json.dumps({'build_id': value['id'], 'status': value['status']}))


def provision():
    config = checked_config(read_json(ROOT / 'youtube_validation/notion_config.json'))
    # Do not create, rotate or replace the pre-existing connection token.
    gcloud(['secrets', 'describe', config['token_secret'], '--format=value(name)'])
    existing = gcloud(['iam', 'service-accounts', 'describe', RUNTIME, '--format=json'], optional=True)
    if existing.returncode:
        gcloud(['iam', 'service-accounts', 'create', 'youtube-notion-report',
                '--display-name=YouTube Notion report'])
    elif json.loads(existing.stdout).get('displayName') != 'YouTube Notion report':
        raise RuntimeError('기존 서비스 계정의 용도가 다릅니다.')
    member = 'serviceAccount:' + RUNTIME
    exact = 'projects/_/buckets/' + BUCKET + '/objects/state/comments.sqlite3.gz'
    prefix = 'projects/_/buckets/' + BUCKET + '/objects/notion/'
    gcloud(['storage', 'buckets', 'add-iam-policy-binding', 'gs://' + BUCKET,
        '--member=' + member, '--role=roles/storage.objectViewer',
        '--condition=title=youtube-report-source-read,expression=resource.name == "' + exact + '"'])
    gcloud(['storage', 'buckets', 'add-iam-policy-binding', 'gs://' + BUCKET,
        '--member=' + member, '--role=roles/storage.objectAdmin',
        '--condition=title=youtube-report-state-write,expression=resource.name.startsWith("' + prefix + '")'])
    gcloud(['secrets', 'add-iam-policy-binding', config['token_secret'],
        '--member=' + member, '--role=roles/secretmanager.secretAccessor'])
    print(json.dumps({'runtime': RUNTIME, 'source_read_only': exact,
                      'state_write_prefix': prefix, 'existing_token_secret': config['token_secret']}))


def deploy():
    release = read_json(RELEASE / 'release.json')
    build_id = read_json(RELEASE / 'build.json')['id']
    if gcloud(['builds', 'describe', build_id, '--format=value(status)']).stdout.strip() != 'SUCCESS':
        raise RuntimeError('성공한 보고 이미지 빌드가 필요합니다.')
    existing = gcloud(['run', 'jobs', 'describe', JOB, '--region=' + REGION, '--format=json'], optional=True)
    if existing.returncode == 0 and json.loads(existing.stdout).get('metadata', {}).get('labels', {}).get('app') != 'youtube-notion-report':
        raise RuntimeError('기존 작업의 용도가 다릅니다.')
    action = 'create' if existing.returncode else 'update'
    gcloud(['run', 'jobs', action, JOB, '--region=' + REGION, '--image=' + release['image'],
        '--service-account=' + RUNTIME, '--command=python', '--args=-m,youtube_validation.notion_report',
        '--tasks=1', '--parallelism=1', '--max-retries=0', '--task-timeout=600s',
        '--memory=512Mi', '--cpu=1', '--labels=app=youtube-notion-report'])
    gcloud(['run', 'jobs', 'add-iam-policy-binding', JOB, '--region=' + REGION,
            '--member=serviceAccount:' + SCHEDULER, '--role=roles/run.invoker'])
    write_json(RELEASE / 'deployment.json', {'job': JOB, 'image': release['image'], 'runtime': RUNTIME})
    print(json.dumps({'deployed': True, 'job': JOB, 'image': release['image']}))


def execute():
    value = json.loads(gcloud(['run', 'jobs', 'execute', JOB, '--region=' + REGION,
                               '--async', '--format=json']).stdout)
    write_json(RELEASE / 'execution.json', value)
    print(json.dumps({'execution': value['metadata']['name']}))


def schedule():
    config = checked_config(read_json(ROOT / 'youtube_validation/notion_config.json'))
    url = f'https://run.googleapis.com/v2/projects/{PROJECT}/locations/{REGION}/jobs/{JOB}:run'
    existing = gcloud(['scheduler', 'jobs', 'describe', JOB, '--location=' + REGION, '--format=json'], optional=True)
    if existing.returncode == 0:
        target = json.loads(existing.stdout).get('httpTarget', {})
        if target.get('uri') != url or target.get('oauthToken', {}).get('serviceAccountEmail') != SCHEDULER:
            raise RuntimeError('기존 예약의 대상이 다릅니다.')
    action = 'create' if existing.returncode else 'update'
    gcloud(['scheduler', 'jobs', action, 'http', JOB, '--location=' + REGION,
        '--schedule=' + config['schedule'], '--time-zone=' + config['time_zone'], '--uri=' + url,
        '--http-method=POST', '--message-body={}', '--headers=Content-Type=application/json',
        '--oauth-service-account-email=' + SCHEDULER,
        '--oauth-token-scope=https://www.googleapis.com/auth/cloud-platform'])
    print(json.dumps({'scheduled': True, 'job': JOB, 'schedule': config['schedule'], 'time_zone': config['time_zone']}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='YouTube Notion 보고 전용 리소스 배포')
    parser.add_argument('action', choices=['prepare', 'build', 'provision', 'deploy', 'execute', 'schedule'])
    args = parser.parse_args()
    globals()[args.action]()
