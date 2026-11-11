"""Build a concrete webhook release; never applies IAM or deploys implicitly."""

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import shutil
import secrets
import subprocess

from cloud_common import PROJECT, REGION, gcloud

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT/'.local/webhook-release'


def prepare(config):
    settings = json.loads(Path(config).read_text())
    if settings.get('cloud', {}).get('project_id') != PROJECT:
        raise SystemExit('기존 운영 설정을 지정해주세요.')
    baseline = json.loads(json.dumps(settings))
    settings['cloud']['webhook_enabled'] = True
    folder = RELEASE/'context'
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT/'sns_comment', folder/'sns_comment', dirs_exist_ok=True,
        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for name in ('Dockerfile', 'Dockerfile.webhook', '.dockerignore', '.gcloudignore'):
        shutil.copy2(ROOT/name, folder/name)
    (folder/'config.json').write_text(json.dumps(settings, ensure_ascii=False, indent=2)+'\n')
    recovered = json.loads(json.dumps(settings))
    if 'webhook_enabled' in baseline['cloud']:
        recovered['cloud']['webhook_enabled'] = baseline['cloud']['webhook_enabled']
    else:
        recovered['cloud'].pop('webhook_enabled')
    if recovered != baseline:
        raise SystemExit('웹훅 활성화 외에 기존 운영 설정이 달라졌습니다.')
    tag = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S')+'-webhook'
    base = REGION+'-docker.pkg.dev/'+PROJECT+'/containers/'
    images = {'receiver': base+'sns-comments-webhook:'+tag,
        'worker': base+'sns-comments:'+tag}
    steps = [{'name': 'gcr.io/cloud-builders/docker', 'args': ['build', '-f', dockerfile, '-t', images[kind], '.']}
        for kind, dockerfile in [('receiver', 'Dockerfile.webhook'), ('worker', 'Dockerfile')]]
    (RELEASE/'cloudbuild.json').write_text(json.dumps({'steps': steps, 'images': list(images.values()),
        'options': {'logging': 'GCS_ONLY'}}, indent=2))
    manifest = {'project': PROJECT, 'region': REGION, 'service': 'sns-comments-webhook', 'images': images,
        'config_source': str(Path(config).resolve()), 'config_change': 'cloud.webhook_enabled=true only',
        'source_hashes': {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob('*')) if p.is_file()}}
    (RELEASE/'release.json').write_text(json.dumps(manifest, indent=2))
    bucket = PROJECT+'-state'
    prefix = 'projects/_/buckets/'+bucket+'/objects/'
    identity = 'serviceAccount:sns-webhook@'+PROJECT+'.iam.gserviceaccount.com'
    plan = {'status': 'prepared_only_not_applied', 'service_identity': identity,
        'permissions': [
            {'resource': 'gs://'+bucket, 'role': 'roles/storage.objectViewer', 'member': identity,
                'condition': 'resource.name == "'+prefix+'web/registry.json" || resource.name.startsWith("'+prefix+'webhook/done/")'},
            {'resource': 'gs://'+bucket, 'role': 'roles/storage.objectCreator', 'member': identity,
                'condition': 'resource.name.startsWith("'+prefix+'webhook/pending/")'},
            *[{'resource': secret, 'role': 'roles/secretmanager.secretAccessor', 'member': identity}
                for secret in ('sns-meta-app-secret', 'sns-webhook-verify-token')],
            {'resource': 'Cloud Run sns-comments-webhook only', 'role': 'roles/run.invoker', 'member': 'allUsers'}],
        'service': {'min_instances': 0, 'max_instances': 2, 'cpu': 1, 'memory': '256Mi',
            'concurrency': 8, 'timeout': '60s', 'ingress': 'all', 'iap': False,
            'path': '/webhooks/instagram'},
        'log_exclusion': {'name': 'sns-webhook-verification', 'sink': '_Default',
            'filter': 'resource.type="cloud_run_revision" AND resource.labels.service_name="sns-comments-webhook" '
                'AND log_id("run.googleapis.com/requests") AND httpRequest.requestMethod="GET" '
                'AND httpRequest.requestUrl:"/webhooks/instagram"',
            'reason': 'Meta verification places the verify token in the query string; keep POST request metrics and audit logs.'},
        'worker': {'job': 'sns-comments-daily', 'change': 'image only; existing 08h scheduler and secrets unchanged'},
        'meta': {'candidate_app_id': '1607262264749356', 'candidate_app_name': 'SNS Comments Analyze',
            'current_callback': 'empty, visually verified 2026-11-10', 'field': 'comments',
            'required_before_subscription': 'Confirm production token app ID, login route and account asset access; no subscription written.'},
        'hosted_management_required': False}
    (RELEASE/'permission-plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2))
    print(json.dumps({'release': str(RELEASE), 'images': images, 'config_preserved': True,
        'permission_plan': str(RELEASE/'permission-plan.json')}, ensure_ascii=False, indent=2))


def build():
    info = json.loads(gcloud(['builds', 'submit', str(RELEASE/'context'), '--config='+str(RELEASE/'cloudbuild.json'),
        '--service-account=projects/'+PROJECT+'/serviceAccounts/sns-build@'+PROJECT+'.iam.gserviceaccount.com',
        '--gcs-log-dir=gs://'+PROJECT+'-build/logs', '--gcs-source-staging-dir=gs://'+PROJECT+'-build/source',
        '--async', '--format=json']).stdout)
    (RELEASE/'build.json').write_text(json.dumps(info, indent=2))
    print(json.dumps({'id': info['id'], 'status': info['status']}))


def configure():
    """Apply only the four scopes explicitly approved in the chat."""
    plan = json.loads((RELEASE/'permission-plan.json').read_text())
    if not (RELEASE/'bucket-iam-before.json').exists():
        (RELEASE/'bucket-iam-before.json').write_text(gcloud(['storage', 'buckets', 'get-iam-policy',
            'gs://'+PROJECT+'-state', '--format=json']).stdout)
    if not (RELEASE/'log-sinks-before.json').exists():
        (RELEASE/'log-sinks-before.json').write_text(gcloud(['logging', 'sinks', 'list', '--format=json']).stdout)
    identity = 'sns-webhook@'+PROJECT+'.iam.gserviceaccount.com'
    if gcloud(['iam', 'service-accounts', 'describe', identity], optional=True).returncode:
        gcloud(['iam', 'service-accounts', 'create', 'sns-webhook', '--display-name=SNS comment webhook receiver'])
    for permission in plan['permissions'][:2]:
        gcloud(['storage', 'buckets', 'add-iam-policy-binding', permission['resource'],
            '--member='+permission['member'], '--role='+permission['role'],
            '--condition=expression='+permission['condition']+',title=sns_webhook_'+permission['role'].split('.')[-1]])
    for name in ('sns-meta-app-secret', 'sns-webhook-verify-token'):
        if gcloud(['secrets', 'describe', name], optional=True).returncode:
            gcloud(['secrets', 'create', name, '--replication-policy=automatic', '--labels=app=sns-webhook'])
        gcloud(['secrets', 'add-iam-policy-binding', name, '--member=serviceAccount:'+identity,
            '--role=roles/secretmanager.secretAccessor'])
    # Random verify token goes directly to Secret Manager over stdin.
    current = gcloud(['secrets', 'versions', 'list', 'sns-webhook-verify-token',
        '--filter=state=ENABLED', '--limit=1', '--format=value(name)']).stdout.strip()
    if not current:
        token = secrets.token_urlsafe(48)
        result = subprocess.run(['gcloud', 'secrets', 'versions', 'add', 'sns-webhook-verify-token', '--data-file=-',
            '--project='+PROJECT, '--account=syan@madeone.kr', '--quiet'], input=token, capture_output=True, text=True)
        if result.returncode:
            raise SystemExit('웹훅 Verify Token 보관 실패. 비밀 값은 출력하지 않았습니다.')
    exclusion = plan['log_exclusion']
    sink = json.loads(gcloud(['logging', 'sinks', 'describe', '_Default', '--format=json']).stdout)
    action = 'update' if any(e['name'] == exclusion['name'] for e in sink.get('exclusions', [])) else 'add'
    gcloud(['logging', 'sinks', 'update', '_Default', '--'+action+'-exclusion=name='+exclusion['name']+
        ',filter='+exclusion['filter']+',description=Exclude only webhook GET verification URLs containing verify credentials'])
    print(json.dumps({'status': 'configured', 'identity': identity, 'verify_token_stored': True,
        'app_secret_value_required': True, 'deployed': False}))


def deploy_worker():
    manifest = json.loads((RELEASE/'release.json').read_text())
    build_id = json.loads((RELEASE/'build.json').read_text())['id']
    if gcloud(['builds', 'describe', build_id, '--format=value(status)']).stdout.strip() != 'SUCCESS':
        raise SystemExit('검증된 빌드가 완료되지 않았습니다.')
    before = json.loads(gcloud(['run', 'jobs', 'describe', 'sns-comments-daily', '--region='+REGION, '--format=json']).stdout)
    if not (RELEASE/'job-before.json').exists():
        (RELEASE/'job-before.json').write_text(json.dumps(before, indent=2))
    gcloud(['run', 'jobs', 'update', 'sns-comments-daily', '--region='+REGION, '--image='+manifest['images']['worker']])
    after = json.loads(gcloud(['run', 'jobs', 'describe', 'sns-comments-daily', '--region='+REGION, '--format=json']).stdout)
    expected = before['spec']['template']['spec']['template']['spec']
    observed = after['spec']['template']['spec']['template']['spec']
    expected['containers'][0]['image'] = manifest['images']['worker']
    if expected != observed:
        raise SystemExit('이미지 외 작업 설정이 달라졌습니다. 실행하지 말고 구성을 대조해주세요.')
    (RELEASE/'job-after.json').write_text(json.dumps(after, indent=2))
    print(json.dumps({'worker_updated': True, 'image_only_verified': True, 'executed': False}))


def deploy_receiver():
    manifest = json.loads((RELEASE/'release.json').read_text())
    versions = gcloud(['secrets', 'versions', 'list', 'sns-meta-app-secret', '--filter=state=ENABLED',
        '--limit=1', '--format=value(name)']).stdout.strip()
    if not versions:
        raise SystemExit('App Secret을 안전한 입력으로 먼저 등록해주세요.')
    gcloud(['run', 'deploy', 'sns-comments-webhook', '--region='+REGION,
        '--image='+manifest['images']['receiver'], '--service-account=sns-webhook@'+PROJECT+'.iam.gserviceaccount.com',
        '--cpu=1', '--memory=256Mi', '--min-instances=0', '--max-instances=2', '--concurrency=8', '--timeout=60s',
        '--cpu-throttling', '--ingress=all', '--allow-unauthenticated',
        '--set-env-vars=SNS_STATE_BUCKET='+PROJECT+'-state',
        '--set-secrets=SNS_META_APP_SECRET=sns-meta-app-secret:latest,SNS_WEBHOOK_VERIFY_TOKEN=sns-webhook-verify-token:latest'])
    service = json.loads(gcloud(['run', 'services', 'describe', 'sns-comments-webhook', '--region='+REGION, '--format=json']).stdout)
    (RELEASE/'receiver-service.json').write_text(json.dumps(service, indent=2))
    result = {'service': 'sns-comments-webhook', 'url': service['status']['url'],
        'callback_url': service['status']['url']+'/webhooks/instagram', 'receiver_deployed': True, 'meta_registered': False}
    (RELEASE/'deployment.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'build', 'configure', 'deploy-worker', 'deploy-receiver'])
    parser.add_argument('--config', type=Path, default=ROOT/'config.json')
    args = parser.parse_args()
    {'prepare': lambda: prepare(args.config), 'build': build, 'configure': configure,
        'deploy-worker': deploy_worker, 'deploy-receiver': deploy_receiver}[args.action]()
