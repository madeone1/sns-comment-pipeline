"""Notion rulebook parsing, validation, and fallback. No live Notion calls."""

import copy
import io
import json
import unittest
from unittest import mock

from sns_comment.api import PilotError
from sns_comment.moderation import load_rulebook, moderate, policy_sha
from sns_comment.moderation_sheet import _note
from sns_comment.rulebook_source import RulebookError, build_rulebook, resolve_rulebook


ACCOUNT = "17841454752492067"
EDITED = "2026-11-17T01:00:00.000Z"
RESULT = {"삭제": "숨김", "사람판단": "사람 판단", "유지": "유지", "폐지": "폐지"}


class MemoryState:
    def __init__(self):
        self.data, self.generations, self.counter = {}, {}, 0

    def json_get(self, name, default=None):
        return copy.deepcopy(self.data.get(name, default)), self.generations.get(name, 0)

    def json_put(self, name, value, generation=None):
        self.counter += 1
        self.data[name], self.generations[name] = copy.deepcopy(value), self.counter
        return self.counter

    def list_objects(self, prefix, limit=100):
        names = sorted(name for name in self.data if name.startswith(prefix))
        return [{"name": name} for name in names[:limit]], len(names) > limit


class Notion:
    def __init__(self, rows, edited=EDITED, error=None):
        self.rows, self.edited, self.error, self.calls = rows, edited, error, []

    def call(self, suffix, arguments):
        self.calls.append(suffix)
        if self.error:
            raise self.error
        if suffix == "fetch":
            return {"last_edited_time": self.edited, "id": arguments["id"]}
        if suffix == "query-data-sources":
            return {"results": self.rows}
        raise AssertionError(suffix)


def rows_from_file():
    book = load_rulebook()
    return [{
        "이름": rule["name"], "코드": rule["id"], "결과": RESULT[rule["label"]], "순서": index,
        "설명": rule["definition"], "예외": rule["exceptions"], "사용": True,
    } for index, rule in enumerate(book["rules"], start=1)]


class ParseTests(unittest.TestCase):
    def test_current_table_matches_the_bundled_book(self):
        built = build_rulebook(rows_from_file(), EDITED)
        bundled = load_rulebook()
        self.assertEqual(built["rules"], bundled["rules"])
        self.assertEqual(built["hide_rule_ids"], bundled["hide_rule_ids"])
        self.assertEqual(built["abolished"], bundled["abolished"])
        self.assertEqual(policy_sha(built), policy_sha(bundled))
        self.assertEqual(built["policy_ref"], "notion:" + EDITED + ":" + policy_sha(bundled)[:12])

    def test_rows_follow_order_and_unused_or_abolished_rows_are_not_active_rules(self):
        rows = rows_from_file()
        rows[0]["순서"] = 50
        rows.append({"이름": "초안", "코드": "D99", "결과": "숨김", "순서": 1, "설명": "쓰지 않음", "예외": "", "사용": False})
        built = build_rulebook(rows, EDITED)
        self.assertEqual(built["rules"][-1]["id"], "D1")
        self.assertNotIn("D99", [rule["id"] for rule in built["rules"]])
        self.assertIn("H2", built["abolished"])
        self.assertNotIn("H2", built["hide_rule_ids"])

    def test_validation_failures(self):
        missing = rows_from_file()
        del missing[0]["설명"]
        with self.assertRaises(RulebookError):
            build_rulebook(missing, EDITED)
        unknown = rows_from_file()
        unknown[0]["결과"] = "보류"
        with self.assertRaises(RulebookError):
            build_rulebook(unknown, EDITED)
        duplicate = rows_from_file()
        duplicate[1]["코드"] = "D1"
        with self.assertRaises(RulebookError):
            build_rulebook(duplicate, EDITED)
        blank = rows_from_file()
        blank[0]["설명"] = "  "
        with self.assertRaises(RulebookError):
            build_rulebook(blank, EDITED)
        keep_only = [row for row in rows_from_file() if row["코드"].startswith("K")]
        with self.assertRaises(RulebookError):
            build_rulebook(keep_only, EDITED)

    def test_a_decision_and_sheet_note_record_the_notion_version(self):
        book = build_rulebook(rows_from_file(), EDITED)
        state = MemoryState()

        class Model:
            def classify_comments(self, instruction, comments):
                return [{"id": comments[0]["id"], "label": "유지", "rule_id": "K1", "confidence": "high", "reason": "응원"}], {
                    "calls": 1, "prompt_tokens": 1, "output_tokens": 1, "total_tokens": 2}

        comment = {"id": "30001", "account_id": ACCOUNT, "media_id": "20001", "text": "안녕하세요",
                   "is_owner": 0, "written_at": "2026-11-17T01:00:00+00:00", "username": "fan"}
        moderate([comment], state, {"moderation": {"mode": "dry_run"}}, Model(), book=book,
                 account_username="waterbomb_toronto_official", region="토론토")
        stored = next(value for name, value in state.data.items() if name.endswith("/30001.json"))
        self.assertEqual(stored["policy_version"], "v2.3")
        self.assertEqual(stored["policy_sha"], policy_sha(book))
        self.assertEqual(stored["policy_ref"], book["policy_ref"])
        self.assertIn("기준 " + book["policy_ref"], _note(stored, False))


class FallbackTests(unittest.TestCase):
    def _logs(self, settings, state, client):
        stream = io.StringIO()
        with mock.patch("sys.stdout", stream):
            book = resolve_rulebook(settings, state, client)
        events = [json.loads(line) for line in stream.getvalue().splitlines()]
        return book, events

    def test_a_good_fetch_is_cached_and_a_later_failure_reuses_it(self):
        state = MemoryState()
        settings = {"moderation": {"rulebook": {"source": "notion"}}}
        book, events = self._logs(settings, state, Notion(rows_from_file()))
        self.assertEqual(events[-1]["source"], "notion")
        self.assertEqual(events[-1]["severity"] if "severity" in events[-1] else "INFO", "INFO")
        self.assertIn("notion:", book["policy_ref"])
        failed = Notion(rows_from_file(), error=PilotError("Notion MCP HTTP 404. 연결·권한 확인 필요."))
        again, warnings = self._logs(settings, state, failed)
        self.assertEqual(again["policy_ref"], book["policy_ref"])
        self.assertEqual(warnings[-1]["severity"], "WARNING")
        self.assertEqual(warnings[-1]["source"], "cache")
        self.assertEqual(failed.calls, ["fetch"])

    def test_without_a_cache_the_git_file_is_the_fallback(self):
        settings = {"moderation": {"rulebook": {"source": "notion"}}}
        book, events = self._logs(settings, MemoryState(), Notion([], error=PilotError("Notion 연결이 없습니다.")))
        self.assertTrue(book["policy_ref"].startswith("file:v2.3:"))
        self.assertEqual(policy_sha(book), policy_sha(load_rulebook()))
        self.assertEqual(events[-1]["severity"], "WARNING")
        self.assertEqual(events[-1]["source"], "file")
        self.assertIn("저장소 파일", events[-1]["message"])

    def test_file_source_does_not_call_notion(self):
        client = Notion(rows_from_file(), error=AssertionError("file mode must not call Notion"))
        book, events = self._logs({"moderation": {"rulebook": {"source": "file"}}}, MemoryState(), client)
        self.assertEqual(client.calls, [])
        self.assertTrue(book["policy_ref"].startswith("file:v2.3:"))
        self.assertEqual(events[-1]["source"], "file")
        self.assertNotIn("severity", events[-1])


if __name__ == "__main__":
    unittest.main()
