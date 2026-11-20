"""Past-comment sweep: dry-run unless apply, checkpoint, and the moderation lease."""

import contextlib
import copy
import datetime as dt
import io
import json
import os
import time
import unittest
from unittest import mock

from sns_comment.api import GraphError
from sns_comment.author_cap import remember_author_posts
from sns_comment.cloud_state import MODERATION_LEASE, CloudError
from sns_comment.moderation import decision_name, load_rulebook
from sns_comment.moderation_sheet import HIDE_TAB
from sns_comment.past_sweep import (
    CHECKPOINT, city_of, guarded_run, main, parse_rule_ids, run_chunk, select_sweep_account,
)
from tests.test_author_cap import speak
from tests.test_moderation import ACCOUNT, FakeSheets, MemoryState


TOKEN = "SHOULD-NOT-LOG-TOKEN"
SEOUL = "17841401245057628"
NOW = dt.datetime(2026, 11, 20, 4, 10, 0, tzinfo=dt.timezone.utc)


def book():
    loaded = copy.deepcopy(load_rulebook())
    loaded["conditions"] = [
        {"id": "D14", "label": "삭제", "kind": "keyword", "threshold": None, "scope": "post",
         "targets": ["금지어"], "order": 1, "reason": "금지어"},
        {"id": "D12", "label": "삭제", "kind": "author_count", "threshold": 2, "scope": "post",
         "targets": [], "order": 12, "reason": "한 게시물에 같은 작성자의 3번째 댓글입니다."},
    ]
    return loaded


def toronto():
    return {"account_id": ACCOUNT, "expected_username": "waterbomb_toronto_official", "region_label": "토론토"}


def seoul():
    return {"account_id": SEOUL, "expected_username": "waterbomb_official", "region_label": "대표(공식)"}


def busan():
    return {"account_id": "17841411527874641", "expected_username": "waterbomb_busan_official", "region_label": "부산"}


def sokcho():
    return {"account_id": "17841455831114060", "expected_username": "waterbomb_sokcho_official", "region_label": "속초"}


def fans(count=4):
    return [speak(str(50000 + index), "2026-11-17T00:0" + str(index) + ":00:00+00:00",
                  text="글 " + str(index), username="fan", author_id="101")
            for index in range(count)]


class State(MemoryState):
    def __init__(self, busy=False):
        super().__init__()
        self.busy = busy
        self.leases = []
        self.held = 0
        self.data["web/registry.json"] = {"schema": 1, "accounts": {}, "groups": {}}

    @contextlib.contextmanager
    def lease(self, seconds=3900, name="state/lease.json"):
        if self.busy:
            raise CloudError("다른 클라우드 실행이 진행 중입니다")
        self.leases.append((seconds, name))
        self.held += 1
        try:
            yield
        finally:
            self.held -= 1


class HideClient:
    def __init__(self, fail_times=0):
        self.hidden = []
        self.fail_times = fail_times
        self.token = TOKEN

    def hide_comment(self, comment_id):
        if self.fail_times:
            self.fail_times -= 1
            raise GraphError(429, 4)
        self.hidden.append(comment_id)
        return {"success": True}

    def get(self, path, **_params):
        return {"hidden": True}

    def delete_comment(self, comment_id):
        raise AssertionError(comment_id)

    def unhide_comment(self, comment_id):
        raise AssertionError(comment_id)


def checkpoint(state):
    saved, _generation = state.json_get(CHECKPOINT.format(account=ACCOUNT))
    return saved


