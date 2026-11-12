"""Local gcloud account selection for the token tool. No live GCP or Meta calls."""

import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'tests'))

import cloud_common
import webhook_connect
from sns_comment.api import PilotError
from sns_comment.gcloud_account import resolve_operator
from sns_comment.profiles import read_settings
from sns_comment.token_cli import LocalTransport, main
from account_fixtures import fixture


JOSH = 'josh@madeone.kr'
SYAN = 'syan@madeone.kr'
PROJECT = 'waterbomb-sns-pipeline'


def completed(stdout='', code=0, stderr=''):
    return subprocess.CompletedProcess([], code, stdout, stderr)


def pinned(argv):
    return [part for part in argv if str(part).startswith('--account=')]


class AuthDoubles(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.env = mock.patch.dict(os.environ, {'SNS_GCLOUD_ACCOUNT': ''})
        self.env.start()
        self.addCleanup(self.env.stop)
        cloud_common._resolved = None
        webhook_connect._operator = None
        self.which = mock.patch('sns_comment.gcloud_account.shutil.which', return_value='gcloud')
        self.run = mock.patch('sns_comment.gcloud_account.subprocess.run', side_effect=self.fake_run)
        self.which.start()
        self.run.start()
        self.addCleanup(self.which.stop)
        self.addCleanup(self.run.stop)

    def fake_run(self, argv, **kwargs):
        self.calls.append(list(argv))
        joined = ' '.join(argv)
        if 'auth' in argv and 'list' in argv and 'status:ACTIVE' in joined:
            return completed(self.active)
        if 'auth' in argv and 'list' in argv:
            return completed(self.known)
        if 'get-value' in argv:
            return completed(self.configured)
        return completed(self.command_stdout, self.command_code, self.command_stderr)

    def use(self, active=JOSH + '\n', known=None, configured='(unset)\n', stdout='', code=0, stderr=''):
        self.active = active
        self.known = known if known is not None else active
        self.configured = configured
        self.command_stdout = stdout
        self.command_code = code
        self.command_stderr = stderr

    def assert_no_syan(self):
        for argv in self.calls:
            self.assertNotIn(SYAN, ' '.join(argv))
            self.assertNotIn('--account=' + SYAN, argv)


class ActiveAccountSelectionTests(AuthDoubles):
    def test_active_account_omits_the_account_flag_and_does_not_insert_syan(self):
        self.use()
        transport = LocalTransport(PROJECT, JOSH, explicit=False)
        transport.gcloud(['auth', 'print-access-token'])
        self.assertEqual(self.calls, [['gcloud', 'auth', 'print-access-token', '--project=' + PROJECT, '--quiet']])
        self.assertEqual(pinned(self.calls[0]), [])
        self.assert_no_syan()

    def test_explicit_account_flag_is_the_only_account_attached(self):
        self.use(known=JOSH + '\n' + SYAN + '\n')
        email, explicit = resolve_operator(JOSH, SYAN, PROJECT)
        self.assertEqual((email, explicit), (JOSH, True))
        LocalTransport(PROJECT, email, explicit).gcloud(['auth', 'print-access-token'])
        operational = self.calls[-1]
        self.assertEqual(pinned(operational), ['--account=' + JOSH])
        self.assert_no_syan()

    def test_environment_overrides_a_configured_account(self):
        self.use(known=JOSH + '\n' + SYAN + '\n')
        with mock.patch.dict(os.environ, {'SNS_GCLOUD_ACCOUNT': JOSH}):
            email, explicit = resolve_operator(None, SYAN, PROJECT)
        self.assertEqual((email, explicit), (JOSH, True))
        for argv in self.calls:
            self.assertEqual(pinned(argv), [])

    def test_flag_overrides_environment_and_uses_the_logged_in_spelling(self):
        self.use(known=JOSH + '\n')
        with mock.patch.dict(os.environ, {'SNS_GCLOUD_ACCOUNT': SYAN}):
            email, explicit = resolve_operator('Josh@Madeone.kr', SYAN, PROJECT)
        self.assertEqual((email, explicit), (JOSH, True))

    def test_non_empty_config_account_is_optional_pin_not_a_default_person(self):
        self.use(known=JOSH + '\n')
        email, explicit = resolve_operator(None, JOSH, PROJECT)
        self.assertTrue(explicit)
        LocalTransport(PROJECT, email, explicit).gcloud(['secrets', 'versions', 'access', 'latest', '--secret=sns-meta-token'])
        self.assertEqual(pinned(self.calls[-1]), ['--account=' + JOSH])
        self.assert_no_syan()

    def test_several_active_accounts_follow_gcloud_config_without_a_flag(self):
        self.use(active=JOSH + '\nother@madeone.kr\n', configured=JOSH + '\n')
        email, explicit = resolve_operator(None, None, PROJECT)
        self.assertEqual((email, explicit), (JOSH, False))
        LocalTransport(PROJECT, email, explicit).gcloud(['auth', 'print-access-token'])
        self.assertEqual(pinned(self.calls[-1]), [])

    def test_missing_login_explains_gcloud_auth_login_without_asking_for_a_token(self):
        self.use(active='', known='', configured='(unset)\n')
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch('sns_comment.token_cli.prompt_meta_token') as prompt, \
                mock.patch('sns_comment.token_cli.CloudState') as state, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(['--check'])
        self.assertEqual(code, 1)
        prompt.assert_not_called()
        state.assert_not_called()
        message = stderr.getvalue()
        self.assertIn('로그인된 Google Cloud 계정이 없습니다.', message)
        self.assertIn('gcloud auth login', message)
        self.assertIn('토큰은 입력하지 않았습니다.', message)
        self.assertIn('sns-meta-token', message)
        self.assertNotIn(SYAN, message)
        self.assertNotIn('운영 연결', stdout.getvalue())
        self.assert_no_syan()

    def test_unknown_override_names_that_account_and_does_not_prompt(self):
        self.use(known=SYAN + '\n')
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch('sns_comment.token_cli.prompt_meta_token') as prompt, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(['--account', JOSH, '--check'])
        self.assertEqual(code, 1)
        prompt.assert_not_called()
        self.assertIn('gcloud auth login ' + JOSH, stderr.getvalue())
        self.assertNotIn(SYAN, stderr.getvalue())

    def test_rejected_account_value_is_not_echoed(self):
        secret = 'EAAG' + ('synthetic-token-value' * 4)
        with self.assertRaises(PilotError) as caught:
            resolve_operator(secret, None, PROJECT)
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn('synthetic-token-value', str(caught.exception))
        self.assertIn('이메일', str(caught.exception))

    def test_missing_gcloud_stops_before_the_token_prompt(self):
        with mock.patch('sns_comment.gcloud_account.shutil.which', return_value=None), \
                mock.patch('sns_comment.token_cli.prompt_meta_token') as prompt:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = main(['--check'])
        self.assertEqual(code, 1)
        prompt.assert_not_called()
        self.assertIn('Google Cloud CLI가 필요합니다.', stderr.getvalue())

    def test_permission_failure_names_the_active_account_and_does_not_touch_the_network(self):
        self.use(stdout='synthetic-access-token\n', code=1, stderr='PERMISSION_DENIED synthetic-access-token')
        opener = mock.Mock()
        opener.open.side_effect = AssertionError('network')
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch('sns_comment.cloud_state.urllib.request.build_opener', return_value=opener), \
                mock.patch('sns_comment.token_cli.prompt_meta_token') as prompt, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(['--check'])
        self.assertEqual(code, 1)
        prompt.assert_not_called()
        opener.open.assert_not_called()
        message = stderr.getvalue()
        self.assertIn(JOSH, message)
        self.assertIn('비밀 저장소는 변경하지 않았습니다.', message)
        self.assertIn('gcloud auth login', message)
        self.assertIn('sns-meta-token', message)
        self.assertNotIn('synthetic-access-token', message)
        self.assertNotIn('PERMISSION_DENIED', message)
        operational = [argv for argv in self.calls if 'print-access-token' in argv]
        self.assertEqual(len(operational), 1)
        self.assertEqual(pinned(operational[0]), [])
        self.assert_no_syan()

    def test_check_announces_the_active_account_and_keeps_stdout_free_of_the_token(self):
        self.use()
        settings, _ = fixture()
        settings['cloud']['project_id'] = PROJECT
        settings['cloud']['account'] = None
        plan = {'accounts': [{'label': '서울', 'username': 'waterbomb_official', 'id': '10001'}],
            'changes': {'kept': ['10001'], 'added': [], 'paused': []}, 'unknown': [], 'available': [], 'status': 'verified'}
        state = mock.MagicMock()
        state.json_get.return_value = ({}, 0)
        stdout = io.StringIO()
        with mock.patch('sns_comment.token_cli.read_settings', return_value=settings), \
                mock.patch('sns_comment.token_cli.CloudState', return_value=state), \
                mock.patch('sns_comment.token_cli.read_token', return_value=('synthetic-meta-token', 'projects/' + PROJECT + '/secrets/sns-meta-token/versions/3')), \
                mock.patch('sns_comment.token_cli.inspect_token', return_value=plan), \
                mock.patch('sns_comment.token_cli.prompt_meta_token') as prompt, \
                contextlib.redirect_stdout(stdout):
            code = main(['--check'])
        self.assertEqual(code, 0)
        prompt.assert_not_called()
        text = stdout.getvalue()
        self.assertIn('Google Cloud 계정(현재 활성 계정): ' + JOSH, text)
        self.assertIn('sns-meta-token', text)
        self.assertIn('저장하거나 수집을 실행하지 않았습니다.', text)
        self.assertNotIn('synthetic-meta-token', text)
        self.assertNotIn(SYAN, text)
        self.assert_no_syan()

    def test_status_json_stays_machine_readable_and_the_account_goes_to_stderr(self):
        self.use()
        state = mock.MagicMock()
        state.json_get.return_value = ({'stage': 'synced', 'delivery': []}, 1)
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch('sns_comment.token_cli.CloudState', return_value=state), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(['--status', '--json'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())['stage'], 'synced')
        self.assertIn(JOSH, stderr.getvalue())
        self.assertNotIn(JOSH, stdout.getvalue())

    def test_actor_recorded_for_a_token_update_is_the_active_account(self):
        self.use()
        settings, _ = fixture()
        settings['cloud']['project_id'] = PROJECT
        settings['cloud']['account'] = None
        plan = {'accounts': [{'label': '서울', 'username': 'waterbomb_official', 'id': '10001'}],
            'changes': {'kept': ['10001'], 'added': [], 'paused': []}, 'unknown': [], 'available': [], 'status': 'verified'}
        state = mock.MagicMock()
        state.json_get.return_value = ({}, 0)
        with mock.patch('sns_comment.token_cli.read_settings', return_value=settings), \
                mock.patch('sns_comment.token_cli.CloudState', return_value=state), \
                mock.patch('sns_comment.token_cli.read_token', return_value=('synthetic-meta-token', 'projects/' + PROJECT + '/secrets/sns-meta-token/versions/3')), \
                mock.patch('sns_comment.token_cli.inspect_token', return_value=plan), \
                mock.patch('sns_comment.token_cli.prompt_meta_token') as prompt, \
                mock.patch('sns_comment.token_cli.apply_token', return_value={'revision': 'rev'}) as apply, \
                contextlib.redirect_stdout(io.StringIO()):
            code = main(['--adopt-current'])
        self.assertEqual(code, 0)
        prompt.assert_not_called()
        self.assertEqual(apply.call_args.args[4], JOSH)
        self.assertNotIn('synthetic-meta-token', apply.call_args.args[4])

    def test_shared_cloud_commands_and_webhook_secret_read_use_the_active_account(self):
        self.use(stdout='synthetic-secret-value\n')
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            listed = cloud_common.gcloud(['storage', 'buckets', 'list'])
            cloud_common._resolved = None
            value = webhook_connect.secret('sns-meta-token')
        self.assertEqual(listed.stdout, 'synthetic-secret-value\n')
        self.assertEqual(value, 'synthetic-secret-value')
        self.assertNotIn('synthetic-secret-value', captured.getvalue())
        self.assertIn(JOSH, captured.getvalue())
        self.assertTrue(any('buckets' in argv for argv in self.calls))
        self.assertTrue(any('--secret=sns-meta-token' in argv for argv in self.calls))
        for argv in self.calls:
            self.assertEqual(pinned(argv), [])
        self.assert_no_syan()

    def test_webhook_secret_failure_does_not_include_the_secret_value(self):
        self.use(stdout='synthetic-secret-value\n', code=1, stderr='denied synthetic-secret-value')
        with self.assertRaises(PilotError) as caught, contextlib.redirect_stderr(io.StringIO()):
            webhook_connect.secret('sns-meta-token')
        self.assertIn(JOSH, str(caught.exception))
        self.assertIn('sns-meta-token', str(caught.exception))
        self.assertNotIn('synthetic-secret-value', str(caught.exception))
        self.assert_no_syan()


class ConfigAndSourceTests(unittest.TestCase):
    def test_shipped_config_does_not_pin_a_person(self):
        settings = read_settings()
        self.assertIn('account', settings['cloud'])
        self.assertIsNone(settings['cloud']['account'])

    def test_operator_entrypoints_do_not_hardcode_syan(self):
        paths = [
            'sns_comment/token_cli.py',
            'sns_comment/gcloud_account.py',
            'scripts/cloud_common.py',
            'scripts/webhook_connect.py',
            'scripts/webhook_release.py',
            'scripts/webhook_secret_input.py',
            'scripts/legacy/cloud_setup.py',
            'scripts/legacy/comment_windows_release.py',
            'README.md',
            'TOKEN-CONNECTIONS.md',
            'CLOUD.md',
        ]
        for name in paths:
            self.assertNotIn(SYAN, (ROOT / name).read_text(), name)

    def test_help_mentions_the_override_without_contacting_gcloud(self):
        stdout = io.StringIO()
        with mock.patch('sns_comment.gcloud_account.subprocess.run', side_effect=AssertionError('gcloud')), \
                contextlib.redirect_stdout(stdout), self.assertRaises(SystemExit) as caught:
            main(['--help'])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn('SNS_GCLOUD_ACCOUNT', stdout.getvalue())
        self.assertIn('--account', stdout.getvalue())


if __name__ == '__main__':
    unittest.main()
