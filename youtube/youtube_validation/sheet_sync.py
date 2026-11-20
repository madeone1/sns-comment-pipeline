"""Idempotent literal-value projection into the dedicated syan-owned Sheet."""

from contextlib import closing
import hashlib
import json
import sqlite3
import time
from urllib.parse import quote

from .api import ValidationError
from .google_cloud import Google
from .projection import HEADERS, LOG_HEADERS, snapshot


class Sheets:
    def __init__(self, spreadsheet_id, google=None):
        if not spreadsheet_id or '/' in spreadsheet_id:
            raise ValidationError('invalid_spreadsheet_id')
        self.id = spreadsheet_id
        self.google = google or Google()
        self.base = 'https://sheets.googleapis.com/v4/spreadsheets/' + spreadsheet_id
        self.last_write = 0

    def get(self, suffix='', **params):
        from urllib.parse import urlencode
        return self.google.json('GET', self.base + suffix + ('?' + urlencode(params) if params else ''), sheets=True)

    def write(self, suffix, value):
        delay = 1.1 - (time.monotonic() - self.last_write)
        if delay > 0:
            time.sleep(delay)
        result = self.google.json('POST', self.base + suffix, value, sheets=True)
        self.last_write = time.monotonic()
        return result

    def metadata(self):
        return self.get(fields='spreadsheetId,properties(title),sheets(properties,basicFilter)')

    def values(self, address):
        return self.get('/values/' + quote(address, safe=''), valueRenderOption='UNFORMATTED_VALUE').get('values', [])


