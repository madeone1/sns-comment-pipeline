from contextlib import nullcontext
import copy
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from youtube_validation.notion_report import (
    LEGACY_LABEL, LAYOUT_VERSION, NotionClient, NotionReportError, paragraph, report_title, run,
)
from youtube_validation.notion_summary import KST, ReportDeferred


SOURCE = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
CONFIG = {'daily_data_source_id': SOURCE, 'spreadsheet_id': 'youtube-sheet',
          'token_secret': 'projects/example/secrets/notion', 'expected_connection_name': 'WSH'}
SUMMARY = {'report_day': '2026-11-20', 'period_start': '2026-11-19', 'period_end': '2026-11-19',
           'period_comments': 3, 'period_parent_comments': 2, 'period_replies': 1,
           'held_for_review': 2, 'likely_spam': 0, 'status_not_returned': 1,
           'collected_at': '2026-11-20T09:00:00+09:00', 'observed_comments': 40,
           'published_time_not_returned': 0}
NOW = datetime(2026, 11, 20, 9, 5, tzinfo=KST)


def item(kind, label, identifier, **attributes):
    return {'id': identifier, 'type': kind, kind: {'rich_text': [{'text': {'content': label}}], **attributes}}


class FakeNotion(NotionClient):
    def __init__(self):
        super().__init__('synthetic-connection-token', CONFIG)
        self.writes = []
        self.root = [item('heading_3', 'Develop', 'develop'),
            item('heading_3', '💬 인스타 댓글 관리 (2026-11-20)', 'instagram-report', is_toggleable=True),
            item('divider', '', 'divider'), item('heading_3', 'Info', 'info')]
        self.rows = {'instagram-report': [item('paragraph', '인스타 원래 요약', 'original-summary'),
                                         {'type': 'child_database', 'id': 'decision-table'}]}
        self.fail_after_write = False
        self.fail_delete = False
        self.fail_readback = False
        self.deleted = []

    def add_legacy(self):
        legacy = item('toggle', LEGACY_LABEL, 'legacy-youtube')
        self.rows['instagram-report'].append(legacy)
        self.rows[legacy['id']] = self.expected_children(SUMMARY)
        return legacy

    def request(self, method, path, payload=None):
        if method == 'POST' and path.endswith('/query'):
            return {'results': [{'id': 'daily', 'url': 'https://www.notion.so/daily',
                'parent': {'data_source_id': SOURCE}, 'properties': {
                    '유형': {'select': {'name': '오늘 업무'}}, '이벤트 시간': {'date': {'start': '2026-11-20'}}}}]}
        if method == 'GET' and path.startswith('blocks/'):
            identifier = path.split('/')[1]
            return {'results': self.root if identifier == 'daily' else self.rows.get(identifier, [])}
        if method == 'PATCH':
            self.writes.append((path, payload))
            self.asserted_path = path
            child = payload['children'][0]
            child = {**child, 'id': 'youtube-report'}
            self.asserted_position = payload.get('position')
            anchor = payload['position']['after_block']['id']
            index = next(index for index, block in enumerate(self.root) if block['id'] == anchor)
            self.root.insert(index + 1, child)
            self.rows['youtube-report'] = copy.deepcopy(child[child['type']]['children']) if not self.fail_readback else []
            for row in self.rows['youtube-report']:
                for part in row['paragraph']['rich_text']:
                    part['text'].setdefault('link', None)
            if self.fail_after_write:
                raise NotionReportError('notion_transport_failed')
            return {'results': [child]}
        if method == 'DELETE':
            if self.fail_delete:
                raise NotionReportError('notion_transport_failed')
            identifier = path.split('/')[1]
            self.deleted.append(identifier)
            for key in self.rows:
                self.rows[key] = [row for row in self.rows[key] if row.get('id') != identifier]
            return {'id': identifier, 'in_trash': True}
        raise AssertionError((method, path))


