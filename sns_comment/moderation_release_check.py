"""Read-only candidate verification for moderation; no legacy daily report needed."""

import copy
import datetime as dt
import json
import os
import time

from .api import GraphClient, PilotError
from .cloud_state import CloudState, NOTION_OAUTH_LEASE, lease_retry
from .connection_registry import REGISTRY_OBJECT, runtime_configs
from .mcp import McpClient
from .moderation_job import comment_from_payload, open_sheet
from .moderation_polling import Polling, state_name
from .moderation_sheet import HIDE_TAB, REVIEW_TAB, _read_rows
from .notion_oauth import cloud_credentials
from .profiles import read_settings
from .token_connections import read_token, source_secret


def check_settings(settings):
    mod = settings.get('moderation') or {}
    interval = int(mod.get('interval_minutes') or 0)
    if interval != 30 or int(mod.get('max_age_minutes') or 0) < 2 * interval:
        raise PilotError('30분 주기와 60분 이상 중복 조회 범위가 필요합니다.')
    if settings.get('cloud', {}).get('daily_enabled') is not False or mod.get('run_with_daily'):
        raise PilotError('종료한 daily 작업의 실행 경로가 남아 있습니다.')
    if not 1 <= int(mod.get('poll_workers') or 0) <= 8 or not 1 <= int(mod.get('poll_requests_per_account') or 0) <= 240:
        raise PilotError('댓글 조회의 병렬 수와 계정별 요청 상한을 확인해주세요.')


class MemoryState:
    def __init__(self):
        self.values = {}
    def json_get(self, name, default=None):
        return copy.deepcopy(self.values.get(name, default)), 0
    def json_put(self, name, value, generation=None):
        self.values[name] = copy.deepcopy(value)
        return 0
    def list_all(self, prefix):
        return [{'name': name, 'generation': 0} for name in self.values if name.startswith(prefix)]


def verify(settings, shared):
    check_settings(settings)
    for flag in ('SNS_MODERATION_HIDE', 'SNS_HUMAN_DECISION_HIDE', 'SNS_NOTION_DECISIONS'):
        if os.environ.get(flag) == '1':
            raise PilotError('검증 작업에는 운영 쓰기 스위치를 넣지 않습니다.')
    registry, generation = shared.json_get(REGISTRY_OBJECT)
    configs = runtime_configs(settings, registry)
    if not configs:
        raise PilotError('활성 계정이 없습니다.')
    accounts = []
    for config in configs:
        reference = config.get('_credential_secret_version') or source_secret(settings) + '/versions/latest'
        token, _ = read_token(shared.transport, reference)
        client = GraphClient(token, settings['api_version'], max_requests=96,
                             api_host=config.get('_meta_api_host', 'graph.facebook.com'))
        del token
        identity = client.get(config['account_id'], fields='id,username')
        if identity.get('id') != config['account_id']:
            raise PilotError('운영 계정 ID 대조가 실패했습니다.')
        memory = MemoryState()
        polling = Polling(memory, config['account_id'], config['expected_username'],
                          {**settings['moderation'], 'media_limit': 1},
                          dt.datetime.now(dt.timezone.utc), comment_from_payload)
        polling.scan(client, 'recent', 2)
        if polling.error:
            raise polling.error
        # Exercise the candidate against a COPY of the live pagination. No
        # production cursor, queue or exception record can be changed here.
        checkpoint, _ = shared.json_get(state_name(config['account_id']), {})
        memory.values[state_name(config['account_id'])] = copy.deepcopy(checkpoint)
        failures = list(checkpoint.get('receipt_failures', {}))[:12]
        for name in failures:
            event, _ = shared.json_get(name)
            if isinstance(event, dict):
                memory.values[name] = copy.deepcopy(event)
        resumed = Polling(memory, config['account_id'], config['expected_username'],
                          settings['moderation'], dt.datetime.now(dt.timezone.utc), comment_from_payload)
        began = time.monotonic()
        resumed.webhooks(client, limit=12)
        resumed.scan(client, 'catalog', 32)
        if resumed.error:
            raise resumed.error
        accounts.append({'id': config['account_id'], 'username': config['expected_username'],
                         'api_reads': client.request_count,
                         'queue_simulated': len(polling.data['queue']),
                         'resume_pages': resumed.coverage()['catalog_pages_read'],
                         'resume_seconds': round(time.monotonic() - began, 3),
                         'resume_unavailable': resumed.coverage()['unavailable_count'],
                         'unavailable_receipts_checked': len(failures),
                         'peak_concurrent_reads': client.metrics()['peak_concurrent_requests']})
    sheet = open_sheet(settings, shared)
    if sheet is None:
        raise PilotError('댓글 관리 기준표를 읽지 못했습니다.')
    rows = {title: len(_read_rows(sheet, title)) for title in (HIDE_TAB, REVIEW_TAB)}
    with lease_retry(shared, 60, NOTION_OAUTH_LEASE, attempts=4, wait_seconds=2):
        auth = cloud_credentials(shared.transport, os.environ['SNS_NOTION_OAUTH_SECRET'])
        auth.refresh()
    mcp = McpClient(auth)
    mcp.call('fetch', {'id': settings['moderation']['notion']['parent_page_id']})
    unchanged, current_generation = shared.json_get(REGISTRY_OBJECT)
    if current_generation != generation or unchanged != registry:
        raise PilotError('검증 중 계정 연결이 변경됐습니다. 새 상태에서 다시 검증해주세요.')
    return {'status': 'verified', 'accounts': accounts, 'sheet_rows': rows,
            'notion_read': True, 'polling_writes': 0, 'registry_writes': 0,
            'sheet_writes': 0, 'notion_content_writes': 0, 'model_calls': 0, 'instagram_writes': 0}


def main():
    settings = read_settings()
    shared = CloudState(os.environ['SNS_STATE_BUCKET'])
    result = verify(settings, shared)
    result['image'] = os.environ['SNS_RELEASE_IMAGE']
    result['checked_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
    key = 'verification/moderation/candidates/' + result['image'].rsplit(':', 1)[-1] + '.json'
    shared.json_put(key, result)
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
