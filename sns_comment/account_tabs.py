"""Bind workbook history to Instagram IDs, independently of tokens and titles."""

import hashlib
import re

from .api import PilotError
from .sheets import HEADERS as LEGACY_HEADERS, DROPDOWNS, cell


ACCOUNT_KEY = 'sns_comment_account_id'


def account_metadata(sheet):
    values = {m.get('metadataValue') for m in sheet.get('developerMetadata', [])
              if m.get('metadataKey') == ACCOUNT_KEY}
    if len(values) > 1 or any(not isinstance(v, str) or not v.isdigit() for v in values):
        raise PilotError('계정 탭의 ID 연결 정보가 충돌합니다.')
    return next(iter(values), None)


def row_account(rows, header):
    """Adopt legacy tabs only from consistent original IDs, never their label."""
    owners = set()
    for row in rows[1:]:
        if not any(v not in ('', None) for v in row):
            continue
        key = row[0] if row else None
        if not isinstance(key, str) or ':' not in key:
            raise PilotError('계정 탭의 연결 키를 확인해주세요.')
        owner, comment = key.split(':', 1)
        if (not owner.isdigit() or not comment or len(row) <= header.index('댓글 ID')
                or row[header.index('계정 ID')] != owner or row[header.index('댓글 ID')] != comment):
            raise PilotError('계정 탭의 연결 키·계정 ID·댓글 ID가 다릅니다.')
        owners.add(owner)
    if len(owners) > 1:
        raise PilotError('계정 탭에 다른 계정 자료가 섞여 있습니다.')
    return next(iter(owners), None)


def _binding(sheet_id, identifier):
    return {'createDeveloperMetadata': {'developerMetadata': {
        'metadataKey': ACCOUNT_KEY, 'metadataValue': identifier,
        'location': {'sheetId': sheet_id}, 'visibility': 'DOCUMENT'}}}


def _title(preferred, identifier, used):
    name = re.sub(r'[\\/\?\*\[\]:\x00-\x1f]', ' ', preferred).strip().strip("'") or '계정'
    title = name[:100]
    index = 0
    while title.casefold() in used:
        suffix = ' · ' + identifier + (f' ({index})' if index else '')
        title = name[:100-len(suffix)] + suffix
        index += 1
    used.add(title.casefold())
    return title


def _create(title, sheet_id, identifier, header):
    width, height = len(header), 1000
    requests = [
        {'addSheet': {'properties': {'sheetId': sheet_id, 'title': title,
            'gridProperties': {'rowCount': height, 'columnCount': max(26, width), 'frozenRowCount': 1}}}},
        _binding(sheet_id, identifier),
        {'updateCells': {'range': {'sheetId': sheet_id, 'startRowIndex': 0, 'endRowIndex': 1,
            'startColumnIndex': 0, 'endColumnIndex': width},
            'rows': [{'values': [cell(h) for h in header]}], 'fields': 'userEnteredValue'}},
        {'repeatCell': {'range': {'sheetId': sheet_id, 'startRowIndex': 0, 'endRowIndex': 1},
            'cell': {'userEnteredFormat': {'textFormat': {'bold': True}}}, 'fields': 'userEnteredFormat.textFormat.bold'}},
        {'setBasicFilter': {'filter': {'range': {'sheetId': sheet_id, 'startRowIndex': 0,
            'endRowIndex': height, 'startColumnIndex': 0, 'endColumnIndex': width}}}},
    ]
    for column, choices in DROPDOWNS['분류와 처리'].items():
        label = LEGACY_HEADERS['분류와 처리'][column]
        if label in header:
            j = header.index(label)
            requests.append({'setDataValidation': {'range': {'sheetId': sheet_id, 'startRowIndex': 1,
                'endRowIndex': height, 'startColumnIndex': j, 'endColumnIndex': j+1},
                'rule': {'condition': {'type': 'ONE_OF_LIST', 'values': [{'userEnteredValue': v} for v in choices]},
                         'showCustomUi': True, 'strict': True}}})
    date_column = header.index('작성 시각 (KST)')
    requests.append({'repeatCell': {'range': {'sheetId': sheet_id, 'startRowIndex': 1,
        'startColumnIndex': date_column, 'endColumnIndex': date_column+1},
        'cell': {'userEnteredFormat': {'numberFormat': {'type': 'DATE_TIME', 'pattern': 'yyyy-mm-dd hh:mm'}}},
        'fields': 'userEnteredFormat.numberFormat'}})
    return requests


