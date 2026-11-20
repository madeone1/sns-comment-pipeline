from contextlib import closing
import copy
import json
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from youtube_validation.api import HANDLE, SCOPE, ValidationError, YouTube, verify_channel
from youtube_validation.auth import create_flow, finish_flow, import_client, validate_client
from youtube_validation.collector import collect
from youtube_validation.storage import read_json

CHANNEL = 'UC' + 'A' * 22


def comment(comment_id, parent=None, status='published'):
    snippet = {'channelId': CHANNEL, 'videoId': 'video-1', 'textDisplay': 'test comment',
               'authorDisplayName': 'sample', 'moderationStatus': status}
    if parent:
        snippet['parentId'] = parent
    return {'id': comment_id, 'snippet': snippet}


def thread():
    return {'snippet': {'videoId': 'video-1', 'topLevelComment': comment('top-1'),
                        'totalReplyCount': 3},
            'replies': {'comments': [comment('reply-1', 'top-1', 'heldForReview')]}}


class FakeAPI:
    def __init__(self, fail_status=None, budget=100, repeated=False, empty=False):
        self.requests = 0
        self.fail_status, self.budget = fail_status, budget
        self.repeated, self.empty = repeated, empty
        self.calls = []

    def get(self, resource, **params):
        if self.requests >= self.budget:
            raise ValidationError('request_budget_exhausted')
        self.requests += 1
        self.calls.append((resource, params))
        if resource == 'channels':
            return {'items': [{'id': CHANNEL, 'snippet': {'title': 'WATERBOMB'}}]}
        if resource == 'commentThreads':
            status = params['moderationStatus']
            if status == self.fail_status:
                raise ValidationError('forbidden', 403)
            if self.repeated:
                return {'items': [], 'nextPageToken': 'same-page'}
            if self.empty or status == 'likelySpam':
                return {'items': []}
            if params.get('pageToken'):
                return {'items': []}
            result = {'items': [copy.deepcopy(thread())]}
            if status == 'published':
                result['nextPageToken'] = 'next-public-page'
            return result
        if params.get('pageToken'):
            return {'items': [comment('reply-3', 'top-1')]}
        return {'items': [comment('reply-1', 'top-1', 'heldForReview'),
                          comment('reply-2', 'top-1')], 'nextPageToken': 'next-replies'}


class CollectorTests(unittest.TestCase):
    def test_permanent_revocation_is_distinguished_from_transient_transport_failure(self):
        from google.auth.exceptions import RefreshError
        session=Mock()
        session.get.side_effect=RefreshError('redacted',{'error':'invalid_grant'})
        with self.assertRaisesRegex(ValidationError,'oauth_authorization_revoked'):
            YouTube(None,session=session).get('channels',mine='true')
        session.get.side_effect=RuntimeError('temporary network failure')
        with self.assertRaisesRegex(ValidationError,'network_or_token_refresh_failed'):
            YouTube(None,session=session).get('channels',mine='true')

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state = Path(self.temporary.name) / 'private'

    def test_all_statuses_pagination_replies_and_deduplication(self):
        api = FakeAPI()
        summary = collect(api, self.state, HANDLE)
        rows = read_json(self.state / 'runs' / summary['run_id'] / 'comments.json')
        self.assertTrue(summary['complete'])
        self.assertEqual(summary['comment_ids_in_run'], 4)
        self.assertEqual(summary['replies'], 3)
        self.assertEqual(summary['statuses']['published']['pages'], 2)
        self.assertEqual(summary['reply_queries']['top-1']['pages'], 2)
        top = next(row for row in rows if row['id'] == 'top-1')
        self.assertEqual(top['moderation_status'], 'published')
        self.assertEqual(top['thread_query_statuses'], ['heldForReview', 'published'])
        self.assertEqual({params['moderationStatus'] for method, params in api.calls
                          if method == 'commentThreads'}, {'published', 'heldForReview', 'likelySpam'})

    def test_permission_error_is_incomplete_not_successful_zero(self):
        result = collect(FakeAPI(fail_status='heldForReview'), self.state, HANDLE)
        self.assertFalse(result['complete'])
        self.assertFalse(result['statuses']['heldForReview']['complete'])
        self.assertEqual(result['statuses']['heldForReview']['error'],
                         {'reason': 'forbidden', 'http_status': 403})
        self.assertTrue(result['statuses']['published']['complete'])

    def test_budget_exhaustion_preserves_partial_evidence(self):
        result = collect(FakeAPI(budget=4), self.state, HANDLE)
        self.assertFalse(result['complete'])
        self.assertEqual(result['api_requests'], 4)
        self.assertEqual(result['statuses']['published']['error']['reason'],
                         'request_budget_exhausted')
        self.assertEqual(result['stored_comment_ids'], 2)

    def test_repeated_page_token_stops(self):
        result = collect(FakeAPI(repeated=True), self.state, HANDLE)
        self.assertFalse(result['complete'])
        self.assertEqual(result['statuses']['published']['error']['reason'],
                         'repeated_or_invalid_page_token')
        self.assertLess(result['api_requests'], 10)

    def test_second_run_is_idempotent_and_keeps_observations(self):
        first = collect(FakeAPI(), self.state, HANDLE)
        second = collect(FakeAPI(), self.state, HANDLE)
        self.assertEqual(first['new_comment_ids'], 4)
        self.assertEqual(second['new_comment_ids'], 0)
        self.assertEqual(second['stored_comment_ids'], 4)
        with closing(sqlite3.connect(self.state / 'comments.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM observations').fetchone()[0], 8)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM runs').fetchone()[0], 2)

    def test_disappearing_comments_are_not_deleted(self):
        collect(FakeAPI(), self.state, HANDLE)
        result = collect(FakeAPI(empty=True), self.state, HANDLE)
        self.assertTrue(result['complete'])
        self.assertEqual(result['comment_ids_in_run'], 0)
        self.assertEqual(result['stored_comment_ids'], 4)

    def test_wrong_channel_does_not_write_database(self):
        api = FakeAPI()
        original = api.get
        def get(resource, **params):
            if params.get('mine'):
                return {'items': [{'id': 'UC' + 'B' * 22}]}
            return original(resource, **params)
        api.get = get
        with self.assertRaisesRegex(ValidationError, 'authenticated_channel_does_not_match'):
            collect(api, self.state, HANDLE)
        self.assertFalse((self.state / 'comments.sqlite3').exists())

    def test_private_evidence_permissions(self):
        collect(FakeAPI(), self.state, HANDLE)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        for path in (self.state / 'comments.sqlite3', self.state / 'latest.json'):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_sample_completion_is_not_full_history_completion(self):
        result = collect(FakeAPI(), self.state, HANDLE, pages_per_status=1)
        self.assertFalse(result['complete'])
        self.assertTrue(result['requested_scope_complete'])
        self.assertTrue(result['statuses']['published']['limited'])
        self.assertEqual(result['coverage'], 'sampled_threads')
        self.assertTrue(result['reply_queries']['top-1']['complete'])

    def test_status_queries_share_budget_fairly(self):
        api = FakeAPI(budget=5)
        result = collect(api, self.state, HANDLE)
        self.assertEqual([params['moderationStatus'] for resource, params in api.calls
                          if resource == 'commentThreads'][:3],
                         ['published', 'heldForReview', 'likelySpam'])
        self.assertTrue(result['statuses']['likelySpam']['complete'])


