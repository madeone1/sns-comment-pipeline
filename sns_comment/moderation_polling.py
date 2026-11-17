"""Persist fetched comments and pagination before classification or cursor advance.

Recent media have a fast lane; an independent, resumable catalog walk also visits
older posts and their reply pages. A failed run never advances either time floor.
"""

import datetime as dt
import hashlib
import time

from .api import GraphError, PilotError, RequestBudgetExceeded, next_cursor, page_data
from .cloud_state import CloudError


UTC = dt.timezone.utc
FIELDS = 'id,text,timestamp,username,from,hidden'
COMMENT_FIELDS = FIELDS + ',replies.limit(50){' + FIELDS + '}'


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
    def __init__(self, state, account_id, owner, settings, now, convert):
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
        self.error = None
        from .moderation import load_rulebook, policy_sha
        self.policy = policy_sha(load_rulebook(settings.get('policy_version')))

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
        # Deleted/unreadable IDs remain visible as failures, but cannot keep
        # newer webhook receipts behind the same failing first batch forever.
        objects.sort(key=lambda obj: (self.data['receipt_failures'].get(obj['name'], 0), obj['name']))
        for obj in objects:
            if used >= limit or (deadline and time.monotonic() >= deadline):
                break
            name = obj['name']
            if self.data['receipts'].get(name) == obj.get('generation'):
                continue
            event, generation = read_retry(lambda: self.state.json_get(name))
            if not isinstance(event, dict) or event.get('account_id') != self.account_id:
                continue
            identifier = event.get('comment_id')
            if not isinstance(identifier, str) or not identifier.isdigit():
                raise PilotError('웹훅 댓글 ID 형식을 확인해주세요.')
            used += 1
            try:
                item = client.get(identifier, fields=FIELDS)
                media_id = event.get('media_id')
                link = self.data['media_links'].get(media_id)
                if not link and isinstance(media_id, str) and media_id.isdigit():
                    media = client.get(media_id, fields='permalink')
                    link = media.get('permalink')
                    if link:
                        self.data['media_links'][media_id] = link
                self.enqueue(item, media_id, parent_id=event.get('parent_id'), link=link)
                # Preserve original webhook evidence. Its generation is ACKed
                # in the same atomic object as the fetched comment, not before.
                self.data['receipts'][name] = obj.get('generation', generation)
                self.save()
            except (GraphError, RequestBudgetExceeded) as error:
                self.error = error
                failures = self.data['receipt_failures']
                failures[name] = failures.get(name, 0) + 1
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
                payload = client.get(media_id + '/comments', fields=COMMENT_FIELDS, limit=50)
                for row in page_data(payload):
                    self.enqueue(row, media_id, link=item.get('permalink'), since=since)
                    if isinstance(row.get('replies'), dict):
                        for reply in page_data(row['replies']):
                            self.enqueue(reply, media_id, row['id'], item.get('permalink'), since)
                self.save()
        except PilotError as error:
            self.error = error
            self.save()

    def scan(self, client, lane, pages, deadline=None):
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
        for _ in range(pages):
            if not checkpoint['tasks'] or (deadline and time.monotonic() >= deadline):
                break
            task = checkpoint['tasks'][0]
            params = {'fields': 'id,permalink,timestamp' if task['kind'] == 'media' else
                      COMMENT_FIELDS if task['kind'] == 'comments' else FIELDS,
                      'limit': min(50, max(1, int(self.settings.get('media_limit') or 8)))
                      if task['kind'] == 'media' and lane == 'recent' else 50}
            if task.get('after'):
                params['after'] = task['after']
            try:
                payload = client.get(task['path'], **params)
                rows = page_data(payload)
                cursor = next_cursor(payload)
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
                        self.enqueue(item, task['media_id'], task.get('parent_id'),
                                     task.get('link'), checkpoint['since'])
                        if task['kind'] == 'comments':
                            replies = item.get('replies')
                            if isinstance(replies, dict):
                                for reply in page_data(replies):
                                    self.enqueue(reply, task['media_id'], item['id'],
                                                 task.get('link'), checkpoint['since'])
                                after = next_cursor(replies)
                                if not after:
                                    continue
                            else:
                                after = None
                            followups.append({'kind': 'replies', 'path': item['id'] + '/replies',
                                              'after': after, 'media_id': task['media_id'],
                                              'parent_id': item['id'], 'link': task.get('link')})
                marker = task['path'] + ':' + str(task.get('after') or '')
                if cursor and (task['kind'] != 'media' or lane == 'catalog'):
                    next_marker = task['path'] + ':' + cursor
                    if next_marker == marker or next_marker in checkpoint['visited']:
                        raise PilotError('댓글 조회 페이지 커서가 반복됩니다. 완료로 처리하지 않습니다.')
                    followups.append({**task, 'after': cursor})
                checkpoint['tasks'] = checkpoint['tasks'][1:] + followups
                checkpoint['visited'].append(marker)
                if not checkpoint['tasks']:
                    checkpoint['completed_through'] = checkpoint['through']
                # Queue and next cursors are committed together after each page.
                self.save()
            except PilotError as error:
                self.error = error
                if isinstance(error, GraphError) and error.code == 100 and task.get('after'):
                    task.pop('after', None)
                    checkpoint['visited'] = [v for v in checkpoint['visited']
                                             if not v.startswith(task['path'] + ':')]
                self.save()
                break

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
                'recent_complete': not self.data.get('recent', {}).get('tasks'),
                'catalog_complete': not self.data.get('catalog', {}).get('tasks'),
                'recent_through': self.data.get('recent', {}).get('completed_through'),
                'catalog_through': self.data.get('catalog', {}).get('completed_through')}
