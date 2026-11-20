"""Read-only progress and exception list. Never changes a checkpoint or receipt."""

import argparse
import json

from .api import PilotError
from .cloud_state import CloudState
from .connection_registry import REGISTRY_OBJECT, initial_registry, runtime_configs
from .gcloud_account import resolve_operator
from .moderation_polling import state_name
from .profiles import read_settings
from .token_cli import LocalTransport


def summarize(data):
    exceptions = [row for row in data.get('unavailable', {}).values() if row.get('status') == 'unavailable']
    lanes = {lane: data.get(lane, {}) for lane in ('recent', 'catalog')}
    complete = all(c.get('completed_through') and not c.get('tasks') for c in lanes.values())
    waiting = bool(data.get('queue') or data.get('webhook_pending') or data.get('last_error'))
    status = ('complete_with_exceptions' if exceptions else 'complete') if complete and not waiting else 'pending'
    return {
        'status': status, 'updated_at': data.get('updated_at'), 'last_error': data.get('last_error'),
        'queued_comments': len(data.get('queue', {})), 'webhook_pending': data.get('webhook_pending', 0),
        'lanes': {lane: {'pending_pages': len(c.get('tasks', [])),
                         'visited_pages': len(c.get('visited', [])),
                         'completed_through': c.get('completed_through'),
                         'completion_status': c.get('completion_status')}
                  for lane, c in lanes.items()},
        'unavailable_count': len(exceptions),
        'unavailable_objects': len({row['path'] for row in exceptions}),
        'exceptions': [{k: row.get(k) for k in ('source', 'lane', 'path', 'receipt', 'attempts',
                       'first_seen_at', 'last_checked_at', 'next_retry_at', 'error')}
                       for row in sorted(exceptions, key=lambda row: row['next_retry_at'])],
        'last_run': data.get('progress', {}),
    }


def main():
    parser = argparse.ArgumentParser(description='댓글 순회 진행과 조회 불가 항목을 읽기 전용으로 확인합니다.')
    parser.add_argument('--account', help='Google Cloud 로그인 계정')
    args = parser.parse_args()
    try:
        settings = read_settings()
        project = settings['cloud']['project_id']
        account, explicit = resolve_operator(args.account, settings['cloud'].get('account'), project)
        state = CloudState(project + '-state', LocalTransport(project, account, explicit))
        registry, _ = state.json_get(REGISTRY_OBJECT, initial_registry(settings))
        accounts = []
        for config in runtime_configs(settings, registry):
            if not config.get('account_id'):
                continue
            data, _ = state.json_get(state_name(config['account_id']), {})
            accounts.append({'account': config['expected_username'], **summarize(data)})
        print(json.dumps({'accounts': accounts}, ensure_ascii=False, indent=2))
        return 0
    except PilotError as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
