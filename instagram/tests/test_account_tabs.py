import copy
import json
import unittest
from unittest import mock

from sns_comment import workflow
from sns_comment.account_tabs import ACCOUNT_KEY
from sns_comment.api import PilotError
from account_fixtures import fixture
from sns_comment.connection_registry import apply_group, initial_registry, runtime_configs
from test_workflow import row, source


def projection(identifier, name, comments=None, label=None):
    config = {'account_id': identifier, 'expected_username': name}
    if label:
        config['region_label'] = label
    return workflow.make_account(config, source([row(1, '환불 문의')] if comments is None else comments), {})


class Workbook:
    """Emulate the stateful Sheets operations used by account transitions."""
    def __init__(self):
        self.rows = {name: [header[:]] for name, header in workflow.HEADERS.items()}
        self.meta = {'sheets': [{'properties': {'title': name, 'sheetId': i,
            'gridProperties': {'rowCount': 1000, 'columnCount': len(header)}}}
            for i, (name, header) in enumerate(workflow.HEADERS.items())]}
        self.writes = []
        self.lose_response = False

    def sheet(self, name):
        return next(s for s in self.meta['sheets'] if s['properties']['title'] == name)

    def title(self, sheet_id):
        return next(s['properties']['title'] for s in self.meta['sheets'] if s['properties']['sheetId'] == sheet_id)

    def snapshot(self):
        return copy.deepcopy(self.meta), copy.deepcopy(self.rows)

    def request(self, suffix, body):
        self.writes.append(copy.deepcopy(body))
        for request in body['requests']:
            if 'addSheet' in request:
                properties = copy.deepcopy(request['addSheet']['properties'])
                assert properties['title'] not in self.rows
                assert all(s['properties']['sheetId'] != properties['sheetId'] for s in self.meta['sheets'])
                self.meta['sheets'].append({'properties': properties})
                self.rows[properties['title']] = []
            elif 'createDeveloperMetadata' in request:
                item = copy.deepcopy(request['createDeveloperMetadata']['developerMetadata'])
                self.sheet(self.title(item['location']['sheetId'])).setdefault('developerMetadata', []).append(item)
            elif 'updateSheetProperties' in request:
                prop = request['updateSheetProperties']['properties']
                self.sheet(self.title(prop['sheetId']))['properties'].update(prop)
            elif 'updateCells' in request:
                update = request['updateCells']; area = update['range']
                table = self.rows[self.title(area['sheetId'])]
                while len(table) < area['endRowIndex']:
                    table.append([])
                for i, record in enumerate(update['rows'], area['startRowIndex']):
                    while len(table[i]) < area['endColumnIndex']:
                        table[i].append('')
                    for j, cell in enumerate(record['values'], area['startColumnIndex']):
                        table[i][j] = next(iter(cell.get('userEnteredValue', {}).values()), '')
            elif 'appendDimension' in request:
                item = request['appendDimension']
                self.sheet(self.title(item['sheetId']))['properties']['gridProperties']['rowCount'] += item['length']
            elif 'setBasicFilter' in request:
                item = request['setBasicFilter']['filter']
                self.sheet(self.title(item['range']['sheetId']))['basicFilter'] = copy.deepcopy(item)
        if self.lose_response:
            self.lose_response = False
            raise PilotError('반영 후 응답 유실')