class IndependentNotionPublishTests(unittest.TestCase):
    def test_creates_a_heading_toggle_beside_instagram_and_retry_is_idempotent(self):
        client = FakeNotion()
        original = json.dumps(client.rows['instagram-report'])
        result = client.publish(SUMMARY)
        self.assertEqual(result['status'], 'inserted')
        self.assertEqual(client.asserted_path, 'blocks/daily/children')
        self.assertEqual(client.asserted_position['after_block']['id'], 'instagram-report')
        self.assertEqual(json.dumps(client.rows['instagram-report']), original)
        created = client.writes[0][1]['children'][0]
        self.assertEqual(created['type'], 'heading_3')
        self.assertTrue(created['heading_3']['is_toggleable'])
        self.assertEqual(created['heading_3']['rich_text'][0]['text']['content'], report_title(SUMMARY['report_day']))
        self.assertEqual(client.publish(SUMMARY)['status'], 'unchanged')
        self.assertEqual(len(client.writes), 1)
        self.assertEqual(client.writes[0][1]['notifications'], {'mode': 'silent'})

    def test_ambiguous_network_result_is_reconciled_without_a_second_append(self):
        client = FakeNotion()
        client.fail_after_write = True
        with self.assertRaises(NotionReportError):
            client.publish(SUMMARY)
        self.assertEqual(client.publish(SUMMARY)['status'], 'unchanged')
        self.assertEqual(len(client.writes), 1)

    def test_missing_develop_is_deferred_without_a_write(self):
        for root in [[item('heading_3', 'Info', 'info'),
                      item('heading_3', '💬 인스타 댓글 관리 (2026-11-20)', 'outside', is_toggleable=True)],
                     []]:
            client = FakeNotion(); client.root = root
            with self.assertRaises(ReportDeferred):
                client.publish(SUMMARY)
            self.assertEqual(client.writes, [])

    def test_instagram_report_is_not_required_for_youtube_to_publish(self):
        client = FakeNotion()
        client.root = [item('heading_3', 'Develop', 'develop'), item('divider', '', 'divider')]
        self.assertEqual(client.publish(SUMMARY)['status'], 'inserted')
        self.assertEqual(client.asserted_position['after_block']['id'], 'develop')

    def test_legacy_content_is_verified_in_its_new_location_before_only_the_old_copy_is_trashed(self):
        client = FakeNotion()
        original = copy.deepcopy(client.rows['instagram-report'])
        legacy = client.add_legacy()
        result = client.publish(SUMMARY)
        self.assertEqual(result['status'], 'migrated')
        self.assertEqual(client.deleted, [legacy['id']])
        self.assertEqual(client.rows['instagram-report'], original)
        self.assertEqual(client.publish(SUMMARY)['status'], 'unchanged')
        self.assertEqual(len(client.writes), 1)

    def test_interrupted_legacy_cleanup_retries_without_creating_another_report(self):
        client = FakeNotion(); client.add_legacy(); client.fail_delete = True
        with self.assertRaises(NotionReportError): client.publish(SUMMARY)
        client.fail_delete = False
        self.assertEqual(client.publish(SUMMARY)['status'], 'migrated')
        self.assertEqual(len(client.writes), 1)

    def test_failed_readback_keeps_the_old_nested_report(self):
        client = FakeNotion(); legacy = client.add_legacy(); client.fail_readback = True
        with self.assertRaises(NotionReportError): client.publish(SUMMARY)
        self.assertEqual(client.deleted, [])
        self.assertIn(legacy, client.rows['instagram-report'])

    def test_manual_notes_in_the_legacy_report_are_never_deleted(self):
        client = FakeNotion(); legacy = client.add_legacy()
        client.rows[legacy['id']].append(paragraph('사람이 추가한 메모'))
        with self.assertRaises(NotionReportError): client.publish(SUMMARY)
        self.assertEqual(client.writes, [])
        self.assertEqual(client.deleted, [])

    def test_existing_manual_or_changed_youtube_content_is_not_overwritten(self):
        client = FakeNotion()
        client.publish(SUMMARY)
        client.rows['youtube-report'].append(paragraph('사람이 추가한 메모'))
        with self.assertRaises(NotionReportError):
            client.publish(SUMMARY)
        self.assertEqual(len(client.writes), 1)

    def test_duplicate_parent_or_duplicate_subsection_is_rejected(self):
        client = FakeNotion()
        client.root.insert(2, client.root[1])
        with self.assertRaises(NotionReportError):
            client.publish(SUMMARY)
        self.assertFalse(client.writes)

    def test_transport_never_follows_redirects_or_echoes_a_secret_exception(self):
        session = Mock(); session.request.side_effect = requests.RequestException('synthetic-connection-token')
        client = NotionClient('synthetic-connection-token', CONFIG, session)
        with self.assertRaises(NotionReportError) as raised:
            client.request('GET', 'users/me')
        self.assertNotIn('synthetic-connection-token', str(raised.exception))
        self.assertFalse(session.request.call_args.kwargs['allow_redirects'])
        session.request.assert_called_once()

    def test_daily_page_query_duplicates_and_wrong_dates_are_rejected(self):
        client = FakeNotion()
        page = client.daily_page('2026-11-20')
        with patch.object(client, 'request', return_value={'results': [page, page]}):
            with self.assertRaises(NotionReportError): client.daily_page('2026-11-20')
        page['properties']['이벤트 시간']['date']['start'] = '2026-11-19'
        with patch.object(client, 'request', return_value={'results': [page]}):
            with self.assertRaises(NotionReportError): client.daily_page('2026-11-20')


