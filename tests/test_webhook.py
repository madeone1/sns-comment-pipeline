import copy
import datetime as dt
import hashlib
import hmac
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from sns_comment.api import PilotError
from sns_comment.cloud_state import CloudError
from sns_comment.storage import Store
from sns_comment.webhook import Callback, notifications, event_path
from sns_comment.webhook_worker import process, target_day


ACCOUNT = '10001'
SETTINGS = {'expected_username': 'waterbomb_official', 'account_id': ACCOUNT, 'accounts': {}, 'enabled': True}
CONFIG = {'expected_username': 'waterbomb_official', 'account_id': ACCOUNT}
START, END, DAY = '2026-11-02T23:00:00+00:00', '2026-11-09T23:00:00+00:00', '2026-11-10'


class MemoryState:
    def __init__(self):
        self.data, self.generations, self.counter = {}, {}, 0
        self.fail_write, self.fail_delete = None, False

    def json_get(self, name, default=None):
        return copy.deepcopy(self.data.get(name, default)), self.generations.get(name, 0)

    def json_put(self, name, value, generation=None):
        if self.fail_write and self.fail_write(name):
            raise CloudError('synthetic storage failure', 503)
        if generation is not None and generation != self.generations.get(name, 0):
            raise CloudError('synthetic conflict', 412)
        self.counter += 1
        self.data[name], self.generations[name] = copy.deepcopy(value), self.counter
        return self.counter

    def list_objects(self, prefix, limit=100):
        names = sorted(n for n in self.data if n.startswith(prefix))
        return [{'name': n, 'generation': self.generations[n]} for n in names[:limit]], len(names) > limit

    def delete(self, name, generation):
        if self.fail_delete:
            raise CloudError('synthetic ack failure', 503)
        if name not in self.data:
            raise CloudError('missing', 404)
        if self.generations[name] != generation:
            raise CloudError('conflict', 412)
        del self.data[name]
        del self.generations[name]


def body(account=ACCOUNT, comment='30001', media='20001', text='PRIVATE TEXT'):
    return json.dumps({'object': 'instagram', 'entry': [{'id': account, 'time': 1794265200,
        'changes': [{'field': 'comments', 'value': {'id': comment, 'media': {'id': media},
            'from': {'username': 'PRIVATE NAME'}, 'text': text}}]}]}).encode()


