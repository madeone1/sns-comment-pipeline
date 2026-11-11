"""Account identities and history, independent of any hosted interface."""

import copy
import re

from .api import PilotError
from .profiles import select, username

REGISTRY_OBJECT = 'web/registry.json'
GROUP_ID = re.compile(r'[a-z][a-z0-9_-]{0,31}')
ACCOUNT_ID = re.compile(r'[0-9]{5,30}')
SECRET_VERSION = re.compile(r'projects/[a-z0-9-]+/secrets/(?:sns-meta-token|sns-web-meta-[a-z0-9_-]+)/versions/[1-9][0-9]*')


def initial_registry(settings):
    result = {'schema': 1, 'groups': {}, 'accounts': {}, 'audit': []}
    primary = settings['expected_username'].lstrip('@')
    labels = {'waterbomb_official': '대표(공식)', 'waterbomb_busan_official': '부산', 'waterbomb_sokcho_official': '속초'}
    scheduled = settings.get('cloud', {}).get('scheduled_accounts', [primary])
    for name in [primary, *settings.get('accounts', {})]:
        config = select(settings, name)
        if not config.get('account_id'):
            continue
        key = settings.get('accounts', {}).get(name, {}).get('meta_credential', 'primary' if name == primary else name)
        group = key or name
        result['groups'].setdefault(group, {'id': group, 'label': '기존 운영 연결' if group == 'primary' else group,
            'secret_version': None, 'legacy': group == 'primary', 'verified_at': None})
        result['accounts'][config['account_id']] = {'id': config['account_id'], 'username': name,
            'label': labels.get(name, name), 'group_id': group, 'active': bool(name in scheduled and config.get('enabled', True)),
            'source_prefix': config.get('_cloud_prefix', ''), 'verified_at': config.get('verified_at')}
    return result


def checked_account(value):
    if not isinstance(value, dict) or not isinstance(value.get('id'), str) or not ACCOUNT_ID.fullmatch(value.get('id', '')):
        raise PilotError('Instagram 계정 ID를 확인해주세요.')
    name = username(value.get('username'))
    label = value.get('label', name)
    if not isinstance(label, str) or not 1 <= len(label.strip()) <= 50:
        raise PilotError('지역 이름은 1~50자로 입력해주세요.')
    return {'id': value['id'], 'username': name, 'label': label.strip()}


def runtime_configs(settings, registry):
    """Snapshot pointers once per scheduled run; never mutate process env vars."""
    if not isinstance(registry, dict) or registry.get('schema') != 1:
        raise PilotError('앱의 수집 계정 설정 형식을 확인해주세요.')
    configs = []
    seen_names = set()
    seen_prefixes = set()
    roster = []
    for identifier, account in registry.get('accounts', {}).items():
        verified = checked_account(account)
        if (identifier != verified['id'] or verified['username'] in seen_names
                or not isinstance(account.get('active'), bool)):
            raise PilotError('앱의 수집 계정이 중복되었거나 ID가 다릅니다.')
        seen_names.add(verified['username'])
        prefix = account.get('source_prefix')
        if not isinstance(prefix, str) or (prefix and not re.fullmatch(r'accounts/(?:[a-z0-9_][a-z0-9_.]{0,29}|ig_[0-9]{5,30})', prefix)):
            raise PilotError('계정별 원본 저장 경로를 확인해주세요.')
        if prefix in seen_prefixes:
            raise PilotError('서로 다른 계정이 같은 원본 저장 경로를 사용할 수 없습니다.')
        seen_prefixes.add(prefix)
        roster.append({**verified, 'active': account['active']})
        if account['active'] is not True:
            continue
        group = registry.get('groups', {}).get(account.get('group_id'))
        if not group:
            raise PilotError('수집 계정의 연결 정보가 없습니다.')
        config = select(settings, settings['expected_username'])
        config.update(account_id=identifier, expected_username=verified['username'], _account=verified['username'],
            region_label=verified['label'], _cloud_prefix=prefix, enabled=True)
        config['meta_auth'] = 'facebook_login'
        config['_meta_api_host'] = 'graph.facebook.com'
        version = group.get('secret_version')
        if version:
            if not SECRET_VERSION.fullmatch(version):
                raise PilotError('앱 연결의 비밀 저장소 참조 형식이 다릅니다.')
            config['_web_secret_version'] = version
            config['_credential_secret_version'] = version
        elif not group.get('legacy'):
            raise PilotError('검증된 연결 정보가 없는 수집 계정입니다.')
        configs.append(config)
    for config in configs:
        config['_sheet_roster'] = copy.deepcopy(roster)
        config['_report_roster'] = copy.deepcopy(roster)
        config['_connection_revision'] = registry.get('token_control', {}).get('revision')
    return configs


def apply_group(registry, group, accounts, actor, instant):
    result = copy.deepcopy(registry)
    group_id = group['id']
    chosen = {a['id'] for a in accounts}
    # Keep complete connection transitions privately. Public Sheets only carry
    # account IDs; credential values and Secret Manager pointers stay here.
    history = result.setdefault('connection_history', [])
    previous = result['groups'].get(group_id)
    if previous and not any(h['group_id'] == group_id for h in history):
        history.append({'at': previous.get('verified_at'), 'by': None, 'action': 'existing_connection',
            'group_id': group_id, 'secret_version': previous.get('secret_version'),
            'account_ids': sorted(a['id'] for a in result['accounts'].values()
                                  if a['group_id'] == group_id and a['active'])})
    for old in result['accounts'].values():
        if old['group_id'] == group_id and old['id'] not in chosen:
            old['active'] = False
    for account in accounts:
        old = result['accounts'].get(account['id'], {})
        result['accounts'][account['id']] = {**old, **account, 'group_id': group_id, 'active': True,
            'source_prefix': old.get('source_prefix', 'accounts/ig_' + account['id']), 'verified_at': instant}
    result['groups'][group_id] = {**group, 'verified_at': instant}
    history.append({'at': instant, 'by': actor, 'action': 'apply_connection', 'group_id': group_id,
        'secret_version': group.get('secret_version'), 'account_ids': sorted(chosen)})
    result['audit'] = [*result.get('audit', []), {'at': instant, 'by': actor, 'action': 'apply_connection',
        'group_id': group_id, 'account_ids': sorted(chosen)}][-100:]
    return result
