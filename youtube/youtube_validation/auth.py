"""Google's OAuth library; credentials are stored only after channel verification."""

import json
from pathlib import Path

from .api import HANDLE, SCOPE, ValidationError, YouTube, verify_channel
from .storage import STATE, read_json, write_json

CALLBACK_PATH = '/oauth/youtube/callback'
PORT = 8765


def validate_client(data, redirect_uri):
    client = data.get('web') if isinstance(data, dict) else None
    if not isinstance(client, dict):
        raise ValidationError('웹 애플리케이션 유형의 OAuth JSON 파일이 필요합니다.')
    if client.get('auth_uri') != 'https://accounts.google.com/o/oauth2/auth':
        raise ValidationError('Google 공식 인증 주소를 확인해주세요.')
    if client.get('token_uri') != 'https://oauth2.googleapis.com/token':
        raise ValidationError('Google 공식 토큰 주소를 확인해주세요.')
    if not str(client.get('client_id', '')).endswith('.apps.googleusercontent.com') or not client.get('client_secret'):
        raise ValidationError('OAuth 클라이언트 ID와 시크릿을 확인해주세요.')
    if redirect_uri not in client.get('redirect_uris', []):
        raise ValidationError('승인된 리디렉션 URI에 ' + redirect_uri + ' 를 추가해주세요.')
    return {'web': client}


def import_client(path, state=STATE, port=PORT):
    redirect = f'http://127.0.0.1:{port}' + CALLBACK_PATH
    try:
        data = validate_client(read_json(path), redirect)
    except (OSError, ValueError):
        raise ValidationError('OAuth JSON 파일을 읽을 수 없습니다.') from None
    write_json(Path(state) / 'client.json', data)
    return {'project_id': data['web'].get('project_id'), 'redirect_uri': redirect}


def create_flow(state=STATE, port=PORT):
    from google_auth_oauthlib.flow import Flow
    redirect = f'http://127.0.0.1:{port}' + CALLBACK_PATH
    data = validate_client(read_json(Path(state) / 'client.json'), redirect)
    flow = Flow.from_client_config(data, scopes=[SCOPE], autogenerate_code_verifier=True)
    flow.redirect_uri = redirect
    return flow


def finish_flow(flow, code, state=STATE, handle=HANDLE):
    try:
        flow.fetch_token(code=code, timeout=30)
        credentials = flow.credentials
        granted = credentials.granted_scopes or credentials.scopes or []
        if SCOPE not in granted or not credentials.refresh_token:
            raise ValidationError('required_scope_or_refresh_token_missing')
        channel = verify_channel(YouTube(credentials, max_requests=4), handle)
    except ValidationError:
        raise
    except Exception:
        raise ValidationError('oauth_exchange_failed') from None
    write_json(Path(state) / 'authorized-user.json', json.loads(credentials.to_json()))
    write_json(Path(state) / 'connection.json', {'channel': channel, 'scopes': [SCOPE]})
    return channel


def load_credentials(state=STATE):
    from google.oauth2.credentials import Credentials
    data = read_json(Path(state) / 'authorized-user.json')
    if data.get('token_uri') != 'https://oauth2.googleapis.com/token':
        raise ValidationError('invalid_token_endpoint')
    if SCOPE not in data.get('scopes', []) or not data.get('refresh_token'):
        raise ValidationError('required_scope_or_refresh_token_missing')
    return Credentials.from_authorized_user_info(data, scopes=[SCOPE])
