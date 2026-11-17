"""Notion condition columns drive count and pattern rules. No live calls."""

import copy
import io
import json
import unittest
from unittest import mock

from sns_comment.count_rules import active_rules, structural_decisions
from sns_comment.moderation import load_rulebook, moderate
from sns_comment.rulebook_source import build_rulebook
from tests.test_author_cap import speak
from tests.test_moderation import ACCOUNT, AUTO_ON, Hides, MemoryState, Model, answer


def rows_from_file():
    labels = {"삭제": "숨김", "사람판단": "사람 판단", "유지": "유지", "폐지": "폐지"}
    return [{"이름": rule["name"], "코드": rule["id"], "결과": labels[rule["label"]], "순서": index,
             "설명": rule["definition"], "예외": rule["exceptions"], "사용": True}
            for index, rule in enumerate(load_rulebook()["rules"], start=1)]


class ConditionParseTests(unittest.TestCase):
    def test_a_bad_condition_is_skipped_and_the_other_rules_remain(self):
        rows = rows_from_file()
        rows.append({"이름": "잘못된 규칙", "코드": "D99", "결과": "숨김", "순서": 99,
                     "설명": "숫자가 없음", "예외": "", "사용": True, "조건 종류": "같은 작성자 댓글 수 초과"})
        rows.append({"이름": "금지어", "코드": "D13", "결과": "숨김", "순서": 13,
                     "설명": "금지어가 있으면 숨김", "예외": "", "사용": True,
                     "조건 종류": "특정 단어 포함", "대상": "금지어, 스팸"})
        stream = io.StringIO()
        with mock.patch("sys.stdout", stream):
            book = build_rulebook(rows, "2026-11-17T00:00:00Z")
        self.assertNotIn("D99", [rule["id"] for rule in book["rules"]])
        self.assertIn("D13", [rule["id"] for rule in book["rules"]])
        self.assertIn("moderation_rule_skipped", stream.getvalue())
        kinds = {item["id"]: item["kind"] for item in book["conditions"]}
        self.assertEqual(kinds["D13"], "keyword")
        self.assertEqual(book["conditions"][0]["targets"] if False else next(item["targets"] for item in book["conditions"] if item["id"] == "D13"), ["금지어", "스팸"])

    def test_count_keyword_and_author_rules_honor_result_and_order(self):
        book = copy.deepcopy(load_rulebook())
        book["conditions"] = [
            {"id": "D12", "label": "삭제", "kind": "author_count", "threshold": 2, "scope": "post",
             "targets": [], "order": 12, "reason": "3개째"},
            {"id": "D14", "label": "삭제", "kind": "author_count", "threshold": 2, "scope": "account",
             "targets": [], "order": 20, "reason": "계정 전체 3개째"},
            {"id": "D13", "label": "사람판단", "kind": "keyword", "threshold": None, "scope": "post",
             "targets": ["금지어"], "order": 5, "reason": "금지어"},
            {"id": "D15", "label": "삭제", "kind": "author_id", "threshold": None, "scope": "post",
             "targets": ["spammer"], "order": 4, "reason": "지정 작성자"},
            {"id": "D11", "label": "삭제", "kind": "repeat_text", "threshold": 3, "scope": "post",
             "targets": [], "order": 11, "reason": "세 번째 반복"},
        ]
        state, client = MemoryState(), Hides()
        client.get = lambda path, **_params: {"hidden": True}
        keyword = speak("31001", "2026-11-17T00:00:00+00:00", text="여기는 금지어 입니다", username="fan", author_id="101")
        spammer = speak("31002", "2026-11-17T00:10:00+00:00", text="안녕", username="Spammer", author_id="999")
        copies = [speak(str(31010 + index), "2026-11-17T01:0" + str(index) + ":00:00+00:00", text="같은 글",
                        username="other", author_id="202") for index in range(3)]
        posts = [speak(str(31020 + index), "2026-11-17T02:0" + str(index) + ":00:00+00:00", text="게시물 " + str(index),
                       media=str(20010 + index), username="cross", author_id="303") for index in range(3)]
        model = Model({row["id"]: {**answer("유지", "K1"), "id": row["id"]} for row in copies[:2] + posts[:2] + [keyword]})
        result = moderate([keyword, spammer, *copies, *posts], state, AUTO_ON, model, client=client, book=book,
                          account_username="waterbomb_toronto_official", region="토론토")
        self.assertEqual(state.data["moderation/decisions/" + ACCOUNT + "/31001.json"]["status"], "human_review")
        self.assertEqual(state.data["moderation/decisions/" + ACCOUNT + "/31002.json"]["rule_id"], "D15")
        self.assertEqual(state.data["moderation/decisions/" + ACCOUNT + "/31012.json"]["rule_id"], "D11")
        self.assertNotEqual(state.data["moderation/decisions/" + ACCOUNT + "/31011.json"]["rule_id"], "D11")
        self.assertEqual(state.data["moderation/decisions/" + ACCOUNT + "/31022.json"]["rule_id"], "D14")
        self.assertIn("31002", client.hidden)
        self.assertIn("31022", client.hidden)
        self.assertNotIn("31001", client.hidden)
        self.assertGreaterEqual(result["human_review"], 1)

    def test_an_official_repeat_is_included_when_the_rule_is_configured(self):
        book = copy.deepcopy(load_rulebook())
        book["conditions"] = [{"id": "D11", "label": "삭제", "kind": "repeat_text", "threshold": 2,
                               "scope": "post", "targets": [], "order": 11, "reason": "반복"}]
        state, client = MemoryState(), Hides()
        client.get = lambda path, **_params: {"hidden": True}
        first = speak("32001", "2026-11-17T00:00:00+00:00", text="공식 안내", username="waterbomb_official",
                      author_id="1", owner=1)
        second = speak("32002", "2026-11-17T01:00:00+00:00", text="공식 안내", username="waterbomb_official",
                       author_id="1", owner=1)
        moderate([first, second], state, AUTO_ON, Model({}), client=client, book=book,
                 account_username="waterbomb_official", region="서울")
        self.assertEqual(state.data["moderation/decisions/" + ACCOUNT + "/32001.json"]["rule_id"], "K3")
        self.assertEqual(state.data["moderation/decisions/" + ACCOUNT + "/32002.json"]["rule_id"], "D11")
        self.assertEqual(client.hidden, ["32002"])
        self.assertTrue(active_rules(book)[0]["kind"] == "repeat_text")
        self.assertIn("32002", structural_decisions([first, second], {}, book))


if __name__ == "__main__":
    unittest.main()