class SweepBehaviorTests(unittest.TestCase):
    def test_dry_run_does_not_hide_or_replace_an_apply_checkpoint(self):
        state, client, sheet = State(), HideClient(), FakeSheets()
        remember_author_posts(state, fans(4))
        preview = run_chunk(state, client, sheet, book(), toronto(), ["D12"], False, time.monotonic() + 30)
        self.assertEqual(preview["status"], "dry_run")
        self.assertEqual(preview["would_hide"], 2)
        self.assertEqual(preview["hidden"], 0)
        self.assertEqual(client.hidden, [])
        self.assertIsNone(checkpoint(state))
        self.assertEqual(sheet.updates, [])
        run_chunk(state, client, sheet, book(), toronto(), ["D12"], True, time.monotonic() + 30, limit=1)
        self.assertEqual(client.hidden, ["50002"])
        saved = checkpoint(state)
        self.assertEqual([item["id"] for item in saved["remaining"]], ["50003"])
        run_chunk(state, client, sheet, book(), toronto(), ["D12"], False, time.monotonic() + 30)
        self.assertEqual([item["id"] for item in checkpoint(state)["remaining"]], ["50003"])
        self.assertEqual(client.hidden, ["50002"])

    def test_a_pause_resumes_the_next_hide_once_and_logs_the_sheet(self):
        state, client, sheet = State(), HideClient(), FakeSheets()
        remember_author_posts(state, fans(4))
        paused = run_chunk(state, client, sheet, book(), toronto(), ["D12"], True, time.monotonic() + 30, limit=1)
        self.assertEqual(paused["status"], "paused")
        self.assertEqual(paused["hidden"], 1)
        self.assertFalse(paused["done"])
        self.assertEqual([item["id"] for item in checkpoint(state)["remaining"]], ["50003"])
        finished = run_chunk(state, client, sheet, book(), toronto(), ["D12"], True, time.monotonic() + 30)
        self.assertEqual(finished["status"], "complete")
        self.assertTrue(finished["done"])
        self.assertEqual(client.hidden, ["50002", "50003"])
        rows = [row for row in sheet.data(HIDE_TAB) if row[6] in {"50002", "50003"}]
        self.assertEqual(sorted(row[6] for row in rows), ["50002", "50003"])
        self.assertTrue(all(row[8] == "D12" and row[11] == "숨김 완료" and row[1] == "토론토 댓글" for row in rows))
        self.assertTrue(any(row[6] == "99999" for row in sheet.data(HIDE_TAB)))

    def test_a_rate_limit_keeps_the_comment_for_the_next_run(self):
        state, sheet = State(), FakeSheets()
        remember_author_posts(state, fans(3))
        paused = run_chunk(state, HideClient(fail_times=1), sheet, book(), toronto(), ["D12"], True, time.monotonic() + 30)
        self.assertEqual(paused["status"], "rate_limited")
        self.assertEqual(paused["hidden"], 0)
        self.assertEqual([item["id"] for item in checkpoint(state)["remaining"]], ["50002"])
        graph = HideClient()
        finished = run_chunk(state, graph, sheet, book(), toronto(), ["D12"], True, time.monotonic() + 30)
        self.assertEqual(finished["status"], "complete")
        self.assertEqual(graph.hidden, ["50002"])
        self.assertEqual(len([row for row in sheet.data(HIDE_TAB) if row[6] == "50002"]), 1)

    def test_a_human_keep_added_after_the_checkpoint_is_not_hidden(self):
        state, client, sheet = State(), HideClient(), FakeSheets()
        remember_author_posts(state, fans(4))
        run_chunk(state, client, sheet, book(), toronto(), ["D12"], True, time.monotonic() + 30, limit=1)
        state.json_put(decision_name(ACCOUNT, "50003"), {
            "comment_id": "50003", "account_id": ACCOUNT, "status": "kept", "human_decision": "유지",
            "label": "유지", "text": "글 3", "written_at": "2026-11-17T00:03:00+00:00",
        }, 0)
        finished = run_chunk(state, client, sheet, book(), toronto(), ["D12"], True, time.monotonic() + 30)
        self.assertEqual(client.hidden, ["50002"])
        self.assertEqual(finished["human_kept"], 1)
        self.assertTrue(finished["done"])

    def test_official_comments_and_unselected_rules_are_not_hidden(self):
        state, client, sheet = State(), HideClient(), FakeSheets()
        official = [speak(str(51000 + index), "2026-11-17T01:0" + str(index) + ":00:00+00:00",
                          text="공식 " + str(index), username="waterbomb_toronto_official", author_id="")
                    for index in range(3)]
        remember_author_posts(state, official)
        state.json_put("moderation/polling/" + ACCOUNT + ".json", {"queue": {
            "51010": {"id": "51010", "account_id": ACCOUNT, "text": "금지어 있어요", "is_owner": 1,
                      "username": "fan", "written_at": "2026-11-17T02:00:00+00:00", "media_id": "20001"},
            "51011": {"id": "51011", "account_id": ACCOUNT, "text": "금지어 있어요", "is_owner": 0,
                      "username": "fan", "written_at": "2026-11-17T02:01:00+00:00", "media_id": "20001"},
        }}, 0)
        skipped = run_chunk(state, client, sheet, book(), toronto(), ["D12"], True, time.monotonic() + 30)
        self.assertEqual(skipped["official_skipped"], 4)
        self.assertEqual(skipped["would_hide"], 0)
        self.assertEqual(client.hidden, [])
        keyword = run_chunk(state, client, sheet, book(), toronto(), ["D14"], True, time.monotonic() + 30)
        self.assertEqual(client.hidden, ["51011"])
        self.assertEqual(keyword["official_skipped"], 4)
        self.assertEqual([row[6] for row in sheet.data(HIDE_TAB) if row[8] == "D14"], ["51011"])

    def test_sheet_failure_stays_queued_until_the_next_run(self):
        state, client, sheet = State(), HideClient(), FakeSheets()
        remember_author_posts(state, fans(3))
        sheet.fail = True
        first = run_chunk(state, client, sheet, book(), toronto(), ["D12"], True, time.monotonic() + 30)
        self.assertEqual(first["hidden"], 1)
        self.assertFalse(any(row[6] == "50002" for row in sheet.data(HIDE_TAB)))
        pending, _generation = state.json_get("moderation/sheet-pending.json", {"items": {}})
        self.assertIn(ACCOUNT + ":50002", pending["items"])
        sheet.fail = False
        again = run_chunk(state, client, sheet, book(), toronto(), ["D12"], True, time.monotonic() + 30)
        self.assertEqual(again["hidden"], 0)
        self.assertEqual(client.hidden, ["50002"])
        self.assertEqual(len([row for row in sheet.data(HIDE_TAB) if row[6] == "50002"]), 1)

    def test_the_lease_is_released_and_a_busy_lock_or_the_half_hour_yields(self):
        state, client = State(), HideClient()
        result = guarded_run(state, client, FakeSheets(), book(), toronto(), ["D12"], True, now=NOW)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(state.held, 0)
        self.assertEqual(state.leases[0][1], MODERATION_LEASE)
        self.assertLess(state.leases[0][0], 10 * 60)
        busy = State(busy=True)
        skipped = guarded_run(busy, client, FakeSheets(), book(), toronto(), ["D12"], True, now=NOW)
        self.assertEqual(skipped["status"], "skipped")
        self.assertEqual(busy.leases, [])
        near = dt.datetime(2026, 11, 20, 4, 29, 0, tzinfo=dt.timezone.utc)
        yielded = guarded_run(State(), client, FakeSheets(), book(), toronto(), ["D12"], True, now=near)
        self.assertEqual(yielded["status"], "yielded")
        self.assertEqual(client.hidden, [])