def call(app, raw=b'', signature=None, method='POST', query='', path='/webhooks/instagram'):
    responses = []
    env = {'REQUEST_METHOD': method, 'PATH_INFO': path, 'QUERY_STRING': query,
        'CONTENT_LENGTH': str(len(raw)), 'wsgi.input': io.BytesIO(raw)}
    if signature is not None:
        env['HTTP_X_HUB_SIGNATURE_256'] = signature
    result = b''.join(app(env, lambda status, headers: responses.append((status, headers))))
    return responses[0][0], result


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.state = MemoryState()
        self.app = Callback(self.state, SETTINGS, 'synthetic_app_secret', 'synthetic_verify_token')

    def signed(self, raw):
        return 'sha256='+hmac.new(b'synthetic_app_secret', raw, hashlib.sha256).hexdigest()

    def test_challenge_requires_token_mode_and_exact_path(self):
        query = 'hub.mode=subscribe&hub.verify_token=synthetic_verify_token&hub.challenge=12345'
        self.assertEqual(call(self.app, method='GET', query=query), ('200 OK', b'12345'))
        self.assertEqual(call(self.app, method='GET', query=query.replace('synthetic_verify_token', 'wrong'))[0], '403 Forbidden')
        self.assertEqual(call(self.app, method='GET', query=query, path='/')[0], '404 Not Found')

    def test_signature_account_and_unsupported_fields_never_enqueue(self):
        raw = body()
        self.assertEqual(call(self.app, raw, 'sha256='+'0'*64)[0], '403 Forbidden')
        other = body(account='99999')
        self.assertEqual(call(self.app, other, self.signed(other))[0], '200 OK')
        self.assertFalse(self.state.data)

    def test_durable_sanitized_deduplicated_and_done_replay(self):
        raw = body()
        for _ in range(2):
            self.assertEqual(call(self.app, raw, self.signed(raw))[0], '200 OK')
        self.assertEqual(len(self.state.data), 1)
        stored = json.dumps(self.state.data)
        self.assertNotIn('PRIVATE', stored)
        self.assertNotIn('synthetic_app_secret', stored)
        event = next(iter(self.state.data.values()))
        self.state.json_put(event_path(event, 'done'), event, 0)
        self.state.delete(event_path(event), self.state.generations[event_path(event)])
        self.assertEqual(call(self.app, raw, self.signed(raw))[0], '200 OK')
        self.assertEqual(len(self.state.data), 1, '처리 완료 재전송은 새 pending을 만들지 않음')

    def test_storage_failure_is_retryable_not_success(self):
        self.state.fail_write = lambda name: True
        raw = body()
        self.assertEqual(call(self.app, raw, self.signed(raw))[0], '503 Service Unavailable')

    def test_disabled_account_and_alternate_comment_id(self):
        registry = {'accounts': {ACCOUNT: {'id': ACCOUNT, 'active': False}}}
        self.state.json_put('web/registry.json', registry)
        raw = body()
        self.assertEqual(call(self.app, raw, self.signed(raw))[0], '200 OK')
        self.assertEqual(len(self.state.data), 1)
        raw = raw.replace(b'"id": "30001"', b'"comment_id": "30001"')
        self.assertEqual(notifications(raw, {ACCOUNT})[0]['comment_id'], '30001')

    def test_bad_shape_and_body_limit(self):
        raw = b'{"object":"instagram","entry":null}'
        self.assertEqual(call(self.app, raw, self.signed(raw))[0], '400 Bad Request')
        self.assertEqual(call(self.app, b'a'*1_000_001, 'x')[0], '413 Payload Too Large')

    def test_ambiguous_or_excessive_challenge_query_is_rejected(self):
        query = 'hub.mode=subscribe&hub.verify_token=synthetic_verify_token&hub.challenge=12345'
        self.assertEqual(call(self.app, method='GET', query=query+'&hub.challenge=9')[0], '403 Forbidden')
        self.assertEqual(call(self.app, method='GET', query='&'.join('x=1' for _ in range(11)))[0], '400 Bad Request')

    def test_partial_batch_storage_retry_preserves_all_events(self):
        raw_payload = json.loads(body())
        raw_payload['entry'][0]['changes'].append({'field': 'comments', 'value': {'id': '30002', 'media': {'id': '20001'}}})
        raw = json.dumps(raw_payload).encode()
        second = notifications(raw, {ACCOUNT})[1]
        self.state.fail_write = lambda name: name == event_path(second)
        self.assertEqual(call(self.app, raw, self.signed(raw))[0], '503 Service Unavailable')
        self.assertEqual(len(self.state.data), 1)
        self.state.fail_write = None
        self.assertEqual(call(self.app, raw, self.signed(raw))[0], '200 OK')
        self.assertEqual(len(self.state.data), 2)


