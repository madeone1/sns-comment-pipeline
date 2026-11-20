from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from youtube_validation.pipeline import EXPECTED_CHANNEL, initialize
from youtube_validation.notion_summary import KST, ReportDeferred, lines, period, summarize


NOW = datetime(2026, 11, 20, 9, 30, tzinfo=KST)


class YouTubeReportSourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'comments.sqlite3'
        with closing(sqlite3.connect(self.path)) as db, db:
            initialize(db)
            db.execute('INSERT INTO pipeline_cycles VALUES(?,?,?,?)',
                       ('cycle', '2026-11-19T23:30:00+00:00', '2026-11-20T00:00:00+00:00', 'complete'))

    def add(self, identifier, published, parent=None, status='published', observed=True):
        with closing(sqlite3.connect(self.path)) as db, db:
            row = {'id': identifier, 'published_at': published, 'parent_id': parent,
                   'moderation_status': status, 'text': 'PRIVATE COMMENT BODY', 'author': 'PRIVATE AUTHOR'}
            db.execute('INSERT INTO comments VALUES(?,?,?,?,?)',
                       (EXPECTED_CHANNEL, identifier, '2026-11-19T23:30:00+00:00',
                        '2026-11-20T00:00:00+00:00', json.dumps(row)))
            if observed:
                db.execute('INSERT INTO pipeline_seen VALUES(?,?)', ('cycle', identifier))

    def test_kst_date_boundaries_replies_and_review_states_without_bodies(self):
        self.add('start', '2026-11-18T15:00:00Z', status='heldForReview')
        self.add('reply', '2026-11-19T14:59:59Z', parent='start', status=None)
        self.add('next-day', '2026-11-19T15:00:00Z', status='likelySpam')
        self.add('not-observed', '2026-11-19T01:00:00Z', observed=False)
        report = summarize(self.path, NOW)
        self.assertEqual(report['period_comments'], 2)
        self.assertEqual(report['period_replies'], 1)
        self.assertEqual(report['observed_comments'], 3)
        self.assertEqual(report['held_for_review'], 1)
        self.assertEqual(report['likely_spam'], 1)
        self.assertEqual(report['status_not_returned'], 1)
        self.assertNotIn('PRIVATE', json.dumps(report))
        text, link = lines(report, 'synthetic-sheet')
        self.assertNotIn('PRIVATE', '\n'.join(text))
        self.assertIn('synthetic-sheet', link)

    def test_a_confirmed_empty_cycle_is_a_real_zero(self):
        self.assertEqual(summarize(self.path, NOW)['period_comments'], 0)

    def test_incomplete_and_stale_sources_are_never_reported_as_zero(self):
        for status, finished in [('running', None), ('complete', '2026-11-19T00:00:00Z')]:
            with self.subTest(status=status, finished=finished), closing(sqlite3.connect(self.path)) as db, db:
                db.execute('UPDATE pipeline_cycles SET state=?,finished_at=?', (status, finished))
            with self.assertRaises(ReportDeferred):
                summarize(self.path, NOW)

    def test_missing_database_is_not_created(self):
        missing = self.path.with_name('missing.sqlite3')
        with self.assertRaises(ReportDeferred):
            summarize(missing, NOW)
        self.assertFalse(missing.exists())

    def test_monday_and_unreported_days_are_covered(self):
        monday = datetime(2026, 11, 23, 9, tzinfo=KST).date()
        self.assertEqual(tuple(x.isoformat() for x in period(monday)), ('2026-11-20', '2026-11-22'))
        self.assertEqual(tuple(x.isoformat() for x in period(NOW.date(), '2026-11-17')),
                         ('2026-11-18', '2026-11-19'))
        for value in ['2026-11-19', '2026-11-20', 'invalid', '2026-10-01']:
            with self.assertRaises(ReportDeferred):
                period(NOW.date(), value)

    def test_missing_published_time_is_explicit_and_source_bytes_stay_unchanged(self):
        self.add('unknown', None)
        before = self.path.read_bytes()
        report = summarize(self.path, NOW)
        self.assertEqual(report['published_time_not_returned'], 1)
        self.assertEqual(report['period_comments'], 0)
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