def reconcile(source, metadata, snapshot, header, roster=None):
    """Return a routed projection and resumable structure requests.

    Only a current registry snapshot may change tab visibility. Replaying old
    workflow.json files can restore rows but cannot pause current accounts.
    """
    regional = source.get('regional_tabs', {})
    if len(set(regional.values())) != len(regional):
        raise PilotError('한 계정에 여러 원본 탭을 지정할 수 없습니다.')
    wanted = {}
    for name, identifier in regional.items():
        if not isinstance(identifier, str) or not identifier.isdigit():
            raise PilotError('계정 탭의 ID 형식을 확인해주세요.')
        rows = source['tabs'][name]
        if rows[:1] != [header] or row_account(rows, header) not in (None, identifier):
            raise PilotError('계정 탭 원본의 ID·헤더가 다릅니다.')
        wanted[identifier] = name
    roster_by_id = {}
    for account in roster or []:
        identifier = account.get('id')
        if (not isinstance(identifier, str) or not identifier.isdigit() or identifier in roster_by_id
                or not isinstance(account.get('active'), bool) or not isinstance(account.get('label'), str)):
            raise PilotError('계정 탭의 현재 수집 목록을 확인해주세요.')
        roster_by_id[identifier] = account
        if account['active']:
            wanted.setdefault(identifier, account['label'])

    sheets = metadata.get('sheets', [])
    by_title = {s['properties']['title']: s for s in sheets}
    bindings, owners, requests = {}, {}, []
    for sheet in sheets:
        name = sheet['properties']['title']
        bound = account_metadata(sheet)
        rows = snapshot.get(name, [])
        if rows[:1] == [header]:
            observed = row_account(rows, header)
            if bound and observed and bound != observed:
                raise PilotError('탭에 연결된 계정 ID와 실제 원본이 다릅니다: ' + name)
            identifier = bound or observed
        elif bound:
            raise PilotError('계정에 연결된 탭의 헤더가 다릅니다: ' + name)
        else:
            continue
        if identifier:
            if identifier in bindings:
                raise PilotError('같은 계정 ID에 연결된 탭이 중복됩니다: ' + identifier)
            bindings[identifier], owners[name] = name, identifier
            if not bound:
                requests.append(_binding(sheet['properties']['sheetId'], identifier))

    used_titles = {name.casefold() for name in by_title}
    used_ids = {s['properties']['sheetId'] for s in sheets}
    for identifier, preferred in wanted.items():
        if identifier in bindings:
            continue
        # A header-only legacy tab has no history to merge. Bind it once.
        if preferred in by_title and preferred not in owners and snapshot.get(preferred) == [header]:
            name = preferred
            requests.append(_binding(by_title[name]['properties']['sheetId'], identifier))
        else:
            name = _title(preferred, identifier, used_titles)
            sheet_id = int(hashlib.sha256(('instagram:' + identifier).encode()).hexdigest()[:8], 16) % 2_147_483_647
            while sheet_id in used_ids:
                sheet_id = (sheet_id + 1) % 2_147_483_647
            used_ids.add(sheet_id)
            requests.extend(_create(name, sheet_id, identifier, header))
        bindings[identifier], owners[name] = name, identifier

    # Keep excluded accounts' rows and reviews intact. A later reconnect uses
    # the same sheetId, including after a manual tab or username rename.
    visibility = []
    for identifier, account in roster_by_id.items():
        name = bindings.get(identifier)
        if name not in by_title:
            continue
        prop = by_title[name]['properties']
        hidden = not account['active']
        if bool(prop.get('hidden', False)) != hidden:
            visibility.append({'updateSheetProperties': {'properties': {'sheetId': prop['sheetId'], 'hidden': hidden}, 'fields': 'hidden'}})
    # Sheets requires at least one visible tab. With no active accounts leave
    # the last visible history tab available if there is no common overview.
    final_hidden = {r['updateSheetProperties']['properties']['sheetId']: r['updateSheetProperties']['properties']['hidden'] for r in visibility}
    if (any('addSheet' in r for r in requests)
            or any(not final_hidden.get(s['properties']['sheetId'], s['properties'].get('hidden', False)) for s in sheets)):
        # Reveal a returning account before hiding the last visible old one.
        requests.extend(sorted(visibility, key=lambda r: r['updateSheetProperties']['properties']['hidden']))

    rename = {name: bindings[identifier] for name, identifier in regional.items()}
    routed = {**source, 'tabs': {rename.get(name, name): rows for name, rows in source['tabs'].items()},
              'headers': {rename.get(name, name): columns for name, columns in source['headers'].items()},
              'regional_tabs': {bindings[identifier]: identifier for identifier in regional.values()}}
    return routed, requests, bindings
