from datetime import datetime,timezone
from contextlib import closing
from pathlib import Path
import hashlib
import sqlite3
import tempfile
import unittest

from youtube_validation.privacy_audit import audit_local,cloud_metadata_summary,deletion_procedure

class PrivacyAuditTests(unittest.TestCase):
    def test_read_only_audit_counts_refresh_age_without_changing_source(self):
        now=datetime(2026,11,16,tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'comments.sqlite3'
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('CREATE TABLE comments(last_seen TEXT)')
                db.executemany('INSERT INTO comments VALUES(?)',[('2026-10-01T00:00:00Z',),('2026-11-15T00:00:00Z',)])
            before=hashlib.sha256(path.read_bytes()).hexdigest()
            result=audit_local(tmp,now)
            self.assertEqual(result['comment_freshness']['not_refreshed_30_days'],1)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),before)
            self.assertFalse((Path(tmp)/'authorized-user.json').exists())

    def test_empty_state_does_not_create_a_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(audit_local(tmp)['table_counts'],{})
            self.assertEqual(list(Path(tmp).iterdir()),[])

    def test_metadata_does_not_claim_database_rows_were_inspected(self):
        live=[{'name':'raw/a.json','generation':'1','size':'20','timeCreated':'2026-10-01T00:00:00Z'},
              {'name':'state/comments.sqlite3.gz','generation':'3','size':'50','timeCreated':'2026-11-15T00:00:00Z'},
              {'name':'raw/invalid.json','size':'1','timeCreated':'invalid'}]
        summary=cloud_metadata_summary(live,[{'size':'70','hardDeleteTime':'2026-11-23T00:00:00Z'}],datetime(2026,11,16,tzinfo=timezone.utc))
        self.assertEqual(summary['expired_objects'],[{'name':'raw/a.json','generation':'1'}])
        self.assertEqual(summary['invalid_timestamp_count'],1)
        self.assertFalse(summary['payloads_downloaded'])
        self.assertEqual(summary['database_row_freshness'],'not_inspected_in_cloud_metadata_audit')
        self.assertFalse(deletion_procedure()['production_changes_applied'])

if __name__=='__main__': unittest.main()
