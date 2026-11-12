"""Rule order, dry-run, idempotency, and the hide flag. No live Meta or GCP calls."""

import copy
import datetime as dt
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import urllib.parse

from sns_comment.api import GraphClient, GraphError, PilotError
from sns_comment.moderation import (decision_name, finalize, guard, hannam_slur, hiding_enabled,
                                    load_rulebook, moderate, prompt_for, recent_rows)
from sns_comment.moderation import backfill_sheet
from sns_comment.moderation_sheet import HIDE_COLUMNS, HIDE_SHEET_ID, HIDE_TAB, REVIEW_COLUMNS, REVIEW_SHEET_ID, REVIEW_TAB
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


def audience(text, comment_id="30001", account=ACCOUNT, owner=0, written="2026-11-12T06:10:00+00:00", media="20001",
             username="fan", author_id=None):
    row = {"id": comment_id, "account_id": account, "media_id": media, "text": text, "is_owner": owner,
           "written_at": written, "username": username}
    if author_id:
        row["author_id"] = author_id
    return row


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
        self.assertIn("같은 작성자", text)
        d11 = next(rule for rule in book["rules"] if rule["id"] == "D11")
        self.assertIn("같은 작성자", d11["definition"])
        self.assertIn("다른 사람", d11["exceptions"])
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

    def test_same_author_later_copies_are_d11_and_other_authors_are_kept(self):
        state, model, client = MemoryState(), Model({}), Hides()
        first = audience("@x.in_official", "30031", written="2026-11-12T06:00:00+00:00", username="_capybara.capybara_", author_id="101")
        second = audience("@x.in_official", "30032", written="2026-11-12T06:01:00+00:00", username="_capybara.capybara_", author_id="101")
        third = audience("@x.in_official", "30033", written="2026-11-12T06:02:00+00:00", username="_capybara.capybara_", author_id="101")
        others = [
            audience("@x.in_official", "30034", written="2026-11-12T06:03:00+00:00", username="zezo_lessico", author_id="102"),
            audience("@x.in_official", "30035", written="2026-11-12T06:04:00+00:00", username="xin_readyskiteam_russia", author_id="103"),
            audience("News", "30036", written="2026-11-12T06:05:00+00:00", username="one_fan", author_id="201"),
            audience("News", "30037", written="2026-11-12T06:06:00+00:00", username="two_fan", author_id="202"),
        ]
        for comment in (first, *others):
            model.answers[comment["id"]] = {**answer("유지", "K1"), "id": comment["id"]}
        result = moderate([third, second, *others, first], state, DRY, model, client=client,
                          account_username="waterbomb_toronto_official", region="토론토")
        self.assertEqual(set(model.seen), {"30031", "30034", "30035", "30036", "30037"})
        self.assertEqual(result["would_hide"], 2)
        self.assertEqual(result["kept"], 5)
        self.assertEqual(state.data[decision_name(ACCOUNT, "30032")]["rule_id"], "D11")
        self.assertEqual(state.data[decision_name(ACCOUNT, "30033")]["rule_id"], "D11")
        self.assertEqual(state.data[decision_name(ACCOUNT, "30034")]["label"], "유지")
        self.assertEqual(state.data[decision_name(ACCOUNT, "30037")]["label"], "유지")

    def test_a_later_run_hides_only_the_same_authors_next_copy(self):
        state, model, client = MemoryState(), Model({}), Hides()
        first = audience("PLEASE X:IN @x.in_official", "30041", written="2026-11-12T06:00:00+00:00",
                         username="readyxin01", author_id="301")
        model.answers["30041"] = {**answer("유지", "K1"), "id": "30041"}
        moderate([first], state, DRY, model, client=client, account_username="waterbomb_toronto_official", region="토론토")
        repeat = audience("PLEASE X:IN @x.in_official", "30042", written="2026-11-12T07:00:00+00:00",
                          username="readyxin01", author_id="301")
        altered = audience("PLEASE X:IN", "30043", written="2026-11-12T07:05:00+00:00", username="readyxin01", author_id="301")
        model.answers["30043"] = {**answer("유지", "K1"), "id": "30043"}
        result = moderate([repeat, altered], state, DRY, model, client=client,
                          account_username="waterbomb_toronto_official", region="토론토")
        self.assertEqual(model.seen, ["30041", "30043"])
        self.assertEqual(result["would_hide"], 1)
        self.assertEqual(state.data[decision_name(ACCOUNT, "30042")]["rule_id"], "D11")
        self.assertEqual(state.data[decision_name(ACCOUNT, "30043")]["label"], "유지")

    def test_same_author_id_matches_even_when_the_username_changes(self):
        state, model = MemoryState(), Model({})
        first = audience("Xin", "30051", written="2026-11-12T06:00:00+00:00", username="ReadyXin", author_id="401")
        model.answers["30051"] = {**answer("유지", "K1"), "id": "30051"}
        moderate([first], state, DRY, model, client=Hides(), account_username="waterbomb_toronto_official")
        later = audience("Xin", "30052", written="2026-11-12T06:10:00+00:00", username="readyxin", author_id="401")
        result = moderate([later], state, DRY, model, client=Hides(), account_username="waterbomb_toronto_official")
        self.assertEqual(model.seen, ["30051"])
        self.assertEqual(result["would_hide"], 1)
        self.assertEqual(state.data[decision_name(ACCOUNT, "30052")]["rule_id"], "D11")


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
            "20001": {"id": "20001", "permalink": "https://www.instagram.com/reel/hook/"},
            "20009": {"id": "20009", "permalink": "https://www.instagram.com/p/poll/"},
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
        self.assertEqual(settings["moderation"]["sheet"]["spreadsheet_id"], "1496AQpUgceCZCNaahpJ89_SSMbiO6nmOtwFi6u4QzMY")
        self.assertNotEqual(settings["moderation"]["sheet"]["spreadsheet_id"], settings["workflow"]["google_sheets"]["spreadsheet_id"])
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


