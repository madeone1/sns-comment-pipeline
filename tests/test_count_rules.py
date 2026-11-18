"""Notion condition columns drive count and pattern rules. No live calls."""

import contextlib
import copy
import datetime as dt
import io
import json
import os
import unittest
from unittest import mock

from sns_comment.count_rules import active_rules, comment_targets, structural_decisions
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


PHRASES = (
    "X:IN!!!!!",
    "X:IN 🔥🔥🔥",
    "PLEASE @x.in_official",
    "aespa and x:in!!!",
    "XIN PLEASE",
    "ITS TIME FOR X:IN 🔥",
    "Xin🙌",
)


def burst_book(threshold=10, minutes=60, targets=None):
    book = copy.deepcopy(load_rulebook())
    book["rules"] = list(book["rules"]) + [{
        "id": "D13", "label": "삭제", "name": "짧은 시간에 같은 대상이 몰림",
        "definition": "한 게시물에서 같은 대상이 짧은 시간에 많이 달리면 숨깁니다.",
        "exceptions": "사람이 유지로 둔 댓글은 숨기지 않습니다.",
    }]
    book["hide_rule_ids"] = list(book["hide_rule_ids"]) + ["D13"]
    book["conditions"] = [{
        "id": "D13", "label": "삭제", "kind": "burst", "threshold": threshold, "scope": "post",
        "targets": list(targets or []), "minutes": minutes, "order": 11.6,
        "reason": "짧은 시간에 같은 대상이 몰림",
    }]
    return book


def at(minute, comment_id, text, author, media="20001", owner=0):
    written = (dt.datetime(2026, 11, 18, 12, 0, tzinfo=dt.timezone.utc) + dt.timedelta(minutes=minute)).isoformat()
    return speak(str(comment_id), written, text=text, username="fan" + str(author), author_id=str(author),
                 owner=owner, media=media)


