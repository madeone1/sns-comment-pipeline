"""Persist fetched comments and pagination before classification or cursor advance.

Recent media have a fast lane; an independent, resumable catalog walk also visits
older posts and their reply pages. A failed run never advances either time floor.
"""

import datetime as dt
import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .api import GraphError, PilotError, RequestBudgetExceeded, next_cursor, page_data
from .cloud_state import CloudError


UTC = dt.timezone.utc
FIELDS = 'id,text,timestamp,username,from,hidden'
COMMENT_FIELDS = FIELDS + ',replies.limit(50){' + FIELDS + '}'


def unavailable(error):
    # Meta does not distinguish deletion from object-specific access loss.
    return isinstance(error, GraphError) and error.code == 100 and error.subcode == 33


class ReadBudget:
    """Bound one account's polling without consuming the budget reserved for hides."""
    def __init__(self, client, limit):
        self.client, self.limit, self.used = client, limit, 0
        self.lock = threading.Lock()

    @property
    def remaining(self):
        return max(0, self.limit - self.used)

    def get(self, path, **params):
        with self.lock:
            if self.used >= self.limit:
                raise RequestBudgetExceeded('계정별 댓글 조회 상한에 도달했습니다. 다음 실행에서 재개합니다.')
            self.used += 1
        return self.client.get(path, **params)


def transient(error):
    return isinstance(error, CloudError) and error.status in {0, 429, 500, 502, 503, 504}


def read_retry(operation):
    for attempt in range(3):
        try:
            return operation()
        except CloudError as error:
            if not transient(error) or attempt == 2:
                raise
            time.sleep(0.5 * (2 ** attempt))


def state_name(account_id):
    return 'moderation/polling/' + account_id + '.json'


def instant(value):
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise PilotError('댓글 조회 체크포인트의 시간대를 확인해주세요.')
    return parsed.astimezone(UTC)


