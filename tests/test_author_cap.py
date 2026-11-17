"""Two comments per author per post. No live Instagram calls."""

import copy
import json
import unittest

from sns_comment.api import GraphError
from sns_comment.author_cap import author_cap_enabled, sweep_account_cap
from sns_comment.moderation import load_rulebook, moderate
from sns_comment.moderation_sheet import HIDE_TAB
from tests.test_moderation import ACCOUNT, AUTO_ON, DRY, FakeSheets, Hides, MemoryState, Model, answer, audience


def cap_book():
    book = copy.deepcopy(load_rulebook())
    book["rules"].append({"id": "D12", "label": "삭제", "name": "한 게시물 2개",
                          "definition": "3개째부터 숨김", "exceptions": "사람 유지는 숨기지 않습니다."})
    book["hide_rule_ids"].append("D12")
    return book


def speak(comment_id, written, username="fan", author_id="101", owner=0, text="안녕하세요", media="20001"):
    row = audience(text, comment_id, written=written, username=username, author_id=author_id, owner=owner, media=media)
    return row


class CapRuleTests(unittest.TestCase):
    def test_the_third_comment_is_hidden_and_an_official_account_is_not_exempt(self):
        state, client = MemoryState(), Hides()
        client.get = lambda path, **_params: {"hidden": True}
        comments = [
            speak("30001", "2026-11-17T00:00:00+00:00", username="waterbomb_toronto_official", author_id="1", owner=1),
            speak("30002", "2026-11-17T01:00:00+00:00", username="waterbomb_toronto_official", author_id="1", owner=1, text="두번째"),
            speak("30003", "2026-11-17T02:00:00+00:00", username="waterbomb_toronto_official", author_id="1", owner=1, text="세번째"),
            speak("30004", "2026-11-17T03:00:00+00:00", username="waterbomb_toronto_official", author_id="1", owner=1,
                  text="답글", media="20001"),
        ]
        comments[3]["parent_id"] = "30001"
        result = moderate(comments, state, AUTO_ON, Model({}), client=client, book=cap_book(),
                          account_username="waterbomb_toronto_official", region="토론토")
        self.assertEqual(result["hidden"], 2)
        self.assertEqual(client.hidden, ["30003", "30004"])
        self.assertEqual(state.data["moderation/decisions/" + ACCOUNT + "/30001.json"]["rule_id"], "K3")
        self.assertEqual(state.data["moderation/decisions/" + ACCOUNT + "/30003.json"]["rule_id"], "D12")

    def test_a_second_exact_copy_stays_d11_and_another_post_does_not_count(self):
        state = MemoryState()
        book = cap_book()
        first = speak("30011", "2026-11-17T00:00:00+00:00", text="같은 문장")
        second = speak("30012", "2026-11-17T01:00:00+00:00", text="같은 문장")
        other = speak("30013", "2026-11-17T02:00:00+00:00", text="다른 게시물", media="20002")
        model = Model({first["id"]: {**answer("유지", "K1"), "id": first["id"]},
                       other["id"]: {**answer("유지", "K1"), "id": other["id"]}})
        result = moderate([second, other, first], state, DRY, model, client=Hides(), book=book,
                          account_username="waterbomb_toronto_official", region="토론토")
        self.assertEqual(state.data["moderation/decisions/" + ACCOUNT + "/30012.json"]["rule_id"], "D11")
        self.assertEqual(state.data["moderation/decisions/" + ACCOUNT + "/30013.json"]["rule_id"], "K1")
        self.assertEqual(result["would_hide"], 1)

    def test_without_d12_the_cap_is_off_and_a_human_keep_is_not_hidden(self):
        self.assertFalse(author_cap_enabled(load_rulebook()))
        state, client = MemoryState(), Hides()
        comments = [speak(str(30020 + index), "2026-11-17T0" + str(index) + ":00:00+00:00", text="문장 " + str(index))
                    for index in range(3)]
        model = Model({row["id"]: {**answer("유지", "K1"), "id": row["id"]} for row in comments})
        result = moderate(comments, state, AUTO_ON, model, client=client, account_username="waterbomb_toronto_official")
        self.assertEqual(client.hidden, [])
        self.assertEqual(result["kept"], 3)
        book = cap_book()
        kept = speak("30030", "2026-11-17T03:00:00+00:00")
        state.json_put("moderation/decisions/" + ACCOUNT + "/30030.json", {
            "comment_id": "30030", "account_id": ACCOUNT, "status": "kept", "human_decision": "유지",
            "label": "유지", "text": kept["text"], "written_at": kept["written_at"],
        }, 0)
        earlier = [speak("30031", "2026-11-17T00:00:00+00:00"), speak("30032", "2026-11-17T01:00:00+00:00")]
        moderate([kept], state, AUTO_ON, Model({}), client=client, book=book, known_comments=earlier,
                 account_username="waterbomb_toronto_official", region="토론토")
        self.assertEqual(client.hidden, [])
        later = speak("30033", "2026-11-17T04:00:00+00:00", text="네번째")
        moderate([later], state, AUTO_ON, Model({}), client=client, book=book,
                 account_username="waterbomb_toronto_official", region="토론토")
        self.assertEqual(client.hidden, ["30033"])

    def test_turning_usage_off_removes_the_rule_from_the_loaded_book(self):
        from sns_comment.rulebook_source import build_rulebook
        rows = []
        for index, rule in enumerate(load_rulebook()["rules"], start=1):
            rows.append({"이름": rule["name"], "코드": rule["id"],
                         "결과": {"삭제": "숨김", "사람판단": "사람 판단", "유지": "유지", "폐지": "폐지"}[rule["label"]],
                         "순서": index, "설명": rule["definition"], "예외": rule["exceptions"], "사용": True})
        rows.append({"이름": "한 게시물 2개", "코드": "D12", "결과": "숨김", "순서": 11.5,
                     "설명": "3개째부터 숨김", "예외": "사람 유지는 숨기지 않습니다.", "사용": False})
        self.assertFalse(author_cap_enabled(build_rulebook(rows, "2026-11-17T00:00:00Z")))
        rows[-1]["사용"] = True
        enabled = build_rulebook(rows, "2026-11-17T00:00:00Z")
        self.assertTrue(author_cap_enabled(enabled))
        self.assertIn("D12", enabled["hide_rule_ids"])


