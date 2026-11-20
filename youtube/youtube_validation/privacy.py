"""Retention and verified stored-copy deletion. Never writes to YouTube."""
from datetime import datetime, timedelta, timezone
import copy
import hashlib
import json
import re
import requests

from .api import ValidationError

RETENTION_DAYS=21


def digest_id(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def initialize(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS privacy_exclusions (
            kind TEXT NOT NULL, target_hash TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY(kind,target_hash));
        CREATE TABLE IF NOT EXISTS privacy_pending_sheet (
            comment_id TEXT PRIMARY KEY, include_memos INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS privacy_requests (
            request_id TEXT PRIMARY KEY, received_at TEXT NOT NULL, processed_at TEXT NOT NULL,
            kind TEXT NOT NULL, removed_count INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS privacy_state (key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS privacy_expired_authors (comment_id TEXT PRIMARY KEY,author_hash TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS privacy_memo_registry (
            row_number INTEGER PRIMARY KEY,comment_hash TEXT NOT NULL,author_hash TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS privacy_pending_memo_rows (row_number INTEGER PRIMARY KEY);
    ''')


def exclusions(db):
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'privacy_exclusions' not in tables: return set()
    return {(r[0],r[1]) for r in db.execute('SELECT kind,target_hash FROM privacy_exclusions')}


def blocked(row, rules):
    return ('comment',digest_id(row.get('id',''))) in rules or (
        bool(row.get('author_channel_id')) and ('author',digest_id(row['author_channel_id'])) in rules)


def row_blocked(db,row):
    return blocked(row,exclusions(db))


def filtered_response(resource,value,rules):
    """Filter only the stored copy; the API pagination response is unchanged."""
    if not rules or resource not in ('comments','commentThreads'): return value
    result=copy.deepcopy(value)
    def hidden(item):
        snippet=item.get('snippet',{})
        return blocked({'id':item.get('id'),'author_channel_id':(snippet.get('authorChannelId') or {}).get('value')},rules)
    if resource=='comments':
        result['items']=[item for item in result.get('items',[]) if not hidden(item)]
    else:
        kept=[]
        for thread in result.get('items',[]):
            if hidden(thread.get('snippet',{}).get('topLevelComment',{})): continue
            if 'replies' in thread:
                thread['replies']['comments']=[item for item in thread['replies'].get('comments',[]) if not hidden(item)]
            kept.append(thread)
        result['items']=kept
    result['_stored_copy_privacy_filtered']=True
    return result


def validate_control(control):
    if not control: return {'schema':1,'collection_enabled':True,'requests':[],'backups':[]}
    if control.get('schema')!=1 or not isinstance(control.get('collection_enabled',True),bool):
        raise ValidationError('invalid_privacy_control')
    for item in control.get('requests',[]):
        if item.get('kind') not in ('comment','author','all') or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}',str(item.get('request_id',''))):
            raise ValidationError('invalid_privacy_request')
        if not item.get('verified_by') or not item.get('received_at'):
            raise ValidationError('unverified_privacy_request')
        if item['kind']!='all' and (not item.get('target_hashes') or any(not re.fullmatch('[a-f0-9]{64}',x) for x in item['target_hashes'])):
            raise ValidationError('invalid_privacy_target')
        if item['kind']=='all' and control.get('collection_enabled',True):
            raise ValidationError('disconnect_must_disable_collection')
    if not control.get('collection_enabled',True) and not any(x.get('kind')=='all' for x in control.get('requests',[])):
        raise ValidationError('verified_disconnect_request_required')
    return control


def _time(value):
    value=str(value)
    if re.match(r'^\d{8}T',value):
        return datetime.strptime(value[:8],'%Y%m%d').replace(tzinfo=timezone.utc)
    parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
    if parsed.tzinfo is None: raise ValueError('timezone_required')
    return parsed.astimezone(timezone.utc)


def apply(db,control=None,now=None):
    control=validate_control(control)
    now=now or datetime.now(timezone.utc)
    cutoff=now-timedelta(days=RETENTION_DAYS)
    initialize(db)
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    removed=set(); explicit=set(); processed=[]
    with db:
        db.execute("INSERT OR REPLACE INTO privacy_state VALUES('disconnected',?)",('false' if control.get('collection_enabled',True) else 'true',))
        rows=list(db.execute('SELECT comment_id,last_seen,payload_json FROM comments')) if 'comments' in tables else []
        for identifier,last_seen,payload in rows:
            if _time(last_seen)<cutoff: removed.add(identifier)
        for request in control.get('requests',[]):
            if db.execute('SELECT 1 FROM privacy_requests WHERE request_id=?',(request['request_id'],)).fetchone(): continue
            kind=request['kind']; targets=set(request.get('target_hashes',[]))
            matched=set()
            for identifier,_,payload in rows:
                row=json.loads(payload)
                if kind=='all' or (kind=='comment' and digest_id(identifier) in targets) or (kind=='author' and digest_id(row.get('author_channel_id','')) in targets):
                    matched.add(identifier)
            if kind!='all':
                db.executemany('INSERT OR IGNORE INTO privacy_exclusions VALUES(?,?,?)',[(kind,target,now.isoformat()) for target in targets])
                db.executemany('INSERT OR IGNORE INTO privacy_exclusions VALUES(?,?,?)',[('comment',digest_id(identifier),now.isoformat()) for identifier in matched])
            removed.update(matched); explicit.update(matched)
            for number,comment_hash,author_hash in list(db.execute('SELECT * FROM privacy_memo_registry')):
                if kind=='all' or (kind=='comment' and comment_hash in targets) or (kind=='author' and author_hash in targets):
                    db.execute('INSERT OR IGNORE INTO privacy_pending_memo_rows VALUES(?)',(number,))
                    db.execute('DELETE FROM privacy_memo_registry WHERE row_number=?',(number,))
            if kind=='all': db.execute("INSERT OR REPLACE INTO privacy_state VALUES('clear_all_sheet','true')")
            db.execute('INSERT INTO privacy_requests VALUES(?,?,?,?,?)',(request['request_id'],request['received_at'],now.isoformat(),kind,len(matched)))
            db.execute("INSERT OR REPLACE INTO privacy_state VALUES('purge_raw','true')")
            processed.append({'request_id':request['request_id'],'removed':len(matched),'kind':kind})
        if removed:
            for identifier,_,payload in rows:
                if identifier in removed and identifier not in explicit:
                    db.execute('INSERT OR REPLACE INTO privacy_expired_authors VALUES(?,?)',
                        (identifier,digest_id(json.loads(payload).get('author_channel_id',''))))
            db.executemany('INSERT INTO privacy_pending_sheet VALUES(?,?) ON CONFLICT(comment_id) DO UPDATE SET include_memos=MAX(include_memos,excluded.include_memos)',[(x,int(x in explicit)) for x in removed])
            for table,column in (('comments','comment_id'),('observations','comment_id'),('pipeline_seen','comment_id'),('pipeline_sheet_rows','comment_id')):
                if table in tables: db.executemany('DELETE FROM '+table+' WHERE '+column+'=?',[(x,) for x in removed])
            for table in ('pipeline_cursors','pipeline_page_tokens'):
                if table in tables: db.executemany('DELETE FROM '+table+" WHERE kind='replies' AND target=?",[(x,) for x in removed])
        # Historical snapshots have their own expiry, even when the latest row is refreshed.
        if 'runs' in tables:
            for (identifier,) in list(db.execute('SELECT run_id FROM runs')):
                if processed or _time(identifier)<cutoff:
                    if 'observations' in tables: db.execute('DELETE FROM observations WHERE run_id=?',(identifier,))
                    db.execute('DELETE FROM runs WHERE run_id=?',(identifier,))
        if 'pipeline_cycles' in tables:
            for identifier,started in list(db.execute('SELECT id,started_at FROM pipeline_cycles')):
                if _time(started)<cutoff:
                    for table in ('pipeline_cursors','pipeline_page_tokens','pipeline_seen','pipeline_attempts'):
                        if table in tables: db.execute('DELETE FROM '+table+' WHERE cycle_id=?',(identifier,))
                    db.execute('DELETE FROM pipeline_cycles WHERE id=?',(identifier,))
        if processed and 'pipeline_attempts' in tables:
            # Summaries may contain historical status/ID-derived data; keep only current counts.
            db.execute('UPDATE pipeline_attempts SET summary_json=NULL')
        if any(x['kind']=='all' for x in processed):
            for table in ('observations','runs','pipeline_seen','pipeline_cursors','pipeline_page_tokens','pipeline_attempts','pipeline_cycles','pipeline_sheet_rows'):
                if table in tables: db.execute('DELETE FROM '+table)
    if removed or processed: db.execute('VACUUM')
    return {'retention_days':RETENTION_DAYS,'expired_or_requested_rows':len(removed),
            'explicit_request_rows':len(explicit),'new_requests':processed,'collection_enabled':control.get('collection_enabled',True)}


def purge_cloud(cloud,db,now=None):
    now=now or datetime.now(timezone.utc)
    cutoff=now-timedelta(days=RETENTION_DAYS)
    state=db.execute("SELECT value FROM privacy_state WHERE key='purge_raw'").fetchone()
    all_raw=bool(state and state[0]=='true')
    candidates=[]
    for item in cloud.list_objects('raw/'):
        if all_raw or _time(item['timeCreated'])<cutoff:
            candidates.append(item)
    # The dedicated job lease prevents a second collector mutating these objects.
    batch=candidates[:500]
    for item in batch: cloud.delete_object(item['name'],item['generation'])
    if all_raw and len(candidates)<=len(batch):
        with db: db.execute("UPDATE privacy_state SET value='false' WHERE key='purge_raw'")
    return {'deleted_raw_objects':len(batch),'remaining_raw_objects':len(candidates)-len(batch),
            'requested_purge':all_raw,'storage_recovery_days':7}


def clear_processing_history(sheets,db,now=None):
    """The separate moderation log is also a stored API-data copy."""
    now=now or datetime.now(timezone.utc)
    tabs={x['properties']['title']:x['properties'] for x in sheets.metadata()['sheets']}
    if '처리 이력' not in tabs: return 0
    props=tabs['처리 이력']
    header=sheets.values("'처리 이력'!A1:T1")
    if not header or len(header[0])<14 or header[0][12:14]!=['댓글 ID','작성자 채널 ID']:
        raise ValidationError('privacy_history_headers_changed')
    rows=sheets.values(f"'처리 이력'!A2:T{props['gridProperties']['rowCount']}")
    rules=exclusions(db)
    state=db.execute("SELECT value FROM privacy_state WHERE key='disconnected'").fetchone()
    all_request=bool(state and state[0]=='true')
    cutoff_serial=(now+timedelta(hours=9)-timedelta(days=RETENTION_DAYS)-datetime(1899,12,30,tzinfo=timezone.utc)).total_seconds()/86400
    clears=[]
    for offset,row in enumerate(rows):
        if not any(x!='' for x in row): continue
        row=row+['']*(20-len(row))
        expired=isinstance(row[0],(float,int)) and row[0]<cutoff_serial
        if all_request or expired or ('comment',digest_id(row[12])) in rules or ('author',digest_id(row[13])) in rules:
            clears.append({'updateCells':{'range':{'sheetId':props['sheetId'],'startRowIndex':offset+1,
                'endRowIndex':offset+2,'startColumnIndex':0,'endColumnIndex':20},'rows':[],
                'fields':'userEnteredValue,note,textFormatRuns,chipRuns,userEnteredFormat.textFormat.link'}})
    for offset in range(0,len(clears),200): sheets.write(':batchUpdate',{'requests':clears[offset:offset+200]})
    return len(clears)


def revoke_connection(secret,db):
    token_hash=digest_id(secret['refresh_token'])
    prior=db.execute("SELECT value FROM privacy_state WHERE key='oauth_revoked_hash'").fetchone()
    if prior and prior[0]==token_hash: return 'revoked'
    try:
        response=requests.post('https://oauth2.googleapis.com/revoke',data={'token':secret['refresh_token']},timeout=30)
        invalid=response.status_code==400 and response.json().get('error')=='invalid_token'
        if response.status_code!=200 and not invalid: return 'retry_required'
    except Exception:
        return 'retry_required'
    with db: db.execute("INSERT OR REPLACE INTO privacy_state VALUES('oauth_revoked_hash',?)",(token_hash,))
    return 'revoked'