class FakeSheets:
    def __init__(self):
        note = ["기존 안내"] + [""] * 13 + ["결정 완료", "37 / 37"]
        existing = ["12", "서울 댓글", "2026-11-01 09:00", "old", "기존 댓글", "https://www.instagram.com/p/old/",
                    "99999", "", "K1", "기존", "높음", "숨김 완료", "사람이 적은 행"]
        self.tabs = {
            HIDE_TAB: {"id": HIDE_SHEET_ID, "rows": [list(note), list(HIDE_COLUMNS), list(existing)]},
            REVIEW_TAB: {"id": REVIEW_SHEET_ID, "rows": [list(note), list(REVIEW_COLUMNS)]},
        }
        self.original = {title: [list(row) for row in tab["rows"]] for title, tab in self.tabs.items()}
        self.updates, self.fail = [], False

    def request(self, suffix="", query=None, body=None):
        if self.fail:
            raise PilotError("Google Sheets 요청 실패 (HTTP 403). 원본 DB는 유지됩니다.")
        if isinstance(body, dict) and body.get("requests"):
            self.updates.append(body)
            for request in body["requests"]:
                self.assert_no_structural_edit(request)
                update = request["updateCells"]
                title = next(name for name, tab in self.tabs.items() if tab["id"] == update["range"]["sheetId"])
                start = update["range"]["startRowIndex"]
                written = [[item.get("userEnteredValue", {}).get("stringValue", "") for item in row["values"]]
                           for row in update["rows"]]
                tab = self.tabs[title]
                while len(tab["rows"]) < start:
                    tab["rows"].append([])
                tab["rows"][start:start + len(written)] = written
            return {"replies": []}
        if suffix.startswith("/values/"):
            title = urllib.parse.unquote(suffix.split("/values/", 1)[1]).split("!", 1)[0].strip("'")
            return {"values": self.tabs[title]["rows"]}
        return {"sheets": [{"properties": {"sheetId": tab["id"], "title": name, "gridProperties": {"rowCount": 5000}}}
                           for name, tab in self.tabs.items()]}

    def assert_no_structural_edit(self, request):
        if "addSheet" in request or "deleteSheet" in request:
            raise AssertionError("existing tabs must not be created or deleted")
        start = request["updateCells"]["range"]["startRowIndex"]
        if start < 2:
            raise AssertionError("note and header rows must stay untouched")

    def data(self, title):
        return self.tabs[title]["rows"][2:]


