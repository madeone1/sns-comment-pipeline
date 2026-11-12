"""Temporary loopback form: approved Meta App Secret -> Secret Manager stdin.

No credential is placed in argv, a file, response body or application logs.
The verify token is shown only in a password field on this guarded local page.
"""

import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import html
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sns_comment.api import PilotError, https_context, NoRedirect
from sns_comment.gcloud_account import resolve_operator, run_gcloud

PROJECT = 'waterbomb-sns-pipeline'
APP_ID = '1607262264749356'
RELEASE = ROOT/'.local/webhook-release'
PORT = 8796
GUARD = secrets.token_urlsafe(32)


_resolved = None


def operator():
    global _resolved
    if _resolved is None:
        email, explicit = resolve_operator(project=PROJECT)
        print('Google Cloud 계정: ' + email, file=sys.stderr, flush=True)
        _resolved = (email, email if explicit else None)
    return _resolved


def gcloud(args, value=None):
    _, account = operator()
    return run_gcloud(args, project=PROJECT, account=account, input=value, timeout=60)


class Input(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def allowed(self):
        return self.headers.get('Host') == f'127.0.0.1:{PORT}' and hmac.compare_digest(self.path, '/setup/'+GUARD)

    def page(self, message='', success=False):
        token = ''
        if success:
            result = gcloud(['secrets', 'versions', 'access', 'latest', '--secret=sns-webhook-verify-token'])
            if not result.returncode:
                token = result.stdout.strip()
        deployment = json.loads((RELEASE/'deployment.json').read_text(encoding="utf-8")) if (RELEASE/'deployment.json').exists() else {}
        markup = f'''<!doctype html><html lang="ko"><meta charset="utf-8"><title>웹훅 비밀 등록</title>
        <style>body{{font:16px system-ui;max-width:680px;margin:70px auto;padding:24px;color:#17382c}}input{{box-sizing:border-box;width:100%;padding:14px;margin:12px 0}}button{{padding:12px 20px}}p{{line-height:1.6}}</style>
        <h1>웹훅 연결 준비</h1><p>대상: SNS Comments Analyze ({APP_ID})</p><p>{html.escape(message)}</p>'''
        if success:
            markup += f'''<p id="saved">App Secret 검증과 비밀 저장을 완료했습니다.</p>
                <label>Meta 인증 토큰<input aria-label="Meta 인증 토큰" type="password" readonly value="{html.escape(token)}"></label>
                <p id="callback">{html.escape(deployment.get('callback_url', '수신 서버 배포 준비 중'))}</p>'''
        else:
            markup += '''<form method="post"><label>Meta 앱 시크릿<input name="app_secret" aria-label="Meta 앱 시크릿" type="password" autocomplete="off" required minlength="32" maxlength="64"></label><button>검증하고 비밀 저장소에 등록</button></form>'''
        raw = markup.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'")
        self.send_header('Referrer-Policy', 'same-origin')
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if not self.allowed():
            self.send_error(404)
            return
        self.page(success=(RELEASE/'app-secret-registration.json').exists())

    def do_POST(self):
        if not self.allowed() or self.headers.get('Origin') != f'http://127.0.0.1:{PORT}':
            self.send_error(403)
            return
        try:
            size = int(self.headers.get('Content-Length', 0))
            if not 1 <= size <= 4096:
                raise ValueError
            params = urllib.parse.parse_qs(self.rfile.read(size).decode(), strict_parsing=True, max_num_fields=2)
            value = params['app_secret'][0]
            if len(params['app_secret']) != 1 or not re.fullmatch(r'[a-fA-F0-9]{32}', value):
                raise ValueError
            # Confirm this is the secret of the already-verified production app.
            req = urllib.request.Request('https://graph.facebook.com/v26.0/app?fields=id,name',
                headers={'Authorization': 'Bearer '+APP_ID+'|'+value, 'Accept': 'application/json'})
            opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=https_context()))
            with opener.open(req, timeout=30) as response:
                identity = json.load(response)
            if identity.get('id') != APP_ID:
                raise ValueError
            result = gcloud(['secrets', 'versions', 'add', 'sns-meta-app-secret', '--data-file=-', '--format=json'], value)
            if result.returncode:
                self.page('비밀 저장에 실패했습니다. 기존 운영 설정은 유지됩니다.')
                return
            info = json.loads(result.stdout)
            (RELEASE/'app-secret-registration.json').write_text(json.dumps({'app_id': APP_ID, 'app_name': identity.get('name'),
                'secret_version': info['name'], 'secret_value_logged': False}, ensure_ascii=False, indent=2), encoding="utf-8")
            self.page(success=True)
        except (ValueError, KeyError, OSError, subprocess.SubprocessError, PilotError) as error:
            if isinstance(error, PilotError):
                print(str(error), file=sys.stderr)
            self.page('앱 시크릿 검증 또는 저장에 실패했습니다. 비밀 값은 출력하지 않았습니다.')


if __name__ == '__main__':
    os.umask(0o077)
    try:
        operator()
    except PilotError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
    (RELEASE/'setup-local.json').write_text(json.dumps({'url': f'http://127.0.0.1:{PORT}/setup/'+GUARD}), encoding="utf-8")
    print(json.dumps({'listening': 'loopback only', 'port': PORT, 'secret_values_logged': False}), flush=True)
    HTTPServer(('127.0.0.1', PORT), Input).serve_forever()
