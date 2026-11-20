from http.client import HTTPConnection
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from youtube_validation.server import make_server


class LocalAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.server = make_server(Path(self.temp.name), port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        def cleanup():
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=2)
        self.addCleanup(cleanup)
        self.port = self.server.server_address[1]

    def request(self, method, path, headers=None):
        connection = HTTPConnection('127.0.0.1', self.port, timeout=2)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode()
        finally:
            connection.close()

    def cookie(self):
        status, headers, _ = self.request('GET', urlsplit(self.server.launch_url).path)
        self.assertEqual(status, 303)
        return headers['Set-Cookie'].split(';')[0]

    def test_requires_local_session_and_has_no_cached_credentials(self):
        self.assertEqual(self.request('GET', '/')[0], 401)
        status, headers, body = self.request('GET', '/', {'Cookie': self.cookie()})
        self.assertEqual(status, 200)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['Referrer-Policy'], 'same-origin')
        self.assertIn('@waterbomb_official', body)
        self.assertIn('OAuth 클라이언트 파일 등록', body)

    def test_rejects_dns_rebinding_host(self):
        status, _, _ = self.request('GET', '/', {'Host': 'evil.example', 'Cookie': self.cookie()})
        self.assertEqual(status, 403)

    def test_rejects_cross_site_authorize_request(self):
        status, _, _ = self.request('POST', '/authorize',
            {'Cookie': self.cookie(), 'Origin': 'https://evil.example'})
        self.assertEqual(status, 403)

    def test_rejects_unsolicited_callback(self):
        status, _, body = self.request('GET', '/oauth/youtube/callback?state=wrong&code=secret-code',
                                       {'Cookie': self.cookie()})
        self.assertEqual(status, 400)
        self.assertNotIn('secret-code', body)
        self.assertFalse((Path(self.temp.name) / 'authorized-user.json').exists())

    def test_local_authorize_redirects_and_callback_is_one_use(self):
        cookie = self.cookie()
        flow = Mock()
        flow.authorization_url.return_value = ('https://accounts.google.com/o/oauth2/auth?state=one-use', 'one-use')
        with patch('youtube_validation.server.create_flow', return_value=flow):
            status, headers, _ = self.request('POST', '/authorize',
                {'Cookie': cookie, 'Origin': f'http://127.0.0.1:{self.port}'})
        self.assertEqual(status, 303)
        self.assertTrue(headers['Location'].startswith('https://accounts.google.com/'))
        with patch('youtube_validation.server.finish_flow') as finish:
            callback = '/oauth/youtube/callback?state=one-use&code=private-code'
            self.assertEqual(self.request('GET', callback, {'Cookie': cookie})[0], 303)
            self.assertEqual(self.request('GET', callback, {'Cookie': cookie})[0], 400)
            finish.assert_called_once()


if __name__ == '__main__':
    unittest.main()
