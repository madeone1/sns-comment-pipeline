"""Choose the gcloud user for local operator tools.

The active authorized account is the default. ``--account``, the
``SNS_GCLOUD_ACCOUNT`` environment variable, or a non-empty
``config.json`` ``cloud.account`` can pin one instead. No personal
account is hard-coded. The Meta token is never read here.
"""

import os
import re
import shutil
import subprocess
import sys

from .api import PilotError


ENV_ACCOUNT = 'SNS_GCLOUD_ACCOUNT'
EMAIL = re.compile(r'^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$')


def normalize_account(value, label='Google 계정', required=False):
    """Return an email, or None when the value is empty and optional.

    Rejected input is not echoed. A pasted token must not appear in the error.
    """
    if not isinstance(value, str):
        if value is None and not required:
            return None
        raise PilotError(label + ' 형식을 확인해주세요. 이메일 주소만 지정할 수 있습니다.')
    value = value.strip()
    if not value or value == '(unset)':
        if required:
            raise PilotError(label + ' 형식을 확인해주세요. 예: josh@madeone.kr')
        return None
    if not EMAIL.fullmatch(value):
        raise PilotError(label + ' 형식을 확인해주세요. 이메일 주소만 지정할 수 있습니다. 예: josh@madeone.kr')
    return value


def gcloud_argv(args, project=None, account=None):
    """Build a gcloud command. ``--account`` is attached only when chosen."""
    command = ['gcloud', *args]
    if project:
        command.append('--project=' + project)
    if account:
        command.append('--account=' + account)
    command.append('--quiet')
    return command


def run_gcloud(args, project=None, account=None, **kwargs):
    if shutil.which('gcloud') is None:
        raise PilotError('Google Cloud CLI가 필요합니다. 설치 후 이 파일을 다시 실행해주세요. https://cloud.google.com/sdk/docs/install')
    return subprocess.run(gcloud_argv(args, project=project, account=account), capture_output=True, text=True, **kwargs)


def _accounts_from(stdout):
    found = []
    for line in (stdout or '').splitlines():
        line = line.strip()
        if EMAIL.fullmatch(line):
            found.append(line)
    return found


def _match(wanted, found):
    if not wanted:
        return None
    for email in found:
        if email.casefold() == wanted.casefold():
            return email
    return None


def _listed_accounts(project, active_only=False):
    args = ['auth', 'list', '--format=value(account)']
    if active_only:
        args = ['auth', 'list', '--filter=status:ACTIVE', '--format=value(account)']
    result = run_gcloud(args, project=project)
    if result.returncode:
        return None
    return _accounts_from(result.stdout)


def _configured_account(project):
    result = run_gcloud(['config', 'get-value', 'account'], project=project)
    if result.returncode:
        return None
    values = _accounts_from(result.stdout)
    return values[0] if values else None


def active_account(project=None):
    """Email gcloud would use when ``--account`` is omitted, or None."""
    active = _listed_accounts(project, active_only=True)
    if active is None:
        return None
    if len(active) == 1:
        return active[0]
    chosen = _configured_account(project)
    if len(active) > 1:
        return _match(chosen, active) or active[0]
    if not chosen:
        return None
    known = _listed_accounts(project, active_only=False)
    if not known:
        return None
    return _match(chosen, known)


def explicit_account(flag=None, configured=None):
    """Flag, then ``SNS_GCLOUD_ACCOUNT``, then a non-empty config value."""
    if flag is not None:
        return normalize_account(flag, '지정한 Google 계정', required=True)
    env = os.environ.get(ENV_ACCOUNT)
    if env is not None and env.strip():
        return normalize_account(env, '환경변수 SNS_GCLOUD_ACCOUNT', required=True)
    if isinstance(configured, str) and configured.strip():
        return normalize_account(configured, 'config.json cloud.account', required=True)
    return None


def resolve_operator(flag=None, configured=None, project=None):
    """Return ``(email, explicit)``.

    ``explicit`` is true when gcloud commands must pass ``--account``.
    A missing login raises before any token prompt.
    """
    override = explicit_account(flag, configured)
    if override:
        known = _listed_accounts(project, active_only=False)
        if known is None:
            raise PilotError(login_help(project, override, '지정한 계정의 로그인 상태를 확인하지 못했습니다.'))
        match = _match(override, known)
        if not match:
            raise PilotError(login_help(project, override, '지정한 Google Cloud 계정이 이 컴퓨터에 로그인되어 있지 않습니다.'))
        return match, True
    email = active_account(project)
    if not email:
        raise PilotError(login_help(project, reason='로그인된 Google Cloud 계정이 없습니다.'))
    return email, False


def login_help(project=None, account=None, reason=None):
    lines = []
    if reason:
        lines.append(reason)
    lines.append('토큰은 입력하지 않았습니다. 비밀 저장소는 바꾸지 않았습니다.')
    if account:
        lines.append('다음을 실행한 뒤 다시 시도해주세요: gcloud auth login ' + account)
    else:
        lines.append('회사 Google 계정으로 로그인한 뒤 다시 실행해주세요. 예: gcloud auth login')
    scope = ('프로젝트 ' + project + '의 ') if project else ''
    lines.append(scope + 'Secret Manager 비밀 sns-meta-token, 상태 버킷, Cloud Run Job 실행 권한이 필요합니다.')
    lines.append('계정을 지정하려면 --account 이메일 또는 환경변수 SNS_GCLOUD_ACCOUNT 를 사용할 수 있습니다.')
    return '\n'.join(lines)


def permission_help(account, project):
    who = account or '현재 로그인된 계정'
    return (
        'Google Cloud 계정 ' + who + ' 로 프로젝트 ' + project + ' 작업에 실패했습니다. '
        '비밀 저장소는 변경하지 않았습니다.\n'
        '이 계정에 Secret Manager 비밀 sns-meta-token 읽기·버전 추가, 상태 버킷 읽기·쓰기, '
        'Cloud Run Job 실행 권한이 있는지 확인해주세요.\n'
        '로그인이 없거나 만료됐으면 gcloud auth login 으로 다시 로그인한 뒤 실행해주세요. '
        '다른 계정은 환경변수 SNS_GCLOUD_ACCOUNT 또는 토큰 도구의 --account 이메일로 지정할 수 있습니다.'
    )


def announce(account, project, explicit, stream=None):
    stream = stream or sys.stdout
    kind = '지정한 계정' if explicit else '현재 활성 계정'
    print('Google Cloud 계정(' + kind + '): ' + account, file=stream, flush=True)
    print('프로젝트 ' + project + '의 Secret Manager 비밀 sns-meta-token에 이 계정으로 접근합니다.', file=stream, flush=True)
