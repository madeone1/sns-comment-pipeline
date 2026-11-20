"""Bounded, resumable webhook batches inside the existing daily writer lease."""

import datetime as dt
import json

from .account import collect_continuously, previous_month_start
from .api import PilotError
from .cloud_state import CloudError, CloudState
from .collector import Collector, post_key
from .reporting import KST, counts, utc
from .storage import now, timestamp
from .webhook import event_path, ID, RECEIPT


def daily_evidence(db, account_id, day, bounded=False):
    end = dt.datetime.combine(dt.date.fromisoformat(day), dt.time(8), tzinfo=KST)
    start = end-dt.timedelta(days=1)
    media_since = previous_month_start(end.isoformat()) if bounded else None
    extra = " AND julianday(replace(m.written_at,'+0000','+00:00'))>=julianday(?)" if media_since else ''
    params = (account_id, utc(start.isoformat()), utc(end.isoformat()), utc(end.isoformat()))
    if media_since:
        params += (media_since,)
    rows = [dict(r) for r in db.execute('''SELECT c.id,c.media_id,c.parent_id,c.author_id,c.username,c.is_owner,c.text,
        c.written_at,c.payload_hash,m.permalink,m.caption,m.written_at AS media_written_at
        FROM comments c JOIN media m ON m.account_id=c.account_id AND m.id=c.media_id
        WHERE c.account_id=? AND c.written_at>=? AND c.written_at<? AND m.written_at<?
        '''+extra+' ORDER BY c.written_at,c.id', params) ]
    return {'account_id': account_id, 'period': {'key': 'daily', 'target_day': day, 'start': start.isoformat(), 'end': end.isoformat()},
        'comments': rows, 'counts': counts(rows), 'basis': 'api_verified_original_written_at', 'updated_at': now()}


def target_day(written_at):
    written = dt.datetime.fromisoformat(timestamp(written_at)).astimezone(KST)
    # The interval is [previous 08:00, today's 08:00), including the boundary.
    return (written.date()+dt.timedelta(days=int(written.hour >= 8))).isoformat()


def _schema(store):
    store.db.execute('''CREATE TABLE IF NOT EXISTS webhook_batches (
        scope_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, events_json TEXT NOT NULL,
        status TEXT NOT NULL, created_at TEXT NOT NULL)''')
    store.db.commit()


def _pending(root, account_id):
    objects, more = root.list_objects('webhook/pending/'+account_id+'/', 100)
    events, media = [], set()
    for obj in objects:
        event, generation = root.json_get(obj['name'])
        if event is None:
            continue
        valid = event.get('version') == 1 and event.get('account_id') == account_id and event.get('field') == 'comments'
        valid = valid and all(isinstance(event.get(k), str) and ID.fullmatch(event[k]) for k in ('account_id', 'media_id', 'comment_id'))
        valid = valid and isinstance(event.get('receipt'), str) and RECEIPT.fullmatch(event['receipt'])
        if not valid or obj['name'] != event_path(event):
            raise PilotError('웹훅 수신함의 계정·식별자 대조에 실패했습니다.')
        timestamp(event.get('received_at'))
        if event['media_id'] not in media and len(media) >= 25:
            more = True
            continue
        media.add(event['media_id'])
        events.append({'event': event, 'name': obj['name'], 'generation': generation})
    return events, more


