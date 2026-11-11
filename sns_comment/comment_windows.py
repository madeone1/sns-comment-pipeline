"""Comment-time collection coverage and bounded historical discovery.

Recent media discovery, verified notifications and known activity are refreshed
before projection. One resumable full-media baseline also visits old posts that
predate webhook connection. Its cursor survives reporting-day rollover.
"""

import datetime as dt
import time

from .account import collect_continuously
from .api import PilotError
from .reporting import snapshot, written_periods
from .storage import now


def projections(db, account_id, instant, media_since=None):
    periods = written_periods(instant)
    if media_since:
        periods = [{**p, 'media_since': media_since} for p in periods]
    daily = periods[0]
    week = {**daily, 'key': 'recent_seven_days', 'label': '최근 7일 댓글',
        'start': (dt.datetime.fromisoformat(daily['end'])-dt.timedelta(days=7)).isoformat()}
    return [snapshot(db, account_id, p) for p in periods], snapshot(db, account_id, week)


def baseline(config, store, client, start, end, checkpoint):
    """Budget-bounded baseline, never restarted merely because the day changed."""
    store.db.execute('''CREATE TABLE IF NOT EXISTS comment_baselines (
        account_id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, created_at TEXT NOT NULL)''')
    store.db.commit()
    row = store.db.execute('SELECT scope_id FROM comment_baselines WHERE account_id=?', (config['account_id'],)).fetchone()
    owner = config['expected_username'].lstrip('@')
    scope = store.scope(row['scope_id']) if row else None
    if scope and scope['owner'] != owner:
        scope = None
    if scope is None:
        scope = store.collection_scope(config['account_id'], owner, start, end, restart=True, media_policy='all_accessible')
        with store.db:
            store.db.execute('INSERT OR REPLACE INTO comment_baselines VALUES(?,?,?)', (config['account_id'], scope['id'], now()))
        checkpoint()
    if scope['status'] == 'complete':
        return {'status': 'complete', 'scope_id': scope['id'], 'api_requests': 0,
            'completed_at': scope['completed_at'], 'media_discovered': store.scope_progress(scope['id'])['media_discovered']}
    allowance = min(config.get('cloud', {}).get('historical_baseline_requests', 1000), 5000-client.request_count)
    if allowance < 2:
        return {'status': 'pending', 'scope_id': scope['id'], 'reason': 'request_budget', 'api_requests': 0}
    last_save = time.monotonic()
    def progress(value):
        nonlocal last_save
        if time.monotonic()-last_save >= 90:
            checkpoint()
            last_save = time.monotonic()
    try:
        result = collect_continuously(client, store, owner, scope['window_start'], scope['window_end'],
            account_id=config['account_id'], resume_scope_id=scope['id'], media_policy='all_accessible',
            max_requests=min(250, allowance), max_total_requests=allowance,
            workers=config.get('collection', {}).get('workers', 3), progress=progress)
        checkpoint()
        return {**result, 'status': 'complete' if result['status'] == 'complete' else 'pending'}
    except PilotError as error:
        checkpoint()
        return {'status': 'pending', 'scope_id': scope['id'], 'error': str(error)}


def aggregate(primary, tracking, webhook, historical, store, account_id, start, end, media_since=None):
    complete = all(x and x.get('status') == 'complete' for x in (primary, tracking))
    complete = complete and historical and (historical.get('status') == 'complete' or
        (media_since and historical.get('status') == 'not_required'))
    complete = complete and (webhook is None or webhook.get('status') == 'complete')
    media_ids = [r[0] for r in store.db.execute("SELECT id FROM media WHERE account_id=? AND julianday(replace(written_at,'+0000','+00:00'))>=julianday(?) AND julianday(replace(written_at,'+0000','+00:00'))<julianday(?)",
        (account_id, media_since, end))] if media_since else None
    return {**primary, 'status': 'complete' if complete else 'partial', 'scope': 'comment_written_at',
        'basis': 'comment_written_at', 'media_age_restricted': bool(media_since), 'media_since':media_since,
        'stored_in_window': store.summary(account_id, start=start, end=end, media_ids=media_ids),
        'last_scope_completed_at': now() if complete else None, 'last_checked_at': now(),
        'coverage': {'status': 'complete' if complete else 'pending', 'historical_baseline': historical,
            'recent_media': primary, 'known_activity': tracking, 'webhook': webhook}}
