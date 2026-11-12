"""Rule order, dry-run, idempotency, and the hide flag. No live Meta or GCP calls."""

import copy
import datetime as dt
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from sns_comment.api import GraphClient, GraphError, PilotError
from sns_comment.moderation import (decision_name, finalize, guard, hannam_slur, hiding_enabled,
                                    load_rulebook, moderate, prompt_for, recent_rows)
from sns_comment.moderation_job import sweep_account
from sns_comment.profiles import read_settings
from sns_comment.storage import Store


ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = "17841454752492067"
SEOUL = "17841401245057628"
DRY = {"moderation": {"policy_version": "v2.3", "mode": "dry_run", "hide_enabled": False, "batch_size": 20}}
AUTO = {"moderation": {"policy_version": "v2.3", "mode": "auto_hide", "hide_enabled": False}}
AUTO_ON = {"moderation": {"policy_version": "v2.3", "mode": "auto_hide", "hide_enabled": True}}


class MemoryState:
    def __init__(self):
        self.data, self.generations, self.counter = {}, {}, 0

    def json_get(self, name, default=None):
        return copy.deepcopy(self.data.get(name, default)), self.generations.get(name, 0)

    def json_put(self, name, value, generation=None):
        if generation is not None and generation != self.generations.get(name, 0):
            from sns_comment.cloud_state import CloudError
            raise CloudError("synthetic conflict", 412)
        self.counter += 1
        self.data[name], self.generations[name] = copy.deepcopy(value), self.counter
        return self.counter

    def list_objects(self, prefix, limit=100):
        names = sorted(name for name in self.data if name.startswith(prefix))
        return [{"name": name, "generation": self.generations[name]} for name in names[:limit]], len(names) > limit


class Model:
    def __init__(self, answers):
        self.answers = answers
        self.seen = []

    def classify_comments(self, instruction, comments):
        self.seen.extend(comment["id"] for comment in comments)
        return [self.answers[comment["id"]] for comment in comments], {
            "calls": 1, "prompt_tokens": 12, "output_tokens": 4, "total_tokens": 16}


class Hides:
    def __init__(self, error=None):
        self.hidden = []
        self.error = error

    def hide_comment(self, comment_id):
        if self.error:
            raise self.error
        self.hidden.append(comment_id)
        return {"success": True}


def audience(text, comment_id="30001", account=ACCOUNT, owner=0, written="2026-11-12T06:10:00+00:00", media="20001"):
    return {"id": comment_id, "account_id": account, "media_id": media, "text": text, "is_owner": owner,
            "written_at": written, "username": "fan"}


def answer(label, rule_id, confidence="high", reason="모델 판정"):
    return {"id": None, "label": label, "rule_id": rule_id, "confidence": confidence, "reason": reason}


