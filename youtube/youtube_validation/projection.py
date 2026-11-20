"""Readable source records and operational status for the new Google Sheet."""

from contextlib import closing
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import sqlite3

from .pipeline import EXPECTED_CHANNEL, initialize, summarize
from .storage import STATE, write_json

HEADERS = ['댓글 ID', '상위 댓글 ID', '구분', '작성일 (KST)', '작성자', '댓글 원문', '좋아요',
    '현재 검토 상태', '상태 확인 근거', '영상 URL', '댓글 URL', '작성자 채널 URL', '채널 ID',
    '최초 수집 (KST)', '최근 수집 (KST)', '이번 수집 관측', '조회 상태 이력', '원문 갱신일 (KST)']
LOG_HEADERS = ['실행 ID', '전체 수집 ID', '시작 (KST)', '종료 (KST)', '상태', 'API 요청',
               '신규 댓글', '저장 댓글', '이번 순회 관측', '전체 수집 완료', '오류']
STATUS = {'published': '공개', 'heldForReview': '검토 대기', 'likelySpam': '스팸 의심', 'rejected': '거부됨'}
PHASES = {'collecting': '전체 수집 진행 중', 'partial': '다음 실행에서 계속', 'complete': '전체 수집 완료',
          'idle': '다음 정기 수집 대기', 'error': '확인 필요', 'running': '실행 중', 'interrupted':'중단 후 재개',
          'disconnected':'연결 해제 · 저장 사본 정리'}


def excel_time(value):
    if not value:
        return ''
    try:
        try:
            date = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            date = datetime.strptime(value.split('Z')[0] + 'Z', '%Y%m%dT%H%M%S%fZ').replace(tzinfo=timezone.utc)
        local = date.astimezone(timezone(timedelta(hours=9))).replace(tzinfo=None)
        return (local - datetime(1899, 12, 30)).total_seconds() / 86400
    except (ValueError, TypeError):
        return ''


