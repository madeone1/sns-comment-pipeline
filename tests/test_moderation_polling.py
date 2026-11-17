"""Polling gaps, pagination, durable overflow, and failure recovery."""

import copy
import datetime as dt
import unittest
from unittest import mock

from sns_comment.api import GraphError, PilotError, RequestBudgetExceeded
from sns_comment.moderation import decision_name
from sns_comment.moderation_job import comment_from_payload, sweep_account
from sns_comment.moderation_polling import Polling, state_name
from tests.test_moderation import ACCOUNT, DRY, MemoryState, Model, answer


NOW = dt.datetime(2026, 11, 17, 3, 0, tzinfo=dt.timezone.utc)
CONFIG = {'account_id': ACCOUNT, 'expected_username': 'waterbomb_toronto_official', 'region_label': '토론토'}


def comment(identifier, minutes=35, replies=None):
    row = {'id': str(identifier), 'text': '🔥', 'username': 'fan',
           'timestamp': (NOW - dt.timedelta(minutes=minutes)).isoformat()}
    row['replies'] = replies if replies is not None else {'data': []}
    return row


def page(rows, after=None):
    value = {'data': rows}
    if after:
        value['paging'] = {'next': 'https://graph.facebook.com/v26.0/1/comments?after=' + after}
    return value


class Api:
    def __init__(self, pages=None):
        self.pages = pages or {}
        self.calls = []

    def get(self, path, **params):
        key = (path, params.get('after'))
        self.calls.append(key)
        value = self.pages.get(key, {'data': []})
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)


def polling(state=None, now=NOW, **settings):
    return Polling(state or MemoryState(), ACCOUNT, CONFIG['expected_username'],
                   {'interval_minutes': 30, 'max_age_minutes': 20, **settings}, now, comment_from_payload)