class BurstRuleTests(unittest.TestCase):
    def test_campaign_phrases_fold_to_the_same_target(self):
        for text in PHRASES:
            self.assertIn("xin", comment_targets(text), text)
        self.assertEqual(comment_targets("aespa and x:in!!!"), ["aespa", "xin"])
        self.assertEqual(comment_targets("PLEASE @x.in_official"), ["xin"])
        self.assertEqual(comment_targets("ITS TIME FOR X:IN 🔥"), ["xin"])
        self.assertNotIn("xin", comment_targets("x in"))
        self.assertEqual(comment_targets("PLEASE PLS"), [])
        self.assertEqual(comment_targets("X:IN, @x.in_official"), ["xin"])

    def test_more_than_ten_names_inside_one_hour_hides_the_tail(self):
        comments = [at(index, 41000 + index, PHRASES[index % len(PHRASES)], 700 + index) for index in range(11)]
        comments[3]["parent_id"] = comments[0]["id"]
        comments[-1]["username"] = "waterbomb_toronto_official"
        comments[-1]["is_owner"] = 1
        book = burst_book()
        kept = {row["id"]: {**answer("유지", "K1"), "id": row["id"]} for row in comments[:-1]}
        state, client, model = MemoryState(), Hides(), Model(kept)
        client.get = lambda path, **_params: {"hidden": True}
        result = moderate(comments, state, AUTO_ON, model, client=client, book=book,
                          account_username="waterbomb_toronto_official", region="토론토")
        self.assertNotIn("41010", model.seen)
        self.assertEqual(state.data["moderation/decisions/" + ACCOUNT + "/41010.json"]["rule_id"], "D13")
        self.assertEqual(client.hidden, ["41010"])
        self.assertEqual(result["hidden"], 1)
        for index in range(10):
            self.assertNotEqual(state.data["moderation/decisions/" + ACCOUNT + "/" + str(41000 + index) + ".json"]["rule_id"], "D13")

    def test_a_later_comment_is_kept_once_the_hour_has_passed(self):
        early = [at(index, 42000 + index, "XIN PLEASE", 800 + index) for index in range(12)]
        late = at(121, 42020, "Xin🙌", 900)
        book = burst_book()
        hidden = structural_decisions(early + [late], {}, book)
        self.assertEqual(set(hidden), {"42010", "42011"})
        self.assertNotIn("42020", hidden)
        slow = [at(index * 10, 42100 + index, "X:IN!!!!!", 910 + index) for index in range(11)]
        self.assertEqual(structural_decisions(slow, {}, book), {})

    def test_the_window_boundary_is_inclusive_and_another_post_does_not_count(self):
        book = burst_book(threshold=1, minutes=60)
        inside = structural_decisions([
            at(0, 43001, "X:IN 🔥🔥🔥", 1),
            at(60, 43002, "PLEASE @x.in_official", 2),
        ], {}, book)
        self.assertEqual(set(inside), {"43002"})
        outside = structural_decisions([
            at(0, 43011, "X:IN!!!!!", 3),
            at(61, 43012, "Xin🙌", 4),
        ], {}, book)
        self.assertEqual(outside, {})
        other = [at(index, 43100 + index, "XIN PLEASE", 20 + index, media="20002") for index in range(11)]
        alone = at(5, 43120, "ITS TIME FOR X:IN 🔥", 40, media="20001")
        decided = structural_decisions(other + [alone], {}, book)
        self.assertNotIn("43120", decided)
        self.assertIn("43110", decided)

    def test_a_listed_target_ignores_other_names_and_a_human_keep_still_counts(self):
        xin = [at(index, 44000 + index, "X:IN!!!!!", 300 + index) for index in range(11)]
        aespa = [at(index, 44100 + index, "aespa please", 400 + index) for index in range(11)]
        limited = structural_decisions(xin + aespa, {}, burst_book(targets=["xin"]))
        self.assertIn("44010", limited)
        self.assertFalse(any(item.startswith("441") for item in limited))
        book = burst_book()
        state, client = MemoryState(), Hides()
        client.get = lambda path, **_params: {"hidden": True}
        kept = xin[-1]
        answered = {row["id"]: {**answer("유지", "K1"), "id": row["id"]} for row in xin[:-1]}
        state.json_put("moderation/decisions/" + ACCOUNT + "/" + kept["id"] + ".json", {
            "comment_id": kept["id"], "account_id": ACCOUNT, "status": "kept", "human_decision": "유지",
            "label": "유지", "text": kept["text"], "written_at": kept["written_at"],
        }, 0)
        extra = at(12, 44011, "Xin🙌", 399)
        moderate(xin + [extra], state, AUTO_ON, Model(answered), client=client, book=book,
                 account_username="waterbomb_toronto_official", region="토론토")
        self.assertNotIn(kept["id"], client.hidden)
        self.assertIn("44011", client.hidden)
        self.assertEqual(state.data["moderation/decisions/" + ACCOUNT + "/" + kept["id"] + ".json"]["human_decision"], "유지")

    def test_a_new_comment_uses_timestamps_already_stored_for_the_post(self):
        book = burst_book()
        state, client = MemoryState(), Hides()
        client.get = lambda path, **_params: {"hidden": True}
        first = [at(index, 45000 + index, PHRASES[index % len(PHRASES)], 500 + index) for index in range(10)]
        model = Model({row["id"]: {**answer("유지", "K1"), "id": row["id"]} for row in first})
        moderate(first, state, AUTO_ON, model, client=client, book=book,
                 account_username="waterbomb_toronto_official", region="토론토")
        self.assertEqual(client.hidden, [])
        arrived = at(20, 45010, "PLEASE @x.in_official", 590)
        moderate([arrived], state, AUTO_ON, Model({}), client=client, book=book,
                 account_username="waterbomb_toronto_official", region="토론토")
        self.assertEqual(client.hidden, ["45010"])
        self.assertEqual(state.data["moderation/decisions/" + ACCOUNT + "/45010.json"]["rule_id"], "D13")

    def test_blank_numbers_default_and_a_bad_burst_row_is_skipped(self):
        rows = rows_from_file()
        rows.append({"이름": "짧은 시간에 같은 대상이 몰림", "코드": "D13", "결과": "숨김", "순서": 11.6,
                     "설명": "한 게시물에서 같은 대상이 짧은 시간에 많이 달리면 숨깁니다.",
                     "예외": "사람이 유지로 둔 댓글은 숨기지 않습니다.", "사용": True,
                     "조건 종류": "같은 대상 짧은 시간 몰림", "범위": "게시물별"})
        book = build_rulebook(rows, "2026-11-18T00:00:00Z")
        rule = next(item for item in book["conditions"] if item["id"] == "D13")
        self.assertEqual(rule["threshold"], 10)
        self.assertEqual(rule["minutes"], 60)
        self.assertEqual(rule["targets"], [])
        rows[-1]["시간(분)"] = "soon"
        stream = io.StringIO()
        with mock.patch("sys.stdout", stream):
            skipped = build_rulebook(rows, "2026-11-18T00:00:00Z")
        self.assertNotIn("D13", [item["id"] for item in skipped["rules"]])
        self.assertIn("moderation_rule_skipped", stream.getvalue())
        rows[-1]["시간(분)"] = 60
        rows[-1]["범위"] = "계정 전체"
        with mock.patch("sys.stdout", stream):
            account_scope = build_rulebook(rows, "2026-11-18T00:00:00Z")
        self.assertNotIn("D13", [item["id"] for item in account_scope["rules"]])

    def test_from_stored_judges_the_bucket_without_listing_instagram(self):
        from sns_comment.author_cap import remember_author_posts, sweep_account_stored
        from sns_comment.moderation_sheet import HIDE_TAB
        from tests.test_author_cap import FakeSheets
        book = burst_book()
        state = MemoryState()
        comments = [at(index, 46000 + index, "X:IN!!!!!", 600 + index) for index in range(11)]
        remember_author_posts(state, comments)
        config = {"account_id": ACCOUNT, "expected_username": "waterbomb_toronto_official", "region_label": "토론토"}

        class Listing:
            def get(self, path, **_params):
                raise AssertionError(path)

        preview = sweep_account_stored(config, state, Listing(), {"moderation": {}}, FakeSheets(), book, False)
        self.assertEqual(preview["would_hide"], 1)
        self.assertEqual(preview["within_limit"], 10)
        self.assertEqual(preview["source"], "stored")
        self.assertEqual(preview["hidden"], 0)

        class HideOnly:
            def __init__(self):
                self.hidden, self.gets = [], []

            def get(self, path, **params):
                self.gets.append((path, params.get("fields")))
                return {"hidden": True}

            def hide_comment(self, comment_id):
                self.hidden.append(comment_id)
                return {"success": True}

        graph, sheet = HideOnly(), FakeSheets()
        state = MemoryState()
        remember_author_posts(state, comments)
        applied = sweep_account_stored(config, state, graph, {"moderation": {}}, sheet, book, True)
        self.assertEqual(applied["hidden"], 1)
        self.assertEqual(graph.hidden, ["46010"])
        self.assertEqual(graph.gets, [("46010", "hidden")])
        rows = [row for row in sheet.data(HIDE_TAB) if row[6] == "46010"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][8], "D13")
        again = sweep_account_stored(config, state, graph, {"moderation": {}}, sheet, book, True)
        self.assertEqual(again["hidden"], 0)
        self.assertEqual(len([row for row in sheet.data(HIDE_TAB) if row[6] == "46010"]), 1)

    def test_a_stored_hide_pause_resumes_without_a_second_sheet_row(self):
        from sns_comment.api import GraphError
        from sns_comment.author_cap import remember_author_posts, sweep_account_stored
        from sns_comment.moderation_sheet import HIDE_TAB
        from tests.test_author_cap import FakeSheets
        book = burst_book()
        state, sheet = MemoryState(), FakeSheets()
        remember_author_posts(state, [at(index, 47000 + index, "Xin🙌", 800 + index) for index in range(11)])
        config = {"account_id": ACCOUNT, "expected_username": "waterbomb_toronto_official", "region_label": "토론토"}

        class Pause:
            def get(self, path, **_params):
                return {"hidden": False}

            def hide_comment(self, comment_id):
                raise GraphError(429, 4)

        paused = sweep_account_stored(config, state, Pause(), {"moderation": {}}, sheet, book, True)
        self.assertEqual(paused["status"], "rate_limited")
        self.assertEqual(paused["hidden"], 0)
        self.assertFalse(any(len(row) > 6 and row[6] == "47010" for row in sheet.data(HIDE_TAB)))

        class HideOnly:
            def __init__(self):
                self.hidden = []

            def get(self, path, **_params):
                return {"hidden": True}

            def hide_comment(self, comment_id):
                self.hidden.append(comment_id)
                return {"success": True}

        graph = HideOnly()
        finished = sweep_account_stored(config, state, graph, {"moderation": {}}, sheet, book, True)
        self.assertEqual(finished["status"], "complete")
        self.assertEqual(graph.hidden, ["47010"])
        self.assertEqual(len([row for row in sheet.data(HIDE_TAB) if row[6] == "47010" and row[8] == "D13"]), 1)

    def test_the_sweep_command_can_target_one_stored_account(self):
        from sns_comment.moderation_job import main, selected_configs
        toronto = {"account_id": ACCOUNT, "expected_username": "waterbomb_toronto_official", "region_label": "토론토"}
        seoul = {"account_id": "17841401245057628", "expected_username": "waterbomb_official", "region_label": "서울"}
        self.assertEqual(selected_configs([seoul, toronto], "toronto"), [toronto])
        self.assertEqual(selected_configs([seoul, toronto], "@waterbomb_toronto_official"), [toronto])
        self.assertEqual(selected_configs([seoul, toronto], ACCOUNT), [toronto])
        self.assertEqual(selected_configs([seoul, toronto], "없는계정"), [])
        seen = {}

        def fake_sweep(chosen, state, clients, settings, sheet, book, apply, source="graph"):
            seen["ids"] = [item["account_id"] for item in chosen]
            seen["source"] = source
            seen["apply"] = apply
            return {"apply": apply, "source": source, "accounts": []}

        state = mock.Mock()
        state.json_get.return_value = ({"schema": 1, "accounts": {}, "groups": {}}, 0)
        state.lease.return_value = contextlib.nullcontext()
        with mock.patch.dict(os.environ, {"SNS_STATE_BUCKET": "bucket"}), \
             mock.patch("sns_comment.moderation_job.CloudState", return_value=state), \
             mock.patch("sns_comment.moderation_job.read_settings", return_value={"moderation": {"rulebook": {"source": "file"}}}), \
             mock.patch("sns_comment.rulebook_source.resolve_rulebook", return_value=burst_book()), \
             mock.patch("sns_comment.rulebook_source.rulebook_source", return_value="file"), \
             mock.patch("sns_comment.moderation_job.runtime_configs", return_value=[seoul, toronto]), \
             mock.patch("sns_comment.moderation_job.account_clients") as clients, \
             mock.patch("sns_comment.author_cap.sweep_author_cap", side_effect=fake_sweep):
            code = main(["--apply-count-rules", "--account", "toronto", "--from-stored"])
        self.assertEqual(code, 0)
        self.assertEqual(seen["ids"], [ACCOUNT])
        self.assertEqual(seen["source"], "stored")
        self.assertIs(seen["apply"], False)
        clients.assert_not_called()
        seen.clear()
        stream = io.StringIO()
        with mock.patch.dict(os.environ, {"SNS_STATE_BUCKET": "bucket"}), \
             mock.patch("sys.stdout", stream), \
             mock.patch("sns_comment.moderation_job.CloudState", return_value=state), \
             mock.patch("sns_comment.moderation_job.read_settings", return_value={"moderation": {"rulebook": {"source": "file"}}}), \
             mock.patch("sns_comment.rulebook_source.resolve_rulebook", return_value=burst_book()), \
             mock.patch("sns_comment.rulebook_source.rulebook_source", return_value="file"), \
             mock.patch("sns_comment.moderation_job.runtime_configs", return_value=[seoul, toronto]), \
             mock.patch("sns_comment.author_cap.sweep_author_cap", side_effect=fake_sweep):
            missing = main(["--apply-count-rules", "--account", "없는계정", "--from-stored"])
        self.assertEqual(missing, 0)
        self.assertNotIn("ids", seen)
        self.assertIn("count_rules_account", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