def process(config, state, store, client, start, end, day, checkpoint, root=None):
    """Checkpoint the exact scope before API traversal and before queue ACK.

    A crash resumes the same scope, even on a later reporting day. Newly arrived
    events stay pending for the next batch. No completed scope is reused for a
    new delivery. Original timestamps determine any late-arrival projection.
    """
    root = root or CloudState(state.bucket, state.transport)
    account_id, owner = config['account_id'], config['expected_username'].lstrip('@')
    bounded = config.get('cloud',{}).get('post_history')=='previous_month'
    floor = previous_month_start(end) if bounded else None
    _schema(store)
    batch = store.db.execute("SELECT * FROM webhook_batches WHERE account_id=? AND status!='done' ORDER BY created_at LIMIT 1", (account_id,)).fetchone()
    more, events = False, []
    if batch:
        scope = store.scope(batch['scope_id'])
        events = json.loads(batch['events_json'])
    else:
        events, more = _pending(root, account_id)
        if events:
            # No catalog walk: validate only the explicitly signaled media.
            client.max_requests = min(5000, client.request_count+1000)
            Collector(client, store, owner).account(account_id)
            media = []
            for media_id in sorted({e['event']['media_id'] for e in events}):
                item = client.get(media_id, fields='id,permalink,caption,timestamp,owner')
                if item.get('id') != media_id or not isinstance(item.get('owner'), dict) or item['owner'].get('id') != account_id:
                    raise PilotError('웹훅 게시물의 실제 소유 계정을 확인하지 못했습니다.')
                post_key(item.get('permalink'))
                timestamp(item.get('timestamp'))
                if not floor or timestamp(item['timestamp'])>=floor:
                    media.append(item)
                else:
                    # Keep the notification and metadata, without traversing
                    # comments outside the user-selected publication period.
                    store.save_media(account_id,item)
            scope = store.collection_scope(account_id, owner, start, end, restart=True, media_policy='tracked_activity')
            store.save_catalog_page(scope['id'], account_id, media, None)
            for event in events:
                value = event['event']
                store.track_media(account_id, value['media_id'], value['received_at'], 'verified_webhook')
            with store.db:
                store.db.execute('INSERT INTO webhook_batches VALUES(?,?,?,?,?)',
                    (scope['id'], account_id, json.dumps(events), 'pending', now()))
            # Scope identity and exact queue snapshot are durable together.
            checkpoint()
            batch = store.db.execute('SELECT * FROM webhook_batches WHERE scope_id=?', (scope['id'],)).fetchone()
    result = {'status': 'complete', 'events': len(events), 'more_pending': more, 'unresolved_comments': 0,
        'basis': 'verified_media_edges', 'updated_at': now()}
    days = {day}
    if events:
        excluded = set()
        if floor:
            excluded = {r[0] for r in store.db.execute('''SELECT sm.media_id FROM scope_media sm JOIN media m
                ON m.account_id=sm.account_id AND m.id=sm.media_id WHERE sm.scope_id=? AND m.written_at<?''', (scope['id'],floor))}
            # A batch started before the scope change may still contain old
            # posts. End only that work; raw comments and observations remain.
            with store.db:
                for media_id in excluded:
                    store.db.execute('UPDATE scope_edges SET complete=1 WHERE scope_id=? AND media_id=?',(scope['id'],media_id))
                    store.finish_scope_media(scope['id'],media_id,'complete')
        if batch['status'] == 'pending':
            budget = min(1000, 5000-client.request_count)
            if budget < 2:
                raise PilotError('웹훅 원문 확인의 요청 예산이 부족합니다. 알림을 다음 실행에 보존합니다.')
            collection = collect_continuously(client, store, owner, scope['window_start'], scope['window_end'],
                account_id=account_id, resume_scope_id=scope['id'], media_policy='tracked_activity', workers=3,
                max_requests=min(250, budget), max_total_requests=budget, progress=lambda value: checkpoint())
            checkpoint()
            result['collection'] = collection
            if collection['status'] != 'complete':
                result['status'] = 'pending'
                state.json_put(f'runs/{day}/webhook-daily.json', daily_evidence(store.db, account_id, day, bounded))
                state.json_put(f'runs/{day}/webhook.json', result)
                return result
            with store.db:
                store.db.execute("UPDATE webhook_batches SET status='collected' WHERE scope_id=?", (scope['id'],))
            checkpoint()
        for value in events:
            event = value['event']
            post = store.db.execute('SELECT written_at FROM media WHERE account_id=? AND id=?',(account_id,event['media_id'])).fetchone()
            if floor and post and timestamp(post['written_at'])<floor:
                value['outcome']='outside_post_window'
                continue
            row = store.db.execute('SELECT written_at FROM comments WHERE account_id=? AND media_id=? AND id=?',
                (account_id, event['media_id'], event['comment_id'])).fetchone()
            value['outcome'] = 'observed' if row else 'not_observed_after_complete_scan'
            if row:
                days.add(target_day(row['written_at']))
            else:
                result['unresolved_comments'] += 1
    # API evidence is projected before ACK. Primary report files stay intact.
    for target in sorted(days):
        state.json_put(f'runs/{target}/webhook-daily.json', daily_evidence(store.db, account_id, target, bounded))
    for value in events:
        event = value['event']
        try:
            root.json_put(event_path(event, 'done'), {**event, 'processed_at': now(), 'scope_id': scope['id'],
                'outcome': value['outcome']}, 0)
        except CloudError as error:
            if error.status != 412:
                raise
        try:
            root.delete(value['name'], value['generation'])
        except CloudError as error:
            if error.status not in (404, 412):
                raise
    if events:
        with store.db:
            store.db.execute("UPDATE webhook_batches SET status='done' WHERE scope_id=?", (scope['id'],))
        checkpoint()
    # Include notifications received during traversal, not just the first page.
    remaining, _ = root.list_objects('webhook/pending/'+account_id+'/', 1)
    result['more_pending'] = bool(remaining)
    result['status'] = 'pending' if remaining or result['unresolved_comments'] else 'complete'
    state.json_put(f'runs/{day}/webhook.json', result)
    return result