class ReportCoverageTests(unittest.TestCase):
    def test_coverage_is_advanced_only_after_verified_publish(self):
        for failure in [True, False]:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                cloud = Mock(); cloud.get.return_value = (None, 0); cloud.restore.return_value = 42
                client = Mock(); client.publish.side_effect = NotionReportError('failed') if failure else None
                client.publish.return_value = {'status': 'inserted', 'day': SUMMARY['report_day']}
                with patch('youtube_validation.notion_report.STATE', Path(temporary)), \
                     patch('youtube_validation.notion_report.report_lease', return_value=nullcontext()), \
                     patch('youtube_validation.notion_report.summarize', return_value=SUMMARY):
                    if failure:
                        with self.assertRaises(NotionReportError): run(CONFIG, cloud, NOW, lambda _: client)
                    else:
                        run(CONFIG, cloud, NOW, lambda _: client)
                state_writes = [call for call in cloud.put.call_args_list if call.args[0] == 'notion/report-state.json']
                self.assertEqual(len(state_writes), 0 if failure else 1)
                if state_writes:
                    self.assertEqual(json.loads(state_writes[0].args[1])['covered_through'], '2026-11-19')

    def test_already_reported_day_does_not_read_the_collection_or_notion_again(self):
        cloud = Mock(); cloud.get.return_value = (json.dumps({
            'last_report_day': '2026-11-20', 'layout_version': LAYOUT_VERSION}).encode(), 1)
        factory = Mock()
        with patch('youtube_validation.notion_report.report_lease', return_value=nullcontext()):
            self.assertEqual(run(CONFIG, cloud, NOW, factory)['status'], 'already_reported')
        cloud.restore.assert_not_called(); factory.assert_not_called()

    def test_same_day_legacy_layout_reconciles_the_original_period_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            cloud = Mock(); cloud.restore.return_value = 42
            cloud.get.return_value = (json.dumps({'last_report_day': '2026-11-20',
                'covered_through': '2026-11-19', 'last_report': {'period_start': '2026-11-19',
                'period_end': '2026-11-19'}}).encode(), 1)
            client = Mock(); client.publish.return_value = {'status': 'migrated'}
            with patch('youtube_validation.notion_report.STATE', Path(temporary)), \
                 patch('youtube_validation.notion_report.report_lease', return_value=nullcontext()), \
                 patch('youtube_validation.notion_report.summarize', return_value=SUMMARY) as summarize:
                self.assertEqual(run(CONFIG, cloud, NOW, lambda _: client)['status'], 'migrated')
            self.assertEqual(summarize.call_args.args[2], '2026-11-18')
            state_write = next(call for call in cloud.put.call_args_list if call.args[0] == 'notion/report-state.json')
            self.assertEqual(json.loads(state_write.args[1])['layout_version'], LAYOUT_VERSION)
            self.assertEqual(json.loads(state_write.args[1])['covered_through'], '2026-11-19')


if __name__ == '__main__':
    unittest.main()
