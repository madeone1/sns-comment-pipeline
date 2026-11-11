"""Synthetic account and comment fixtures; no operational data or credentials."""

import datetime as dt

from sns_comment.reporting import KST, counts, windows


def fixture():
    settings = {'expected_username': 'waterbomb_official', 'account_id': '10001', 'api_version': 'v26.0',
        'accounts': {'waterbomb_busan_official': {'account_id': '10002', 'enabled': True, 'meta_credential': 'primary'},
                     'waterbomb_sokcho_official': {'account_id': '10003', 'enabled': True, 'meta_credential': 'primary'}},
        'cloud': {'scheduled_accounts': ['waterbomb_official', 'waterbomb_busan_official', 'waterbomb_sokcho_official']}}
    day = windows()[0]['target_day']
    sources = {}
    examples = [
        ('티켓 현장 수령은 몇 시부터 가능한가요?', 'summer_archive', False),
        ('이번 라인업 너무 기대돼요 🔥', 'blueweekend', False),
        ('감사합니다! 일정 안내는 프로필 링크에서 확인해주세요.', 'waterbomb_official', True),
        ('락커 크기와 이용 요금이 궁금합니다.', 'daylight_notes', False),
        ('🔥🔥🔥', 'our_saturday', False),
        (None, 'unknown_reader', False),
        ('지난번 공연 영상도 더 올라오면 좋겠어요!', 'waterday_27', False),
        ('우천 시에도 정상 진행되는지 알고 싶어요.', 'seaside_log', False),
        ('친구랑 같이 갑니다 💙', 'goodday.j', False),
    ]
    for offset in range(7):
        date = (dt.date.fromisoformat(day)-dt.timedelta(days=offset)).isoformat()
        end = dt.datetime.combine(dt.date.fromisoformat(date), dt.time(8), tzinfo=KST)
        period = {'key': 'daily', 'label': '일일', 'target_day': date, 'start': (end-dt.timedelta(days=1)).isoformat(), 'end': end.isoformat(), 'media_start': (end-dt.timedelta(days=7)).isoformat()}
        for i, identifier in enumerate(('10001', '10002', '10003')):
            rows = [{'id': str(90000+offset*100+i*20+j), 'media_id': '80001', 'parent_id': '90000' if j == 2 else None,
                'author_id': '7000'+str(j), 'username': user, 'text': text, 'is_owner': int(owner),
                'written_at': (end-dt.timedelta(hours=2+j*2)).isoformat(), 'payload_hash': 'example-'+str(j),
                'permalink': 'https://www.instagram.com/waterbomb_official/'}
                for j, (text, user, owner) in enumerate(examples[:9-i*3])]
            view = {'period': period, 'comments': rows, 'counts': counts(rows), 'media': []}
            sources[(identifier, date)] = {'views': [view], 'recent_seven_days': view,
                'collection': {'status': 'complete', 'account_id': identifier, 'last_scope_completed_at': (end+dt.timedelta(minutes=3)).isoformat()}}
    return settings, sources
