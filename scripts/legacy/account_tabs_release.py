# Historical release tooling. See scripts/legacy/README.md before use.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
"""Deploy account-owned Sheets history while preserving collection/report rules."""

import argparse
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import shutil
import ssl
import urllib.request

from cloud_common import PROJECT, REGION, ROOT, gcloud

RELEASE = ROOT / '.local/account-tabs-release'
PIPELINE = 'sns-pipeline@' + PROJECT + '.iam.gserviceaccount.com'
WEB = 'sns-web@' + PROJECT + '.iam.gserviceaccount.com'
ROLE = 'projects/' + PROJECT + '/roles/snsSheetsTokenMinter'
SHEET = '184SNRc-z2MfVKcsaKeZcR9kMrYBv1l5Y8JlswXs6210'


def save(name, value):
    (RELEASE / (name + '.json')).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding="utf-8")


def read(name):
    return json.loads((RELEASE / (name + '.json')).read_text(encoding="utf-8"))


def prepare():
    baseline = read('config-before')
    settings = copy.deepcopy(baseline)
    settings['workflow'] = {'enabled': False, 'policy_version': 'v2.0', 'google_sheets': {
        'enabled': True, 'spreadsheet_id': SHEET, 'owner_email': 'syan@madeone.kr',
        'auth_mode': 'service_account', 'service_account': PIPELINE}, 'google_drive': {'enabled': False}}
    assert {k: v for k, v in settings.items() if k != 'workflow'} == baseline
    folder = RELEASE / 'context'
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / 'sns_comment', folder / 'sns_comment', dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for name in ['Dockerfile', 'Dockerfile.web', 'web-requirements.txt', '.dockerignore', '.gcloudignore']:
        shutil.copy2(ROOT / name, folder / name)
    (folder / 'config.json').write_text(json.dumps(settings, ensure_ascii=False, indent=2) + '\n', encoding="utf-8")
    tag = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S') + '-account-tabs'
    base = REGION + '-docker.pkg.dev/' + PROJECT + '/containers/'
    images = {'worker': base + 'sns-comments:' + tag, 'web': base + 'sns-comments-web:' + tag}
    save('release', {'images': images, 'admins': ['syan@madeone.kr'], 'service': 'sns-comments-web',
        'files': {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in sorted(folder.rglob('*')) if p.is_file()},
        'config_change': 'Sheets projection only; collection and Notion rules preserved'})
    save('cloudbuild', {'steps': [{'name': 'gcr.io/cloud-builders/docker', 'args':
        ['build', '-f', dockerfile, '-t', images[kind], '.']}
        for kind, dockerfile in [('worker', 'Dockerfile'), ('web', 'Dockerfile.web')]],
        'images': list(images.values()), 'options': {'logging': 'GCS_ONLY'}})
    save('permission-plan', {'sheet_id': SHEET, 'sheet_editor': PIPELINE,
        'new_apis': ['sheets.googleapis.com', 'iap.googleapis.com', 'cloudresourcemanager.googleapis.com'],
        'token_role': {'name': ROLE, 'permissions': ['iam.serviceAccounts.getAccessToken'],
                       'resource': PIPELINE, 'member': 'serviceAccount:' + PIPELINE},
        'web_identity': WEB, 'web_users': ['syan@madeone.kr'],
        'storage': [{'role': 'roles/storage.objectViewer', 'scope': 'existing state bucket'},
                    {'role': 'roles/storage.objectUser', 'scope': 'web/ only'}],
        'secrets': [{'name': 'sns-meta-token', 'role': 'roles/secretmanager.secretAccessor', 'member': WEB},
                    {'name': 'sns-web-meta-connections', 'role': 'roles/secretmanager.secretAccessor', 'member': WEB},
                    {'name': 'sns-web-meta-connections', 'role': 'roles/secretmanager.secretVersionAdder', 'member': WEB},
                    {'name': 'sns-web-meta-connections', 'role': 'roles/secretmanager.secretAccessor', 'member': PIPELINE}],
        'public_access': False, 'service_account_keys_created': False,
        'drive_archive_enabled': False, 'schedule_change': False})
    print(json.dumps({'prepared': True, 'images': images, 'permission_plan': str(RELEASE / 'permission-plan.json')}))


