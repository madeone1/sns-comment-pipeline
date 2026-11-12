"""Windows token launcher and hidden input. No live GCP or Meta calls."""

import contextlib
import io
import unittest
from pathlib import Path
from unittest import mock

import getpass

from sns_comment import credentials
from sns_comment.api import PilotError


ROOT = Path(__file__).resolve().parents[1]
BAT = ROOT / 'change-access-token.bat'
PS1 = ROOT / 'change-access-token.ps1'


class WindowsLauncherFileTests(unittest.TestCase):
    def setUp(self):
        self.bat = BAT.read_bytes()
        self.ps1 = PS1.read_bytes()
        self.script = self.ps1.decode('utf-8-sig')

    def test_batch_file_is_ascii_crlf_and_prepares_utf8_python(self):
        self.assertTrue(self.bat.endswith(b'\r\n'))
        self.assertNotIn(b'\n', self.bat.replace(b'\r\n', b''))
        self.assertTrue(all(byte < 128 for byte in self.bat))
        text = self.bat.decode('ascii')
        self.assertIn('cd /d "%~dp0"', text)
        self.assertIn('chcp 65001', text)
        self.assertIn('PYTHONUTF8=1', text)
        self.assertIn('PYTHONIOENCODING=utf-8', text)
        self.assertIn('change-access-token.ps1" %*', text)
        self.assertIn('exit /b %RESULT%', text)

    def test_powershell_launcher_matches_the_mac_wrapper_contract(self):
        self.assertTrue(self.ps1.startswith(b'\xef\xbb\xbf'))
        self.assertNotIn(b'\n', self.ps1.replace(b'\r\n', b''))
        self.assertIn('sns_comment.token_cli', self.script)
        self.assertIn("('gcloud.cmd', 'gcloud')", self.script)
        self.assertIn('*.ps1', self.script)
        self.assertIn('ValueFromRemainingArguments', self.script)
        self.assertIn('https://www.python.org/downloads/windows/', self.script)
        self.assertIn('https://cloud.google.com/sdk/docs/install', self.script)
        self.assertIn('gcloud auth login josh@madeone.kr', self.script)
        self.assertIn('Enter를 누르면 창을 닫습니다.', self.script)
        self.assertIn("version_info >= (3, 12)", self.script)
        self.assertIn('WindowsApps', self.script)
        self.assertIn("-3.12", self.script)
        self.assertNotIn('pip install', self.script.lower())
        self.assertNotIn('-m venv', self.script)
        self.assertNotIn('virtualenv', self.script.lower())
        # Arguments such as --check are forwarded; the script does not call gcloud itself.
        self.assertNotIn('gcloud auth', self.script.replace('gcloud auth login josh@madeone.kr', ''))
        self.assertNotIn('secrets ', self.script)

    def test_line_ending_attributes_keep_windows_launchers_usable(self):
        attributes = (ROOT / '.gitattributes').read_text(encoding='utf-8')
        self.assertIn('*.bat text eol=crlf', attributes)
        self.assertIn('*.ps1 text eol=crlf', attributes)


class WindowsHiddenInputTests(unittest.TestCase):
    def test_windows_getpass_accepts_a_pasted_token_without_echoing_it(self):
        prompt = '운영 연결 Meta 토큰(숨김 입력): '
        shown = []
        keys = list('secret-token\r')

        class Msvcrt:
            def putwch(self, char):
                shown.append(char)

            def getwch(self):
                return keys.pop(0)

        previous = getattr(getpass, 'msvcrt', None)
        getpass.msvcrt = Msvcrt()
        try:
            with mock.patch.object(getpass.sys, 'stdin', getpass.sys.__stdin__):
                value = getpass.win_getpass(prompt)
        finally:
            if previous is None:
                del getpass.msvcrt
            else:
                getpass.msvcrt = previous
        self.assertEqual(value, 'secret-token')
        self.assertEqual(''.join(shown), prompt + '\r\n')
        self.assertNotIn('secret-token', ''.join(shown))

    def test_windows_getpass_backspace_edits_the_hidden_value(self):
        shown = []
        keys = list('ab\bc\r')

        class Msvcrt:
            def putwch(self, char):
                shown.append(char)

            def getwch(self):
                return keys.pop(0)

        getpass.msvcrt = Msvcrt()
        try:
            with mock.patch.object(getpass.sys, 'stdin', getpass.sys.__stdin__):
                value = getpass.win_getpass('token: ')
        finally:
            del getpass.msvcrt
        self.assertEqual(value, 'ac')
        self.assertEqual(''.join(shown), 'token: \r\n')

    def test_windows_prompt_uses_getpass_and_does_not_print_the_token(self):
        token = 'EAA' + 'synthetic-hidden'
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(credentials.sys, 'platform', 'win32'), \
                mock.patch.object(credentials.sys.stdin, 'isatty', return_value=True), \
                mock.patch.object(credentials.getpass, 'getpass', return_value='  ' + token + '  ') as prompt, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            value = credentials.prompt_meta_token('운영 연결')
        self.assertEqual(value, token)
        self.assertEqual(prompt.call_args.args[0], '운영 연결 Meta 토큰(숨김 입력): ')
        self.assertNotIn(token, prompt.call_args.args[0])
        self.assertNotIn(token, stdout.getvalue() + stderr.getvalue())

    def test_windows_prompt_stops_when_the_console_cannot_hide_input(self):
        with mock.patch.object(credentials.sys, 'platform', 'win32'), \
                mock.patch.object(credentials.sys.stdin, 'isatty', return_value=True), \
                mock.patch.object(credentials.getpass, 'getpass', side_effect=getpass.GetPassWarning('echo')):
            with self.assertRaises(PilotError) as caught:
                credentials.prompt_meta_token('운영 연결')
        self.assertIn('숨김 입력', str(caught.exception))
        self.assertNotIn('echo', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
