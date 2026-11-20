from http.client import HTTPConnection
from pathlib import Path
import json
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from youtube_validation.review_demo import make_review_server, sample


class ReviewDemoTests(unittest.TestCase):
    def test_review_pages_and_sample_require_the_local_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            server=make_review_server(Path(tmp),port=0)
            thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
            def request(method,path,headers=None):
                conn=HTTPConnection('127.0.0.1',server.server_port,timeout=3)
                try:
                    conn.request(method,path,headers=headers or {})
                    result=conn.getresponse()
                    return result.status,dict(result.getheaders()),result.read().decode()
                finally: conn.close()
            try:
                self.assertEqual(request('GET','/evidence')[0],401)
                _,headers,_=request('GET',urlsplit(server.launch_url).path)
                cookie=headers['Set-Cookie'].split(';')[0]
                status,headers,body=request('GET','/privacy',{'Cookie':cookie})
                self.assertEqual(status,200)
                self.assertIn('https://www.waterbombfestival.com/privacy',body)
                self.assertIn('21-day retention',body)
                self.assertIn('Stored-copy deletion does not delete original YouTube comments.',body)
                self.assertEqual(headers['Cache-Control'],'no-store')
                with patch('youtube_validation.review_demo.sample') as runner:
                    self.assertEqual(request('POST','/sample',{'Cookie':cookie,'Origin':'https://evil.example'})[0],403)
                    runner.assert_not_called()
                self.assertEqual(request('GET','/privacy',{'Cookie':cookie,'Host':'evil.example'})[0],403)
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=3)

    def test_live_sample_persists_only_redacted_evidence(self):
        class API:
            requests=0
            def get(self,resource,**params):
                self.requests+=1
                if resource=='channels':
                    return {'items':[{'id':'UCSk-o7uCpprI1GNvmt070cQ','snippet':{'title':'WATERBOMB'}}]}
                if resource=='commentThreads':
                    return {'items':[{'snippet':{'topLevelComment':{'id':'private-comment-id','snippet':{'textOriginal':'PRIVATE BODY','authorDisplayName':'PRIVATE AUTHOR'}},'totalReplyCount':1}}]}
                return {'items':[{'id':'private-reply','snippet':{'textOriginal':'PRIVATE REPLY'}}]}
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp); credentials=Mock(); credentials.to_json.return_value='{"refresh_token":"PRIVATE TOKEN"}'
            api=API()
            with patch('youtube_validation.review_demo.YouTube',return_value=api),patch('youtube_validation.review_demo.load_credentials',return_value=credentials):
                result=sample(state)
            self.assertEqual(result['api_requests'],6)
            self.assertEqual(len(result['responses']),4)
            evidence=(state/'review-evidence.json').read_text()
            for secret in ('PRIVATE BODY','PRIVATE AUTHOR','PRIVATE REPLY','PRIVATE TOKEN','private-comment-id'):
                self.assertNotIn(secret,evidence)


if __name__=='__main__': unittest.main()
