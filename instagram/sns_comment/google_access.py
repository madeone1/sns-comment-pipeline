"""Explicit Google OAuth or scoped service identity; never browser credentials."""

import base64
import json
import os
import re
import urllib.parse
import urllib.request

from .api import NoRedirect, PilotError, https_context


def access_token(settings, cloud_transport=None, opener=None):
    if settings.get('auth_mode') == 'service_account':
        identity = settings.get('service_account', '')
        if (not cloud_transport or not isinstance(identity, str)
                or not re.fullmatch(r'[a-z][a-z0-9-]*@[a-z][a-z0-9-]*\.iam\.gserviceaccount\.com', identity)):
            raise PilotError('시트 자동 동기화의 서비스 계정 설정을 확인해주세요.')
        result = cloud_transport.json('POST', 'https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/'
            + urllib.parse.quote(identity, safe='@') + ':generateAccessToken',
            {'scope': ['https://www.googleapis.com/auth/spreadsheets'], 'lifetime': '3600s'})
        token = result.get('accessToken')
        if not isinstance(token, str) or not token or any(c.isspace() for c in token):
            raise PilotError('Google 서비스 계정 인증 실패. 인증정보는 출력하지 않습니다.')
        return token
    token=os.environ.get('SNS_GOOGLE_ACCESS_TOKEN')
    if token:
        if any(c.isspace() for c in token):raise PilotError('Google 액세스 토큰 형식 오류')
        return token
    raw=os.environ.get('SNS_GOOGLE_OAUTH_JSON')
    secret=settings.get('oauth_secret')
    if not raw and secret and cloud_transport:
        if not re.fullmatch(r'projects/[a-z0-9-]+/secrets/[A-Za-z0-9_-]+',secret):
            raise PilotError('Google OAuth Secret 경로를 확인해주세요.')
        result=cloud_transport.json('GET','https://secretmanager.googleapis.com/v1/'+secret+'/versions/latest:access')
        raw=base64.b64decode(result['payload']['data']).decode()
    if not raw:raise PilotError('Google 자동 동기화 인증 미연결. 브라우저 로그인과 별도입니다.')
    try:
        credentials=json.loads(raw)
        data=urllib.parse.urlencode({key:credentials[key] for key in ('client_id','client_secret','refresh_token')} |
                                    {'grant_type':'refresh_token'}).encode()
        request=urllib.request.Request('https://oauth2.googleapis.com/token',data=data,
            headers={'Content-Type':'application/x-www-form-urlencoded'},method='POST')
        opener=opener or urllib.request.build_opener(NoRedirect(),urllib.request.HTTPSHandler(context=https_context()))
        with opener.open(request,timeout=30) as response:result=json.load(response)
        token=result['access_token']
        if not isinstance(token,str) or not token or any(c.isspace() for c in token):raise ValueError
        return token
    except (OSError,ValueError,KeyError,TypeError):
        raise PilotError('Google OAuth 갱신 실패. 인증정보는 출력하지 않습니다.') from None