def digest(row):
    return hashlib.sha256(json.dumps(row, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def batches(rows):
    batch, size = [], 0
    for item in rows:
        amount = len(json.dumps(item[1],ensure_ascii=False).encode())+150
        if batch and (len(batch)>=300 or size+amount>1_400_000):
            yield batch
            batch, size = [], 0
        batch.append(item)
        size += amount
    if batch:
        yield batch


def sync(path, spreadsheet_id, result=None, google=None):
    source = snapshot(path, result)
    sheets = Sheets(spreadsheet_id, google)
    metadata = sheets.metadata()
    tabs = {tab['properties']['title']: tab for tab in metadata['sheets']}
    if not {'수집 현황','댓글','수집 이력'} <= tabs.keys():
        raise ValidationError('required_youtube_tabs_missing')
    if sheets.values("'댓글'!A1:R1") != [HEADERS]:
        raise ValidationError('youtube_comment_headers_changed')
    if sheets.values("'수집 이력'!A1:K1") != [LOG_HEADERS]:
        raise ValidationError('youtube_log_headers_changed')
    row_count = tabs['댓글']['properties']['gridProperties']['rowCount']
    existing, last = {}, 1
    # Read the bounded key column in one response. Separate page reads can
    # observe different row orders while the spreadsheet is being sorted.
    values = sheets.values(f"'댓글'!A2:A{row_count}")
    for offset, row in enumerate(values):
        if row and row[0] != '':
            key = str(row[0])
            if key in existing:
                raise ValidationError('duplicate_comment_ids_in_sheet')
            existing[key] = 2 + offset
            last = max(last, 2 + offset)
    pending, updates = [], 0
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        from .privacy import initialize as initialize_privacy, digest_id
        initialize_privacy(db)
        privacy_pending={row['comment_id']:bool(row['include_memos']) for row in db.execute('SELECT * FROM privacy_pending_sheet')}
        source_ids={row[0] for row in source['comments']}
        clears=[]
        clear_all=db.execute("SELECT value FROM privacy_state WHERE key='clear_all_sheet'").fetchone()
        clear_all=bool(clear_all and clear_all[0]=='true')
        memo_rows=[row[0] for row in db.execute('SELECT row_number FROM privacy_pending_memo_rows')]
        if clear_all:
            clears.append({'updateCells':{'range':{'sheetId':tabs['댓글']['properties']['sheetId'],
                'startRowIndex':1,'endRowIndex':row_count,'startColumnIndex':0,'endColumnIndex':26},'rows':[],
                'fields':'userEnteredValue,note,textFormatRuns,chipRuns,userEnteredFormat.textFormat.link'}})
        else:
            for number in memo_rows:
                clears.append({'updateCells':{'range':{'sheetId':tabs['댓글']['properties']['sheetId'],
                    'startRowIndex':number-1,'endRowIndex':number,'startColumnIndex':0,'endColumnIndex':26},'rows':[],
                    'fields':'userEnteredValue,note,textFormatRuns,chipRuns,userEnteredFormat.textFormat.link'}})
        for identifier,include_memos in privacy_pending.items():
            if identifier in source_ids and not include_memos:
                continue  # Refreshed this cycle: update in place and keep its memo association.
            number=existing.get(identifier)
            if number is not None and not clear_all:
                if not include_memos:
                    author=db.execute('SELECT author_hash FROM privacy_expired_authors WHERE comment_id=?',(identifier,)).fetchone()
                    with db: db.execute('INSERT OR REPLACE INTO privacy_memo_registry VALUES(?,?,?)',
                        (number,digest_id(identifier),author[0] if author else digest_id('')))
                clears.append({'updateCells':{'range':{'sheetId':tabs['댓글']['properties']['sheetId'],
                    'startRowIndex':number-1,'endRowIndex':number,'startColumnIndex':0,
                    'endColumnIndex':26 if include_memos else 18},'rows':[],
                    'fields':'userEnteredValue,note,textFormatRuns,chipRuns,userEnteredFormat.textFormat.link' if include_memos else 'userEnteredValue'}})
        for start in range(0,len(clears),200):
            sheets.write(':batchUpdate',{'requests':clears[start:start+200]})
        if privacy_pending:
            with db:
                db.execute('DELETE FROM privacy_pending_sheet')
                db.execute('DELETE FROM privacy_expired_authors')
            for identifier,include_memos in privacy_pending.items():
                if identifier not in source_ids or include_memos: existing.pop(identifier,None)
        with db:
            db.execute('DELETE FROM privacy_pending_memo_rows')
            if clear_all:
                db.execute("UPDATE privacy_state SET value='false' WHERE key='clear_all_sheet'")
                db.execute('DELETE FROM privacy_memo_registry')
                existing={}
        memo_highwater=db.execute('SELECT MAX(row_number) FROM privacy_memo_registry').fetchone()[0] or 1
        # Avoid reusing a row with an operator memo after its API-owned cells expire.
        memos=sheets.values(f"'댓글'!S2:Z{row_count}")
        last=max(last,memo_highwater,max((i+2 for i,row in enumerate(memos) if any(value!='' for value in row)),default=1))
        prior = {row['comment_id']:dict(row) for row in db.execute('SELECT * FROM pipeline_sheet_rows')}
        for row in source['comments']:
            identifier, hashed = row[0], digest(row)
            number = existing.get(identifier)
            if number is None:
                last += 1
                number = last
                existing[identifier] = number
            cached = prior.get(identifier)
            if not cached or cached['row_number'] != number or cached['payload_hash'] != hashed:
                pending.append((number, row, hashed))
        # Clear only moved cache slots before swaps; comments and sheet rows stay.
        moved=[(identifier,) for identifier,cached in prior.items()
               if existing.get(identifier)!=cached['row_number']]
        db.executemany('DELETE FROM pipeline_sheet_rows WHERE comment_id=?',moved)
        db.commit()
        required = max(last, 2)
        requests = []
        for name, rows, cols in (('댓글',required,18), ('수집 이력',max(len(source['log'])+1,2),11), ('수집 현황',30,5)):
            props = tabs[name]['properties']
            if props['gridProperties']['rowCount'] < rows:
                requests.append({'updateSheetProperties': {'properties': {'sheetId':props['sheetId'],
                    'gridProperties':{'rowCount':rows}}, 'fields':'gridProperties.rowCount'}})
        comment_id = tabs['댓글']['properties']['sheetId']
        if required > row_count:
            added = {'sheetId':comment_id,'startRowIndex':row_count,'endRowIndex':required,
                     'startColumnIndex':0,'endColumnIndex':18}
            requests.extend([
                {'repeatCell':{'range':added,'cell':{'userEnteredFormat':{
                    'textFormat':{'fontFamily':'Arial','fontSize':10},'verticalAlignment':'MIDDLE',
                    'wrapStrategy':'CLIP'}},'fields':'userEnteredFormat(textFormat,verticalAlignment,wrapStrategy)'}},
                {'repeatCell':{'range':dict(added,startColumnIndex=5,endColumnIndex=6),
                    'cell':{'userEnteredFormat':{'wrapStrategy':'WRAP'}},'fields':'userEnteredFormat.wrapStrategy'}},
                {'updateDimensionProperties':{'range':{'sheetId':comment_id,'dimension':'ROWS',
                    'startIndex':row_count,'endIndex':required},'properties':{'pixelSize':44},'fields':'pixelSize'}},
            ])
        log_id = tabs['수집 이력']['properties']['sheetId']
        for column in (2,3):
            requests.append({'repeatCell':{'range':{'sheetId':log_id,'startRowIndex':1,
                'endRowIndex':max(len(source['log'])+1,2),'startColumnIndex':column,'endColumnIndex':column+1},
                'cell':{'userEnteredFormat':{'numberFormat':{'type':'DATE_TIME','pattern':'yyyy-mm-dd hh:mm'}}},
                'fields':'userEnteredFormat.numberFormat'}})
        for column in (3,13,14,17):
            requests.append({'repeatCell': {'range': {'sheetId':comment_id,'startRowIndex':1,
                'endRowIndex':required,'startColumnIndex':column,'endColumnIndex':column+1},
                'cell':{'userEnteredFormat':{'numberFormat':{'type':'DATE_TIME','pattern':'yyyy-mm-dd hh:mm'}}},
                'fields':'userEnteredFormat.numberFormat'}})
        basic_filter = dict(tabs['댓글'].get('basicFilter') or {})
        basic_filter['range'] = {'sheetId':comment_id,'startRowIndex':0,'endRowIndex':required,
                                'startColumnIndex':0,'endColumnIndex':18}
        requests.append({'setBasicFilter':{'filter':basic_filter}})
        sheets.write(':batchUpdate', {'requests':requests})
        # Explicit row destinations make timeout retries idempotent; no append API.
        for batch in batches(pending):
            sheets.write('/values:batchUpdate', {'valueInputOption':'RAW', 'data':[
                {'range':f"'댓글'!A{number}:R{number}", 'values':[row]} for number,row,_ in batch]})
            db.executemany('''INSERT INTO pipeline_sheet_rows VALUES(?,?,?) ON CONFLICT(comment_id)
                DO UPDATE SET row_number=excluded.row_number,payload_hash=excluded.payload_hash''',
                [(row[0],number,hashed) for number,row,hashed in batch])
            db.commit()
            updates += len(batch)
        sheets.write('/values:batchUpdate', {'valueInputOption':'RAW', 'data':[
            {'range':"'수집 현황'!A1:E24", 'values':[row+['']*(5-len(row)) for row in source['overview']]},
            {'range':f"'수집 이력'!A1:K{len(source['log'])+1}", 'values':[LOG_HEADERS,*source['log']]}]})
        # Old log rows can remain after a 200-run window shifts. Clear owned tail.
        old_logs = tabs['수집 이력']['properties']['gridProperties']['rowCount']
        if old_logs > len(source['log'])+1:
            sheets.write('/values:batchClear', {'ranges':[
                f"'수집 이력'!A{len(source['log'])+2}:K{old_logs}"]})
    actual_headers = sheets.values("'댓글'!A1:R1")
    if actual_headers != [HEADERS]:
        raise ValidationError('sheet_verification_failed')
    return {'spreadsheet_id':spreadsheet_id, 'comment_ids':len(source['comments']),
            'updated_rows':updates, 'last_data_row':required, 'verified_headers':True,
            'privacy_cleared_rows':len(clears)}
