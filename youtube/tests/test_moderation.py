import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from youtube_validation.moderation import (
    ModerationError, approve, assess, fetch_policy, prepare_rejection)
from youtube_validation.moderation_demo import demo
from youtube_validation.pipeline import EXPECTED_CHANNEL


class ModerationTests(unittest.TestCase):
    def setUp(self):
        self.identifier = '00000000-0000-4000-8000-000000000001'
        self.page = {'id': self.identifier, 'text': '정상 비판은 보존. 도박 광고는 숨김 후보.',
                     'page_last_edited_at': '2026-11-11T00:00:00Z'}
        self.policy = fetch_policy(lambda _: self.page, self.identifier, EXPECTED_CHANNEL, adopted=True)
        self.envelope = {'comment': {'id': 'comment-1', 'channel_id': EXPECTED_CHANNEL,
            'text': '도박 광고 가입하세요', 'parent_id': None}, 'parent': None, 'known_reply_ids': ['reply-1']}
        self.output = {'outcome': 'reject_candidate', 'policy_quote': '도박 광고는 숨김 후보.',
            'comment_quote': '도박 광고', 'reason': '무관한 광고', 'protected': False, 'urgent': False}
        self.now = datetime(2026, 11, 11, 1, tzinfo=timezone.utc)

    def candidate(self, policy=None, envelope=None, output=None):
        return assess(policy or self.policy, envelope or self.envelope,
                      lambda *_: self.output if output is None else output, model='test-model')

    def approval(self, candidate):
        return approve(candidate, operator='reviewer', reason='원문 검수',
                       acknowledge_thread_effects=True, now=self.now)

    def test_notions_missing_failed_partial_wrong_page_and_scope_are_not_zero(self):
        for patch in ({'text': ''}, {'text': '<content>\n\n</content>'},
                      {'truncated': True}, {'unknown_block_count': 1},
                      {'id': '00000000-0000-4000-8000-000000000002'},
                      {'page_last_edited_at': None}, {'isError': True},
                      {'text': '<page url="https://example.test">보관</page>'}):
            with self.subTest(patch=patch), self.assertRaises(ModerationError):
                fetch_policy(lambda _: {**self.page, **patch}, self.identifier, EXPECTED_CHANNEL)

    def test_arbitrary_natural_language_needs_no_rule_ids_or_version(self):
        self.assertEqual(self.policy.text, self.page['text'])
        self.assertFalse(fetch_policy(lambda _: self.page, self.identifier, EXPECTED_CHANNEL).adopted)

    def test_draft_policy_cannot_approve(self):
        with self.assertRaises(ModerationError):
            self.approval(self.candidate(policy=replace(self.policy, adopted=False)))

    def test_wrong_channel_rejected_before_model(self):
        row = copy.deepcopy(self.envelope)
        row['comment']['channel_id'] = 'other'
        with self.assertRaises(ModerationError):
            self.candidate(envelope=row)

    def test_missing_parent_and_official_author_do_not_call_model(self):
        def fail(*_):
            self.fail('model should not be called')
        row = copy.deepcopy(self.envelope)
        row['comment']['parent_id'] = 'missing'
        self.assertEqual(assess(self.policy, row, fail, model='test')['assessment']['outcome'], 'review')
        row['comment']['author_channel_id'] = EXPECTED_CHANNEL
        self.assertEqual(assess(self.policy, row, fail, model='test')['assessment']['outcome'], 'keep')

    def test_comment_id_cannot_expand_request_into_other_comments(self):
        for identifier in ('a,b', 'a\nb', '', None):
            row = copy.deepcopy(self.envelope)
            row['comment']['id'] = identifier
            with self.subTest(identifier=identifier), self.assertRaises(ModerationError):
                self.candidate(envelope=row)

    def test_unrelated_parent_is_missing_context(self):
        row = copy.deepcopy(self.envelope)
        row['comment']['parent_id'] = 'parent-1'
        row['parent'] = {'id': 'unrelated', 'channel_id': EXPECTED_CHANNEL, 'text': 'other'}
        self.assertEqual(self.candidate(envelope=row)['assessment']['outcome'], 'review')

    def test_fabricated_quotes_and_protection_conflict_go_to_review(self):
        for patch in ({'policy_quote': '규정에 없는 이유'}, {'comment_quote': '없는 원문'},
                      {'protected': True}, {'policy_quote': ''}):
            with self.subTest(patch=patch):
                self.assertEqual(self.candidate(output={**self.output, **patch})['assessment']['outcome'], 'review')

    def test_bad_model_results_are_classification_errors(self):
        for value in ({}, [], {**self.output, 'urgent': 'true'}, {**self.output, 'outcome': 'execute'}):
            with self.subTest(value=value):
                self.assertEqual(self.candidate(output=value)['classification_status'], 'classification_error')

    def test_model_failure_is_preserved(self):
        def fail(*_):
            raise RuntimeError('private provider error')
        result = assess(self.policy, self.envelope, fail, model='test')
        self.assertEqual(result['classification_status'], 'classification_error')
        self.assertNotIn('private provider error', str(result))

    def test_all_replies_effect_requires_explicit_acknowledgement(self):
        with self.assertRaises(ModerationError):
            approve(self.candidate(), operator='reviewer', reason='검수')

    def test_request_plan_is_rejected_without_ban_or_any_transport(self):
        candidate = self.candidate()
        result = prepare_rejection(candidate, self.approval(candidate), self.policy,
                                   self.envelope, now=self.now)
        self.assertEqual(result['params'], {'id': 'comment-1', 'moderationStatus': 'rejected', 'banAuthor': 'false'})
        self.assertFalse(result['sent'])

    def test_policy_body_or_revision_changes_invalidate_approval(self):
        candidate = self.candidate()
        for policy in (replace(self.policy, text=self.policy.text + ' 변경'),
                       replace(self.policy, edited_at='2026-11-11T01:00:00Z'),
                       replace(self.policy, adopted=False)):
            with self.subTest(policy=policy), self.assertRaises(ModerationError):
                prepare_rejection(candidate, self.approval(candidate), policy, self.envelope, now=self.now)

    def test_comment_and_reply_context_changes_invalidate_approval(self):
        candidate = self.candidate()
        for patch in ({'parent': {'text': 'changed'}}, {'known_reply_ids': ['new-reply']},
                      {'comment': {**self.envelope['comment'], 'text': '수정된 댓글'}}):
            with self.subTest(patch=patch), self.assertRaises(ModerationError):
                prepare_rejection(candidate, self.approval(candidate), self.policy,
                                  {**self.envelope, **patch}, now=self.now)

    def test_approval_expiry_future_and_unapproved_record_are_blocked(self):
        candidate = self.candidate()
        for approval in ({}, {**self.approval(candidate), 'operator': ''},
                         {**self.approval(candidate), 'acknowledge_thread_effects': False}):
            with self.subTest(approval=approval), self.assertRaises(ModerationError):
                prepare_rejection(candidate, approval, self.policy, self.envelope, now=self.now)
        for now in (self.now - timedelta(seconds=1), self.now + timedelta(hours=25)):
            with self.assertRaises(ModerationError):
                prepare_rejection(candidate, self.approval(candidate), self.policy, self.envelope, now=now)

    def test_candidate_mutation_does_not_reuse_approval(self):
        candidate = self.candidate()
        approval = self.approval(candidate)
        candidate['comment_id'] = 'other-comment'
        with self.assertRaises(ModerationError):
            prepare_rejection(candidate, approval, self.policy, self.envelope, now=self.now)

    def test_synthetic_demo_neither_connects_nor_executes(self):
        result = demo()
        self.assertEqual([item['assessment']['outcome'] for item in result['candidates']],
                         ['keep', 'keep', 'reject_candidate', 'reject_candidate', 'review'])
        self.assertFalse(result['notion_connected'])
        self.assertFalse(result['model_executed'])
        self.assertEqual(result['youtube_requests_sent'], 0)


if __name__ == '__main__':
    unittest.main()