class PollingTests(unittest.TestCase):
    def test_retired_daily_exits_before_authentication_collection_or_reporting(self):
        from sns_comment import cloud
        with mock.patch.object(cloud, 'CloudState') as state, mock.patch.object(cloud, 'cloud_credentials') as oauth:
            self.assertEqual(cloud.main([]), 0)
        state.assert_not_called()
        oauth.assert_not_called()

    def test_newest_page_is_refreshed_while_old_pagination_is_unfinished(self):
        state = MemoryState()
        p = polling(state)
        api = Api({(ACCOUNT + '/media', None): page([{'id': '20001'}]),
                   ('20001/comments', None): page([comment(30001)], 'old')})
        p.scan(api, 'recent', 2)
        later = polling(state, NOW + dt.timedelta(minutes=30))
        api.pages[('20001/comments', None)] = page([comment(30002, 0)], 'old')
        later.peek(api)
        self.assertIn('30002', later.data['queue'])
        self.assertEqual(later.data['recent']['tasks'][0]['after'], 'old')

    def test_thirty_minute_schedule_includes_35_and_60_minute_boundaries(self):
        p = polling()
        api = Api({(ACCOUNT + '/media', None): page([{'id': '20001'}]),
                   ('20001/comments', None): page([comment(30001, 35), comment(30002, 60), comment(30003, 61)])})
        p.scan(api, 'recent', 10)
        self.assertEqual(set(p.data['queue']), {'30001', '30002'})
        self.assertEqual(p.data['recent']['completed_through'], NOW.isoformat())

    def test_delayed_run_starts_at_last_complete_checkpoint_with_overlap(self):
        state = MemoryState()
        p = polling(state)
        p.scan(Api(), 'recent', 2)
        later = NOW + dt.timedelta(hours=4)
        again = polling(state, later)
        again.scan(Api(), 'recent', 2)
        self.assertEqual(again.data['recent']['since'], (NOW - dt.timedelta(minutes=10)).isoformat())

    def test_second_comment_page_and_nested_reply_page_are_not_dropped(self):
        p = polling()
        replies = page([comment(31001, 20)], 'r2')
        api = Api({(ACCOUNT + '/media', None): page([{'id': '20001'}]),
                   ('20001/comments', None): page([comment(30001, 300, replies)], 'c2'),
                   ('20001/comments', 'c2'): page([comment(30002)]),
                   ('30001/replies', 'r2'): page([comment(31002, 10)])})
        p.scan(api, 'recent', 10)
        self.assertEqual(set(p.data['queue']), {'30002', '31001', '31002'})
        self.assertEqual(p.data['queue']['31002']['parent_id'], '30001')
        self.assertIsNotNone(p.data['recent']['completed_through'])

    def test_catalog_follows_media_pages_and_checks_replies_on_old_parents(self):
        p = polling()
        api = Api({(ACCOUNT + '/media', None): page([{'id': '20001'}], 'media2'),
                   (ACCOUNT + '/media', 'media2'): page([{'id': '20002'}]),
                   ('20002/comments', None): page([comment(30001, 9000)]),
                   # An omitted expanded replies field must be queried explicitly.
                   ('30001/replies', None): page([comment(31001, 5)])})
        api.pages[('20002/comments', None)]['data'][0].pop('replies')
        p.scan(api, 'catalog', 20)
        self.assertIn('31001', p.data['queue'])
        self.assertNotIn('30001', p.data['queue'])
        self.assertIn((ACCOUNT + '/media', 'media2'), api.calls)

    def test_page_budget_keeps_frozen_window_and_resumes_after_outage(self):
        state = MemoryState()
        api = Api({(ACCOUNT + '/media', None): page([{'id': '20001'}]),
                   ('20001/comments', None): page([comment(30001)], 'next'),
                   ('20001/comments', 'next'): page([comment(30002)])})
        p = polling(state)
        p.scan(api, 'recent', 2)
        floor = p.data['recent']['since']
        self.assertEqual(p.data['recent']['tasks'][0]['after'], 'next')
        self.assertIsNone(p.data['recent']['completed_through'])
        later = polling(state, NOW + dt.timedelta(days=2))
        later.scan(api, 'recent', 2)
        self.assertEqual(later.data['recent']['since'], floor)
        self.assertEqual(later.data['recent']['completed_through'], NOW.isoformat())
        self.assertEqual(set(later.data['queue']), {'30001', '30002'})

    def test_api_failure_preserves_page_and_already_fetched_comments(self):
        state = MemoryState()
        api = Api({(ACCOUNT + '/media', None): page([{'id': '20001'}]),
                   ('20001/comments', None): page([comment(30001)], 'next'),
                   ('20001/comments', 'next'): GraphError(429, 4)})
        p = polling(state)
        p.scan(api, 'recent', 10)
        self.assertIsInstance(p.error, GraphError)
        self.assertIn('30001', state.data[p.name]['queue'])
        self.assertEqual(state.data[p.name]['recent']['tasks'][0]['after'], 'next')
        api.pages[('20001/comments', 'next')] = page([comment(30002)])
        again = polling(state)
        again.scan(api, 'recent', 10)
        self.assertEqual(set(again.data['queue']), {'30001', '30002'})

    def test_repeating_cursor_does_not_mark_scan_complete(self):
        p = polling()
        api = Api({(ACCOUNT + '/media', None): page([{'id': '20001'}]),
                   ('20001/comments', None): page([], 'loop'),
                   ('20001/comments', 'loop'): page([], 'loop')})
        p.scan(api, 'recent', 10)
        self.assertIsInstance(p.error, PilotError)
        self.assertIsNone(p.data['recent']['completed_through'])

    def test_request_budget_preserves_checkpoint(self):
        p = polling()
        p.scan(Api({(ACCOUNT + '/media', None): RequestBudgetExceeded('budget')}), 'recent', 10)
        self.assertIsNone(p.data['recent']['completed_through'])
        self.assertEqual(p.data['recent']['tasks'], [{'kind': 'media', 'path': ACCOUNT + '/media'}])

    def test_expired_cursor_restarts_same_edge_without_moving_the_time_floor(self):
        p = polling()
        api = Api({(ACCOUNT + '/media', None): page([{'id': '20001'}]),
                   ('20001/comments', None): page([comment(30001)], 'expired'),
                   ('20001/comments', 'expired'): GraphError(400, 100)})
        p.scan(api, 'recent', 10)
        self.assertNotIn('after', p.data['recent']['tasks'][0])
        self.assertIn('30001', p.data['queue'])
        self.assertIsNone(p.data['recent']['completed_through'])

    def test_api_budget_rotates_account_order_on_next_scheduled_run(self):
        from sns_comment.moderation_job import sweep
        configs = [{'account_id': str(10000 + i)} for i in range(4)]
        orders = []
        def process(config, *_args, **_kwargs):
            orders[-1].append(config['account_id'])
            return {'status': 'pending'}
        with mock.patch('sns_comment.moderation_job.sweep_account', side_effect=process):
            for i in range(4):
                orders.append([])
                sweep(configs, MemoryState(), None, None, DRY, now=NOW + dt.timedelta(minutes=30 * i))
        self.assertEqual({order[0] for order in orders}, {c['account_id'] for c in configs})

    def test_webhook_ack_does_not_starve_events_after_first_batch_or_delete_evidence(self):
        state = MemoryState()
        pages = {('20001', None): {'permalink': 'https://www.instagram.com/p/example/'}}
        for i in range(1, 51):
            state.json_put(f'webhook/pending/{ACCOUNT}/{i:03}.json',
                           {'account_id': ACCOUNT, 'comment_id': str(30000 + i), 'media_id': '20001'})
            pages[(str(30000 + i), None)] = comment(30000 + i)
        p = polling(state)
        p.webhooks(Api(pages))
        again = polling(state)
        again.webhooks(Api(pages))
        third = polling(state)
        third.webhooks(Api(pages))
        self.assertEqual(len(third.data['queue']), 50)
        self.assertEqual(len(third.data['receipts']), 50)
        self.assertEqual(len([k for k in state.data if k.startswith('webhook/pending/')]), 50)

    def test_unavailable_webhook_does_not_block_new_receipts_or_api_polling(self):
        state = MemoryState()
        for i in range(25):
            state.json_put(f'webhook/pending/{ACCOUNT}/{i:03}.json',
                           {'account_id': ACCOUNT, 'comment_id': str(30000 + i), 'media_id': '20001'})
        api = Api({(str(30000 + i), None): GraphError(400, 100, 33) for i in range(24)})
        api.pages[('30024', None)] = comment(30024)
        p = polling(state)
        p.webhooks(api)
        self.assertFalse(p.blocked)
        again = polling(state)
        again.webhooks(api)
        self.assertIn('30024', again.data['queue'])

    def test_overflow_beyond_40_survives_and_next_run_classifies_it_once(self):
        state = MemoryState()
        api = Api({(ACCOUNT + '/media', None): page([{'id': '20001'}]),
                   ('20001/comments', None): page([comment(30000 + i) for i in range(65)])})
        result = sweep_account(CONFIG, state, api, Model({}), DRY, NOW)
        self.assertEqual(result['classified'], 40)
        self.assertEqual(len(state.data[state_name(ACCOUNT)]['queue']), 25)
        # Repeated polling must not put the same first 40 ahead of the overflow.
        result = sweep_account(CONFIG, state, api, Model({}), DRY, NOW + dt.timedelta(minutes=30))
        self.assertEqual(result['classified'], 25)
        self.assertEqual(len(state.data[state_name(ACCOUNT)]['queue']), 0)
        self.assertEqual(len([n for n in state.data if n.startswith('moderation/decisions/')]), 65)

    def test_classification_failure_leaves_raw_queue_for_replay(self):
        state = MemoryState()
        raw = comment(30001)
        raw['text'] = '질문입니다'
        api = Api({(ACCOUNT + '/media', None): page([{'id': '20001'}]),
                   ('20001/comments', None): page([raw])})
        class Broken:
            def classify_comments(self, *_):
                raise PilotError('synthetic model error')
        with self.assertRaises(PilotError):
            sweep_account(CONFIG, state, api, Broken(), DRY, NOW)
        self.assertIn('30001', state.data[state_name(ACCOUNT)]['queue'])
        model = Model({'30001': {**answer('유지', 'K1'), 'id': '30001'}})
        sweep_account(CONFIG, state, Api(), model, DRY, NOW + dt.timedelta(hours=3))
        self.assertIn(decision_name(ACCOUNT, '30001'), state.data)
        self.assertNotIn('30001', state.data[state_name(ACCOUNT)]['queue'])


if __name__ == '__main__':
    unittest.main()
