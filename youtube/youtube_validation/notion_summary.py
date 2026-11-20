"""Aggregate confirmed YouTube collection data without copying comment bodies."""

from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

from .pipeline import EXPECTED_CHANNEL


KST = ZoneInfo('Asia/Seoul')


class ReportDeferred(Exception):
    """The source is missing or incomplete; it must never become a normal zero."""


def timestamp(value):
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return result.astimezone(KST) if result.tzinfo else None
    except (TypeError, ValueError):
        return None


def period(today, covered_through=None):
    end = today - timedelta(days=1)
    if covered_through:
        try:
            start = date.fromisoformat(covered_through) + timedelta(days=1)
        except (TypeError, ValueError):
            raise ReportDeferred('invalid_report_coverage') from None
        if start > end:
            raise ReportDeferred('report_period_already_covered')
    else:
        start = today - timedelta(days=3 if today.weekday() == 0 else 1)
    if (end - start).days >= 21:
        raise ReportDeferred('report_period_exceeds_stored_retention')
    return start, end


def summarize(path, now=None, covered_through=None):
    now = (now or datetime.now(timezone.utc)).astimezone(KST)
    start, end = period(now.date(), covered_through)
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        raise ReportDeferred('collection_database_missing')
    minimum = datetime.combine(start, time.min, tzinfo=KST)
    maximum = datetime.combine(end + timedelta(days=1), time.min, tzinfo=KST)
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute('BEGIN')
            cycle = db.execute('SELECT * FROM pipeline_cycles ORDER BY started_at DESC LIMIT 1').fetchone()
            if not cycle or cycle['state'] != 'complete':
                raise ReportDeferred('collection_not_complete')
            completed = timestamp(cycle['finished_at'])
            if completed is None or completed.date() != now.date() or completed > now:
                raise ReportDeferred('collection_not_current')
            counts = {'observed_comments': 0, 'parent_comments': 0, 'replies': 0,
                      'held_for_review': 0, 'likely_spam': 0, 'status_not_returned': 0,
                      'period_comments': 0, 'period_parent_comments': 0, 'period_replies': 0,
                      'published_time_not_returned': 0}
            for stored in db.execute('''SELECT c.payload_json FROM comments c
                    JOIN pipeline_seen s ON s.comment_id=c.comment_id
                    WHERE c.channel_id=? AND s.cycle_id=?''', (EXPECTED_CHANNEL, cycle['id'])):
                row = json.loads(stored['payload_json'])
                reply = bool(row.get('parent_id'))
                counts['observed_comments'] += 1
                counts['replies' if reply else 'parent_comments'] += 1
                status = row.get('moderation_status')
                if status == 'heldForReview':
                    counts['held_for_review'] += 1
                elif status == 'likelySpam':
                    counts['likely_spam'] += 1
                elif not status:
                    counts['status_not_returned'] += 1
                published = timestamp(row.get('published_at'))
                if published is None:
                    counts['published_time_not_returned'] += 1
                elif minimum <= published < maximum:
                    counts['period_comments'] += 1
                    counts['period_replies' if reply else 'period_parent_comments'] += 1
            return {'schema': 'youtube-notion-summary-v1', 'report_day': now.date().isoformat(),
                    'period_start': start.isoformat(), 'period_end': end.isoformat(),
                    'collection_cycle_id': cycle['id'], 'collection_complete': True,
                    'collected_at': completed.isoformat(), 'channel_id': EXPECTED_CHANNEL,
                    **counts}
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        raise ReportDeferred('collection_database_invalid') from None


def title(summary):
    return '🎬 유튜브 댓글 (' + summary['report_day'] + ')'


def lines(summary, spreadsheet_id):
    start, end = summary['period_start'], summary['period_end']
    window = start if start == end else start + ' ~ ' + end
    result = [
        f"{window} 작성 댓글 {summary['period_comments']:,}건 "
        f"(원댓글 {summary['period_parent_comments']:,} · 대댓글 {summary['period_replies']:,})",
        f"검토 대기 {summary['held_for_review']:,}건 · 스팸 의심 {summary['likely_spam']:,}건 "
        f"· 상태 미제공 {summary['status_not_returned']:,}건",
        f"전체 수집 완료 {summary['collected_at'][11:16]} KST "
        f"· 이번 순회 확인 {summary['observed_comments']:,}건",
    ]
    if summary['published_time_not_returned']:
        result.append(f"작성 시각 미제공 {summary['published_time_not_returned']:,}건은 날짜별 집계에서 제외")
    result.append('댓글 원본 보기 (구글시트)')
    return result, 'https://docs.google.com/spreadsheets/d/' + spreadsheet_id + '/edit'