class AccountTabTests(unittest.TestCase):
    def setUp(self):
        self.book = Workbook()

    def sync(self, accounts, roster=None, **kwargs):
        return workflow.sync(workflow.merge(accounts), self.book, account_roster=roster, **kwargs)

    def test_abc_to_ade_preserves_history_and_reconnects_original_tabs(self):
        abc = [projection(i, name) for i, name in [('10001', 'a'), ('10002', 'b'), ('10003', 'c')]]
        result = self.sync(abc)
        original_ids = {i: self.book.sheet(n)['properties']['sheetId'] for i, n in result['account_tabs'].items()}
        b = result['account_tabs']['10002']
        self.book.rows[b][1][workflow.REGIONAL_HEADERS.index('답변 내용')] = 'B 담당자 답변'
        saved_b = copy.deepcopy(self.book.rows[b])
        roster = [{'id': i, 'username': n, 'label': n, 'active': n in 'ade'}
                  for i, n in [('10001', 'a'), ('10002', 'b'), ('10003', 'c'), ('10004', 'd'), ('10005', 'e')]]
        # First completed account already creates empty D/E tabs from the
        # current verified roster, before their own collection finishes.
        changed = self.sync([abc[0]], roster)
        for i in ['10004', '10005']:
            self.assertEqual(len(self.book.rows[changed['account_tabs'][i]]), 1)
        ade = [abc[0], projection('10004', 'd'), projection('10005', 'e')]
        changed = self.sync(ade, roster)
        self.assertEqual(changed['status'], 'complete')
        self.assertEqual(self.book.rows[b], saved_b)
        for i in ['10002', '10003']:
            self.assertTrue(self.book.sheet(changed['account_tabs'][i])['properties']['hidden'])
        self.assertEqual(self.book.sheet(changed['account_tabs']['10001'])['properties']['sheetId'], original_ids['10001'])
        roster[1]['active'] = True
        returned = self.sync([abc[1]], roster)
        self.assertEqual(self.book.sheet(returned['account_tabs']['10002'])['properties']['sheetId'], original_ids['10002'])
        self.assertFalse(self.book.sheet(b)['properties']['hidden'])
        self.assertEqual(self.book.rows[b], saved_b)

    def test_renamed_username_label_or_tab_retains_id_and_manual_work(self):
        result = self.sync([projection('10001', 'before')])
        name = result['account_tabs']['10001']
        sheet_id = self.book.sheet(name)['properties']['sheetId']
        self.book.sheet(name)['properties']['title'] = '사용자가 고친 탭'
        self.book.rows['사용자가 고친 탭'] = self.book.rows.pop(name)
        column = workflow.REGIONAL_HEADERS.index('답변 내용')
        self.book.rows['사용자가 고친 탭'][1][column] = '수동 답변'
        result = self.sync([projection('10001', 'after', label='새 지역')])
        self.assertEqual(result['account_tabs']['10001'], '사용자가 고친 탭')
        self.assertEqual(self.book.sheet('사용자가 고친 탭')['properties']['sheetId'], sheet_id)
        self.assertEqual(self.book.rows['사용자가 고친 탭'][1][column], '수동 답변')
        self.assertEqual(len(self.book.meta['sheets']), len(workflow.HEADERS)+1)

    def test_retry_after_lost_creation_response_or_batch_limit_does_not_duplicate(self):
        data = projection('10001', 'a')
        self.book.lose_response = True
        with self.assertRaisesRegex(PilotError, '응답 유실'):
            self.sync([data])
        result = self.sync([data], max_batches=1)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(self.sync([data])['batches'], 0)
        self.assertEqual(len(self.book.meta['sheets']), len(workflow.HEADERS)+1)
        result = self.sync([projection('10002', 'b')], max_batches=1)
        self.assertEqual(result['status'], 'pending')
        self.assertEqual(result['phase'], 'rows')
        result = self.sync([projection('10002', 'b')])
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(len(self.book.rows[result['account_tabs']['10002']]), 2)

    def test_old_projection_never_reactivates_an_excluded_account(self):
        a, b = projection('10001', 'a'), projection('10002', 'b')
        result = self.sync([a, b])
        roster = [{'id': '10001', 'label': 'A', 'active': True}, {'id': '10002', 'label': 'B', 'active': False}]
        self.sync([a], roster)
        self.sync([b])  # saved workflow replay has no live roster
        self.assertTrue(self.book.sheet(result['account_tabs']['10002'])['properties']['hidden'])

    def test_legacy_original_ids_bind_even_if_title_changes(self):
        a = projection('10001', 'a')
        self.book.meta['sheets'].append({'properties': {'title': '예전 지역', 'sheetId': 80, 'gridProperties': {'rowCount': 1000}}})
        self.book.rows['예전 지역'] = copy.deepcopy(a['tabs']['계정 · a'])
        result = self.sync([a])
        self.assertEqual(result['account_tabs']['10001'], '예전 지역')
        self.assertEqual(self.book.sheet('예전 지역')['developerMetadata'][0]['metadataValue'], '10001')

    def test_same_display_name_or_unrelated_tab_never_reuses_another_account(self):
        a = projection('10001', 'a', label='같은 지역')
        first = self.sync([a])
        old = first['account_tabs']['10001']
        # An empty user tab already occupies the candidate name for B.
        title = workflow.region_tab('b', '같은 지역')
        self.book.meta['sheets'].append({'properties': {'title': title, 'sheetId': 99, 'gridProperties': {'rowCount': 1000}}})
        self.book.rows[title] = [['직접 만든 자료']]
        result = self.sync([projection('10002', 'b', label='같은 지역')])
        self.assertNotEqual(result['account_tabs']['10002'], old)
        self.assertNotEqual(result['account_tabs']['10002'], title)
        self.assertEqual(self.book.rows[title], [['직접 만든 자료']])
        self.assertEqual(self.book.rows[old][1][0], '10001:1')

    def test_dry_run_and_bad_headers_write_nothing(self):
        result = self.sync([projection('10001', 'a')], dry_run=True)
        self.assertEqual(result['status'], 'dry_run')
        self.assertEqual(self.book.writes, [])
        self.book.rows['설정'] = [['잘못된 헤더']]
        with self.assertRaisesRegex(PilotError, '헤더'):
            self.sync([projection('10001', 'a')])
        self.assertEqual(self.book.writes, [])

    def test_source_or_destination_identity_conflict_stops_before_any_write(self):
        a = projection('10001', 'a')
        result = self.sync([a])
        name = result['account_tabs']['10001']
        self.book.rows[name][1][workflow.REGIONAL_HEADERS.index('계정 ID')] = '10002'
        self.book.writes.clear()
        with self.assertRaisesRegex(PilotError, 'ID'):
            self.sync([a])
        self.assertEqual(self.book.writes, [])
        with self.assertRaisesRegex(PilotError, '다른 계정'):
            projection('10002', 'b', [dict(row(1), account_id='10001')])

    def test_conflicting_or_duplicated_bindings_stop_before_writes(self):
        result = self.sync([projection('10001', 'a')])
        name = result['account_tabs']['10001']
        duplicate = copy.deepcopy(self.book.sheet(name))
        duplicate['properties'].update(title='복제 탭', sheetId=500)
        self.book.meta['sheets'].append(duplicate)
        self.book.rows['복제 탭'] = copy.deepcopy(self.book.rows[name])
        self.book.writes.clear()
        with self.assertRaisesRegex(PilotError, '중복'):
            self.sync([projection('10001', 'a')])
        self.assertEqual(self.book.writes, [])

    def test_no_comments_still_creates_an_account_tab(self):
        result = self.sync([projection('10001', 'a', [])])
        self.assertEqual(self.book.rows[result['account_tabs']['10001']], [workflow.REGIONAL_HEADERS])
        self.assertEqual(self.sync([projection('10001', 'a', [])])['batches'], 0)

    def test_duplicate_projection_keys_stop_before_any_write(self):
        data = projection('10001', 'a')
        data['tabs']['계정 · a'].append(data['tabs']['계정 · a'][1][:])
        with self.assertRaisesRegex(PilotError, '중복'):
            self.sync([data])
        self.assertEqual(self.book.writes, [])

    def test_null_completion_time_matches_a_cleared_sheet_cell_on_retry(self):
        data = projection('10001', 'a')
        column = workflow.HEADERS['수집 로그'].index('종료 시각 (KST)')
        data['tabs']['수집 로그'][1][column] = None
        result = self.sync([data])
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(self.book.rows['수집 로그'][1][column], '')
        self.assertEqual(self.sync([data])['batches'], 0)

    def test_returning_account_is_shown_before_last_visible_old_tab_is_hidden(self):
        a, b = projection('10001', 'a'), projection('10002', 'b')
        result = self.sync([a, b])
        for sheet in self.book.meta['sheets']:
            sheet['properties']['hidden'] = sheet['properties']['title'] != result['account_tabs']['10001']
        self.book.writes.clear()
        roster = [{'id': '10001', 'label': 'A', 'active': False}, {'id': '10002', 'label': 'B', 'active': True}]
        self.sync([b], roster)
        toggles = [r['updateSheetProperties']['properties']['hidden']
                   for body in self.book.writes for r in body['requests'] if 'updateSheetProperties' in r]
        self.assertEqual(toggles, [False, True])

    def test_snapshot_finds_bound_and_legacy_renamed_tabs(self):
        client = workflow.Client('synthetic', 'spreadsheet_id_123456')
        metadata = {'sheets': [{'properties': {'sheetId': 9, 'title': '사용자 탭', 'gridProperties': {'rowCount': 2}},
            'developerMetadata': [{'metadataKey': ACCOUNT_KEY, 'metadataValue': '10001'}]}]}
        original = projection('10001', 'a')['tabs']['계정 · a']
        for bound in [True, False]:
            current = copy.deepcopy(metadata)
            if not bound:
                del current['sheets'][0]['developerMetadata']
            with mock.patch.object(client, 'request', side_effect=[current, {'values': [workflow.REGIONAL_HEADERS]}, {'values': original}]):
                _, snapshot = client.snapshot()
            self.assertEqual(snapshot['사용자 탭'], original)