class Graph:
    def __init__(self):
        self.request_count, self.max_requests, self.paths = 0, 500, []
        self.written_at, self.owner, self.fail = '2026-11-09T10:00:00+0000', ACCOUNT, False

    def get(self, path, **params):
        self.request_count += 1
        self.paths.append(path)
        if self.fail:
            raise PilotError('synthetic Graph failure')
        if path == ACCOUNT:
            return {'id': ACCOUNT, 'username': 'waterbomb_official'}
        if path == '20001':
            return {'id': '20001', 'owner': {'id': self.owner}, 'permalink': 'https://www.instagram.com/p/OldPost/',
                'timestamp': '2025-01-01T00:00:00+0000', 'caption': 'old post'}
        if path == '20001/comments':
            return {'data': [{'id': '30001', 'text': 'verified original', 'timestamp': self.written_at,
                'username': 'audience', 'from': {'id': '40001'}, 'replies': {'data': []}}]}
        raise AssertionError('Unexpected broad traversal: '+path)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name)/'raw.sqlite3')
        self.state, self.graph = MemoryState(), Graph()
        self.event = notifications(body(), {ACCOUNT}, '2026-11-09T22:30:00Z')[0]
        self.state.json_put(event_path(self.event), self.event, 0)
        self.checkpoints = []

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def run_batch(self, checkpoint=None):
        return process(CONFIG, self.state, self.store, self.graph, START, END, DAY,
            checkpoint or (lambda: self.checkpoints.append(len(self.graph.paths))), root=self.state)

    def test_old_post_original_becomes_daily_and_ack_follows_checkpoint(self):
        result = self.run_batch()
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(self.state.data[f'runs/{DAY}/webhook-daily.json']['comments'][0]['text'], 'verified original')
        self.assertNotIn(event_path(self.event), self.state.data)
        self.assertIn(event_path(self.event, 'done'), self.state.data)
        self.assertNotIn(ACCOUNT+'/media', self.graph.paths)
        self.assertTrue(self.checkpoints)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM comments').fetchone()[0], 1)

    def test_previous_month_limit_acknowledges_older_post_without_comment_traversal(self):
        result=process({**CONFIG,'cloud':{'post_history':'previous_month'}},self.state,self.store,self.graph,
            START,END,DAY,lambda:None,root=self.state)
        self.assertEqual(result['status'],'complete')
        self.assertEqual(result['unresolved_comments'],0)
        self.assertNotIn('20001/comments',self.graph.paths)
        self.assertEqual(self.state.data[event_path(self.event,'done')]['outcome'],'outside_post_window')
        self.assertEqual(self.state.data[f'runs/{DAY}/webhook-daily.json']['comments'],[])

    def test_ownership_failure_keeps_pending(self):
        self.graph.owner = '99999'
        with self.assertRaisesRegex(PilotError, '소유'):
            self.run_batch()
        self.assertIn(event_path(self.event), self.state.data)
        self.assertNotIn('20001/comments', self.graph.paths)

    def test_interruption_after_scope_checkpoint_resumes_and_new_delivery_restarts(self):
        def crash():
            raise RuntimeError('simulated process termination after durable checkpoint')
        with self.assertRaises(RuntimeError):
            self.run_batch(crash)
        first_scope = self.store.db.execute('SELECT scope_id FROM webhook_batches').fetchone()[0]
        path = self.store.path
        self.store.close()
        self.store = Store(path)
        self.run_batch()
        self.assertEqual(self.store.db.execute('SELECT scope_id FROM webhook_batches').fetchone()[0], first_scope)
        second = notifications(body(text='changed delivery'), {ACCOUNT})[0]
        self.state.json_put(event_path(second), second, 0)
        self.run_batch()
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM webhook_batches').fetchone()[0], 2)
        self.assertEqual(self.graph.paths.count('20001/comments'), 2)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM comments').fetchone()[0], 1)

    def test_projection_or_ack_failure_retries_without_rescanning(self):
        self.state.fail_delete = True
        with self.assertRaises(CloudError):
            self.run_batch()
        requests = self.graph.request_count
        self.assertIn(event_path(self.event), self.state.data)
        self.state.fail_delete = False
        self.run_batch()
        self.assertEqual(self.graph.request_count, requests)

    def test_partial_scan_preserves_pending_and_the_same_scope(self):
        with mock.patch('sns_comment.webhook_worker.collect_continuously', return_value={'status': 'partial'}) as collect:
            result = self.run_batch()
            first = collect.call_args.kwargs['resume_scope_id']
        self.assertEqual(result['status'], 'pending')
        self.assertIn(event_path(self.event), self.state.data)
        self.assertNotIn(event_path(self.event, 'done'), self.state.data)
        self.run_batch()
        self.assertEqual(self.store.db.execute('SELECT scope_id FROM webhook_batches').fetchone()[0], first)

    def test_late_event_uses_original_day_and_08h_boundary(self):
        self.graph.written_at = '2026-11-07T23:00:00+0000'  # Nov 8 08:00 KST belongs to Nov 9 list.
        self.run_batch()
        self.assertEqual(target_day(self.graph.written_at), '2026-11-09')
        self.assertEqual(target_day('2026-11-07T22:59:59Z'), '2026-11-08')
        late = self.state.data['runs/2026-11-09/webhook-daily.json']
        self.assertEqual(late['counts']['comments_and_replies'], 1)
        self.assertEqual(self.state.data[f'runs/{DAY}/webhook-daily.json']['counts']['comments_and_replies'], 0)

    def test_unobserved_comment_is_explicit_never_reported_as_verified_zero(self):
        self.state.data[event_path(self.event)]['comment_id'] = '77777'
        result = self.run_batch()
        self.assertEqual(result['status'], 'pending')
        self.assertEqual(result['unresolved_comments'], 1)
        self.assertEqual(self.state.data[event_path(self.event, 'done')]['outcome'], 'not_observed_after_complete_scan')


if __name__ == '__main__':
    unittest.main()