class SheetLogTests(unittest.TestCase):
    def test_hide_rows_append_after_the_existing_number_and_do_not_repeat(self):
        state, sheet = MemoryState(), FakeSheets()
        model = Model({"30001": {**answer("숨김", "D3", reason="대상 욕설"), "id": "30001"}})
        comment = audience("=심한 욕")
        comment["permalink"] = "https://www.instagram.com/p/abc/"
        comment["parent_id"] = "30000"
        first = moderate([comment], state, DRY, model, client=Hides(), account_username="waterbomb_toronto_official",
                         region="토론토", sheet_client=sheet)
        self.assertEqual(first["sheet"], "synced")
        self.assertEqual(len(sheet.data(HIDE_TAB)), 2)
        self.assertEqual(sheet.tabs[HIDE_TAB]["rows"][0], sheet.original[HIDE_TAB][0])
        self.assertEqual(sheet.tabs[HIDE_TAB]["rows"][1], HIDE_COLUMNS)
        self.assertEqual(sheet.data(HIDE_TAB)[0][6], "99999")
        row = sheet.data(HIDE_TAB)[1]
        self.assertEqual(row[0], "13")
        self.assertEqual(row[1], "토론토 댓글")
        self.assertEqual(row[2], "2026-11-12 15:10")
        self.assertEqual(row[3], "fan")
        self.assertEqual(row[4], "=심한 욕")
        self.assertEqual(row[5], "https://www.instagram.com/p/abc/")
        self.assertEqual(row[6], "30001")
        self.assertEqual(row[7], "30000")
        self.assertEqual(row[8], "D3")
        self.assertEqual(row[10], "높음")
        self.assertEqual(row[11], "숨겼을 것(시험)")
        self.assertIn("KST 자동 감지(10분 작업)", row[12])
        updates = len(sheet.updates)
        again = moderate([comment], state, DRY, model, client=Hides(), account_username="waterbomb_toronto_official",
                         region="토론토", sheet_client=sheet)
        self.assertEqual(again["skipped"], 1)
        self.assertEqual(len(sheet.updates), updates)
        self.assertEqual(len(sheet.data(HIDE_TAB)), 2)
        frozen = [list(item) for item in sheet.data(HIDE_TAB)]
        client = Hides()
        with mock.patch.dict("os.environ", {"SNS_MODERATION_HIDE": "1"}, clear=False):
            hidden = moderate([comment], state, AUTO, model, client=client, account_username="waterbomb_toronto_official",
                              region="토론토", sheet_client=sheet)
        self.assertEqual(hidden["hidden"], 1)
        self.assertEqual(client.hidden, ["30001"])
        self.assertEqual(sheet.data(HIDE_TAB), frozen)
        self.assertEqual(sheet.data(HIDE_TAB)[1][11], "숨겼을 것(시험)")

    def test_a_sheet_failure_does_not_block_moderation_and_retries_once(self):
        state, sheet = MemoryState(), FakeSheets()
        sheet.fail = True
        model = Model({"30001": {**answer("숨김", "D2", reason="광고"), "id": "30001"}})
        comment = audience("공장 연락처")
        failed = moderate([comment], state, DRY, model, client=Hides(), account_username="waterbomb_busan_official",
                          region="부산", sheet_client=sheet)
        self.assertEqual(failed["would_hide"], 1)
        self.assertEqual(failed["sheet"], "failed")
        self.assertEqual(state.data[decision_name(ACCOUNT, "30001")]["status"], "would_hide")
        self.assertEqual(sheet.tabs[HIDE_TAB]["rows"], sheet.original[HIDE_TAB])
        sheet.fail = False
        moderate([comment], state, DRY, model, client=Hides(), account_username="waterbomb_busan_official",
                 region="부산", sheet_client=sheet)
        self.assertEqual(sheet.data(HIDE_TAB)[-1][1], "부산 댓글")
        self.assertEqual(sheet.data(HIDE_TAB)[-1][11], "숨겼을 것(시험)")
        moderate([comment], state, DRY, model, client=Hides(), account_username="waterbomb_busan_official",
                 region="부산", sheet_client=sheet)
        self.assertEqual(len(sheet.data(HIDE_TAB)), 2)

    def test_human_review_leaves_the_decision_blank_and_hide_failure_stays_in_the_note(self):
        state, sheet = MemoryState(), FakeSheets()
        review = audience("애매합니다", "30008")
        review["is_owner"] = None
        moderate([review], state, DRY, Model({}), client=Hides(), account_username="waterbomb_sokcho_official",
                 region="속초", sheet_client=sheet)
        row = sheet.data(REVIEW_TAB)[0]
        self.assertEqual(row[1], "속초 댓글")
        self.assertEqual(row[11], "")
        self.assertIn("KST 자동 감지", row[12])
        self.assertNotIn("10분 작업", row[12])
        self.assertEqual(sheet.tabs[REVIEW_TAB]["rows"][0], sheet.original[REVIEW_TAB][0])
        self.assertEqual(sheet.tabs[REVIEW_TAB]["rows"][0][-2:], ["결정 완료", "37 / 37"])
        before = len(sheet.data(REVIEW_TAB))
        moderate([review], state, DRY, Model({}), client=Hides(), account_username="waterbomb_sokcho_official",
                 region="속초", sheet_client=sheet)
        self.assertEqual(len(sheet.data(REVIEW_TAB)), before)
        state, sheet = MemoryState(), FakeSheets()
        model = Model({"30001": {**answer("숨김", "D3"), "id": "30001"}})
        moderate([audience("욕")], state, AUTO_ON, model, client=Hides(GraphError(400, 100)),
                 account_username="waterbomb_official", region="대표(공식)", sheet_client=sheet)
        failed = sheet.data(HIDE_TAB)[-1]
        self.assertEqual(failed[1], "서울 댓글")
        self.assertEqual(failed[11], "숨김 실패")
        self.assertIn("숨김 요청 실패", failed[12])

    def test_backfill_appends_stored_decisions_once(self):
        state, sheet = MemoryState(), FakeSheets()
        state.json_put(decision_name(ACCOUNT, "30001"), {
            "account_id": ACCOUNT, "comment_id": "30001", "region": "토론토",
            "account_username": "waterbomb_toronto_official", "author_username": "fan",
            "text": "반복", "permalink": "https://www.instagram.com/p/abc/", "parent_id": "",
            "written_at": "2026-11-12T06:10:00+00:00", "rule_id": "D11", "reason": "같은 작성자",
            "confidence": "high", "status": "would_hide", "updated_at": "2026-11-12T07:00:00+00:00",
            "sheet_synced_status": "숨겼을 것(시험)"}, 0)
        state.json_put(decision_name(ACCOUNT, "99999"), {
            "account_id": ACCOUNT, "comment_id": "99999", "region": "서울", "status": "hidden",
            "confidence": "high", "rule_id": "K1", "reason": "기존", "text": "기존 댓글"}, 0)
        first = backfill_sheet(state, sheet)
        self.assertEqual(first["synced"], 2)
        self.assertEqual([row[6] for row in sheet.data(HIDE_TAB)], ["99999", "30001"])
        second = backfill_sheet(state, sheet)
        self.assertEqual(len(sheet.data(HIDE_TAB)), 2)
        self.assertEqual(second["synced"], 2)
        self.assertEqual(sheet.tabs[HIDE_TAB]["rows"][2], sheet.original[HIDE_TAB][2])

    def test_instagram_comment_time_is_stored_and_older_records_stay_blank(self):
        from sns_comment.moderation_job import comment_from_payload
        parsed = comment_from_payload(
            {"id": "18111760985229472", "text": "안녕", "timestamp": "2026-11-11T21:41:00+0000",
             "username": "fan", "from": {"id": "55", "username": "fan"}},
            ACCOUNT, "waterbomb_toronto_official", "20001")
        self.assertTrue(parsed["written_at"].startswith("2026-11-11T21:41:00"))
        state, sheet = MemoryState(), FakeSheets()
        comment = audience("안녕", "18111760985229472", written="2026-11-11T21:41:00+0000", username="fan")
        model = Model({"18111760985229472": {**answer("사람판단", "H1", confidence="low", reason="애매"), "id": "18111760985229472"}})
        moderate([comment], state, DRY, model, client=Hides(), account_username="waterbomb_toronto_official",
                 region="토론토", sheet_client=sheet)
        stored = state.data[decision_name(ACCOUNT, "18111760985229472")]
        self.assertTrue(stored["written_at"].startswith("2026-11-11T21:41:00"))
        self.assertEqual(sheet.data(REVIEW_TAB)[-1][2], "2026-11-12 06:41")
        state.json_put(decision_name(ACCOUNT, "17936497245410609"), {
            "account_id": ACCOUNT, "comment_id": "17936497245410609", "region": "서울",
            "account_username": "waterbomb_official", "author_username": "fan", "text": "예전",
            "status": "would_hide", "confidence": "high", "rule_id": "D9", "reason": "예전 기록",
            "updated_at": "2026-11-12T07:00:00+00:00"}, 0)
        backfill_sheet(state, sheet)
        old = [row for row in sheet.data(HIDE_TAB) if len(row) > 6 and row[6] == "17936497245410609"]
        self.assertEqual(old[0][2], "")


if __name__ == "__main__":
    unittest.main()
