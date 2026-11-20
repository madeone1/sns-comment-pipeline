"""Read-only YouTube API transport with an explicit request budget."""

import re

SCOPE = 'https://www.googleapis.com/auth/youtube.force-ssl'
HANDLE = '@waterbomb_official'
RESOURCES = frozenset(('channels', 'commentThreads', 'comments'))
ERROR_REASONS = frozenset(('forbidden', 'insufficientPermissions', 'quotaExceeded',
    'dailyLimitExceeded', 'commentsDisabled', 'processingFailure', 'videoNotFound',
    'channelNotFound', 'commentNotFound', 'invalidPageToken', 'badRequest',
    'invalidParameter', 'authError', 'rateLimitExceeded', 'accessNotConfigured',
    'accountDelegationForbidden', 'youtubeSignupRequired', 'invalidFilters'))


class ValidationError(Exception):
    def __init__(self, reason, status=None):
        super().__init__(reason)
        self.reason, self.status = reason, status

    def public(self):
        return {'reason': self.reason, 'http_status': self.status}


class YouTube:
    def __init__(self, credentials, max_requests=100, session=None, record=None):
        if session is None:
            from google.auth.transport.requests import AuthorizedSession
            session = AuthorizedSession(credentials, max_refresh_attempts=0)
        self.session, self.record = session, record
        self.max_requests, self.requests = max_requests, 0

    def get(self, resource, **params):
        if resource not in RESOURCES:
            raise ValidationError('resource_not_allowed')
        if self.requests >= self.max_requests:
            raise ValidationError('request_budget_exhausted')
        self.requests += 1
        try:
            response = self.session.get('https://www.googleapis.com/youtube/v3/' + resource,
                                        params=params, timeout=30, allow_redirects=False)
        except Exception as error:
            from google.auth.exceptions import RefreshError
            if isinstance(error,RefreshError) and any(isinstance(arg,dict) and arg.get('error')=='invalid_grant' for arg in error.args):
                raise ValidationError('oauth_authorization_revoked') from None
            # Transport / refresh exceptions can contain tokens or full request URLs.
            raise ValidationError('network_or_token_refresh_failed') from None
        try:
            data = response.json()
        except (ValueError, TypeError):
            raise ValidationError('invalid_api_response', response.status_code) from None
        if response.status_code != 200:
            error_body = data.get('error') if isinstance(data, dict) else None
            errors = error_body.get('errors', []) if isinstance(error_body, dict) else []
            reason = errors[0].get('reason', '') if errors and isinstance(errors[0], dict) else ''
            if reason not in ERROR_REASONS:
                reason = 'api_request_failed'
            raise ValidationError(reason, response.status_code)
        if not isinstance(data, dict) or not isinstance(data.get('items'), list):
            raise ValidationError('missing_items_in_api_response')
        if self.record:
            self.record(self.requests, resource, data)
        return data


def verify_channel(api, handle=HANDLE):
    target = api.get('channels', part='snippet', forHandle=handle)['items']
    if len(target) != 1 or not re.fullmatch(r'UC[A-Za-z0-9_-]{22}', target[0].get('id', '')):
        raise ValidationError('target_channel_not_found')
    owned = api.get('channels', part='snippet', mine='true', maxResults=50)['items']
    if target[0]['id'] not in {item.get('id') for item in owned}:
        raise ValidationError('authenticated_channel_does_not_match_waterbomb')
    return {'id': target[0]['id'], 'handle': handle,
            'title': target[0].get('snippet', {}).get('title', '')}
