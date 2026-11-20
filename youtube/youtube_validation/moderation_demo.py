"""Synthetic, offline vertical slice. Does not access Google, Notion or a DB."""

import argparse
from dataclasses import replace
import json
from pathlib import Path

from .moderation import assess, approve, fetch_policy, prepare_rejection
from .pipeline import EXPECTED_CHANNEL
from .storage import write_json


def demo():
    identifier = '00000000-0000-4000-8000-000000000001'
    text = ('정상 비판·질문·팬 응원은 보존한다. '
            '영상과 무관한 도박 광고는 검수 후 숨김 후보로 올린다. '
            '타인의 비공개 연락처 노출은 긴급 검토한다.')
    page = {'id': identifier, 'text': text, 'page_last_edited_at': '2026-11-11T00:00:00Z'}
    draft = fetch_policy(lambda _: page, identifier, EXPECTED_CHANNEL)
    # Only a synthetic policy is marked adopted here to exercise approval guards.
    policy = replace(draft, adopted=True)
    samples = [
        ('criticism', '티켓 가격이 비싸고 대기줄 안내가 부족해요.', 'keep',
         '정상 비판·질문·팬 응원은 보존한다.', '티켓 가격이 비싸고', True, False),
        ('support', '다음엔 꼭 와주세요 🔥🔥', 'keep',
         '정상 비판·질문·팬 응원은 보존한다.', '꼭 와주세요', True, False),
        ('spam', '영상 상관없이 도박 광고 가입하세요', 'reject_candidate',
         '영상과 무관한 도박 광고는 검수 후 숨김 후보로 올린다.', '도박 광고 가입하세요', False, False),
        ('privacy', '다른 사람 비공개 연락처는 010-0000-0000', 'reject_candidate',
         '타인의 비공개 연락처 노출은 긴급 검토한다.', '비공개 연락처는 010-0000-0000', False, True),
        ('conflict', '비싸서 불만인데 도박 광고 가입하세요', 'reject_candidate',
         '영상과 무관한 도박 광고는 검수 후 숨김 후보로 올린다.', '도박 광고 가입하세요', True, False),
    ]
    candidates = []
    for identifier, text, outcome, quote, evidence, protected, urgent in samples:
        envelope = {'comment': {'id': 'SYNTHETIC-' + identifier, 'channel_id': EXPECTED_CHANNEL,
                    'video_id': 'SYNTHETIC', 'text': text, 'parent_id': None},
                    'parent': None, 'known_reply_ids': []}
        # Fixed fixtures validate routing, not a real model's semantic accuracy.
        value = {'outcome': outcome, 'policy_quote': quote, 'comment_quote': evidence,
                 'protected': protected, 'urgent': urgent, 'reason': '오프라인 합성 판정 예시'}
        candidates.append(assess(policy, envelope, lambda *_: value, model='fixture-not-ai'))
    target = candidates[2]
    approval = approve(target, operator='synthetic-reviewer', reason='합성 광고 예시 검토',
                       acknowledge_thread_effects=True)
    plan = prepare_rejection(target, approval, policy, target['evidence'])
    return {'synthetic_only': True, 'model_executed': False, 'notion_connected': False,
        'youtube_requests_sent': 0, 'candidates': candidates,
        'example_approval': approval, 'example_request_plan': plan}


def main():
    parser = argparse.ArgumentParser(description='YouTube 댓글 숨김 흐름 오프라인 시험')
    parser.add_argument('--output', type=Path,
                        default=Path('.local/youtube-moderation/demo.json'))
    args = parser.parse_args()
    result = demo()
    write_json(args.output, result)
    print(json.dumps({'output': str(args.output.resolve()), 'synthetic_comments': len(result['candidates']),
        'outcomes': [item['assessment']['outcome'] for item in result['candidates']],
        'youtube_requests_sent': 0}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
