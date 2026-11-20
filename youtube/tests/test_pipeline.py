from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from youtube_validation.pipeline import run, EXPECTED_CHANNEL
from youtube_validation.api import ValidationError


def top(identifier):
    return {'snippet': {'videoId': 'video-1', 'topLevelComment':
        {'id': identifier, 'snippet': {'channelId': EXPECTED_CHANNEL, 'textDisplay': '=literal',
                                      'videoId': 'video-1'}}, 'totalReplyCount': 1}}


class API:
    def __init__(self, limit=100, fail=None):
        self.requests, self.max_requests = 0, limit
        self.calls, self.fail = [], fail

    def get(self, resource, **params):
        if self.requests >= self.max_requests:
            raise ValidationError('request_budget_exhausted')
        self.requests += 1
        self.calls.append((resource, params))
        if resource == 'channels':
            return {'items': [{'id': EXPECTED_CHANNEL, 'snippet': {'title': 'WATERBOMB'}}]}
        if resource == 'comments':
            return {'items': [{'id': 'reply-'+params['parentId'], 'snippet': {
                'parentId': params['parentId'], 'channelId': EXPECTED_CHANNEL, 'textDisplay': 'reply'}}]}
        status = params['moderationStatus']
        if status == self.fail:
            raise ValidationError('forbidden', 403)
        if status != 'published':
            return {'items': []}
        if params.get('pageToken'):
            return {'items': [top('old')]}
        return {'items': [top('new')], 'nextPageToken': 'page-2'}


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)

    def test_checkpoint_resume_does_not_restart_completed_pages(self):
        first = run(API(5), self.state, max_requests=5)
        self.assertEqual(first['state'], 'partial')
        second_api = API()
        second = run(second_api, self.state)
        self.assertEqual(second['cycle_id'], first['cycle_id'])
        self.assertTrue(second['full_cycle_complete'])
        published = [params for resource, params in second_api.calls if resource == 'commentThreads'
                     and params['moderationStatus'] == 'published']
        self.assertTrue(all(params.get('pageToken') == 'page-2' for params in published))
        self.assertEqual(second['stored_comment_ids'], 4)

    def test_idle_and_forced_refresh_preserve_unique_comments(self):
        first = run(API(), self.state)
        self.assertTrue(first['full_cycle_complete'])
        idle_api = API()
        idle = run(idle_api, self.state)
        self.assertEqual(idle['state'], 'idle')
        self.assertEqual(idle_api.requests, 0)
        refreshed = run(API(), self.state, force_new_cycle=True)
        self.assertNotEqual(refreshed['cycle_id'], first['cycle_id'])
        self.assertEqual(refreshed['new_comment_ids'], 0)

    def test_permission_failure_is_recorded_and_can_retry(self):
        failed = run(API(fail='heldForReview'), self.state)
        self.assertEqual(failed['state'], 'error')
        self.assertEqual(failed['statuses']['heldForReview']['error'], 'forbidden')
        success = run(API(), self.state)
        self.assertEqual(success['cycle_id'], failed['cycle_id'])
        self.assertTrue(success['full_cycle_complete'])

    def test_daily_budget_is_preserved_across_attempts(self):
        first = run(API(), self.state, daily_budget=5)
        second_api = API()
        second = run(second_api, self.state, daily_budget=5)
        self.assertFalse(first['full_cycle_complete'])
        self.assertEqual(second['error'], 'daily_request_budget_exhausted')
        self.assertEqual(second_api.requests, 0)

    def test_quota_failure_stops_and_next_attempt_resumes(self):
        api = API()
        original = api.get
        def exhausted(resource, **params):
            if resource == 'commentThreads':
                api.requests += 1
                raise ValidationError('quotaExceeded', 403)
            return original(resource, **params)
        api.get = exhausted
        failed = run(api,self.state)
        self.assertEqual(failed['state'],'error')
        self.assertEqual(failed['error'],'quotaExceeded')
        self.assertEqual(api.requests,3)
        recovered = run(API(),self.state)
        self.assertEqual(recovered['cycle_id'],failed['cycle_id'])
        self.assertTrue(recovered['full_cycle_complete'])

    def test_page_rows_and_cursor_rollback_together(self):
        api = API()
        original = api.get
        def broken(resource, **params):
            if resource == 'commentThreads' and params['moderationStatus'] == 'published':
                return {'items': [top('new'), {'snippet': {}}], 'nextPageToken': 'page-2'}
            return original(resource, **params)
        api.get = broken
        result = run(api, self.state)
        self.assertEqual(result['state'], 'error')
        with closing(sqlite3.connect(self.state/'comments.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM comments').fetchone()[0], 0)
            cursor = db.execute("SELECT pages,page_token FROM pipeline_cursors WHERE target='published'").fetchone()
            self.assertEqual(cursor, (0, None))
