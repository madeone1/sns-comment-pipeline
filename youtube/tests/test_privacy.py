from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock,patch

from youtube_validation.api import ValidationError
from youtube_validation.pipeline import initialize,apply_page,EXPECTED_CHANNEL
from youtube_validation import privacy
from youtube_validation.privacy_admin import prepare

NOW=datetime(2026,11,16,4,tzinfo=timezone.utc)


class PrivacyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'test.sqlite3'
        self.db=sqlite3.connect(self.path);self.addCleanup(self.db.close)
        initialize(self.db);privacy.initialize(self.db)
        self.db.executescript('CREATE TABLE observations(run_id TEXT,comment_id TEXT,payload_json TEXT); CREATE TABLE runs(run_id TEXT,summary_json TEXT);')

    def row(self,identifier,days=0,author='author'):
        payload={'id':identifier,'channel_id':EXPECTED_CHANNEL,'author_channel_id':author,'text':'BODY_'+identifier,'published_at':'2016-01-01T00:00:00Z'}
        when=(NOW-timedelta(days=days)).isoformat()
        with self.db:
            self.db.execute('INSERT INTO comments VALUES(?,?,?,?,?)',(EXPECTED_CHANNEL,identifier,when,when,json.dumps(payload)))
            self.db.execute('INSERT INTO observations VALUES(?,?,?)',('20261115T010000Z-test',identifier,json.dumps(payload)))
            self.db.execute('INSERT INTO pipeline_sheet_rows VALUES(?,?,?)',(identifier,self.db.execute('SELECT COUNT(*) FROM comments').fetchone()[0]+1,'hash'))

    def control(self,kind,targets=None):
        item=prepare({'kind':kind,'targets':targets or [],'verified_by':'syan@madeone.kr','request_id':'request_fixture_01','received_at':NOW.isoformat()},NOW)
        return {'schema':1,'collection_enabled':kind!='all','requests':[item]}

    def test_expiry_uses_last_refresh_and_removes_historical_body(self):
        self.row('expired',22);self.row('old_publication_recent_refresh',1)
        result=privacy.apply(self.db,now=NOW)
        self.assertEqual(result['expired_or_requested_rows'],1)
        self.assertEqual(self.db.execute('SELECT comment_id FROM comments').fetchall(),[('old_publication_recent_refresh',)])
        self.assertEqual(self.db.execute('SELECT comment_id,include_memos FROM privacy_pending_sheet').fetchall(),[('expired',0)])
        self.assertNotIn(b'BODY_expired',self.path.read_bytes())

    def test_verified_author_request_is_idempotent_and_blocks_recollection(self):
        self.row('target',author='excluded');self.row('keep',author='other')
        control=self.control('author',['excluded'])
        first=privacy.apply(self.db,control,NOW);second=privacy.apply(self.db,control,NOW)
        self.assertEqual(first['explicit_request_rows'],1);self.assertEqual(second['new_requests'],[])
        self.assertEqual(self.db.execute('SELECT comment_id,include_memos FROM privacy_pending_sheet').fetchall(),[('target',1)])
        item={'snippet':{'topLevelComment':{'id':'new_target','snippet':{'channelId':EXPECTED_CHANNEL,'authorChannelId':{'value':'excluded'},'textOriginal':'PRIVATE'}},'totalReplyCount':1}}
        apply_page(self.db,'new_cycle',{'kind':'threads','target':'published','page_token':None},{'items':[item]},EXPECTED_CHANNEL,NOW.isoformat())
        self.assertEqual(self.db.execute('SELECT comment_id FROM comments').fetchall(),[('keep',)])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM pipeline_cursors').fetchone()[0],0)
        self.assertTrue(privacy.blocked({'id':'target'},privacy.exclusions(self.db)))

    def test_stored_response_redaction_preserves_original_pagination_response(self):
        source={'items':[{'id':'erase','snippet':{'authorChannelId':{'value':'a'},'textOriginal':'PRIVATE'}}],'nextPageToken':'next'}
        filtered=privacy.filtered_response('comments',source,{('comment',privacy.digest_id('erase'))})
        self.assertEqual(filtered['items'],[])
        self.assertEqual(filtered['nextPageToken'],'next')
        self.assertEqual(source['items'][0]['snippet']['textOriginal'],'PRIVATE')

    def test_disconnect_requires_verified_request_and_clears_all_stored_copies(self):
        with self.assertRaises(ValidationError): privacy.validate_control({'schema':1,'collection_enabled':False,'requests':[]})
        self.row('one');self.row('two')
        result=privacy.apply(self.db,self.control('all'),NOW)
        self.assertFalse(result['collection_enabled'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM comments').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0],0)

    def test_raw_cleanup_is_bounded_and_retries_until_every_live_copy_is_removed(self):
        with self.db: self.db.execute("INSERT INTO privacy_state VALUES('purge_raw','true')")
        class Cloud:
            def __init__(self): self.items=[{'name':f'raw/{i}.json','generation':str(i),'timeCreated':NOW.isoformat()} for i in range(601)]
            def list_objects(self,prefix):
                self.assert_prefix=prefix;return list(self.items)
            def delete_object(self,name,generation): self.items=[x for x in self.items if x['name']!=name]
        cloud=Cloud();first=privacy.purge_cloud(cloud,self.db,NOW)
        self.assertEqual(first['deleted_raw_objects'],500);self.assertEqual(first['remaining_raw_objects'],101)
        self.assertEqual(self.db.execute("SELECT value FROM privacy_state WHERE key='purge_raw'").fetchone()[0],'true')
        second=privacy.purge_cloud(cloud,self.db,NOW)
        self.assertEqual(second['remaining_raw_objects'],0)
        self.assertEqual(self.db.execute("SELECT value FROM privacy_state WHERE key='purge_raw'").fetchone()[0],'false')

    def test_processing_log_clears_expired_and_requested_rows_only(self):
        privacy.apply(self.db,self.control('comment',['erased-id']),NOW)
        serial=(NOW+timedelta(hours=9)-datetime(1899,12,30,tzinfo=timezone.utc)).total_seconds()/86400
        class Sheets:
            def __init__(self): self.calls=[]
            def metadata(self): return {'sheets':[{'properties':{'title':'처리 이력','sheetId':9,'gridProperties':{'rowCount':10}}}]}
            def values(self,address):
                if address.endswith('A1:T1'): return [['']*12+['댓글 ID','작성자 채널 ID']]
                return [[serial-22]+['']*11+['old-id','a'],[serial]+['']*11+['erased-id','b'],[serial]+['']*11+['keep','c']]
            def write(self,suffix,value): self.calls.append(value)
        sheets=Sheets();self.assertEqual(privacy.clear_processing_history(sheets,self.db,NOW),2)
        ranges=[x['updateCells']['range'] for x in sheets.calls[0]['requests']]
        self.assertEqual([x['startRowIndex'] for x in ranges],[1,2])

    def test_revoke_only_uses_google_endpoint_and_never_persists_token(self):
        response=Mock(status_code=200)
        with patch('youtube_validation.privacy.requests.post',return_value=response) as post:
            self.assertEqual(privacy.revoke_connection({'refresh_token':'fixture-token'},self.db),'revoked')
            self.assertEqual(post.call_args.args[0],'https://oauth2.googleapis.com/revoke')
            self.assertEqual(privacy.revoke_connection({'refresh_token':'fixture-token'},self.db),'revoked')
            self.assertEqual(post.call_count,1)
            self.assertEqual(privacy.revoke_connection({'refresh_token':'new-fixture-token'},self.db),'revoked')
            self.assertEqual(post.call_count,2)
        self.assertNotIn(b'fixture-token',self.path.read_bytes())

if __name__=='__main__': unittest.main()