def snapshot(path=STATE/'comments.sqlite3', result=None):
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        initialize(db)
        db.execute('BEGIN')
        cycle = db.execute('SELECT * FROM pipeline_cycles ORDER BY started_at DESC LIMIT 1').fetchone()
        cycle_id = cycle['id'] if cycle else ''
        seen = {row[0] for row in db.execute('SELECT comment_id FROM pipeline_seen WHERE cycle_id=?', (cycle_id,))}
        rows = []
        for stored in db.execute('SELECT * FROM comments WHERE channel_id=? ORDER BY first_seen,comment_id', (EXPECTED_CHANNEL,)):
            row = json.loads(stored['payload_json'])
            video, identifier = row.get('video_id'), row['id']
            source = row.get('status_source')
            video_url = 'https://www.youtube.com/watch?v=' + video if video else ''
            rows.append([identifier, row.get('parent_id') or '', '대댓글' if row.get('parent_id') else '원댓글',
                excel_time(row.get('published_at')), row.get('author') or '', row.get('text') or '',
                row.get('like_count') if row.get('like_count') is not None else '',
                STATUS.get(row.get('moderation_status'), '상태 미제공'),
                {'api_field':'API 반환', 'published_thread_filter':'공개 원댓글 조회', 'not_returned':'API 미제공'}.get(
                    source, 'API 반환' if row.get('moderation_status') else 'API 미제공'),
                video_url, video_url + '&lc=' + identifier if video_url else '',
                'https://www.youtube.com/channel/' + row['author_channel_id'] if row.get('author_channel_id') else '',
                EXPECTED_CHANNEL, excel_time(stored['first_seen']), excel_time(stored['last_seen']),
                '관측' if identifier in seen else '아직 미관측',
                ', '.join(STATUS.get(status, status) for status in row.get('thread_query_statuses', [])),
                excel_time(row.get('updated_at'))])
        attempts = list(db.execute('SELECT * FROM pipeline_attempts ORDER BY started_at DESC LIMIT 200'))
        log = []
        for attempt in attempts:
            summary = json.loads(attempt['summary_json']) if attempt['summary_json'] else {}
            log.append([attempt['id'], attempt['cycle_id'], excel_time(attempt['started_at']),
                excel_time(attempt['finished_at']), PHASES.get(attempt['state'], attempt['state']),
                attempt['api_requests'], attempt['new_ids'], summary.get('stored_comment_ids', ''),
                summary.get('cycle_observed_ids', ''), '완료' if summary.get('full_cycle_complete') else '진행 중',
                summary.get('error') or ''])
        if result is None:
            result = json.loads(attempts[0]['summary_json']) if attempts and attempts[0]['summary_json'] else {}
            if not result and cycle and attempts:
                result = summarize(db,cycle_id,attempts[0]['id'],'collecting',attempts[0]['api_requests'],attempts[0]['new_ids'])
        statuses = result.get('statuses', {})
        latest = result.get('checked_at') or datetime.now(timezone.utc).isoformat()
        overview = [
            ['항목','현재 값','설명'],
            ['채널','WATERBOMB 워터밤','@waterbomb_official'],
            ['채널 ID', EXPECTED_CHANNEL, '인증한 소유 채널과 일치 확인'],
            ['출처','https://www.youtube.com/@waterbomb_official','YouTube Data API'],
            ['수집 상태',PHASES.get(result.get('state'), '전체 수집 준비'),result.get('error') or ''],
            ['최근 갱신 (KST)',excel_time(latest),'시트 반영 기준 시각'],
            ['저장된 댓글·대댓글',len(rows),'과거 관측 이력 포함'],
            ['원댓글',sum(row[2]=='원댓글' for row in rows),'댓글 ID로 중복 제거'],
            ['대댓글',sum(row[2]=='대댓글' for row in rows),'원댓글별 답글 페이지 조회'],
            ['검토 대기',sum(row[7]=='검토 대기' for row in rows),'API가 개별 상태를 반환한 댓글'],
            ['스팸 의심',sum(row[7]=='스팸 의심' for row in rows),'API가 개별 상태를 반환한 댓글'],
            ['상태 미제공',sum(row[7]=='상태 미제공' for row in rows),'상태를 추정하지 않은 댓글'],
            ['이번 전체 순회 관측',len(seen),'미관측은 삭제를 뜻하지 않음'],
            ['전체 순회 완료','완료' if result.get('full_cycle_complete') else '진행 중','상태별 스레드·전체 답글 조회 기준'],
            ['정기 실행','매일 08:30 시작, 미완료 시 매시 30분 재개','Asia/Seoul'],
            ['인증 상태','OAuth 프로덕션 · 브랜딩 인증 완료','민감범위 미인증 · 소수 운영자용 · OAuth 100인 한도'],
            ['수집 범위','API에서 조회 가능한 영상 댓글과 답글','삭제·숨김 댓글 복원 및 라이브 채팅 제외'],
            ['조회 상태 이력','스레드가 관측된 검색 조건','개별 댓글의 현재 검토 상태와 구분'],
            ['', '', ''],
            ['댓글 조회','처리 페이지','스레드 관측','완료','오류'],
        ]
        for status in ('published','heldForReview','likelySpam'):
            item=statuses.get(status,{})
            overview.append([STATUS[status],item.get('pages',0),item.get('threads',0),
                '완료' if item.get('complete') else '진행 중',item.get('error') or ''])
        overview.append(['대댓글 조회',result.get('reply_parents_complete',0),result.get('reply_parents',0),
            '완료' if result.get('full_cycle_complete') else '진행 중','완료 원댓글 / 전체 대상 원댓글'])
        return {'generated_at': latest, 'cycle_id':cycle_id, 'overview':overview,
                'comments_headers':HEADERS,'comments':rows,'log_headers':LOG_HEADERS,'log':log}


if __name__ == '__main__':
    value = snapshot()
    write_json(STATE/'sheet-source.json',value)
    print(json.dumps({'comments':len(value['comments']),'attempts':len(value['log'])}, ensure_ascii=False))
