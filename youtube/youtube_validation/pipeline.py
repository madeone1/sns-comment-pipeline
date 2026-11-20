"""Resumable full-channel collection. Page contents and cursors commit together."""

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time
import uuid
from zoneinfo import ZoneInfo

from .api import HANDLE, ValidationError, verify_channel
from .collector import STATUSES, normalize
from .storage import private_dir, write_json
from .privacy import exclusions, blocked

EXPECTED_CHANNEL = 'UCSk-o7uCpprI1GNvmt070cQ'
PACIFIC = ZoneInfo('America/Los_Angeles')
KST = ZoneInfo('Asia/Seoul')


def instant():
    return datetime.now(timezone.utc).isoformat()


def initialize(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS comments (
            channel_id TEXT NOT NULL, comment_id TEXT NOT NULL,
            first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
            payload_json TEXT NOT NULL, PRIMARY KEY(channel_id, comment_id));
        CREATE TABLE IF NOT EXISTS pipeline_cycles (
            id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
            state TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS pipeline_cursors (
            cycle_id TEXT NOT NULL, kind TEXT NOT NULL, target TEXT NOT NULL,
            video_id TEXT, page_token TEXT, done INTEGER NOT NULL DEFAULT 0,
            pages INTEGER NOT NULL DEFAULT 0, items INTEGER NOT NULL DEFAULT 0,
            restarts INTEGER NOT NULL DEFAULT 0, error TEXT,
            PRIMARY KEY(cycle_id, kind, target));
        CREATE TABLE IF NOT EXISTS pipeline_seen (
            cycle_id TEXT NOT NULL, comment_id TEXT NOT NULL,
            PRIMARY KEY(cycle_id, comment_id));
        CREATE TABLE IF NOT EXISTS pipeline_page_tokens (
            cycle_id TEXT NOT NULL, kind TEXT NOT NULL, target TEXT NOT NULL,
            token TEXT NOT NULL, PRIMARY KEY(cycle_id,kind,target,token));
        CREATE TABLE IF NOT EXISTS pipeline_attempts (
            id TEXT PRIMARY KEY, cycle_id TEXT, started_at TEXT NOT NULL,
            finished_at TEXT, state TEXT NOT NULL, api_requests INTEGER NOT NULL DEFAULT 0,
            new_ids INTEGER NOT NULL DEFAULT 0, summary_json TEXT);
        CREATE TABLE IF NOT EXISTS pipeline_budget (
            pacific_day TEXT PRIMARY KEY, requests INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS pipeline_sheet_rows (
            comment_id TEXT PRIMARY KEY, row_number INTEGER NOT NULL UNIQUE,
            payload_hash TEXT NOT NULL DEFAULT '');
    ''')
    db.commit()


def upsert(db, cycle, row, when):
    prior = db.execute('SELECT first_seen,payload_json FROM comments WHERE channel_id=? AND comment_id=?',
                       (row['channel_id'], row['id'])).fetchone()
    if prior:
        old = json.loads(prior['payload_json'])
        row['thread_query_statuses'] = sorted(set(old.get('thread_query_statuses', [])) |
                                               set(row.get('thread_query_statuses', [])))
    db.execute('''INSERT INTO comments VALUES(?,?,?,?,?) ON CONFLICT(channel_id,comment_id)
        DO UPDATE SET last_seen=excluded.last_seen,payload_json=excluded.payload_json''',
        (row['channel_id'], row['id'], prior['first_seen'] if prior else when,
         when, json.dumps(row, ensure_ascii=False)))
    db.execute('INSERT OR IGNORE INTO pipeline_seen VALUES(?,?)', (cycle, row['id']))
    return int(prior is None)


def apply_page(db, cycle, cursor, data, channel, when):
    """Caller owns one transaction: neither rows nor cursor advance alone."""
    new_ids = 0
    privacy_rules=exclusions(db)
    kind, target = cursor['kind'], cursor['target']
    for item in data['items']:
        if kind == 'threads':
            snippet = item.get('snippet', {})
            top, video = snippet.get('topLevelComment', {}), snippet.get('videoId')
            row = normalize(top, channel, video, target)
            if blocked(row,privacy_rules):
                continue
            # The published filter guarantees the top-level comment is public.
            # It says nothing about the visibility of its individual replies.
            if row['moderation_status'] is None and target == 'published':
                row['moderation_status'] = 'published'
                row['status_source'] = 'published_thread_filter'
            else:
                row['status_source'] = 'api_field' if row['moderation_status'] else 'not_returned'
            new_ids += upsert(db, cycle, row, when)
            inline = item.get('replies', {}).get('comments', [])
            if snippet.get('totalReplyCount', 0) or inline:
                db.execute('''INSERT OR IGNORE INTO pipeline_cursors
                    (cycle_id,kind,target,video_id) VALUES(?,'replies',?,?)''', (cycle, row['id'], video))
            for reply in inline:
                row = normalize(reply, channel, video, target)
                if blocked(row,privacy_rules):
                    continue
                row['status_source'] = 'api_field' if row['moderation_status'] else 'not_returned'
                new_ids += upsert(db, cycle, row, when)
        else:
            if item.get('snippet', {}).get('parentId') != target:
                raise ValidationError('unexpected_reply_parent')
            row = normalize(item, channel, cursor['video_id'])
            if blocked(row,privacy_rules):
                continue
            # A top-level published query must not mark every reply public.
            row['status_source'] = 'api_field' if row['moderation_status'] else 'not_returned'
            new_ids += upsert(db, cycle, row, when)
    token = data.get('nextPageToken')
    previous = db.execute('''SELECT 1 FROM pipeline_page_tokens
        WHERE cycle_id=? AND kind=? AND target=? AND token=?''', (cycle, kind, target, token)).fetchone() if token else None
    if token and (not isinstance(token, str) or token == cursor['page_token'] or previous):
        raise ValidationError('repeated_or_invalid_page_token')
    if cursor['page_token']:
        db.execute('INSERT OR IGNORE INTO pipeline_page_tokens VALUES(?,?,?,?)',
                   (cycle, kind, target, cursor['page_token']))
    db.execute('''UPDATE pipeline_cursors SET page_token=?,done=?,pages=pages+1,
        items=items+?,error=NULL WHERE cycle_id=? AND kind=? AND target=?''',
        (token, int(not token), len(data['items']), cycle, kind, target))
    return new_ids


def select_cycle(db, force=False, now=None):
    now = now or datetime.now(timezone.utc)
    active = db.execute("SELECT * FROM pipeline_cycles WHERE state='collecting' ORDER BY started_at DESC LIMIT 1").fetchone()
    if active:
        return active['id'], True
    last = db.execute('SELECT * FROM pipeline_cycles ORDER BY started_at DESC LIMIT 1').fetchone()
    if last and not force:
        local = now.astimezone(KST)
        started = datetime.fromisoformat(last['started_at']).astimezone(KST)
        if started.date() == local.date() or (local.hour, local.minute) < (8, 30):
            return last['id'], False
    cycle = now.strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8]
    db.execute("INSERT INTO pipeline_cycles VALUES(?,?,NULL,'collecting')", (cycle, now.isoformat()))
    db.executemany('INSERT INTO pipeline_cursors(cycle_id,kind,target) VALUES(?,\'threads\',?)',
                   [(cycle, status) for status in STATUSES])
    db.commit()
    return cycle, True


def summarize(db, cycle, attempt, state, requests, new_ids, error=None):
    cursors = [dict(row) for row in db.execute('SELECT * FROM pipeline_cursors WHERE cycle_id=?', (cycle,))]
    threads = {row['target']: {'pages': row['pages'], 'threads': row['items'],
        'complete': bool(row['done']), 'error': row['error']} for row in cursors if row['kind'] == 'threads'}
    replies = [row for row in cursors if row['kind'] == 'replies']
    stored = db.execute('SELECT COUNT(*) FROM comments WHERE channel_id=?', (EXPECTED_CHANNEL,)).fetchone()[0]
    observed = db.execute('SELECT COUNT(*) FROM pipeline_seen WHERE cycle_id=?', (cycle,)).fetchone()[0]
    return {'schema': 'youtube-pipeline-v1', 'attempt_id': attempt, 'cycle_id': cycle,
        'checked_at': instant(), 'channel_id': EXPECTED_CHANNEL, 'handle': HANDLE,
        'state': state, 'api_requests': requests, 'new_comment_ids': new_ids,
        'stored_comment_ids': stored, 'cycle_observed_ids': observed,
        'statuses': threads, 'reply_parents': len(replies),
        'reply_parents_complete': sum(row['done'] for row in replies), 'error': error,
        'full_cycle_complete': state in ('complete', 'idle') and all(row['done'] for row in cursors)}


def run(api, state, max_requests=2000, daily_budget=8000, seconds=2400,
        checkpoint=None, force_new_cycle=False, progress=None):
    state = private_dir(state)
    path = state / 'comments.sqlite3'
    # Reuse the earlier isolated validation DB; never access the Instagram DB.
    if path.is_symlink():
        raise ValidationError('invalid_database_path')
    with closing(sqlite3.connect(path)) as db:
        path.chmod(0o600)
        db.row_factory = sqlite3.Row
        initialize(db)
        cycle, collect_needed = select_cycle(db, force_new_cycle)
        db.execute("UPDATE pipeline_attempts SET state='interrupted',finished_at=? WHERE state='running'", (instant(),))
        attempt = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ-') + uuid.uuid4().hex[:8]
        db.execute('INSERT INTO pipeline_attempts(id,cycle_id,started_at,state) VALUES(?,?,?,?)',
                   (attempt, cycle, instant(), 'running'))
        db.execute('UPDATE pipeline_cursors SET error=NULL WHERE cycle_id=? AND done=0', (cycle,))
        db.commit()
        if not collect_needed:
            result = summarize(db, cycle, attempt, 'idle', 0, 0)
            db.execute('UPDATE pipeline_attempts SET finished_at=?,state=?,summary_json=? WHERE id=?',
                       (instant(), 'idle', json.dumps(result), attempt))
            db.commit()
            if checkpoint:
                checkpoint(db)
            write_json(state / 'pipeline-latest.json', result)
            return result
        new_ids, steps, last_status, error = 0, 0, -1, None
        phase, started, day = 'collecting', time.monotonic(), None
        api.max_requests = min(api.max_requests, max_requests)
        def budgeted_get(resource, **params):
            nonlocal day
            day = datetime.now(PACIFIC).date().isoformat()
            used = db.execute('SELECT requests FROM pipeline_budget WHERE pacific_day=?', (day,)).fetchone()
            if used and used[0] >= daily_budget:
                raise ValidationError('daily_request_budget_exhausted')
            before = api.requests
            try:
                return api.get(resource, **params)
            finally:
                consumed = api.requests - before
                if consumed:
                    db.execute('''INSERT INTO pipeline_budget VALUES(?,?) ON CONFLICT(pacific_day)
                        DO UPDATE SET requests=requests+excluded.requests''', (day, consumed))
                    db.commit()
        class BudgetAPI:
            get = staticmethod(budgeted_get)
        try:
            channel = verify_channel(BudgetAPI(), HANDLE)
            if channel['id'] != EXPECTED_CHANNEL:
                raise ValidationError('unexpected_target_channel')
            while time.monotonic() - started < seconds:
                pending = list(db.execute('''SELECT * FROM pipeline_cursors WHERE cycle_id=? AND done=0
                    AND error IS NULL ORDER BY kind,target''', (cycle,)))
                if not pending:
                    remaining = db.execute('SELECT COUNT(*) FROM pipeline_cursors WHERE cycle_id=? AND done=0', (cycle,)).fetchone()[0]
                    phase = 'error' if remaining else 'complete'
                    break
                threads = {row['target']: row for row in pending if row['kind'] == 'threads'}
                replies = [row for row in pending if row['kind'] == 'replies']
                cursor = None
                # Alternate a thread page and a reply page when both exist.
                if threads and (steps % 2 == 0 or not replies):
                    for offset in range(1, len(STATUSES) + 1):
                        index = (last_status + offset) % len(STATUSES)
                        if STATUSES[index] in threads:
                            cursor, last_status = threads[STATUSES[index]], index
                            break
                else:
                    cursor = replies[0] if replies else next(iter(threads.values()))
                if cursor['kind'] == 'threads':
                    resource = 'commentThreads'
                    params = {'part': 'snippet,replies', 'allThreadsRelatedToChannelId': EXPECTED_CHANNEL,
                        'moderationStatus': cursor['target'], 'maxResults': 100, 'textFormat': 'plainText', 'order': 'time'}
                else:
                    resource = 'comments'
                    params = {'part': 'snippet', 'parentId': cursor['target'], 'maxResults': 100, 'textFormat': 'plainText'}
                if cursor['page_token']:
                    params['pageToken'] = cursor['page_token']
                try:
                    data = budgeted_get(resource, **params)
                    # Budget accounting commits before the page transaction. A
                    # failed transaction replays this page without duplicate IDs.
                    with db:
                        new_ids += apply_page(db, cycle, cursor, data, EXPECTED_CHANNEL, instant())
                        db.execute('UPDATE pipeline_attempts SET api_requests=?,new_ids=? WHERE id=?',
                                   (api.requests,new_ids,attempt))
                except ValidationError as failure:
                    if failure.reason in ('request_budget_exhausted', 'daily_request_budget_exhausted'):
                        phase, error = 'partial', failure.reason
                        break
                    if failure.reason == 'invalidPageToken' and cursor['restarts'] < 3:
                        db.execute('''UPDATE pipeline_cursors SET page_token=NULL,restarts=restarts+1
                            WHERE cycle_id=? AND kind=? AND target=?''', (cycle, cursor['kind'], cursor['target']))
                        db.execute('DELETE FROM pipeline_page_tokens WHERE cycle_id=? AND kind=? AND target=?',
                                   (cycle, cursor['kind'], cursor['target']))
                        db.commit()
                    else:
                        db.execute('UPDATE pipeline_cursors SET error=? WHERE cycle_id=? AND kind=? AND target=?',
                                   (failure.reason, cycle, cursor['kind'], cursor['target']))
                        db.commit()
                        error = failure.reason
                        if failure.reason in ('quotaExceeded','dailyLimitExceeded','authError',
                                'network_or_token_refresh_failed','oauth_authorization_revoked','accessNotConfigured','rateLimitExceeded'):
                            # Channel-wide failures will affect the other pages
                            # too. Retain the cursor and retry next execution.
                            phase = 'error'
                            break
                steps += 1
                if checkpoint and steps % 20 == 0:
                    checkpoint(db)
                if progress and steps % 50 == 0:
                    progress(summarize(db, cycle, attempt, 'collecting', api.requests, new_ids, error))
            else:
                phase, error = 'partial', 'execution_time_budget_exhausted'
        except ValidationError as failure:
            phase = 'partial' if failure.reason in ('request_budget_exhausted','daily_request_budget_exhausted') else 'error'
            error = failure.reason
        if phase == 'complete':
            db.execute("UPDATE pipeline_cycles SET state='complete',finished_at=? WHERE id=?", (instant(), cycle))
        result = summarize(db, cycle, attempt, phase, api.requests, new_ids, error)
        db.execute('UPDATE pipeline_attempts SET finished_at=?,state=?,api_requests=?,new_ids=?,summary_json=? WHERE id=?',
                   (instant(), phase, api.requests, new_ids, json.dumps(result, ensure_ascii=False), attempt))
        db.commit()
        if checkpoint:
            checkpoint(db)
        write_json(state / 'pipeline-latest.json', result)
        return result