class ConnectionTransitionTests(unittest.TestCase):
    def setUp(self):
        self.settings, _ = fixture()
        self.registry = initial_registry(self.settings)

    def test_token_rotation_and_target_changes_keep_storage_and_full_history(self):
        original = copy.deepcopy(self.registry['accounts'])
        ade = [original['10001'], {'id': '10004', 'username': 'd', 'label': 'D'}, {'id': '10005', 'username': 'e', 'label': 'E'}]
        group = {'id': 'primary', 'label': '연결', 'secret_version': 'projects/demo/secrets/sns-web-meta-connections/versions/1'}
        changed = apply_group(self.registry, group, ade, 'operator', 'first')
        configs = runtime_configs(self.settings, changed)
        self.assertEqual([c['account_id'] for c in configs], ['10001', '10004', '10005'])
        for i in ['10001', '10002', '10003']:
            self.assertEqual(changed['accounts'][i]['source_prefix'], original[i]['source_prefix'])
        self.assertEqual(configs[1]['_cloud_prefix'], 'accounts/ig_10004')
        self.assertEqual(len(configs[0]['_sheet_roster']), 5)
        for i in range(2, 105):
            group = {**group, 'secret_version': f'projects/demo/secrets/sns-web-meta-connections/versions/{i}'}
            changed = apply_group(changed, group, ade, 'operator', str(i))
        self.assertEqual(len(changed['connection_history']), 105)
        self.assertEqual(changed['connection_history'][1]['account_ids'], ['10001', '10004', '10005'])
        self.assertEqual(changed['connection_history'][1]['secret_version'].rsplit('/', 1)[1], '1')
        self.assertNotIn('access_token', json.dumps(changed))

    def test_shared_source_path_is_rejected_even_for_an_inactive_account(self):
        self.registry['accounts']['10002'].update(source_prefix='', active=False)
        with self.assertRaisesRegex(PilotError, '같은 원본'):
            runtime_configs(self.settings, self.registry)

    def test_registry_roster_is_a_fixed_snapshot_and_uses_primary_defaults(self):
        self.settings['default_account'] = 'waterbomb_busan_official'
        self.settings['accounts']['waterbomb_busan_official']['collection'] = {'lookback_days': 99}
        configs = runtime_configs(self.settings, self.registry)
        self.registry['accounts']['10002']['active'] = False
        self.assertTrue(configs[0]['_sheet_roster'][1]['active'])
        self.assertNotEqual(configs[0].get('collection', {}).get('lookback_days'), 99)


if __name__ == '__main__':
    unittest.main()