class EntrypointTests(unittest.TestCase):
    def run_main(self, argv, state, client, sheet, rules_book=None):
        stream = io.StringIO()
        settings = {"expected_username": "waterbomb_official", "accounts": {},
                    "moderation": {"rulebook": {"source": "file"}, "mode": "auto_hide"},
                    "secret": TOKEN}

        def clients(_settings, _state):
            return {"토론토 댓글": client, "서울 댓글": client}

        with mock.patch.dict(os.environ, {"SNS_STATE_BUCKET": "bucket"}, clear=False), \
             mock.patch("sys.stdout", stream), \
             mock.patch("sns_comment.past_sweep.seconds_until_slot", return_value=900), \
             mock.patch("sns_comment.past_sweep.read_settings", return_value=settings), \
             mock.patch("sns_comment.past_sweep.CloudState", return_value=state), \
             mock.patch("sns_comment.past_sweep.runtime_configs", return_value=[seoul(), busan(), sokcho(), toronto()]), \
             mock.patch("sns_comment.past_sweep.resolve_rulebook", return_value=rules_book or book()), \
             mock.patch("sns_comment.past_sweep.account_clients", side_effect=clients) as opened, \
             mock.patch("sns_comment.past_sweep.open_sheet", return_value=sheet):
            code = main(argv)
        return code, stream.getvalue(), opened

    def test_the_default_is_a_dry_run_and_apply_is_explicit(self):
        state, client, sheet = State(), HideClient(), FakeSheets()
        remember_author_posts(state, fans(3))
        code, output, opened = self.run_main(["--account", "토론토", "--rules", "D12"], state, client, sheet)
        self.assertEqual(code, 0)
        self.assertEqual(client.hidden, [])
        self.assertIn("미리보기입니다", output)
        self.assertNotIn(TOKEN, output)
        opened.assert_not_called()
        with mock.patch.dict(os.environ, {"SNS_MODERATION_HIDE": "1", "SNS_PAST_SWEEP_APPLY": "1"}, clear=False):
            again, text, _opened = self.run_main(["--account", "toronto"], state, client, sheet)
        self.assertEqual(again, 0)
        self.assertEqual(client.hidden, [])
        self.assertIn("미리보기입니다", text)
        self.assertNotIn("--apply", text)
        code, output, _opened = self.run_main(["--account", "토론토", "--rules", "D12", "--apply"], state, client, sheet)
        self.assertEqual(code, 0)
        self.assertEqual(client.hidden, ["50002"])
        self.assertIn("숨김 완료", json.dumps(sheet.data(HIDE_TAB), ensure_ascii=False))
        self.assertNotIn(TOKEN, output)
        self.assertNotIn(TOKEN, json.dumps(checkpoint(state), ensure_ascii=False))

    def test_env_apply_selects_one_city_and_a_missing_account_hides_nothing(self):
        state, client, sheet = State(), HideClient(), FakeSheets()
        remember_author_posts(state, fans(3))
        self.assertEqual(parse_rule_ids("d11+D12, D14"), ["D11", "D12", "D14"])
        self.assertIsNone(parse_rule_ids("전체"))
        self.assertEqual(select_sweep_account([seoul(), busan(), sokcho(), toronto()], "서울"), [seoul()])
        self.assertEqual(select_sweep_account([seoul(), busan(), sokcho(), toronto()], "부산"), [busan()])
        self.assertEqual(select_sweep_account([seoul(), busan(), sokcho(), toronto()], "속초"), [sokcho()])
        self.assertEqual(city_of(seoul()), "seoul")
        with mock.patch.dict(os.environ, {"SNS_PAST_SWEEP_ACCOUNT": "토론토", "SNS_PAST_SWEEP_RULES": "D12",
                                           "SNS_PAST_SWEEP_APPLY": "apply"}, clear=False):
            code, output, _opened = self.run_main([], state, client, sheet)
        self.assertEqual(code, 0)
        self.assertEqual(client.hidden, ["50002"])
        self.assertNotIn(TOKEN, output)
        missing, text, opened = self.run_main(["--account", "없는계정", "--apply"], State(), HideClient(), sheet)
        self.assertEqual(missing, 0)
        self.assertIn("찾지 못해", text)
        opened.assert_not_called()
        empty, text, opened = self.run_main([], State(), HideClient(), sheet)
        self.assertEqual(empty, 0)
        self.assertIn("계정을 지정", text)
        opened.assert_not_called()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["--backfill-sheet"])

    def test_a_busy_lease_returns_success_without_hiding(self):
        state, client, sheet = State(busy=True), HideClient(), FakeSheets()
        remember_author_posts(state, fans(3))
        code, output, _opened = self.run_main(["--account", "토론토", "--rules", "D12", "--apply"], state, client, sheet)
        self.assertEqual(code, 0)
        self.assertEqual(client.hidden, [])
        self.assertIn("skipped: lease busy", output)
        self.assertEqual(state.held, 0)
        self.assertNotIn(TOKEN, output)

    def test_the_doc_uses_update_env_vars_and_names_the_console_button(self):
        text = (os.path.join(os.path.dirname(__file__), "..", "CLOUD.md"))
        with open(text, encoding="utf-8") as handle:
            body = handle.read().split("## 과거 댓글 점검", 1)[1]
        self.assertIn("sns-comments-past-sweep", body)
        self.assertIn("--update-env-vars", body)
        self.assertNotIn("--set-env-vars", body)
        self.assertNotIn("backfill", body.casefold())
        self.assertIn("실행", body)
        self.assertIn("SNS_PAST_SWEEP_APPLY=apply", body)


if __name__ == "__main__":
    unittest.main()