def page(items, after=None):
    payload = {"data": items}
    if after:
        payload["paging"] = {"next": "https://graph.facebook.com/v26.0/x?after=" + after}
    return payload


class SweepGraph:
    def __init__(self, limit_at=None):
        self.hidden, self.gets, self.limit_at = [], 0, limit_at
        self.routes = {
            ACCOUNT + "/media|": page([{"id": "20001", "permalink": "https://www.instagram.com/p/abc/"}]),
            "20001/comments|": page([
                {"id": "30001", "text": "하나", "timestamp": "2026-11-17T00:00:00+0000", "username": "fan",
                 "from": {"id": "101"}},
                {"id": "30002", "text": "둘", "timestamp": "2026-11-17T01:00:00+0000", "username": "fan",
                 "from": {"id": "101"}},
                {"id": "30003", "text": "셋", "timestamp": "2026-11-17T02:00:00+0000", "username": "fan",
                 "from": {"id": "101"}},
            ]),
            "30001/replies|": page([
                {"id": "30004", "text": "답", "timestamp": "2026-11-17T03:00:00+0000", "username": "fan",
                 "from": {"id": "101"}},
            ]),
        }

    def get(self, path, **params):
        self.gets += 1
        if self.limit_at and self.gets >= self.limit_at:
            raise GraphError(429, 4)
        if params.get("fields") == "hidden":
            return {"hidden": path in self.hidden}
        return self.routes.get(path + "|" + str(params.get("after") or ""), page([]))

    def hide_comment(self, comment_id):
        self.hidden.append(comment_id)
        return {"success": True}


class SweepTests(unittest.TestCase):
    def config(self):
        return {"account_id": ACCOUNT, "expected_username": "waterbomb_toronto_official", "region_label": "토론토"}

    def test_dry_run_counts_and_apply_hides_without_a_second_row(self):
        state, sheet = MemoryState(), FakeSheets()
        preview = sweep_account_cap(self.config(), state, SweepGraph(), {"moderation": {}}, sheet, cap_book(), False)
        self.assertEqual(preview["would_hide"], 2)
        self.assertEqual(preview["within_limit"], 2)
        self.assertEqual(preview["hidden"], 0)
        self.assertEqual(sheet.updates, [])
        graph = SweepGraph()
        state = MemoryState()
        applied = sweep_account_cap(self.config(), state, graph, {"moderation": {}}, sheet, cap_book(), True)
        self.assertEqual(applied["hidden"], 2)
        self.assertEqual(set(graph.hidden), {"30003", "30004"})
        rows = [row for row in sheet.data(HIDE_TAB) if row[6] in {"30003", "30004"}]
        self.assertEqual(sorted(row[6] for row in rows), ["30003", "30004"])
        self.assertTrue(all(row[8] == "D12" and row[11] == "숨김 완료" for row in rows))
        again = sweep_account_cap(self.config(), state, SweepGraph(), {"moderation": {}}, sheet, cap_book(), True)
        self.assertEqual(again["scanned"], 4)
        self.assertEqual(again["status"], "complete")
        self.assertEqual(len([row for row in sheet.data(HIDE_TAB) if row[6] in {"30003", "30004"}]), 2)

    def test_a_rate_limit_resumes_without_hiding_twice(self):
        state, sheet = MemoryState(), FakeSheets()
        paused = sweep_account_cap(self.config(), state, SweepGraph(limit_at=3), {"moderation": {}}, sheet, cap_book(), True)
        self.assertEqual(paused["status"], "rate_limited")
        self.assertEqual(paused["hidden"], 0)
        finished = sweep_account_cap(self.config(), state, SweepGraph(), {"moderation": {}}, sheet, cap_book(), True)
        self.assertEqual(finished["status"], "complete")
        self.assertEqual(finished["hidden"], 2)
        self.assertEqual(len([row for row in sheet.data(HIDE_TAB) if row[8] == "D12"]), 2)
        logged = json.dumps(finished, ensure_ascii=False)
        self.assertIn("waterbomb_toronto_official", logged)


if __name__ == "__main__":
    unittest.main()
