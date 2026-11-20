"""Collect all three moderation queries and separately paginate replies."""

from datetime import datetime, timezone
import uuid

from .api import ValidationError, verify_channel
from .storage import persist_run

STATUSES = ('published', 'heldForReview', 'likelySpam')
LIMITATIONS = [
    'API에서 반환하는 댓글 범위만 검증합니다. 삭제·숨김 댓글의 복원은 보장하지 않습니다.',
    '조회 상태는 댓글 스레드의 검색 조건입니다. 개별 댓글 상태는 API 반환값만 기록합니다.',
    '수집 중 추가·변경된 댓글 때문에 API 조회 전체가 한 시점의 스냅샷은 아닙니다.',
]


def pages(api, resource, params, coverage, max_pages=None):
    token, seen = None, set()
    while True:
        data = api.get(resource, **params, **({'pageToken': token} if token else {}))
        coverage['pages'] += 1
        yield data['items']
        token = data.get('nextPageToken')
        if not token:
            coverage['complete'] = True
            return
        if max_pages is not None and coverage['pages'] >= max_pages:
            coverage['limited'] = True
            return
        if not isinstance(token, str) or token in seen:
            raise ValidationError('repeated_or_invalid_page_token')
        seen.add(token)


def normalize(comment, channel_id, video_id, queried_status=None):
    if not isinstance(comment, dict):
        raise ValidationError('malformed_comment')
    snippet = comment.get('snippet', {})
    if not comment.get('id') or not isinstance(snippet, dict):
        raise ValidationError('malformed_comment')
    if snippet.get('channelId') not in (None, channel_id):
        raise ValidationError('unexpected_comment_channel')
    return {
        'id': comment['id'], 'channel_id': channel_id,
        'video_id': snippet.get('videoId', video_id),
        'parent_id': snippet.get('parentId'),
        'author': snippet.get('authorDisplayName'),
        'author_channel_id': (snippet.get('authorChannelId') or {}).get('value'),
        'text': snippet.get('textOriginal', snippet.get('textDisplay', '')),
        'like_count': snippet.get('likeCount'),
        'published_at': snippet.get('publishedAt'), 'updated_at': snippet.get('updatedAt'),
        'moderation_status': snippet.get('moderationStatus'),
        'thread_query_statuses': [queried_status] if queried_status else [],
    }


def merge_comment(rows, row):
    prior = rows.get(row['id'])
    if prior:
        row['thread_query_statuses'] = sorted(set(prior['thread_query_statuses']) |
                                               set(row['thread_query_statuses']))
        if row['moderation_status'] is None:
            row['moderation_status'] = prior['moderation_status']
    rows[row['id']] = row


def collect(api, state, handle, run_id=None, pages_per_status=None):
    channel = verify_channel(api, handle)
    run_id = run_id or datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ-') + uuid.uuid4().hex[:8]
    summary = {'schema': 1, 'run_id': run_id, 'channel': channel, 'statuses': {},
               'reply_queries': {}, 'limitations': LIMITATIONS,
               'coverage': 'sampled_threads' if pages_per_status else 'api_visible_comments_only',
               'pages_per_status': pages_per_status, 'complete': False}
    rows, parents, streams = {}, {}, {}
    for status in STATUSES:
        coverage = {'pages': 0, 'threads': 0, 'complete': False, 'limited': False, 'error': None}
        summary['statuses'][status] = coverage
        streams[status] = pages(api, 'commentThreads', {
            'part': 'snippet,replies', 'allThreadsRelatedToChannelId': channel['id'],
            'moderationStatus': status, 'maxResults': 100, 'textFormat': 'plainText',
            'order': 'time'}, coverage, max_pages=pages_per_status)
    # One status with a large history must not consume the entire validation
    # budget before the other authorization/visibility paths have been tried.
    while streams:
        for status in tuple(streams):
            coverage = summary['statuses'][status]
            try:
                batch = next(streams[status])
                for thread in batch:
                    snippet = thread.get('snippet', {})
                    top = snippet.get('topLevelComment', {})
                    video_id = snippet.get('videoId')
                    merge_comment(rows, normalize(top, channel['id'], video_id, status))
                    coverage['threads'] += 1
                    inline = thread.get('replies', {}).get('comments', [])
                    if snippet.get('totalReplyCount', 0) or inline:
                        parents[top['id']] = video_id
                    for reply in inline:
                        merge_comment(rows, normalize(reply, channel['id'], video_id, status))
            except StopIteration:
                del streams[status]
            except ValidationError as error:
                coverage['complete'], coverage['error'] = False, error.public()
                del streams[status]
    for parent_id, video_id in parents.items():
        coverage = {'pages': 0, 'comments': 0, 'complete': False, 'error': None}
        summary['reply_queries'][parent_id] = coverage
        try:
            for batch in pages(api, 'comments', {'part': 'snippet', 'parentId': parent_id,
                'maxResults': 100, 'textFormat': 'plainText'}, coverage):
                for reply in batch:
                    if reply.get('snippet', {}).get('parentId') != parent_id:
                        raise ValidationError('unexpected_reply_parent')
                    merge_comment(rows, normalize(reply, channel['id'], video_id))
                    coverage['comments'] += 1
        except ValidationError as error:
            coverage['complete'], coverage['error'] = False, error.public()
    summary['complete'] = all(item['complete'] for item in summary['statuses'].values()) and all(
        item['complete'] for item in summary['reply_queries'].values())
    summary['requested_scope_complete'] = all(
        item['error'] is None and (item['complete'] or item['limited'])
        for item in summary['statuses'].values()) and all(
        item['complete'] for item in summary['reply_queries'].values())
    summary['api_requests'] = api.requests
    summary['comment_ids_in_run'] = len(rows)
    summary['top_level_comments'] = sum(not row['parent_id'] for row in rows.values())
    summary['replies'] = len(rows) - summary['top_level_comments']
    return persist_run(state, summary, sorted(rows.values(), key=lambda row: row['id']))
