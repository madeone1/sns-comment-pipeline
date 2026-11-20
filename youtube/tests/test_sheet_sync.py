from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from youtube_validation.api import ValidationError
from youtube_validation.pipeline import run, EXPECTED_CHANNEL
from youtube_validation.projection import HEADERS, LOG_HEADERS, snapshot
from youtube_validation.sheet_sync import sync, batches
from test_pipeline import API


class Remote:
    def __init__(self, identifier, google=None):
        self.rows, self.writes = {}, []
        self.duplicate=False

    def metadata(self):
        return {'sheets':[{'properties':{'sheetId':index,'title':name,
            'gridProperties':{'rowCount':100,'columnCount':20}}}
            for index,name in enumerate(('수집 현황','댓글','수집 이력'))]}

    def values(self,address):
        if address=="'댓글'!A1:R1": return [HEADERS]
        if address=="'수집 이력'!A1:K1": return [LOG_HEADERS]
        if "!S2:Z" in address: return []
        if self.duplicate: return [['same'],['same']]
        return [[self.rows.get(number,[''])[0]] for number in range(2,max(self.rows.keys(),default=1)+1)]

    def write(self,suffix,value):
        self.writes.append((suffix,value))
        if suffix=='/values:batchUpdate':
            for block in value['data']:
                if block['range'].startswith("'댓글'!"):
                    number=int(block['range'].split('!A')[1].split(':')[0])
                    self.rows[number]=block['values'][0]


class SheetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state=Path(self.temp.name)
        run(API(),self.state)
        self.remote=Remote('test')
        patcher=patch('youtube_validation.sheet_sync.Sheets',return_value=self.remote)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_literal_text_and_repeat_sync(self):
        first=sync(self.state/'comments.sqlite3','new-sheet')
        self.assertEqual(first['updated_rows'],4)
        self.assertTrue(any(row[5]=='=literal' for row in self.remote.rows.values()))
        writes=[value for suffix,value in self.remote.writes if suffix=='/values:batchUpdate']
        self.assertTrue(all(value['valueInputOption']=='RAW' for value in writes))
        again=sync(self.state/'comments.sqlite3','new-sheet')
        self.assertEqual(again['updated_rows'],0)

    def test_user_sort_is_resolved_by_live_ids(self):
        sync(self.state/'comments.sqlite3','new-sheet')
        first,last=2,max(self.remote.rows)
        self.remote.rows[first],self.remote.rows[last]=self.remote.rows[last],self.remote.rows[first]
        result=sync(self.state/'comments.sqlite3','new-sheet')
        self.assertEqual(result['comment_ids'],4)
        self.assertEqual(len({row[0] for row in self.remote.rows.values()}),4)

    def test_duplicate_remote_keys_fail_before_writes(self):
        self.remote.duplicate=True
        with self.assertRaisesRegex(ValidationError,'duplicate_comment_ids'):
            sync(self.state/'comments.sqlite3','new-sheet')
        self.assertEqual(self.remote.writes,[])

    def test_projection_uses_typed_dates_and_keeps_source_text(self):
        source=snapshot(self.state/'comments.sqlite3')
        self.assertEqual(source['comments_headers'],HEADERS)
        self.assertEqual(len(source['comments']),4)
        self.assertTrue(any(row[5]=='=literal' for row in source['comments']))
        self.assertTrue(all(isinstance(row[13],float) for row in source['comments']))

    def test_large_text_batches_are_bounded_without_truncation(self):
        rows=[(n,['댓글' * 10000],str(n)) for n in range(40)]
        result=list(batches(rows))
        self.assertGreater(len(result),1)
        self.assertEqual(sum(len(batch) for batch in result),40)

    def test_expired_copy_refreshed_in_same_cycle_keeps_existing_memo_row(self):
        sync(self.state/'comments.sqlite3','new-sheet')
        identifier=self.remote.rows[2][0]
        from youtube_validation.privacy import initialize
        with closing(sqlite3.connect(self.state/'comments.sqlite3')) as db:
            initialize(db)
            with db: db.execute('INSERT INTO privacy_pending_sheet VALUES(?,0)',(identifier,))
        self.remote.writes.clear()
        result=sync(self.state/'comments.sqlite3','new-sheet')
        self.assertEqual(result['privacy_cleared_rows'],0)
        self.assertEqual(self.remote.rows[2][0],identifier)

    def test_verified_stored_copy_delete_only_clears_its_scope(self):
        sync(self.state/'comments.sqlite3','new-sheet')
        identifier=self.remote.rows[2][0]
        from youtube_validation.privacy import initialize
        with closing(sqlite3.connect(self.state/'comments.sqlite3')) as db:
            initialize(db)
            with db:
                db.execute('DELETE FROM comments WHERE comment_id=?',(identifier,))
                db.execute('INSERT INTO privacy_pending_sheet VALUES(?,1)',(identifier,))
        self.remote.writes.clear()
        result=sync(self.state/'comments.sqlite3','new-sheet')
        clears=[r['updateCells'] for suffix,v in self.remote.writes if suffix==':batchUpdate' for r in v['requests'] if 'updateCells' in r]
        self.assertEqual(result['privacy_cleared_rows'],1)
        self.assertEqual(len(clears),1)
        self.assertEqual(clears[0]['range']['startRowIndex'],1)
        self.assertEqual(clears[0]['range']['endColumnIndex'],26)

    def test_retention_preserves_notes_and_tracks_them_for_later_author_deletion(self):
        sync(self.state/'comments.sqlite3','new-sheet')
        identifier=self.remote.rows[2][0]
        from youtube_validation.privacy import initialize,digest_id,apply
        path=self.state/'comments.sqlite3'
        with closing(sqlite3.connect(path)) as db:
            initialize(db)
            with db:
                db.execute('DELETE FROM comments WHERE comment_id=?',(identifier,))
                db.execute('INSERT INTO privacy_pending_sheet VALUES(?,0)',(identifier,))
                db.execute('INSERT INTO privacy_expired_authors VALUES(?,?)',(identifier,digest_id('fixture-author')))
        self.remote.writes.clear();sync(path,'new-sheet')
        clears=[r['updateCells'] for suffix,v in self.remote.writes if suffix==':batchUpdate' for r in v['requests'] if 'updateCells' in r]
        self.assertEqual(clears[0]['fields'],'userEnteredValue')
        self.assertEqual(clears[0]['range']['endColumnIndex'],18)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute('SELECT * FROM privacy_memo_registry').fetchall(),[(2,digest_id(identifier),digest_id('fixture-author'))])
            apply(db,{'schema':1,'collection_enabled':True,'requests':[{'request_id':'later_author_request','kind':'author',
                'target_hashes':[digest_id('fixture-author')],'verified_by':'syan@madeone.kr','received_at':'2026-11-16T04:00:00Z'}]})
            self.assertEqual(db.execute('SELECT * FROM privacy_pending_memo_rows').fetchall(),[(2,)])
        self.remote.writes.clear();sync(path,'new-sheet')
        clears=[r['updateCells'] for suffix,v in self.remote.writes if suffix==':batchUpdate' for r in v['requests'] if 'updateCells' in r]
        self.assertEqual(clears[0]['range']['startRowIndex'],1)
        self.assertEqual(clears[0]['range']['endColumnIndex'],26)
        self.assertIn('note',clears[0]['fields'])

    def test_full_disconnect_clears_unmapped_sheet_rows_and_orphan_memos(self):
        sync(self.state/'comments.sqlite3','new-sheet')
        from youtube_validation.privacy import initialize
        with closing(sqlite3.connect(self.state/'comments.sqlite3')) as db:
            initialize(db)
            with db:
                db.execute('DELETE FROM comments')
                db.execute("INSERT INTO privacy_state VALUES('clear_all_sheet','true')")
        self.remote.writes.clear();sync(self.state/'comments.sqlite3','new-sheet')
        clears=[r['updateCells'] for suffix,v in self.remote.writes if suffix==':batchUpdate' for r in v['requests'] if 'updateCells' in r]
        self.assertEqual(len(clears),1)
        self.assertEqual(clears[0]['range']['endRowIndex'],100)
        self.assertEqual(clears[0]['range']['endColumnIndex'],26)
