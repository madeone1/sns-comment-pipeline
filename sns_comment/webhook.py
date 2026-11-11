"""Public Meta callback. Authenticate bytes, durably enqueue IDs, then ACK.

No Graph token, comment text, database or AI calls belong in this process.
"""

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import urllib.parse

from .api import PilotError
from .cloud_state import CloudError, CloudState
from .profiles import read_settings
from .storage import now, timestamp
from .connection_registry import REGISTRY_OBJECT, initial_registry

MAX_BODY = 1_000_000
ID = re.compile(r'[0-9]{1,30}')
RECEIPT = re.compile(r'[a-f0-9]{64}')


def signature_valid(body, signature, secret):
    if not secret or not isinstance(signature, str) or not re.fullmatch(r'sha256=[a-fA-F0-9]{64}', signature):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature[7:].lower())


def notifications(body, active_accounts, received_at=None):
    """Discard personal fields before storage. Arrival never becomes written_at."""
    try:
        payload = json.loads(body)
        if not isinstance(payload, dict) or payload.get('object') != 'instagram':
            raise ValueError
        entries = payload['entry']
        if not isinstance(entries, list) or len(entries) > 100:
            raise ValueError
        events = []
        delivery = hashlib.sha256(body).hexdigest()
        for entry_index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise ValueError
            account = entry.get('id')
            if account not in active_accounts:
                continue
            changes = entry.get('changes', [])
            if not isinstance(changes, list) or len(changes) > 100:
                raise ValueError
            for change_index, change in enumerate(changes):
                if not isinstance(change, dict):
                    raise ValueError
                if change.get('field') != 'comments':
                    continue
                value = change.get('value')
                if not isinstance(value, dict):
                    raise ValueError
                media = value.get('media')
                media_id = media.get('id') if isinstance(media, dict) else None
                comment_id = value.get('id') or value.get('comment_id')
                if not all(isinstance(v, str) and ID.fullmatch(v) for v in (account, media_id, comment_id)):
                    raise ValueError
                receipt = hashlib.sha256(f'{delivery}:{entry_index}:{change_index}'.encode()).hexdigest()
                event_time = entry.get('time')
                events.append({'version': 1, 'receipt': receipt, 'delivery_hash': delivery,
                    'account_id': account, 'media_id': media_id, 'comment_id': comment_id,
                    'field': 'comments', 'event_time': event_time if isinstance(event_time, int) and not isinstance(event_time, bool) else None,
                    'received_at': timestamp(received_at or now())})
        return events
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, PilotError):
        raise PilotError('Instagram 댓글 알림 형식을 확인해주세요.') from None


def event_path(event, folder='pending'):
    if not ID.fullmatch(event['account_id']) or not RECEIPT.fullmatch(event['receipt']):
        raise PilotError('웹훅 수신 식별자 형식을 확인해주세요.')
    return f"webhook/{folder}/{event['account_id']}/{event['receipt']}.json"


def enqueue(state, event):
    done, _ = state.json_get(event_path(event, 'done'))
    if done is not None:
        return False
    try:
        state.json_put(event_path(event), event, 0)
        return True
    except CloudError as error:
        if error.status != 412:
            raise
        # An existing immutable object is an already-durable delivery.
        return False


class Callback:
    def __init__(self, state, settings, app_secret, verify_token):
        if not app_secret or not verify_token or hmac.compare_digest(app_secret, verify_token):
            raise PilotError('서로 다른 App Secret과 Verify Token을 설정해주세요.')
        self.state, self.settings = state, settings
        self.app_secret, self.verify_token = app_secret, verify_token

    def __call__(self, env, start_response):
        def reply(code, text):
            raw = text.encode()
            start_response(code, [('Content-Type', 'text/plain; charset=utf-8'), ('Cache-Control', 'no-store'),
                ('Content-Length', str(len(raw))), ('X-Content-Type-Options', 'nosniff')])
            return [raw]
        method, path = env.get('REQUEST_METHOD'), env.get('PATH_INFO')
        if path == '/healthz' and method == 'GET':
            return reply('200 OK', 'ok')
        if path != '/webhooks/instagram':
            return reply('404 Not Found', 'not found')
        if method == 'GET':
            try:
                query = urllib.parse.parse_qs(env.get('QUERY_STRING', ''), keep_blank_values=True, max_num_fields=10)
            except ValueError:
                return reply('400 Bad Request', 'invalid verification query')
            supplied = query.get('hub.verify_token', [''])[0]
            challenge = query.get('hub.challenge', [''])[0]
            valid = query.get('hub.mode') == ['subscribe'] and len(query.get('hub.verify_token', [])) == 1
            valid = valid and hmac.compare_digest(supplied.encode(), self.verify_token.encode())
            if valid and len(query.get('hub.challenge', [])) == 1 and re.fullmatch(r'[0-9]{1,200}', challenge):
                return reply('200 OK', challenge)
            return reply('403 Forbidden', 'verification failed')
        if method != 'POST':
            return reply('405 Method Not Allowed', 'method not allowed')
        try:
            length = int(env.get('CONTENT_LENGTH', '0'))
            if length < 1 or length > MAX_BODY:
                return reply('413 Payload Too Large', 'invalid body size')
            body = env['wsgi.input'].read(length)
            if len(body) != length:
                return reply('400 Bad Request', 'incomplete body')
            if not signature_valid(body, env.get('HTTP_X_HUB_SIGNATURE_256'), self.app_secret):
                return reply('403 Forbidden', 'invalid signature')
            registry, _ = self.state.json_get(REGISTRY_OBJECT, initial_registry(self.settings))
            active = {a['id'] for a in registry['accounts'].values() if a['active']}
            events = notifications(body, active)
            for event in events:
                enqueue(self.state, event)
            return reply('200 OK', 'EVENT_RECEIVED')
        except (ValueError, PilotError) as error:
            # Never log the query, body, signature, tokens or upstream response.
            if isinstance(error, CloudError):
                return reply('503 Service Unavailable', 'durable storage unavailable')
            return reply('400 Bad Request', 'invalid notification')
        except Exception:
            return reply('503 Service Unavailable', 'callback temporarily unavailable')


def create_app():
    return Callback(CloudState(os.environ['SNS_STATE_BUCKET']),
        read_settings(Path(__file__).resolve().parents[1]/'config.json'),
        os.environ['SNS_META_APP_SECRET'], os.environ['SNS_WEBHOOK_VERIFY_TOKEN'])