class RuleOrderTests(unittest.TestCase):
    def test_prompt_follows_stage_then_delete_then_human_then_keep(self):
        book = load_rulebook()
        self.assertEqual(book["version"], "v2.3")
        self.assertIn("H2", book["abolished"])
        text = prompt_for(book)
        self.assertLess(text.index("0단계"), text.index("D1"))
        self.assertLess(text.index("D1"), text.index("H1"))
        self.assertLess(text.index("H1"), text.index("K1"))
        self.assertIn("폐지", text)
        self.assertEqual(book["labels"]["삭제"], "숨김")

    def test_official_empty_and_slur_rules_run_before_the_model(self):
        self.assertFalse(hannam_slur("제일 섹시한남자"))
        self.assertFalse(hannam_slur("한남자"))
        self.assertTrue(hannam_slur("한남들아 그만"))
        self.assertEqual(guard(audience("한남충", owner=1))["rule_id"], "K3")
        self.assertEqual(guard(audience("  "))["rule_id"], "K10")
        self.assertEqual(guard(audience(None))["rule_id"], "K10")
        self.assertEqual(guard(audience("한남들아 그만"))["rule_id"], "D7")
        self.assertEqual(guard({"id": "1", "text": "누구야", "is_owner": None})["rule_id"], "H1")
        self.assertIsNone(guard(audience("티켓 언제 예매해요?")))
        self.assertEqual(guard(audience("🔥🔥"))["rule_id"], "K1")

    def test_low_confidence_and_abolished_rules_stay_with_a_person(self):
        book = load_rulebook()
        self.assertEqual(finalize({"label": "숨김", "rule_id": "D3", "confidence": "low", "reason": "욕"}, book)["label"], "사람판단")
        self.assertEqual(finalize({"label": "삭제", "rule_id": "D6", "confidence": "high", "reason": "범죄 단정"}, book)["label"], "숨김")
        self.assertEqual(finalize({"label": "사람판단", "rule_id": "H2", "confidence": "high", "reason": "의상"}, book)["rule_id"], "H1")
        self.assertEqual(finalize({"label": "숨김", "rule_id": "K1", "confidence": "high", "reason": "칭찬"}, book)["label"], "사람판단")

    def test_repeat_after_a_keep_is_d11_and_is_not_sent_to_the_model(self):
        state, model, client = MemoryState(), Model({}), Hides()
        first = audience("같은 문장입니다", "30011", written="2026-11-12T06:00:00+00:00")
        second = audience("같은 문장입니다", "30012", written="2026-11-12T06:05:00+00:00")
        model.answers["30011"] = {**answer("유지", "K1"), "id": "30011"}
        result = moderate([second, first], state, DRY, model, client=client, account_username="waterbomb_toronto_official", region="토론토")
        self.assertEqual(model.seen, ["30011"])
        self.assertEqual(result["would_hide"], 1)
        self.assertEqual(result["kept"], 1)
        self.assertEqual(client.hidden, [])
        stored = state.data[decision_name(ACCOUNT, "30012")]
        self.assertEqual(stored["rule_id"], "D11")
        self.assertEqual(stored["status"], "would_hide")
        self.assertEqual(stored["region"], "토론토")


class HideGateTests(unittest.TestCase):
    def setUp(self):
        self.state = MemoryState()
        self.model = Model({"30001": {**answer("숨김", "D3"), "id": "30001"}})
        self.client = Hides()

    def test_dry_run_records_would_hide_and_never_calls_hide(self):
        with mock.patch.dict("os.environ", {"SNS_MODERATION_HIDE": "1"}, clear=False):
            result = moderate([audience("심한 욕")], self.state, DRY, self.model, client=self.client,
                              account_username="waterbomb_toronto_official")
        self.assertEqual(result["would_hide"], 1)
        self.assertEqual(result["hidden"], 0)
        self.assertEqual(self.client.hidden, [])
        record = self.state.data[decision_name(ACCOUNT, "30001")]
        self.assertEqual(record["status"], "would_hide")
        self.assertEqual(record["rule_id"], "D3")
        usage = next(value for name, value in self.state.data.items() if name.startswith("moderation/usage/"))
        self.assertEqual(usage["total_tokens"], 16)
        self.assertGreater(usage["calls"], 0)

    def test_auto_hide_without_the_flag_or_env_does_not_call_hide(self):
        with mock.patch.dict("os.environ", {"SNS_MODERATION_HIDE": ""}, clear=False):
            result = moderate([audience("심한 욕")], self.state, AUTO, self.model, client=self.client,
                              account_username="waterbomb_toronto_official")
        self.assertFalse(hiding_enabled(AUTO))
        self.assertEqual(result["would_hide"], 1)
        self.assertEqual(self.client.hidden, [])

    def test_flag_or_env_hides_once_and_a_second_run_does_not_hide_again(self):
        moderate([audience("심한 욕")], self.state, DRY, self.model, client=self.client, account_username="waterbomb_official")
        seen = list(self.model.seen)
        with mock.patch.dict("os.environ", {"SNS_MODERATION_HIDE": "1"}, clear=False):
            self.assertTrue(hiding_enabled(AUTO))
            again = moderate([audience("심한 욕")], self.state, AUTO, self.model, client=self.client,
                             account_username="waterbomb_official")
        self.assertEqual(again["hidden"], 1)
        self.assertEqual(self.client.hidden, ["30001"])
        self.assertEqual(self.model.seen, seen)
        third = moderate([audience("심한 욕")], self.state, AUTO_ON, self.model, client=self.client,
                         account_username="waterbomb_official")
        self.assertEqual(third["skipped"], 1)
        self.assertEqual(self.client.hidden, ["30001"])
        self.assertEqual(self.state.data[decision_name(ACCOUNT, "30001")]["status"], "hidden")

    def test_a_human_keep_is_not_hidden_or_reclassified(self):
        self.state.json_put(decision_name(ACCOUNT, "30001"), {
            "human_decision": "유지", "label": "유지", "status": "kept", "source": "human_sheet"}, 0)
        result = moderate([audience("한남들아")], self.state, AUTO_ON, self.model, client=self.client,
                          account_username="waterbomb_toronto_official")
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(self.client.hidden, [])
        self.assertEqual(self.model.seen, [])
        self.assertEqual(self.state.data[decision_name(ACCOUNT, "30001")]["human_decision"], "유지")

    def test_rate_limit_stops_later_hides_in_the_same_run(self):
        self.model.answers["30002"] = {**answer("숨김", "D2"), "id": "30002"}
        client = Hides(GraphError(429, 4))
        result = moderate([audience("첫번째", "30001"), audience("두번째", "30002")], self.state, AUTO_ON, self.model, client=client,
                          account_username="waterbomb_official")
        self.assertEqual(result["status"], "rate_limited")
        self.assertEqual(result["hide_failed"], 1)
        self.assertEqual(result["would_hide"], 1)
        self.assertEqual(client.hidden, [])


