"""Approved Meta callback/account registration; credentials stay in memory."""

import argparse
import hashlib
import hmac
import json
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sns_comment.api import NoRedirect, https_context, PilotError
from sns_comment.gcloud_account import permission_help, resolve_operator, run_gcloud
from sns_comment.profiles import select

RELEASE = ROOT/'.local/webhook-release'
PROJECT = 'waterbomb-sns-pipeline'
APP_ID = '1607262264749356'
OPENER = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=https_context()))


_operator = None


def operator():
    global _operator
    if _operator is None:
        email, explicit = resolve_operator(project=PROJECT)
        print('Google Cloud 계정: ' + email, file=sys.stderr, flush=True)
        _operator = (email, email if explicit else None)
    return _operator


def secret(name):
    email, account = operator()
    result = run_gcloud(['secrets', 'versions', 'access', 'latest', '--secret='+name],
        project=PROJECT, account=account)
    if result.returncode:
        raise PilotError(permission_help(email, PROJECT) + '\n지정한 웹훅 비밀 읽기 실패: ' + name)
    return result.stdout.strip()


def graph(path, token, params=None, method='GET'):
    url = 'https://graph.facebook.com/v26.0/'+path
    raw = urllib.parse.urlencode(params).encode() if params and method == 'POST' else None
    if params and method == 'GET':
        url += '?'+urllib.parse.urlencode(params)
    req = urllib.request.Request(url, data=raw, method=method,
        headers={'Authorization': 'Bearer '+token, 'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'})
    try:
        with OPENER.open(req, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        try:
            value = json.loads(error.read()).get('error', {})
        except ValueError:
            value = {}
        message = str(value.get('message', '')).replace(token, '[credential]')
        # App-token contains the secret; never echo either part.
        for part in token.split('|'):
            message = message.replace(part, '[credential]')
        raise PilotError(f"Meta {path} HTTP {error.code}, code={value.get('code')}, subcode={value.get('error_subcode')}: {message}") from None
    except OSError:
        raise PilotError('Meta API TLS·통신 확인 실패. 인증 값은 출력하지 않았습니다.') from None


def request_status(url, raw=None, signature=None):
    headers = {'Content-Type': 'application/json'}
    if signature:
        headers['X-Hub-Signature-256'] = signature
    req = urllib.request.Request(url, data=raw, headers=headers)
    try:
        with OPENER.open(req, timeout=60) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except OSError:
        raise PilotError('웹훅 수신 주소 통신 실패. 인증 URL은 출력하지 않았습니다.') from None


def check():
    deployment = json.loads((RELEASE/'deployment.json').read_text())
    callback = deployment['callback_url']
    app_secret, verify_token = secret('sns-meta-app-secret'), secret('sns-webhook-verify-token')
    # Exercise the actual callback. The platform may reserve healthz paths.
    status, response = request_status(callback)
    if (status, response) != (403, b'verification failed'):
        raise PilotError('웹훅 경로 확인 실패: HTTP '+str(status))
    status, _ = request_status(callback, b'{}', 'sha256='+'0'*64)
    assert status == 403, 'invalid signatures must be rejected'
    # Query credentials remain in memory; the narrow GET log exclusion is set.
    query = urllib.parse.urlencode({'hub.mode': 'subscribe', 'hub.verify_token': verify_token, 'hub.challenge': '89465231'})
    status, response = request_status(callback+'?'+query)
    assert (status, response) == (200, b'89465231'), 'verification challenge failed'
    raw = b'{"object":"instagram","entry":[]}'
    signature = 'sha256='+hmac.new(app_secret.encode(), raw, hashlib.sha256).hexdigest()
    status, _ = request_status(callback, raw, signature)
    assert status == 200, 'signed callback / registry read failed'
    result = {'unsigned_callback_get': 403, 'invalid_signature': 403, 'verification_challenge': 200,
        'signed_empty_delivery': 200, 'natural_comment_verified': False}
    (RELEASE/'receiver-check.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


def subscribe():
    callback = json.loads((RELEASE/'deployment.json').read_text())['callback_url']
    app_secret, verify_token, token = secret('sns-meta-app-secret'), secret('sns-webhook-verify-token'), secret('sns-meta-token')
    identity = graph('app', token, {'fields': 'id,name'})
    if identity.get('id') != APP_ID:
        raise PilotError('운영 토큰의 앱이 승인한 앱과 다릅니다.')
    app_token = APP_ID+'|'+app_secret
    before = graph(APP_ID+'/subscriptions', app_token)
    (RELEASE/'meta-subscriptions-before.json').write_text(json.dumps(before, indent=2))
    for item in before.get('data', []):
        if item.get('object') == 'instagram' and item.get('callback_url') not in (None, callback):
            raise PilotError('Instagram에 다른 콜백이 등록되어 있습니다. 기존 연결 보존을 확인해야 합니다.')
    graph(APP_ID+'/subscriptions', app_token, {'object': 'instagram', 'callback_url': callback,
        'fields': 'comments', 'verify_token': verify_token, 'include_values': 'true'}, 'POST')
    subscription = graph(APP_ID+'/subscriptions', app_token)
    (RELEASE/'meta-subscriptions-after.json').write_text(json.dumps(subscription, indent=2))
    instagram = next((r for r in subscription.get('data', []) if r.get('object') == 'instagram'), None)
    if not instagram or instagram.get('callback_url') != callback:
        raise PilotError('Meta의 콜백 등록 결과를 확인하지 못했습니다.')
    settings = json.loads((RELEASE/'context/config.json').read_text())
    accounts = []
    for name in settings['cloud']['scheduled_accounts']:
        account = select(settings, name)['account_id']
        graph(account+'/subscribed_apps', token, {'subscribed_fields': 'comments'}, 'POST')
        observed = graph(account+'/subscribed_apps', token, {'fields': 'app_id,subscribed_fields'})
        match = next((a for a in observed.get('data', []) if a.get('app_id') == APP_ID), None)
        accounts.append({'username': name, 'account_id': account, 'app_id': APP_ID,
            'subscribed_fields': match.get('subscribed_fields') if match else None, 'app_present': bool(match),
            'app_identity_basis': 'explicit_app_id_field_and_verified_token'})
        (RELEASE/'account-subscriptions-after.json').write_text(json.dumps(accounts, indent=2))
        if not match or 'comments' not in match.get('subscribed_fields', []):
            raise PilotError('계정의 comments 구독 확인 대기: '+name)
    result = {'meta_callback_registered': True, 'app_id': APP_ID, 'accounts': accounts, 'natural_comment_verified': False}
    (RELEASE/'meta-connection.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['check', 'subscribe'])
    args = parser.parse_args()
    try:
        {'check': check, 'subscribe': subscribe}[args.action]()
    except PilotError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