def build():
    info = json.loads(gcloud(['builds', 'submit', str(RELEASE / 'context'), '--config=' + str(RELEASE / 'cloudbuild.json'),
        '--service-account=projects/' + PROJECT + '/serviceAccounts/sns-build@' + PROJECT + '.iam.gserviceaccount.com',
        '--gcs-log-dir=gs://' + PROJECT + '-build/logs', '--gcs-source-staging-dir=gs://' + PROJECT + '-build/source',
        '--async', '--format=json']).stdout)
    save('build', info)
    print(json.dumps({'id': info['id'], 'status': info['status']}))


def configure():
    gcloud(['services', 'enable', 'sheets.googleapis.com', 'iap.googleapis.com', 'cloudresourcemanager.googleapis.com'])
    existing = gcloud(['iam', 'roles', 'describe', 'snsSheetsTokenMinter', '--format=json'], optional=True)
    if existing.returncode:
        gcloud(['iam', 'roles', 'create', 'snsSheetsTokenMinter', '--title=SNS Sheets scoped token minter',
            '--permissions=iam.serviceAccounts.getAccessToken', '--stage=GA'])
    else:
        assert json.loads(existing.stdout)['includedPermissions'] == ['iam.serviceAccounts.getAccessToken']
    gcloud(['iam', 'service-accounts', 'add-iam-policy-binding', PIPELINE,
        '--member=serviceAccount:' + PIPELINE, '--role=' + ROLE, '--condition=None'])
    if gcloud(['iam', 'service-accounts', 'describe', WEB], optional=True).returncode:
        gcloud(['iam', 'service-accounts', 'create', 'sns-web', '--display-name=마케팅 댓글 웹앱'])
    bucket = 'gs://' + PROJECT + '-state'
    if not (RELEASE / 'bucket-iam-before.json').exists():
        save('bucket-iam-before', json.loads(gcloud(['storage', 'buckets', 'get-iam-policy', bucket, '--format=json']).stdout))
    gcloud(['storage', 'buckets', 'add-iam-policy-binding', bucket,
        '--member=serviceAccount:' + WEB, '--role=roles/storage.objectViewer', '--condition=None'])
    condition = 'expression=resource.name.startsWith("projects/_/buckets/' + PROJECT + '-state/objects/web/"),title=sns_web_state'
    gcloud(['storage', 'buckets', 'add-iam-policy-binding', bucket,
        '--member=serviceAccount:' + WEB, '--role=roles/storage.objectUser', '--condition=' + condition])
    if gcloud(['secrets', 'describe', 'sns-web-meta-connections'], optional=True).returncode:
        gcloud(['secrets', 'create', 'sns-web-meta-connections', '--replication-policy=automatic', '--labels=app=sns-web'])
    for secret, identity, role in [('sns-meta-token', WEB, 'roles/secretmanager.secretAccessor'),
            ('sns-web-meta-connections', WEB, 'roles/secretmanager.secretAccessor'),
            ('sns-web-meta-connections', WEB, 'roles/secretmanager.secretVersionAdder'),
            ('sns-web-meta-connections', PIPELINE, 'roles/secretmanager.secretAccessor')]:
        gcloud(['secrets', 'add-iam-policy-binding', secret, '--member=serviceAccount:' + identity, '--role=' + role, '--condition=None'])
    # Use the public Service Usage API; no optional gcloud beta installation.
    number = gcloud(['projects', 'describe', PROJECT, '--format=value(projectNumber)']).stdout.strip()
    token = gcloud(['auth', 'print-access-token']).stdout.strip()
    request = urllib.request.Request('https://serviceusage.googleapis.com/v1beta1/projects/' + number
        + '/services/iap.googleapis.com:generateServiceIdentity', data=b'{}',
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}, method='POST')
    context = ssl.create_default_context()
    if not context.get_ca_certs():
        context.load_verify_locations(cafile='/etc/ssl/cert.pem')
    with urllib.request.urlopen(request, context=context, timeout=60) as response:
        response.read()
    print(json.dumps({'configured': True, 'sheet_sharing_still_required': PIPELINE}))


