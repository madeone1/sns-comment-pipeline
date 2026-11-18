import contextlib
import os
import unittest
from unittest import mock

from sns_comment.api import GraphError, PilotError
from sns_comment import moderation_release_check as check
from sns_comment.connection_registry import REGISTRY_OBJECT
from sns_comment.profiles import read_settings


class ModerationReleaseCheckTests(unittest.TestCase):
    def test_settings_require_full_overlap_and_daily_retirement(self):
        settings = read_settings()
        check.check_settings(settings)
        settings['moderation']['max_age_minutes'] = 20
        with self.assertRaises(PilotError):
            check.check_settings(settings)
        settings = read_settings()
        settings['cloud']['daily_enabled'] = True
        with self.assertRaises(PilotError):
            check.check_settings(settings)

    def test_candidate_rejects_write_flags_before_reading_credentials(self):
        shared = mock.Mock()
        with mock.patch.dict(os.environ, {'SNS_MODERATION_HIDE': '1'}):
            with self.assertRaises(PilotError):
                check.verify(read_settings(), shared)
        shared.json_get.assert_not_called()

    def test_release_requires_bounded_read_concurrency_and_account_budget(self):
        for key, value in [('poll_workers', 20), ('poll_requests_per_account', 2000)]:
            with self.subTest(key=key):
                settings = read_settings()
                settings['moderation'][key] = value
                with self.assertRaises(PilotError):
                    check.check_settings(settings)


SOKCHO = '17841455831114060'
TORONTO = '17841454752492067'


class FakeGraph:
    def __init__(self, account_id, username, fail=None):
        self.account_id, self.username, self.fail = account_id, username, fail
        self.request_count = 0
        self.calls = []

    def get(self, path, **params):
        self.request_count += 1
        self.calls.append((path, params.get('after')))
        if '/' not in path:
            return {'id': self.account_id, 'username': self.username}
        if isinstance(self.fail, GraphError) and self.fail.code == 100:
            if path.endswith('/comments') and params.get('after') == 'stale':
                raise self.fail
        elif self.fail and (path.endswith('/media') or path.endswith('/comments') or path.endswith('/replies')):
            raise self.fail
        if path.endswith('/media'):
            return {'data': [{'id': '20002', 'permalink': 'https://www.instagram.com/p/ok/'}]}
        return {'data': []}

    def metrics(self):
        return {'peak_concurrent_requests': 1}


class Shared:
    def __init__(self, checkpoint=None):
        self.registry = {'schema': 1, 'accounts': {}, 'groups': {}}
        self.checkpoint = checkpoint or {}
        self.writes = []

    def json_get(self, name, default=None):
        if name == REGISTRY_OBJECT:
            return self.registry, 7
        if name.startswith('moderation/polling/'):
            return self.checkpoint, 1
        return {} if default is None else default, 0

    def json_put(self, name, value, generation=None):
        self.writes.append(name)
        return 1

    def lease(self, *_args):
        return contextlib.nullcontext()

    transport = object()


class ReleaseScanTests(unittest.TestCase):
    def _verify(self, graphs, checkpoint=None, notion_error=None, token_error=None):
        settings = read_settings()
        configs = [
            {'account_id': SOKCHO, 'expected_username': 'waterbomb_sokcho_official', 'region_label': '속초'},
            {'account_id': TORONTO, 'expected_username': 'waterbomb_toronto_official', 'region_label': '토론토'},
        ]
        made = []

        def client(_token, _version, **_kwargs):
            graph = graphs[len(made)]
            made.append(graph)
            return graph

        class Notion:
            def __init__(self, _auth):
                pass

            def call(self, name, arguments):
                if notion_error:
                    raise notion_error
                return {'id': arguments['id']}

        shared = Shared(checkpoint)
        with mock.patch.dict(os.environ, {'SNS_NOTION_OAUTH_SECRET': 'projects/test/secrets/sns-notion-oauth'}), \
             mock.patch.object(check, 'runtime_configs', return_value=configs), \
             mock.patch.object(check, 'read_token', side_effect=token_error or (lambda *_args: ('token', 'name'))), \
             mock.patch.object(check, 'GraphClient', side_effect=client), \
             mock.patch.object(check, 'open_sheet', return_value=object()), \
             mock.patch.object(check, '_read_rows', return_value=[]), \
             mock.patch.object(check, 'cloud_credentials', return_value=mock.Mock()), \
             mock.patch.object(check, 'McpClient', Notion):
            return check.verify(settings, shared), made

    def test_a_catalog_cursor_error_warns_and_the_next_account_still_verifies(self):
        checkpoint = {'catalog': {
            'since': '2026-11-01T00:00:00+00:00', 'through': '2026-11-18T00:00:00+00:00',
            'completed_through': None, 'visited': [], 'tasks': [
                {'kind': 'comments', 'path': '20001/comments', 'media_id': '20001', 'after': 'stale'},
                {'kind': 'comments', 'path': '20002/comments', 'media_id': '20002'},
            ]}}
        result, graphs = self._verify([
            FakeGraph(SOKCHO, 'waterbomb_sokcho_official', fail=GraphError(400, 100)),
            FakeGraph(TORONTO, 'waterbomb_toronto_official'),
        ], checkpoint)
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(result['notion_read'], True)
        self.assertTrue(any('20001/comments' in item for item in result['accounts'][0]['warnings']))
        self.assertIn(('20002/comments', None), graphs[0].calls)
        self.assertNotIn(('20001/comments', 'stale'), graphs[0].calls[graphs[0].calls.index(('20001/comments', 'stale')) + 1:])
        self.assertEqual(result['accounts'][1]['warnings'], [])

    def test_a_rate_limit_is_a_warning_and_token_notion_and_scan_bugs_still_fail(self):
        result, _graphs = self._verify([
            FakeGraph(SOKCHO, 'waterbomb_sokcho_official', fail=GraphError(429, 4)),
            FakeGraph(TORONTO, 'waterbomb_toronto_official'),
        ])
        self.assertEqual(result['status'], 'verified')
        self.assertTrue(any('API 호출 제한' in item for item in result['accounts'][0]['warnings']))
        with self.assertRaises(PilotError):
            self._verify([], token_error=PilotError('토큰을 읽지 못했습니다.'))
        with self.assertRaises(PilotError):
            self._verify([
                FakeGraph(SOKCHO, 'waterbomb_sokcho_official'),
                FakeGraph(TORONTO, 'waterbomb_toronto_official'),
            ], notion_error=PilotError('Notion 연결이 없습니다.'))
        with self.assertRaises(PilotError):
            self._verify([
                FakeGraph(SOKCHO, 'waterbomb_sokcho_official', fail=PilotError('댓글 조회 페이지 커서가 반복됩니다.')),
                FakeGraph(TORONTO, 'waterbomb_toronto_official'),
            ])


if __name__ == '__main__':
    unittest.main()