class Polling:
    def __init__(self, state, account_id, owner, settings, now, convert, policy=None):
        self.state, self.account_id, self.owner = state, account_id, owner
        self.settings, self.now, self.convert = settings, now.astimezone(UTC), convert
        self.name = state_name(account_id)
        self.data, self.generation = read_retry(lambda: state.json_get(self.name, {}))
        if not isinstance(self.data, dict):
            raise PilotError('댓글 조회 체크포인트 형식을 확인해주세요.')
        if self.data.get('account_id', account_id) != account_id:
            raise PilotError('댓글 조회 체크포인트의 계정이 다릅니다.')
        self.data.setdefault('queue', {})
        self.data.setdefault('receipts', {})
        self.data.setdefault('acknowledged', {})
        self.data.setdefault('receipt_failures', {})
        self.data.setdefault('media_links', {})
        self.data.setdefault('unavailable', {})
        self.error = None
        self.pages = {'recent': 0, 'catalog': 0}
        self.pause_reason = None
        from .moderation import load_rulebook, policy_sha
        self.policy = policy or policy_sha(load_rulebook(settings.get('policy_version')))

    def active_exceptions(self):
        return {key: row for key, row in self.data['unavailable'].items() if row.get('status') == 'unavailable'}

    def defer(self, key, error, **context):
        prior = self.data['unavailable'].get(key, {})
        attempts = int(prior.get('attempts') or 0) + 1
        # Never call an inaccessible object deleted. Recheck indefinitely, at
        # 1h, 6h, then daily, without holding the normal queues open.
        hours = 1 if attempts == 1 else 6 if attempts == 2 else 24
        self.data['unavailable'][key] = {
            **prior, **context, 'status': 'unavailable', 'attempts': attempts,
            'first_seen_at': prior.get('first_seen_at') or self.now.isoformat(),
            'last_checked_at': self.now.isoformat(),
            'next_retry_at': (self.now + dt.timedelta(hours=hours)).isoformat(),
            'error': str(error), 'code': error.code, 'subcode': error.subcode,
        }

    def resolve(self, key):
        self.data['unavailable'][key].update(status='resolved', resolved_at=self.now.isoformat())

    @staticmethod
    def task_key(lane, task):
        marker = task['path'] + ':' + str(task.get('after') or '')
        return lane + ':' + hashlib.sha256(marker.encode()).hexdigest()

    @property
    def blocked(self):
        error = self.error
        return (isinstance(error, RequestBudgetExceeded) or
                isinstance(error, GraphError) and (error.status == 429 or error.code in {4, 17, 32, 613}))

    def fingerprint(self, comment):
        parts = (self.policy, str(comment.get('text') or ''),
                 str(comment.get('written_at') or ''), str(comment.get('parent_id') or ''))
        return hashlib.sha256('\0'.join(parts).encode()).hexdigest()

    def save(self):
        self.data['account_id'] = self.account_id
        self.data['updated_at'] = self.now.isoformat()
        self.data['last_error'] = str(self.error) if self.error else None
        self.data['progress'] = self.coverage()
        for attempt in range(3):
            try:
                self.generation = self.state.json_put(self.name, self.data, self.generation)
                return
            except CloudError as error:
                if not transient(error):
                    raise
                # A timed-out write can already have committed. Read it back
                # before retrying so its successful generation is never lost.
                current, generation = read_retry(lambda: self.state.json_get(self.name))
                if current == self.data:
                    self.generation = generation
                    return
                if generation != self.generation:
                    raise CloudError('댓글 체크포인트가 다른 경로에서 변경됐습니다.', 412) from None
                if attempt == 2:
                    raise
                time.sleep(0.5 * (2 ** attempt))

    def enqueue(self, item, media_id, parent_id=None, link=None, since=None):
        comment = self.convert(item, self.account_id, self.owner, media_id,
                               parent_id=parent_id, permalink=link)
        identifier = comment.get('id')
        if not isinstance(identifier, str) or not identifier.isdigit():
            raise PilotError('조회된 댓글 ID 형식이 올바르지 않습니다.')
        written = comment.get('written_at')
        if since and written and instant(written) < instant(since):
            return
        if self.data['acknowledged'].get(identifier) == self.fingerprint(comment):
            return
        # An unknown timestamp stays queued for the existing verified lookup.
        self.data['queue'][identifier] = comment

    def webhooks(self, client, limit=24, deadline=None):
        prefix = 'webhook/pending/' + self.account_id + '/'
        if hasattr(self.state, 'list_all'):
            objects = read_retry(lambda: self.state.list_all(prefix))
        else:
            objects, more = read_retry(lambda: self.state.list_objects(prefix, 1000))
            if more:
                raise PilotError('웹훅 수신함을 끝까지 조회하지 못했습니다.')
        used = 0
        active = self.active_exceptions()
        remaining = {obj['name'] for obj in objects
                     if self.data['receipts'].get(obj['name']) != obj.get('generation') and
                     active.get('webhook:' + obj['name'], {}).get('generation') != obj.get('generation')}
        # Deleted/unreadable IDs remain visible as failures, but cannot keep
        # newer webhook receipts behind the same failing first batch forever.
        objects.sort(key=lambda obj: (self.data['receipt_failures'].get(obj['name'], 0), obj['name']))
        for obj in objects:
            if used >= limit or (deadline and time.monotonic() >= deadline):
                break
            name = obj['name']
            if self.data['receipts'].get(name) == obj.get('generation'):
                continue
            key = 'webhook:' + name
            deferred = self.active_exceptions().get(key)
            if deferred and deferred.get('generation') == obj.get('generation'):
                remaining.discard(name)
                continue
            event, generation = read_retry(lambda: self.state.json_get(name))
            if not isinstance(event, dict) or event.get('account_id') != self.account_id:
                continue
            identifier = event.get('comment_id')
            if not isinstance(identifier, str) or not identifier.isdigit():
                raise PilotError('웹훅 댓글 ID 형식을 확인해주세요.')
            used += 1
            try:
                self.read_receipt(client, event)
                # Preserve original webhook evidence. Its generation is ACKed
                # in the same atomic object as the fetched comment, not before.
                self.data['receipts'][name] = obj.get('generation', generation)
                remaining.discard(name)
                if key in self.data['unavailable']:
                    self.resolve(key)
                self.save()
            except (GraphError, RequestBudgetExceeded) as error:
                failures = self.data['receipt_failures']
                failures[name] = failures.get(name, 0) + 1
                if unavailable(error):
                    self.defer(key, error, source='webhook', path=identifier,
                               receipt=name, generation=obj.get('generation', generation), event=event)
                    remaining.discard(name)
                else:
                    self.error = error
                self.save()
                if self.blocked:
                    break
        self.data['webhook_pending'] = len(remaining)
        self.save()

    def read_receipt(self, client, event):
        item = client.get(event['comment_id'], fields=FIELDS)
        media_id = event.get('media_id')
        link = self.data['media_links'].get(media_id)
        if not link and isinstance(media_id, str) and media_id.isdigit():
            try:
                link = client.get(media_id, fields='permalink').get('permalink')
            except GraphError as error:
                if not unavailable(error):
                    raise
                # A missing link must not discard an otherwise readable comment.
            if link:
                self.data['media_links'][media_id] = link
        self.enqueue(item, media_id, parent_id=event.get('parent_id'), link=link)

    def retry_unavailable(self, client, limit=4, deadline=None):
        due = sorted(self.active_exceptions().items(), key=lambda pair: pair[1]['next_retry_at'])
        for key, row in due[:limit]:
            if instant(row['next_retry_at']) > self.now or (deadline and time.monotonic() >= deadline):
                break
            try:
                if row['source'] == 'webhook':
                    event, generation = read_retry(lambda: self.state.json_get(row['receipt']))
                    if not isinstance(event, dict) or event.get('account_id') != self.account_id:
                        raise PilotError('재확인할 웹훅의 계정 정보를 확인해주세요.')
                    self.read_receipt(client, event)
                    self.data['receipts'][row['receipt']] = generation
                else:
                    # Reintroduce the original task and its oldest time floor.
                    # The normal scanner follows every recovered next/reply page.
                    task = {**row['task'], 'since': row['since'], 'trail': []}
                    self.start_lane(row['lane'])
                    payload = client.get(task['path'], **self.params(task, row['lane']))
                    self.apply_page(row['lane'], task, payload, recovering=True)
                self.resolve(key)
            except PilotError as error:
                if unavailable(error):
                    self.defer(key, error)
                else:
                    self.error = error
            self.save()
            if self.blocked:
                break

    def peek(self, client, deadline=None):
        """Refresh newest pages even while an older paginated walk is unfinished."""
        interval = max(30, int(self.settings.get('interval_minutes') or 30))
        age = max(2 * interval, int(self.settings.get('max_age_minutes') or 60))
        since = (self.now - dt.timedelta(minutes=age)).isoformat()
        try:
            media = page_data(client.get(self.account_id + '/media', fields='id,permalink,timestamp',
                                        limit=min(50, int(self.settings.get('media_limit') or 8))))
            for item in media:
                if deadline and time.monotonic() >= deadline:
                    break
                media_id = item['id']
                task = {'kind': 'comments', 'path': media_id + '/comments', 'media_id': media_id,
                        'link': item.get('permalink')}
                key = self.task_key('recent', task)
                if key in self.active_exceptions():
                    continue
                try:
                    payload = client.get(task['path'], fields=COMMENT_FIELDS, limit=50)
                except GraphError as error:
                    if not unavailable(error):
                        raise
                    self.defer(key, error, source='scan', lane='recent', path=task['path'],
                               task=task, since=since)
                    self.save()
                    continue
                for row in page_data(payload):
                    self.enqueue(row, media_id, link=item.get('permalink'), since=since)
                    if isinstance(row.get('replies'), dict):
                        for reply in page_data(row['replies']):
                            self.enqueue(reply, media_id, row['id'], item.get('permalink'), since)
                self.save()
        except PilotError as error:
            self.error = error
            self.save()

    def params(self, task, lane):
        params = {'fields': 'id,permalink,timestamp' if task['kind'] == 'media' else
                  COMMENT_FIELDS if task['kind'] == 'comments' else FIELDS,
                  'limit': min(50, max(1, int(self.settings.get('media_limit') or 8)))
                  if task['kind'] == 'media' and lane == 'recent' else 50}
        if task.get('after'):
            params['after'] = task['after']
        return params

    def apply_page(self, lane, task, payload, recovering=False):
        checkpoint = self.data[lane]
        rows, cursor = page_data(payload), next_cursor(payload)
        since = task.get('since') or checkpoint['since']
        followups = []
        if task['kind'] == 'media':
            for media in rows:
                media_id = media.get('id')
                if not isinstance(media_id, str) or not media_id.isdigit():
                    raise PilotError('게시물 ID 형식을 확인해주세요.')
                link = media.get('permalink') or self.data['media_links'].get(media_id)
                if link:
                    self.data['media_links'][media_id] = link
                followups.append({'kind': 'comments', 'path': media_id + '/comments',
                                  'media_id': media_id, 'link': link})
        else:
            for item in rows:
                self.enqueue(item, task['media_id'], task.get('parent_id'), task.get('link'), since)
                if task['kind'] == 'comments':
                    replies = item.get('replies')
                    if isinstance(replies, dict):
                        for reply in page_data(replies):
                            self.enqueue(reply, task['media_id'], item['id'], task.get('link'), since)
                        after = next_cursor(replies)
                        if not after:
                            continue
                    else:
                        after = None
                    followups.append({'kind': 'replies', 'path': item['id'] + '/replies',
                                      'after': after, 'media_id': task['media_id'],
                                      'parent_id': item['id'], 'link': task.get('link'), 'trail': []})
        marker = task['path'] + ':' + str(task.get('after') or '')
        if cursor and (task['kind'] != 'media' or lane == 'catalog'):
            next_marker = task['path'] + ':' + cursor
            trail = task.get('trail')
            if trail is None:
                # Existing cursors predate per-chain trails. Fresh first pages
                # and recovered objects start independent pagination chains.
                trail = [v for v in checkpoint['visited'] if v.startswith(task['path'] + ':')] if task.get('after') else []
            if next_marker == marker or next_marker in trail:
                raise PilotError('댓글 조회 페이지 커서가 반복됩니다. 완료로 처리하지 않습니다.')
            followups.append({**task, 'after': cursor, 'trail': trail + [marker]})
        # A recovered object can have comments older than the current scan's
        # floor. Its entire pagination retains the original floor.
        if task.get('since'):
            followups = [{**t, 'since': since} for t in followups]
        if not recovering:
            checkpoint['tasks'].remove(task)
        checkpoint['tasks'].extend(followups)
        checkpoint['visited'].append(marker)
        self.pages[lane] += 1

    def finish_lane(self, lane):
        checkpoint = self.data[lane]
        if not checkpoint['tasks']:
            checkpoint['completed_through'] = checkpoint['through']
            checkpoint['completion_status'] = ('complete_with_exceptions' if any(
                row.get('lane') == lane for row in self.active_exceptions().values()) else 'complete')

    def start_lane(self, lane):
        checkpoint = self.data.get(lane)
        if not checkpoint or not checkpoint.get('tasks'):
            interval = max(30, int(self.settings.get('interval_minutes') or 30))
            age = max(interval * 2, int(self.settings.get('max_age_minutes') or 60))
            overlap = max(10, int(self.settings.get('overlap_minutes') or 10))
            floor = self.now - dt.timedelta(minutes=age)
            completed = (checkpoint or {}).get('completed_through')
            if completed:
                floor = min(floor, instant(completed) - dt.timedelta(minutes=overlap))
            checkpoint = {'since': floor.isoformat(), 'through': self.now.isoformat(),
                          'completed_through': completed, 'visited': [],
                          'tasks': [{'kind': 'media', 'path': self.account_id + '/media'}]}
            self.data[lane] = checkpoint
            self.save()
        return checkpoint

    def scan(self, client, lane, pages, deadline=None):
        checkpoint = self.start_lane(lane)
        used = 0
        workers = min(8, max(1, int(self.settings.get('poll_workers') or 4)))
        while used < pages:
            if not checkpoint['tasks'] or (deadline and time.monotonic() >= deadline):
                break
            # A previously deferred object stays in the exception queue, even
            # when a fresh catalog walk discovers it again before its retry.
            active = self.active_exceptions()
            for task in list(checkpoint['tasks']):
                key = self.task_key(lane, task)
                if key in active:
                    row = active[key]
                    row['since'] = min(row['since'], task.get('since') or checkpoint['since'])
                    checkpoint['tasks'].remove(task)
            remaining = getattr(client, 'remaining', pages)
            batch = checkpoint['tasks'][:min(workers, pages - used, remaining)]
            if not batch:
                if remaining == 0:
                    self.pause_reason = 'account_request_budget'
                break
            def fetch(task):
                try:
                    return client.get(task['path'], **self.params(task, lane))
                except PilotError as error:
                    return error
            # Workers only read. The main thread alone mutates state, and all
            # payloads and cursor advances are saved in one conditional write.
            with ThreadPoolExecutor(max_workers=len(batch)) as executor:
                payloads = list(executor.map(fetch, batch))
            used += len(batch)
            stop = False
            for task, payload in zip(batch, payloads):
                try:
                    if isinstance(payload, PilotError):
                        raise payload
                    self.apply_page(lane, task, payload)
                except PilotError as error:
                    if isinstance(error, GraphError) and error.code == 100 and task.get('after'):
                        task.pop('after', None)
                        task['trail'] = []
                        checkpoint['visited'] = [v for v in checkpoint['visited']
                                                 if not v.startswith(task['path'] + ':')]
                        self.error, stop = error, True
                    elif unavailable(error) and task['kind'] != 'media':
                        key = self.task_key(lane, task)
                        self.defer(key, error, source='scan', lane=lane, path=task['path'],
                                   task=dict(task), since=task.get('since') or checkpoint['since'])
                        checkpoint['tasks'].remove(task)
                    else:
                        self.error, stop = error, True
            self.finish_lane(lane)
            self.save()
            if stop:
                break
        self.finish_lane(lane)
        self.save()

    def pending(self, limit=40):
        values = sorted(self.data['queue'].values(),
                        key=lambda row: (str(row.get('written_at') or ''), row['id']))
        return values[:limit]

    def acknowledge(self, comments):
        # moderate() returns only after all decisions in this batch were saved.
        # On a crash/exception no ACK occurs; existing decision IDs make replay safe.
        for comment in comments:
            self.data['queue'].pop(comment['id'], None)
            self.data['acknowledged'][comment['id']] = self.fingerprint(comment)
        self.save()

    def coverage(self):
        return {'queued': len(self.data['queue']),
                'webhook_pending': self.data.get('webhook_pending', 0),
                'unavailable_count': len(self.active_exceptions()),
                'unavailable_objects': len({row['path'] for row in self.active_exceptions().values()}),
                'recent_complete': bool(self.data.get('recent', {}).get('completed_through')) and
                                   not self.data.get('recent', {}).get('tasks'),
                'catalog_complete': bool(self.data.get('catalog', {}).get('completed_through')) and
                                    not self.data.get('catalog', {}).get('tasks'),
                'recent_pending_pages': len(self.data.get('recent', {}).get('tasks', [])),
                'catalog_pending_pages': len(self.data.get('catalog', {}).get('tasks', [])),
                'recent_pages_read': self.pages['recent'],
                'catalog_pages_read': self.pages['catalog'],
                'pause_reason': self.pause_reason,
                'recent_through': self.data.get('recent', {}).get('completed_through'),
                'catalog_through': self.data.get('catalog', {}).get('completed_through')}
