"""Windows runtime compatibility for the token CLI. No live GCP or Meta calls."""

import ast
import errno
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from sns_comment.api import PilotError
from sns_comment.gcloud_account import gcloud_command, quote_windows_arg, run_gcloud, windows_batch_command
from sns_comment.profiles import read_settings
from sns_comment.storage import run_lock
import sns_comment.token_cli as token_cli


ROOT = Path(__file__).resolve().parents[1]
GCLOUD_CMD = r'C:\Users\기\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd'


class TokenCliImportTests(unittest.TestCase):
    def test_token_cli_imports_and_fcntl_is_not_a_module_level_dependency(self):
        self.assertTrue(callable(token_cli.main))
        source = (ROOT / 'sns_comment' / 'storage.py').read_text(encoding='utf-8')
        tree = ast.parse(source)
        for node in tree.body:
            if isinstance(node, ast.Import):
                self.assertFalse(any(alias.name == 'fcntl' for alias in node.names))
            if isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.module, 'fcntl')

    def test_settings_and_lock_work_under_a_non_ascii_directory(self):
        with tempfile.TemporaryDirectory(prefix='기') as directory:
            self.assertIn('기', str(directory))
            config = Path(directory) / 'config.json'
            config.write_text((ROOT / 'config.json').read_text(encoding='utf-8'), encoding='utf-8')
            settings = read_settings(config)
            self.assertEqual(settings['cloud']['project_id'], 'waterbomb-sns-pipeline')
            database = Path(directory) / 'comments.sqlite3'
            with run_lock(database):
                with self.assertRaises(PilotError):
                    with run_lock(database):
                        self.fail('second lock must not be acquired')


class WindowsGcloudCommandTests(unittest.TestCase):
    def test_batch_command_quotes_spaces_parentheses_and_keeps_korean_paths(self):
        command = windows_batch_command([
            GCLOUD_CMD, 'auth', 'list', '--format=value(account)', '--quiet'])
        self.assertIn('cmd.exe', command)
        self.assertIn('/d /v:off /s /c', command)
        self.assertIn('gcloud.cmd', command)
        self.assertIn('기', command)
        self.assertIn('"--format=value(account)"', command)
        self.assertNotIn('.ps1', command.lower())
        self.assertNotIn('powershell', command.lower())
        self.assertEqual(quote_windows_arg('100%'), '"100%%"')

    def test_windows_resolver_uses_gcloud_cmd_instead_of_the_powershell_script(self):
        def which(name):
            if name == 'gcloud.cmd':
                return GCLOUD_CMD
            if name == 'gcloud':
                return r'C:\Users\기\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.ps1'
            return None

        with mock.patch('sns_comment.gcloud_account.WINDOWS', True), \
                mock.patch('sns_comment.gcloud_account.shutil.which', side_effect=which), \
                mock.patch.dict(os.environ, {'ComSpec': r'C:\Windows\System32\cmd.exe'}):
            command = gcloud_command(['auth', 'list'], project='waterbomb-sns-pipeline')
        self.assertIsInstance(command, str)
        self.assertIn(GCLOUD_CMD, command)
        self.assertNotIn('gcloud.ps1', command)
        self.assertIn('--project=waterbomb-sns-pipeline', command)
        self.assertNotIn('shell=True', command)

    def test_windows_run_does_not_use_shell_and_decodes_utf8(self):
        completed = mock.Mock(returncode=0, stdout='', stderr='')
        with mock.patch('sns_comment.gcloud_account.WINDOWS', True), \
                mock.patch('sns_comment.gcloud_account.shutil.which', return_value=GCLOUD_CMD), \
                mock.patch('sns_comment.gcloud_account.subprocess.run', return_value=completed) as run, \
                mock.patch.dict(os.environ, {'ComSpec': r'C:\Windows\System32\cmd.exe'}):
            run_gcloud(['version'], cwd=r'C:\Users\기\sns-comment-pipeline')
        args, kwargs = run.call_args
        self.assertIsInstance(args[0], str)
        self.assertIn('gcloud.cmd', args[0])
        self.assertFalse(kwargs.get('shell', False))
        self.assertEqual(kwargs['encoding'], 'utf-8')
        self.assertEqual(kwargs['cwd'], r'C:\Users\기\sns-comment-pipeline')

    def test_posix_gcloud_command_stays_a_plain_argument_list(self):
        with mock.patch('sns_comment.gcloud_account.WINDOWS', False):
            command = gcloud_command(['auth', 'list'], project='waterbomb-sns-pipeline')
        self.assertEqual(command[0], 'gcloud')
        self.assertIn('--project=waterbomb-sns-pipeline', command)
        self.assertNotIn('--account=syan@madeone.kr', command)


class WindowsLockTests(unittest.TestCase):
    def test_windows_lock_uses_msvcrt_and_rejects_a_busy_file(self):
        class Msvcrt:
            LK_NBLCK = 2
            LK_UNLCK = 0

            def __init__(self):
                self.calls = []
                self.held = False

            def locking(self, fd, mode, nbytes):
                self.calls.append((mode, nbytes))
                if mode == self.LK_NBLCK:
                    if self.held:
                        error = OSError(errno.EACCES, 'locked')
                        error.winerror = 33
                        raise error
                    self.held = True
                elif mode == self.LK_UNLCK:
                    self.held = False

        fake = Msvcrt()
        with tempfile.TemporaryDirectory(prefix='기') as directory:
            database = Path(directory) / 'comments.sqlite3'
            with mock.patch('sns_comment.storage.WINDOWS', True), \
                    mock.patch.dict('sys.modules', {'msvcrt': fake}):
                with run_lock(database):
                    self.assertTrue(fake.held)
                    with self.assertRaises(PilotError) as caught:
                        with run_lock(database):
                            self.fail('busy lock must stop the second caller')
                self.assertIn('이미 실행 중', str(caught.exception))
        self.assertIn((fake.LK_NBLCK, 1), fake.calls)
        self.assertIn((fake.LK_UNLCK, 1), fake.calls)
        self.assertFalse(fake.held)

    @unittest.skipUnless(os.name != 'nt', 'POSIX keeps fcntl.flock')
    def test_posix_lock_still_uses_fcntl_flock(self):
        import fcntl
        calls = []
        real = fcntl.flock

        def spy(file, mode):
            calls.append(mode)
            return real(file, mode)

        with tempfile.TemporaryDirectory() as directory, mock.patch('fcntl.flock', spy):
            with run_lock(Path(directory) / 'comments.sqlite3'):
                pass
        self.assertIn(fcntl.LOCK_EX | fcntl.LOCK_NB, calls)
        self.assertIn(fcntl.LOCK_UN, calls)


if __name__ == '__main__':
    unittest.main()