class GraphHideTests(unittest.TestCase):
    def test_hide_is_a_post_and_cannot_delete_or_unhide(self):
        opener = mock.Mock()
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"success": true}'
        opener.open.return_value = response
        client = GraphClient("synthetic-token", "v26.0", opener=opener)
        self.assertEqual(client.hide_comment("30001")["success"], True)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertIn("hide=true", request.full_url)
        self.assertNotIn("hide=false", request.full_url)
        self.assertNotIn("synthetic-token", request.full_url)
        self.assertNotEqual(request.get_method(), "DELETE")
        self.assertFalse(hasattr(client, "delete_comment"))
        with self.assertRaises(PilotError):
            client._request("30001", {"hide": "false"}, "POST")
        self.assertEqual(opener.open.call_count, 1)


class SweepTests(unittest.TestCase):
    def test_new_account_without_a_webhook_is_polled_and_pending_is_left_in_place(self):
        state = MemoryState()
        receipt = "a" * 64
        pending = f"webhook/pending/{ACCOUNT}/{receipt}.json"
        state.json_put(pending, {"version": 1, "account_id": ACCOUNT, "media_id": "20001", "comment_id": "30001",
                                 "receipt": receipt, "received_at": "2026-11-12T06:58:00+00:00"}, 0)
        payloads = {
            "30001": {"id": "30001", "text": "티켓 언제 예매해요?", "timestamp": "2026-11-12T06:58:00+00:00", "username": "fan"},
            ACCOUNT + "/media": {"data": [{"id": "20009", "timestamp": "2026-11-12T01:00:00+00:00"}]},
            "20009/comments": {"data": [
                {"id": "30002", "text": "공장 OEM 연락처입니다", "timestamp": "2026-11-12T06:50:00+00:00", "username": "seller"},
                {"id": "30003", "text": "오래된 댓글", "timestamp": "2026-11-12T05:00:00+00:00", "username": "fan"},
                {"id": "30004", "text": "🔥", "timestamp": "2026-11-12T06:55:00+00:00", "username": "fan",
                 "replies": {"data": []}},
            ]},
        }

        class Api:
            def __init__(self):
                self.gets, self.hidden = [], []

            def get(self, path, **params):
                self.gets.append(path)
                return payloads[path]

            def hide_comment(self, comment_id):
                raise AssertionError("dry-run must not hide")

        model = Model({
            "30001": {**answer("유지", "K1", reason="예매 질문"), "id": "30001"},
            "30002": {**answer("숨김", "D2", reason="영업 연락처"), "id": "30002"},
        })
        client = Api()
        config = {"account_id": ACCOUNT, "expected_username": "waterbomb_toronto_official", "region_label": "토론토"}
        settings = {**DRY, "moderation": {**DRY["moderation"], "max_age_minutes": 20, "media_limit": 8}}
        result = sweep_account(config, state, client, model, settings, dt.datetime(2026, 11, 12, 7, 0, tzinfo=dt.timezone.utc))
        self.assertEqual(result["would_hide"], 1)
        self.assertEqual(result["kept"], 2)
        self.assertNotIn("30003", model.seen)
        self.assertIn("30002", model.seen)
        self.assertEqual(client.hidden, [])
        self.assertIn(pending, state.data)
        self.assertEqual(state.data[decision_name(ACCOUNT, "30002")]["status"], "would_hide")
        daily = next(value for name, value in state.data.items() if name.startswith("moderation/daily/"))
        self.assertIn(ACCOUNT + ":30002", daily["items"])
        self.assertEqual(daily["items"][ACCOUNT + ":30002"]["status_label"], "숨김 예정(시험)")

    def test_shipped_config_is_record_only_and_the_job_does_not_write_the_secret(self):
        settings = read_settings()
        self.assertEqual(settings["moderation"]["mode"], "dry_run")
        self.assertIs(settings["moderation"]["hide_enabled"], False)
        source = (ROOT / "sns_comment" / "moderation_job.py").read_text(encoding="utf-8")
        self.assertNotIn("addVersion", source)
        self.assertNotIn("hide=false", source)
        cloud = (ROOT / "sns_comment" / "cloud.py").read_text(encoding="utf-8")
        self.assertIn("moderation_daily_recorded", cloud)

    def test_daily_rows_for_a_new_account_are_classified_without_hiding(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "comments.sqlite3")
            try:
                store.db.execute("INSERT INTO media(account_id,id,permalink,raw_json) VALUES(?,?,?,?)",
                                 (SEOUL, "200", "https://www.instagram.com/p/abc/", "{}"))
                store.db.execute("""INSERT INTO comments(account_id,id,media_id,parent_id,author_id,username,is_owner,text,
                    written_at,first_seen_at,last_seen_at,payload_hash,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (SEOUL, "30021", "200", None, "9", "fan", 0, "줄이 너무 길어요", "2026-11-12T06:00:00+00:00",
                     "2026-11-12T06:01:00+00:00", "2026-11-12T06:01:00+00:00", "hash", "{}"))
                store.db.commit()
                rows = recent_rows(store, SEOUL, "2026-11-12T08:00:00+09:00", 100)
            finally:
                store.close()
        self.assertEqual([row["id"] for row in rows], ["30021"])
        model = Model({"30021": {**answer("유지", "K7", reason="현장 불만"), "id": "30021"}})
        client = Hides()
        result = moderate(rows, MemoryState(), DRY, model, client=client, account_username="waterbomb_official", region="대표(공식)")
        self.assertEqual(result["kept"], 1)
        self.assertEqual(client.hidden, [])

    def test_would_hide_log_names_the_comment(self):
        stream = io.StringIO()
        with mock.patch("sys.stdout", stream):
            moderate([audience("심한 욕")], MemoryState(), DRY, Model({"30001": {**answer("숨김", "D3"), "id": "30001"}}),
                     client=Hides(), account_username="waterbomb_sokcho_official")
        line = next(json.loads(item) for item in stream.getvalue().splitlines() if '"would_hide"' in item)
        self.assertEqual(line["message"], "would hide")
        self.assertEqual(line["comment_id"], "30001")
        self.assertEqual(line["account"], "waterbomb_sokcho_official")
        self.assertEqual(line["rule_id"], "D3")
        self.assertEqual(line["reason"], "모델 판정")
        self.assertEqual(line["text"], "심한 욕")


if __name__ == "__main__":
    unittest.main()