def seed():
    import sys
    sys.path.insert(0, str(ROOT))
    from sns_comment.web_registry import initial_registry
    registry = initial_registry(json.loads((RELEASE / 'context/config.json').read_text(encoding="utf-8")))
    save('registry-seed', registry)
    # The generation guard forbids replacing a registry created meanwhile.
    result = gcloud(['storage', 'cp', str(RELEASE / 'registry-seed.json'),
        'gs://' + PROJECT + '-state/web/registry.json', '--if-generation-match=0'], optional=True)
    if result.returncode and '412' not in result.stderr:
        raise SystemExit(result.stderr)
    print(json.dumps({'registry_seeded_or_preserved': True, 'accounts': len(registry['accounts'])}))


def deploy():
    release = read('release')
    status = gcloud(['builds', 'describe', read('build')['id'], '--format=value(status)']).stdout.strip()
    if status != 'SUCCESS':
        raise SystemExit('빌드 상태: ' + status)
    number = gcloud(['projects', 'describe', PROJECT, '--format=value(projectNumber)']).stdout.strip()
    env = {'GOOGLE_CLOUD_PROJECT': PROJECT, 'SNS_STATE_BUCKET': PROJECT + '-state',
        'SNS_WEB_ADMINS': ','.join(release['admins']),
        'SNS_WEB_IAP_AUDIENCE': f'/projects/{number}/locations/{REGION}/services/sns-comments-web',
        'SNS_WEB_ORIGIN': 'https://deployment-pending.invalid'}
    env_file = RELEASE / 'web-env.yaml'
    env_file.write_text('\n'.join(k + ': ' + json.dumps(v) for k, v in env.items()) + '\n', encoding="utf-8")
    gcloud(['run', 'deploy', 'sns-comments-web', '--region=' + REGION, '--image=' + release['images']['web'],
        '--service-account=' + WEB, '--cpu=1', '--memory=512Mi', '--min-instances=0', '--max-instances=2',
        '--concurrency=8', '--timeout=120s', '--cpu-throttling', '--no-allow-unauthenticated', '--iap',
        '--env-vars-file=' + str(env_file)])
    service = json.loads(gcloud(['run', 'services', 'describe', 'sns-comments-web', '--region=' + REGION, '--format=json']).stdout)
    url = service['status']['url']
    gcloud(['run', 'services', 'update', 'sns-comments-web', '--region=' + REGION, '--update-env-vars=SNS_WEB_ORIGIN=' + url])
    gcloud(['run', 'services', 'add-iam-policy-binding', 'sns-comments-web', '--region=' + REGION,
        '--member=serviceAccount:service-' + number + '@gcp-sa-iap.iam.gserviceaccount.com', '--role=roles/run.invoker'])
    for email in release['admins']:
        gcloud(['iap', 'web', 'add-iam-policy-binding', '--member=user:' + email, '--role=roles/iap.httpsResourceAccessor',
            '--region=' + REGION, '--resource-type=cloud-run', '--service=sns-comments-web'])
    before = json.loads(gcloud(['run', 'jobs', 'describe', 'sns-comments-daily', '--region=' + REGION, '--format=json']).stdout)
    save('job-immediately-before', before)
    gcloud(['run', 'jobs', 'update', 'sns-comments-daily', '--region=' + REGION, '--image=' + release['images']['worker']])
    after = json.loads(gcloud(['run', 'jobs', 'describe', 'sns-comments-daily', '--region=' + REGION, '--format=json']).stdout)
    expected = copy.deepcopy(before['spec']['template']['spec']['template']['spec'])
    expected['containers'][0]['image'] = release['images']['worker']
    assert expected == after['spec']['template']['spec']['template']['spec']
    save('job-after', after)
    save('deployment', {'url': url, 'images': release['images'], 'job_settings_preserved': True})
    print(json.dumps(read('deployment')))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'build', 'configure', 'seed', 'deploy'])
    globals()[parser.parse_args().action]()
