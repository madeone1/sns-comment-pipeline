"""Read-only fingerprints of the existing jobs and schedules."""

import hashlib
import json
import subprocess

PROJECT = 'waterbomb-sns-pipeline'
REGION = 'asia-northeast3'
ACCOUNT = 'syan@madeone.kr'
EXISTING = ('sns-comments-daily', 'notion-daily-merge')


def snapshot():
    result = {'project': PROJECT, 'region': REGION, 'jobs': {}, 'schedulers': {}}
    for name in EXISTING:
        for kind, args in (
            ('jobs', ['run', 'jobs', 'describe', name, '--region=' + REGION]),
            ('schedulers', ['scheduler', 'jobs', 'describe', name, '--location=' + REGION])):
            process = subprocess.run(['gcloud', *args, '--project=' + PROJECT,
                '--account=' + ACCOUNT, '--format=json', '--quiet'],
                capture_output=True, text=True, timeout=60)
            if process.returncode:
                raise RuntimeError('기존 클라우드 설정 조회 실패: ' + kind + '/' + name)
            data = json.loads(process.stdout)
            if kind == 'jobs':
                # Status and last executions can change during normal operation.
                config = {'spec': data['spec']}
            else:
                config = {key: data[key] for key in ('name', 'schedule', 'timeZone',
                    'httpTarget', 'pubsubTarget', 'retryConfig', 'attemptDeadline') if key in data}
            digest = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
            result[kind][name] = {'configuration_sha256': digest}
    return result