class TransportTests(unittest.TestCase):
    def test_transport_only_supports_read_resources(self):
        session = Mock()
        api = YouTube(None, session=session)
        with self.assertRaisesRegex(ValidationError, 'resource_not_allowed'):
            api.get('videos/delete')
        session.get.assert_not_called()

    def test_transport_redacts_remote_error_messages(self):
        response = Mock(status_code=403)
        response.json.return_value = {'error': {'message': 'secret-access-token',
            'errors': [{'reason': 'secret-access-token?'}]}}
        session = Mock()
        session.get.return_value = response
        api = YouTube(None, session=session)
        with self.assertRaises(ValidationError) as caught:
            api.get('comments', part='snippet', parentId='p')
        self.assertEqual(caught.exception.public(), {'reason': 'api_request_failed', 'http_status': 403})
        self.assertNotIn('secret-access-token', str(caught.exception))
        self.assertFalse(session.get.call_args.kwargs['allow_redirects'])

    def test_missing_items_is_not_zero_comments(self):
        session = Mock()
        session.get.return_value.status_code = 200
        session.get.return_value.json.return_value = {}
        with self.assertRaisesRegex(ValidationError, 'missing_items'):
            YouTube(None, session=session).get('comments')


class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state = Path(self.temporary.name)
        self.redirect = 'http://127.0.0.1:8765/oauth/youtube/callback'
        self.config = {'web': {'client_id': 'sample.apps.googleusercontent.com',
            'client_secret': 'not-a-real-secret',
            'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
            'token_uri': 'https://oauth2.googleapis.com/token',
            'redirect_uris': [self.redirect], 'project_id': 'isolated-test'}}

    def test_rejects_non_google_token_endpoint(self):
        self.config['web']['token_uri'] = 'https://example.com/steal'
        with self.assertRaises(ValidationError):
            validate_client(self.config, self.redirect)

    def test_rejects_unregistered_redirect(self):
        with self.assertRaises(ValidationError):
            validate_client(self.config, self.redirect + '/')

    def test_pkce_and_offline_access_use_only_youtube_scope(self):
        source = self.state / 'downloaded.json'
        source.write_text(json.dumps(self.config))
        import_client(source, self.state)
        flow = create_flow(self.state)
        url, oauth_state = flow.authorization_url(access_type='offline', prompt='consent select_account')
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query['scope'], [SCOPE])
        self.assertEqual(query['redirect_uri'], [self.redirect])
        self.assertEqual(query['code_challenge_method'], ['S256'])
        self.assertEqual(query['access_type'], ['offline'])
        self.assertEqual(query['state'], [oauth_state])
        self.assertNotIn('not-a-real-secret', url)

    def test_missing_refresh_token_is_not_saved(self):
        flow = Mock()
        flow.credentials.granted_scopes = [SCOPE]
        flow.credentials.refresh_token = None
        with self.assertRaisesRegex(ValidationError, 'required_scope_or_refresh_token_missing'):
            finish_flow(flow, 'one-use-code', self.state)
        self.assertFalse((self.state / 'authorized-user.json').exists())

    def test_wrong_channel_credentials_are_not_saved(self):
        flow = Mock()
        flow.credentials.granted_scopes = [SCOPE]
        flow.credentials.refresh_token = 'not-a-real-refresh-token'
        with patch('youtube_validation.auth.verify_channel',
                   side_effect=ValidationError('authenticated_channel_does_not_match_waterbomb')):
            with patch('youtube_validation.auth.YouTube'):
                with self.assertRaises(ValidationError):
                    finish_flow(flow, 'one-use-code', self.state)
        self.assertFalse((self.state / 'authorized-user.json').exists())


if __name__ == '__main__':
    unittest.main()
