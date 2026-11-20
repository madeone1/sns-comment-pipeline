"""Candidate verification for an account that has no saved run yet."""

import unittest

from sns_comment.account_tabs import reconcile
from sns_comment.api import PilotError
from sns_comment.release_check import load_account_view
from sns_comment.workflow import REGIONAL_HEADERS, region_tab


TORONTO = '17841454752492067'
DAY = '2026-11-11'


class State:
    def __init__(self, objects=None):
        self.objects = objects or {}
        self.writes = []

    def json_get(self, name, default=None):
        if name in self.objects:
            return self.objects[name], 1
        return default, 0

    def json_put(self, name, value, generation=None):
        self.writes.append(name)
        return 2


class Client:
    def __init__(self, fail=False):
        self.paths = []
        self.fail = fail

    def get(self, path, **params):
        self.paths.append(path)
        if self.fail:
            from sns_comment.api import GraphError
            raise GraphError(400, 100)
        if path.endswith('/media'):
            return {'data': [{'id': '900'}]}
        if path.endswith('/comments'):
            return {'data': [{'id': '1'}]}
        return {'id': path, 'username': 'waterbomb_toronto_official'}


def config():
    return {'account_id': TORONTO, 'expected_username': 'waterbomb_toronto_official',
            'region_label': '토론토', 'api_version': 'v26.0',
            '_credential_secret_version': 'projects/synthetic/secrets/sns-meta-token/versions/3'}


class NewAccountVerificationTests(unittest.TestCase):
    def test_missing_source_is_a_read_only_preview_and_plans_the_toronto_tab(self):
        state = State()
        client = Client()
        view = load_account_view(config(), state, DAY, lambda item: client)
        self.assertEqual(view['history'], 'absent')
        self.assertEqual(view['checks'], {'account': True, 'media': True, 'comments': True})
        self.assertEqual(client.paths[0], TORONTO)
        self.assertIn(TORONTO + '/media', client.paths)
        self.assertTrue(any(path.endswith('/comments') for path in client.paths))
        self.assertEqual(state.writes, [])
        self.assertIsNone(view['counts']['comments_and_replies'])
        self.assertEqual(region_tab('waterbomb_toronto_official', '토론토'), '토론토')
        projection = view['projection']
        self.assertEqual(projection['regional_tabs'], {'토론토': TORONTO})
        roster = [{'id': TORONTO, 'username': 'waterbomb_toronto_official', 'label': '토론토', 'active': True}]
        _, requests, bindings = reconcile(projection, {'sheets': []}, {}, REGIONAL_HEADERS, roster)
        self.assertEqual(bindings[TORONTO], '토론토')
        titles = [item['addSheet']['properties']['title'] for item in requests if 'addSheet' in item]
        self.assertIn('토론토', titles)
        rendered = '\n'.join(str(block) for block in view['section'])
        self.assertIn('이전 수집 기록 없음', rendered)
        self.assertIn('@waterbomb_toronto_official', rendered)

    def test_a_saved_run_is_replayed_without_another_account_read(self):
        period = {'key': 'daily', 'label': '일일', 'target_day': DAY,
                  'start': '2026-11-10T23:00:00+00:00', 'end': '2026-11-11T23:00:00+00:00', 'media_start': '2026-11-10T23:00:00+00:00'}
        counts = {'comments_and_replies': 2, 'parent_comments': 2, 'replies': 0, 'audience_comments': 2,
                  'unique_known_authors': 1, 'unknown_author_comments': 0, 'text_unavailable': 0}
        comment = {'id': '1', 'text': '일정', 'is_owner': 0, 'author_id': 'a', 'media_id': 'm', 'parent_id': None,
                   'written_at': '2026-11-11T01:00:00+00:00', 'payload_hash': '1', 'permalink': 'https://www.instagram.com/p/example/'}
        view_body = {'period': period, 'comments': [comment], 'counts': counts, 'media': []}
        source = {'views': [view_body], 'recent_seven_days': {**view_body, 'period': {**period, 'key': 'recent_seven_days'}},
                  'collection': {'status': 'complete', 'account_id': TORONTO, 'run_id': 'run'}}
        state = State({'runs/' + DAY + '/source.json': source,
                       'runs/' + DAY + '/daily-analysis.json': {'annotations': {}},
                       'state/reports.json': {DAY: {'sections': {'daily': ['saved-section']}}}})
        called = []
        view = load_account_view(config(), state, DAY, lambda item: called.append(item))
        self.assertEqual(view['history'], 'replay')
        self.assertEqual(called, [])
        self.assertEqual(view['section'], ['saved-section'])
        self.assertEqual(view['counts']['comments_and_replies'], 2)
        self.assertEqual(state.writes, [])

    def test_unreadable_new_account_stops_before_a_sheet_preview(self):
        state = State()
        with self.assertRaises(PilotError):
            load_account_view(config(), state, DAY, lambda item: Client(fail=True))
        self.assertEqual(state.writes, [])


if __name__ == '__main__':
    unittest.main()
