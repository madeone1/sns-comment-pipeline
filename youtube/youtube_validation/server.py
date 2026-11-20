"""Loopback-only OAuth callback app; never log codes, tokens or callback URLs."""

from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
import html
import secrets
import time
from urllib.parse import parse_qs, urlsplit

from .api import HANDLE, ValidationError
from .auth import CALLBACK_PATH, PORT, create_flow, finish_flow
from .storage import STATE, read_json, write_json


def make_server(state=STATE, port=PORT, handle=HANDLE, authorization_language=None):
    origin = f'http://127.0.0.1:{port}'
    nonce = secrets.token_urlsafe(32)
    pending = {}
    message = {'text': ''}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, body='', location=None, cookie=False):
            self.send_response(status)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            # Chrome serializes the Origin of a form POST as "null" with
            # no-referrer. Keep same-origin forms usable without leaking a
            # callback's query string to Google or any other origin.
            self.send_header('Referrer-Policy', 'same-origin')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https://accounts.google.com; frame-ancestors 'none'")
            if location:
                self.send_header('Location', location)
            if cookie:
                self.send_header('Set-Cookie', 'yt_validation=' + nonce + '; HttpOnly; SameSite=Lax; Path=/')
            self.end_headers()
            self.wfile.write(body.encode('utf-8'))

        def valid_host(self):
            return self.headers.get('Host') == f'127.0.0.1:{port}'

        def authenticated(self):
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get('Cookie', ''))
                value = cookie.get('yt_validation')
                return bool(value and secrets.compare_digest(value.value, nonce))
            except Exception:
                return False

        def do_GET(self):
            if not self.valid_host():
                return self.send(403, '허용되지 않은 호스트입니다.')
            path = urlsplit(self.path).path
            if secrets.compare_digest(path, '/session/' + nonce):
                return self.send(303, location='/', cookie=True)
            if not self.authenticated():
                return self.send(401, '실행 도구에서 안내한 인증 페이지를 열어주세요.')
            if path == CALLBACK_PATH:
                return self.callback()
            if path != '/':
                return self.send(404)
            connected = None
            try:
                connected = read_json(state / 'connection.json')['channel']
            except (OSError, ValueError, KeyError):
                pass
            configured = (state / 'client.json').is_file()
            if connected:
                status = '연결 완료: ' + html.escape(connected['title']) + ' (' + html.escape(connected['id']) + ')'
            elif configured:
                status = '채널 소유 계정의 인증을 기다리고 있습니다.'
            else:
                status = 'OAuth 클라이언트 파일 등록을 기다리고 있습니다.'
            form = '''<form action="/authorize" method="post">
                <button type="submit">Google 계정으로 워터밤 채널 인증</button></form>''' if configured else ''
            self.send(200, '''<!doctype html><html lang="ko"><meta charset="utf-8">
                <meta name="viewport" content="width=device-width,initial-scale=1">
                <title>워터밤 YouTube 채널 인증</title><style>
                body{font:17px/1.7 -apple-system,BlinkMacSystemFont,sans-serif;background:#f5f7fa;color:#17212b;margin:0;padding:48px 20px}
                main{max-width:680px;margin:auto;background:white;padding:36px;border:1px solid #dce2e8;border-radius:16px}
                h1{font-size:26px;line-height:1.35} .status{background:#edf3ff;padding:16px;border-radius:8px}
                button{background:#1649d7;color:white;border:0;border-radius:8px;padding:14px 20px;font:inherit;cursor:pointer}
                small{color:#52606d} code{overflow-wrap:anywhere}</style><main>
                <small>MADEONE · 독립 파이프라인 검증</small><h1>워터밤 YouTube 채널 인증</h1>
                <p>대상 채널: <strong>''' + html.escape(handle) + '''</strong></p>
                <p class="status">''' + status + '''</p>
                <p>워터밤 공식 채널을 소유한 Google 계정으로 로그인해주세요.
                채널 선택 화면이 나오면 워터밤 채널을 선택하세요.</p>
                <p>공개 댓글과 검토 대기·스팸 의심 댓글의 조회를 검증합니다.
                Google의 댓글 API 권한에는 수정·삭제 권한도 포함되어 승인 화면에 표시됩니다.</p>
                ''' + form + '<p>' + html.escape(message['text']) + '''</p>
                <small>채널 ID가 대상과 일치하는지 확인한 뒤 인증정보를 이 컴퓨터의 비공개 저장소에 보관합니다.</small>
                </main></html>''')

        def do_POST(self):
            if (not self.valid_host() or not self.authenticated()
                    or self.headers.get('Origin') != origin):
                return self.send(403, '인증 페이지에서 다시 시작해주세요.')
            if self.path != '/authorize':
                return self.send(404)
            if pending and time.monotonic() - pending['created'] < 600:
                return self.send(409, '이미 열린 Google 승인 화면에서 인증을 마쳐주세요.')
            try:
                flow = create_flow(state, port)
                options = {'hl': authorization_language} if authorization_language else {}
                url, oauth_state = flow.authorization_url(
                    access_type='offline', prompt='consent select_account', **options)
                pending.update(flow=flow, state=oauth_state, created=time.monotonic())
                self.send(303, location=url)
            except (OSError, ValueError, ValidationError):
                message['text'] = 'OAuth 클라이언트 등록과 리디렉션 URI를 확인해주세요.'
                self.send(303, location='/')

        def callback(self):
            params = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
            received = params.get('state', [])
            if (not pending or len(received) != 1
                    or not secrets.compare_digest(received[0], pending['state'])
                    or time.monotonic() - pending['created'] >= 600):
                return self.send(400, '만료되었거나 일치하지 않는 인증 요청입니다. 인증 페이지에서 다시 시작해주세요.')
            flow = pending['flow']
            pending.clear()  # One-use callback, including failed/denied authorizations.
            codes = params.get('code', [])
            if params.get('error') or len(codes) != 1 or not codes[0]:
                message['text'] = 'Google 인증이 완료되지 않았습니다. 필요한 권한을 확인하고 다시 시도해주세요.'
                return self.send(303, location='/')
            try:
                finish_flow(flow, codes[0], state, handle)
                message['text'] = '채널 인증이 완료되었습니다. 댓글 수집 검증을 진행할 수 있습니다.'
            except ValidationError as error:
                if error.reason == 'authenticated_channel_does_not_match_waterbomb':
                    message['text'] = '선택한 계정의 채널이 워터밤 공식 채널과 다릅니다. 채널 소유 계정으로 다시 인증해주세요.'
                else:
                    message['text'] = '인증 또는 채널 확인에 실패했습니다. API 활성화와 계정 권한을 확인해주세요.'
            self.send(303, location='/')

    server = HTTPServer(('127.0.0.1', port), Handler)
    port = server.server_address[1]
    origin = f'http://127.0.0.1:{port}'
    server.launch_url = origin + '/session/' + nonce
    write_json(state / 'auth-ui.json', {'url': server.launch_url,
               'redirect_uri': origin + CALLBACK_PATH, 'target_handle': handle})
    return server
