"""Rule order, dry-run, idempotency, and the hide flag. No live Meta or GCP calls."""

import copy
import datetime as dt
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import urllib.parse

from sns_comment.api import GraphClient, GraphError, PilotError
from sns_comment.moderation import (decision_name, finalize, guard, hannam_slur, hiding_enabled,
                                    load_rulebook, moderate, prompt_for, recent_rows)
from sns_comment.human_decision import apply_human_decisions
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
        self.updates, self.validations, self.fail = [], [], False

    def request(self, suffix="", query=None, body=None):
        if self.fail:
            raise PilotError("Google Sheets 요청 실패 (HTTP 403). 원본 DB는 유지됩니다.")
        if isinstance(body, dict) and body.get("requests"):
            self.updates.append(body)
            for request in body["requests"]:
                if "setDataValidation" in request:
                    self.validations.append(request["setDataValidation"])
                    continue
                if "insertDimension" in request:
                    self._insert_rows(request["insertDimension"])
                    continue
                if "copyPaste" in request:
                    self._copy_row(request["copyPaste"])
                    continue
                if "deleteDimension" in request:
                    self._delete_rows(request["deleteDimension"])
                    continue
                self.assert_no_structural_edit(request)
                update = request["updateCells"]
                title = next(name for name, tab in self.tabs.items() if tab["id"] == update["range"]["sheetId"])
                start = update["range"]["startRowIndex"]
                start_col = update["range"].get("startColumnIndex", 0)
                written = [[item.get("userEnteredValue", {}).get("stringValue", "") for item in row["values"]]
                           for row in update["rows"]]
                tab = self.tabs[title]
                while len(tab["rows"]) < start + len(written):
                    tab["rows"].append([])
                for offset, values in enumerate(written):
                    row = tab["rows"][start + offset]
                    needed = start_col + len(values)
                    if len(row) < needed:
                        row.extend([""] * (needed - len(row)))
                    for column, value in enumerate(values):
                        row[start_col + column] = value
            return {"replies": []}
        if suffix.startswith("/values/"):
            title = urllib.parse.unquote(suffix.split("/values/", 1)[1]).split("!", 1)[0].strip("'")
            return {"values": self.tabs[title]["rows"]}
        return {"sheets": [{"properties": {"sheetId": tab["id"], "title": name, "gridProperties": {"rowCount": 5000}}}
                           for name, tab in self.tabs.items()]}

    def _insert_rows(self, insert):
        span = insert["range"]
        start = span.get("startIndex")
        if span.get("dimension") != "ROWS" or start < 2:
            raise AssertionError("new rows must stay below the header")
        if start == 2 and insert.get("inheritFromBefore") is not False:
            raise AssertionError("the top insert must copy the data row, not the header")
        if start > 2 and insert.get("inheritFromBefore") is not True:
            raise AssertionError("an insert below the top copies the data row above it")
        title = next(name for name, tab in self.tabs.items() if tab["id"] == span["sheetId"])
        tab = self.tabs[title]
        width = max((len(row) for row in tab["rows"]), default=13)
        for _ in range(span["endIndex"] - span["startIndex"]):
            tab["rows"].insert(span["startIndex"], [""] * width)

    def _copy_row(self, paste):
        if paste.get("pasteType") != "PASTE_NORMAL":
            raise AssertionError("a moved row must keep values, formatting, and validation")
        source, dest = paste["source"], paste["destination"]
        if source["sheetId"] != dest["sheetId"] or source["startRowIndex"] < 2 or dest["startRowIndex"] < 2:
            raise AssertionError("only data rows can be copied")
        title = next(name for name, tab in self.tabs.items() if tab["id"] == source["sheetId"])
        tab = self.tabs[title]
        copied = [list(tab["rows"][index]) for index in range(source["startRowIndex"], source["endRowIndex"])]
        start_col, end_col = source["startColumnIndex"], source["endColumnIndex"]
        for offset, values in enumerate(copied):
            row = tab["rows"][dest["startRowIndex"] + offset]
            if len(row) < end_col:
                row.extend([""] * (end_col - len(row)))
            chunk = values[start_col:end_col]
            if len(chunk) < end_col - start_col:
                chunk.extend([""] * (end_col - start_col - len(chunk)))
            row[start_col:end_col] = chunk

    def _delete_rows(self, delete):
        span = delete["range"]
        if span.get("dimension") != "ROWS" or span.get("startIndex") < 2:
            raise AssertionError("the note and header cannot be deleted")
        title = next(name for name, tab in self.tabs.items() if tab["id"] == span["sheetId"])
        del self.tabs[title]["rows"][span["startIndex"]:span["endIndex"]]

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
        self.assertEqual(sheet.data(HIDE_TAB)[1][6], "99999")
        self.assertEqual(sheet.data(HIDE_TAB)[1][:8], sheet.original[HIDE_TAB][2][:8])
        row = sheet.data(HIDE_TAB)[0]
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
        self.assertEqual(sheet.data(HIDE_TAB)[0][11], "숨겼을 것(시험)")

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
        self.assertEqual(sheet.data(HIDE_TAB)[0][1], "부산 댓글")
        self.assertEqual(sheet.data(HIDE_TAB)[0][11], "숨겼을 것(시험)")
        self.assertEqual(sheet.data(HIDE_TAB)[1][6], "99999")
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
        rule = sheet.validations[-1]
        self.assertEqual(rule["range"]["startRowIndex"], 2)
        self.assertGreater(rule["range"]["endRowIndex"], 2)
        self.assertTrue(rule["rule"]["strict"])
        self.assertEqual([item["userEnteredValue"] for item in rule["rule"]["condition"]["values"]], ["숨김", "유지"])
        before = len(sheet.data(REVIEW_TAB))
        moderate([review], state, DRY, Model({}), client=Hides(), account_username="waterbomb_sokcho_official",
                 region="속초", sheet_client=sheet)
        self.assertEqual(len(sheet.data(REVIEW_TAB)), before)
        state, sheet = MemoryState(), FakeSheets()
        model = Model({"30001": {**answer("숨김", "D3"), "id": "30001"}})
        moderate([audience("욕")], state, AUTO_ON, model, client=Hides(GraphError(400, 100)),
                 account_username="waterbomb_official", region="대표(공식)", sheet_client=sheet)
        failed = sheet.data(HIDE_TAB)[0]
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
        self.assertEqual([row[6] for row in sheet.data(HIDE_TAB)], ["30001", "99999"])
        second = backfill_sheet(state, sheet)
        self.assertEqual(len(sheet.data(HIDE_TAB)), 2)
        self.assertEqual(second["synced"], 2)
        self.assertEqual(sheet.tabs[HIDE_TAB]["rows"][0], sheet.original[HIDE_TAB][0])
        self.assertEqual(sheet.tabs[HIDE_TAB]["rows"][1], sheet.original[HIDE_TAB][1])
        self.assertEqual(sheet.data(HIDE_TAB)[1], sheet.original[HIDE_TAB][2])

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
        self.assertEqual(old, [])

    def test_a_missing_timestamp_is_read_before_the_row_is_inserted(self):
        state, sheet = MemoryState(), FakeSheets()
        comment = audience("저급한", "18111760985229472", written="", username="chwj3516")

        class Timed(Hides):
            def get(self, path, **params):
                self.seen = (path, params)
                return {"id": path, "timestamp": "2026-11-11T21:41:00+0000"}

        client = Timed()
        model = Model({"18111760985229472": {**answer("숨김", "D4", reason="의상"), "id": "18111760985229472"}})
        moderate([comment], state, DRY, model, client=client, account_username="waterbomb_official",
                 region="서울", sheet_client=sheet)
        self.assertEqual(client.seen, ("18111760985229472", {"fields": "timestamp"}))
        stored = state.data[decision_name(ACCOUNT, "18111760985229472")]
        self.assertTrue(stored["written_at"].startswith("2026-11-11T21:41:00"))
        row = [item for item in sheet.data(HIDE_TAB) if item[6] == "18111760985229472"][0]
        self.assertEqual(row[2], "2026-11-12 06:41")
        self.assertNotIn("17:05", row[2])
        again = FakeSheets()
        bare = audience("저급한", "18111760985229473", written="", username="chwj3516")
        moderate([bare], state, DRY, Model({"18111760985229473": {**answer("숨김", "D4"), "id": "18111760985229473"}}),
                 client=Hides(), account_username="waterbomb_official", region="서울", sheet_client=again)
        self.assertFalse(any(len(row) > 6 and row[6] == "18111760985229473" for row in again.data(HIDE_TAB)))
        bare["written_at"] = "2026-11-11T21:41:00+0000"
        moderate([bare], state, DRY, Model({}), client=Hides(), account_username="waterbomb_official",
                 region="서울", sheet_client=again)
        filled = [row for row in again.data(HIDE_TAB) if len(row) > 6 and row[6] == "18111760985229473"]
        self.assertEqual(len(filled), 1)
        self.assertEqual(filled[0][2], "2026-11-12 06:41")

    def test_new_comments_are_inserted_under_the_header_newest_first(self):
        state, sheet = MemoryState(), FakeSheets()
        older = audience("먼저", "30061", written="2026-11-12T01:00:00+00:00")
        newer = audience("나중", "30062", written="2026-11-12T05:00:00+00:00")
        model = Model({
            "30061": {**answer("숨김", "D3", reason="앞"), "id": "30061"},
            "30062": {**answer("숨김", "D3", reason="뒤"), "id": "30062"},
        })
        moderate([older, newer], state, DRY, model, client=Hides(), account_username="waterbomb_toronto_official",
                 region="토론토", sheet_client=sheet)
        data = sheet.data(HIDE_TAB)
        self.assertEqual([row[6] for row in data], ["30062", "30061", "99999"])
        self.assertEqual(data[0][0], "14")
        self.assertEqual(data[1][0], "13")
        self.assertEqual(data[2][0], "12")
        self.assertEqual(data[2], sheet.original[HIDE_TAB][2])
        self.assertEqual(sheet.tabs[HIDE_TAB]["rows"][0], sheet.original[HIDE_TAB][0])
        self.assertEqual(sheet.tabs[HIDE_TAB]["rows"][1], HIDE_COLUMNS)
        inserted = [request for body in sheet.updates for request in body["requests"] if "insertDimension" in request]
        self.assertEqual(inserted[0]["insertDimension"]["range"]["startIndex"], 2)
        self.assertIs(inserted[0]["insertDimension"]["inheritFromBefore"], False)

    def test_an_older_comment_is_inserted_below_newer_rows_and_a_blank_time_is_not_inserted(self):
        from sns_comment.moderation_sheet import sync_sheet
        sheet = FakeSheets()
        older = {"status": "hidden", "comment_id": "30070", "region": "서울", "author_username": "a",
                 "text": "오래됨", "permalink": "https://www.instagram.com/p/a/", "rule_id": "D3",
                 "reason": "옛", "confidence": "high", "written_at": "2026-10-01T00:00:00+00:00",
                 "updated_at": "2026-11-13T00:00:00+00:00", "hidden_check": "hidden=true"}
        blank = {**older, "comment_id": "30071", "written_at": "", "text": "시각 없음"}
        sync_sheet(sheet, [older, blank])
        data = sheet.data(HIDE_TAB)
        self.assertEqual([row[6] for row in data], ["99999", "30070"])
        self.assertEqual(data[0][0], "12")
        self.assertEqual(data[1][0], "13")
        self.assertTrue(data[1][2].startswith("2026-10-01"))
        inserted = [request["insertDimension"] for body in sheet.updates for request in body["requests"]
                    if "insertDimension" in request]
        self.assertGreater(inserted[0]["range"]["startIndex"], 2)
        self.assertIs(inserted[0]["inheritFromBefore"], True)

    def test_sort_moves_whole_rows_by_comment_time_and_keeps_numbers(self):
        from sns_comment.human_decision import find_comment_row, set_decision_if_blank
        from sns_comment.moderation_sheet import sort_plan, sort_requests
        header = list(HIDE_COLUMNS)
        old = ["12", "서울 댓글", "2026-11-01 09:00", "old", "기존", "https://www.instagram.com/p/old/",
               "111", "", "K1", "기존", "높음", "숨김 완료", "메모"]
        new = ["4", "부산 댓글", "2026-11-13 09:00", "new", "최근", "https://www.instagram.com/p/new/",
               "222", "", "D3", "최근", "높음", "숨김 완료", "유지 메모"]
        blank = ["9", "속초 댓글", "", "none", "시각없음", "https://www.instagram.com/p/blank/",
                 "333", "", "D1", "없음", "낮음", "", "사람 메모"]
        rows = [["안내"], header, old, new, blank]
        plan = sort_plan(rows)
        self.assertEqual([item["comment_id"] for item in plan["moves"]], ["222", "111"])
        self.assertEqual([item["number"] for item in plan["moves"]], ["4", "12"])
        requests = sort_requests(HIDE_SHEET_ID, rows)
        self.assertEqual(requests[0]["insertDimension"]["range"]["startIndex"], 2)
        self.assertEqual(requests[-1]["deleteDimension"]["range"]["startIndex"], 5)
        self.assertGreaterEqual(requests[-1]["deleteDimension"]["range"]["startIndex"], 2)
        ordered = rows[:2] + [row for _index, row in plan["ordered"]]
        self.assertEqual([row[6] for row in ordered[2:]], ["222", "111", "333"])
        self.assertEqual([row[0] for row in ordered[2:]], ["4", "12", "9"])
        self.assertEqual(find_comment_row(ordered, "222"), 2)
        sheet = FakeSheets()
        sheet.tabs[HIDE_TAB]["rows"] = [list(row) for row in ordered]
        # The decision column on the hide tab is not used; the review lookup is what must survive a move.
        review = FakeSheets()
        review.tabs[REVIEW_TAB]["rows"] = [["안내"], list(REVIEW_COLUMNS),
                                            ["4", "부산 댓글", "2026-11-13 09:00", "new", "최근", "https://www.instagram.com/p/new/",
                                             "222", "", "D3", "최근", "높음", "", "2026-11-13 09:00 KST 자동 감지"],
                                            ["12", "서울 댓글", "2026-11-01 09:00", "old", "기존", "https://www.instagram.com/p/old/",
                                             "111", "", "K1", "기존", "높음", "유지", "사람이 적은 행"]]
        moved = sort_plan(review.tabs[REVIEW_TAB]["rows"])
        review.tabs[REVIEW_TAB]["rows"] = review.tabs[REVIEW_TAB]["rows"][:2] + [row for _index, row in moved["ordered"]]
        result = set_decision_if_blank(review, "222", "숨김")
        self.assertEqual(result["result"], "written")
        self.assertEqual(review.data(REVIEW_TAB)[0][6], "222")
        self.assertEqual(review.data(REVIEW_TAB)[0][11], "숨김")
        self.assertEqual(review.data(REVIEW_TAB)[1][11], "유지")

    def test_blank_times_follow_dated_rows_and_equal_times_keep_the_older_row(self):
        from sns_comment.moderation_sheet import _insertion_index, sort_plan
        dated = ["1", "서울 댓글", "2026-11-13 09:00", "", "", "", "aaa"]
        same = ["2", "서울 댓글", "2026-11-13 09:00", "", "", "", "bbb"]
        older = ["3", "서울 댓글", "2026-10-01 09:00", "", "", "", "ccc"]
        blank = ["4", "서울 댓글", "", "", "", "", "ddd"]
        rows = [["안내"], list(HIDE_COLUMNS), dated, same, older, blank]
        self.assertEqual(_insertion_index(rows, "2026-11-13 09:00"), 4)
        self.assertEqual(_insertion_index(rows, ""), 5)
        self.assertEqual([row[6] for _index, row in sort_plan(rows)["ordered"]], ["aaa", "bbb", "ccc", "ddd"])
        serial = ["5", "서울 댓글", 46339.375, "", "", "", "eee"]
        mixed = [["안내"], list(HIDE_COLUMNS), ["6", "서울 댓글", "2026-11-01 09:00", "", "", "", "fff"], serial]
        self.assertEqual([row[6] for _index, row in sort_plan(mixed)["ordered"]], ["eee", "fff"])

    def test_sort_apply_moves_manual_rows_without_rewriting_them_and_holds_the_tab_lock(self):
        from sns_comment.cloud_state import SHEET_HIDE_LEASE, SHEET_REVIEW_LEASE
        from sns_comment.human_decision import find_comment_row
        from sns_comment.moderation_sheet import sort_tabs
        sheet = FakeSheets()
        old = ["12", "서울 댓글", "2026-11-01 09:00", "old", "기존", "https://www.instagram.com/p/old/",
               "111", "", "K1", "기존", "높음", "숨김 완료", "숨김 메모"]
        newer = ["4", "부산 댓글", "2026-11-13 09:00", "new", "최근", "https://www.instagram.com/p/new/",
                 "222", "", "D3", "최근", "높음", "숨김 완료", "최근 메모"]
        blank = ["9", "속초 댓글", "", "none", "시각없음", "https://www.instagram.com/p/blank/",
                 "333", "", "D1", "없음", "낮음", "", "빈 시각"]
        kept = ["37", "서울 댓글", "2026-07-05 12:23", "fan", "예전 수동", "https://www.instagram.com/p/x/",
                "manual", "", "H1", "애매", "보통", "유지", "사람이 적은 행"]
        pending = ["80", "토론토 댓글", "2026-11-12 18:00", "fan2", "대기", "https://www.instagram.com/p/y/",
                   "pending", "", "H2", "애매", "보통", "숨김", "2026-11-12 18:00 KST 자동 감지"]
        sheet.tabs[HIDE_TAB]["rows"] = [["안내문"], list(HIDE_COLUMNS), old, newer, blank]
        sheet.tabs[REVIEW_TAB]["rows"] = [["안내문"], list(REVIEW_COLUMNS), kept, pending]
        state = _NameLock()
        preview = sort_tabs(sheet, state, apply=False)
        self.assertEqual(sheet.updates, [])
        self.assertEqual(state.names, [SHEET_HIDE_LEASE, SHEET_REVIEW_LEASE])
        self.assertTrue(all(item["applied"] is False and item["moves"] > 0 for item in preview))
        applied = sort_tabs(sheet, state, apply=True)
        self.assertTrue(all(item["applied"] for item in applied))
        self.assertEqual(sheet.tabs[HIDE_TAB]["rows"][0], ["안내문"])
        self.assertEqual(sheet.tabs[HIDE_TAB]["rows"][1], list(HIDE_COLUMNS))
        self.assertEqual(sheet.tabs[REVIEW_TAB]["rows"][0], ["안내문"])
        self.assertEqual(sheet.tabs[REVIEW_TAB]["rows"][1], list(REVIEW_COLUMNS))
        self.assertEqual([row[6] for row in sheet.data(HIDE_TAB)], ["222", "111", "333"])
        self.assertEqual([row[0] for row in sheet.data(HIDE_TAB)], ["4", "12", "9"])
        self.assertEqual(sheet.data(HIDE_TAB)[0][12], "최근 메모")
        self.assertEqual([row[6] for row in sheet.data(REVIEW_TAB)], ["pending", "manual"])
        self.assertEqual(sheet.data(REVIEW_TAB)[0][11], "숨김")
        self.assertEqual(sheet.data(REVIEW_TAB)[1][11], "유지")
        self.assertEqual(sheet.data(REVIEW_TAB)[1][12], "사람이 적은 행")
        self.assertEqual(find_comment_row(sheet.tabs[REVIEW_TAB]["rows"], "manual"), 3)
        self.assertEqual([row[0] for row in sheet.data(REVIEW_TAB)], ["80", "37"])
        again = sort_tabs(sheet, state, apply=True)
        self.assertTrue(all(item["applied"] is False for item in again))
        self.assertNotIn("state/lease.json", state.names)

    def test_sort_command_is_a_preview_until_apply(self):
        from sns_comment.moderation_job import main
        sheet = FakeSheets()
        sheet.tabs[HIDE_TAB]["rows"][2] = ["12", "서울 댓글", "2026-10-01 09:00", "old", "기존",
                                            "https://www.instagram.com/p/old/", "99999", "", "K1", "기존", "높음",
                                            "숨김 완료", "사람이 적은 행"]
        sheet.tabs[HIDE_TAB]["rows"].append(
            ["4", "부산 댓글", "2026-11-13 09:00", "new", "최근", "https://www.instagram.com/p/new/",
             "222", "", "D3", "최근", "높음", "숨김 완료", "최근 메모"])
        with mock.patch.dict(os.environ, {"SNS_STATE_BUCKET": "bucket"}), \
             mock.patch("sns_comment.moderation_job.CloudState", return_value=_NameLock()), \
             mock.patch("sns_comment.moderation_job.open_sheet", return_value=sheet):
            self.assertEqual(main(["--sort-sheet"]), 0)
            self.assertEqual(sheet.updates, [])
            self.assertEqual(main(["--sort-sheet", "--apply"]), 0)
        self.assertEqual(sheet.data(HIDE_TAB)[0][6], "222")
        self.assertEqual(sheet.data(HIDE_TAB)[1][6], "99999")
        self.assertEqual(sheet.data(HIDE_TAB)[1][0], "12")
        turned_off = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        turned_off["moderation"]["mode"] = "off"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(turned_off), encoding="utf-8")
            with mock.patch("sns_comment.moderation_sheet.sort_tabs") as sort:
                self.assertEqual(main(["--config", str(path), "--apply"]), 0)
                sort.assert_not_called()


    def test_fill_missing_dates_writes_column_c_then_sorts_under_the_tab_lock(self):
        from sns_comment.cloud_state import SHEET_HIDE_LEASE, SHEET_REVIEW_LEASE
        from sns_comment.human_decision import find_comment_row
        from sns_comment.moderation_sheet import fill_missing_dates
        sheet = FakeSheets()
        sheet.tabs[HIDE_TAB]["rows"] = [
            ["안내문"], list(HIDE_COLUMNS),
            ["12", "서울 댓글", "2026-11-01 09:00", "old", "기존", "https://www.instagram.com/p/old/",
             "111", "", "K1", "기존", "높음", "숨김 완료", "숨김 메모"],
            ["133", "서울 댓글", "", "chwj3516", "저급한", "https://www.instagram.com/reel/DbNsNMIy92C/",
             "18111760985229472", "", "D4", "의상", "높음", "숨겼을 것(시험)", "2026-11-12 17:05 KST 자동 감지(10분 작업)"],
        ]
        sheet.tabs[REVIEW_TAB]["rows"] = [
            ["안내문"], list(REVIEW_COLUMNS),
            ["40", "서울 댓글", "2026-11-13 09:00", "new", "최근", "https://www.instagram.com/p/new/",
             "222", "", "H1", "최근", "보통", "숨김", "최근 메모"],
            ["38", "서울 댓글", "", "jamjoon0408", "나띠", "https://www.instagram.com/reel/DdOiTeZvelx/",
             "17936497245410609", "", "H1", "애매", "낮음", "", "2026-11-12 17:05 KST 자동 감지"],
        ]
        calls = []

        def lookup(account, comment_id):
            calls.append((account, comment_id))
            if comment_id == "18111760985229472":
                return "2026-11-13T01:42:00+00:00"
            if comment_id == "17936497245410609":
                return "2026-11-11T21:41:00+0000"
            raise PilotError("댓글 작성 시각을 읽지 못했습니다.")

        state = _NameLock()
        preview = fill_missing_dates(sheet, state, lookup, apply=False)
        self.assertFalse(preview["applied"])
        self.assertEqual(sheet.updates, [])
        self.assertEqual(calls, [("서울 댓글", "18111760985229472"), ("서울 댓글", "17936497245410609")])
        self.assertEqual(state.names, [SHEET_HIDE_LEASE, SHEET_REVIEW_LEASE])
        self.assertEqual(preview["rows"][0]["written"], "2026-11-13 10:42")
        self.assertEqual(preview["rows"][0]["sheet_row"], 3)
        applied = fill_missing_dates(sheet, state, lookup, apply=True)
        self.assertTrue(applied["applied"])
        hide = sheet.data(HIDE_TAB)
        self.assertEqual([row[6] for row in hide], ["18111760985229472", "111"])
        self.assertEqual(hide[0][0], "133")
        self.assertEqual(hide[0][2], "2026-11-13 10:42")
        self.assertEqual(hide[0][11], "숨겼을 것(시험)")
        self.assertIn("자동 감지", hide[0][12])
        self.assertEqual(hide[1][12], "숨김 메모")
        review = sheet.data(REVIEW_TAB)
        self.assertEqual([row[6] for row in review], ["222", "17936497245410609"])
        self.assertEqual(review[1][0], "38")
        self.assertEqual(review[1][2], "2026-11-12 06:41")
        self.assertEqual(review[0][11], "숨김")
        self.assertEqual(review[1][12], "2026-11-12 17:05 KST 자동 감지")
        self.assertEqual(find_comment_row(sheet.tabs[REVIEW_TAB]["rows"], "17936497245410609"), 3)
        date_writes = [request["updateCells"] for body in sheet.updates for request in body["requests"] if "updateCells" in request]
        self.assertTrue(date_writes)
        self.assertTrue(all(request["range"]["startColumnIndex"] == 2 and request["range"]["endColumnIndex"] == 3
                            and request["range"]["startRowIndex"] >= 2 for request in date_writes))
        self.assertNotIn("state/lease.json", state.names)

    def test_fill_command_previews_until_apply_and_then_fills_notion(self):
        from sns_comment.moderation_job import main
        sheet = FakeSheets()
        sheet.tabs[HIDE_TAB]["rows"].append(
            ["133", "서울 댓글", "", "chwj3516", "저급한", "https://www.instagram.com/reel/DbNsNMIy92C/",
             "18111760985229472", "", "D4", "의상", "높음", "숨겼을 것(시험)", "자동 감지"])

        class Timed:
            def __init__(self):
                self.params = None

            def get(self, path, **params):
                self.params = params
                return {"id": path, "timestamp": "2026-11-13T01:42:00+0000"}

        client = Timed()
        with mock.patch.dict(os.environ, {"SNS_STATE_BUCKET": "bucket", "SNS_NOTION_DECISIONS": "1"}), \
             mock.patch("sns_comment.moderation_job.CloudState", return_value=_NameLock()), \
             mock.patch("sns_comment.moderation_job.open_sheet", return_value=sheet), \
             mock.patch("sns_comment.moderation_job.account_clients", return_value={"서울 댓글": client}), \
             mock.patch("sns_comment.moderation_job.fill_notion_written_dates", return_value=["18111760985229472"]) as notion:
            self.assertEqual(main(["--fill-missing-dates"]), 0)
            self.assertEqual(sheet.updates, [])
            notion.assert_not_called()
            self.assertEqual(main(["--fill-missing-dates", "--apply"]), 0)
            notion.assert_called_once()
        self.assertEqual(client.params, {"fields": "timestamp"})
        self.assertEqual(sheet.data(HIDE_TAB)[0][6], "18111760985229472")
        self.assertEqual(sheet.data(HIDE_TAB)[0][2], "2026-11-13 10:42")
        self.assertEqual(sheet.data(HIDE_TAB)[1][0], "12")


class _NameLock:
    def __init__(self):
        self.names = []

    def lease(self, seconds=3900, name="state/lease.json"):
        self.names.append(name)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def review_row(number, comment_id, decision, memo):
    return [str(number), "서울 댓글", "2026-07-05 12:23", "fan", "본문", "https://www.instagram.com/p/x/",
            comment_id, "", "H1", "애매", "보통", decision, memo]


class HumanGraph:
    def __init__(self, error=None, hidden=True):
        self.calls, self.error, self.hidden = [], error, hidden

    def hide_comment(self, comment_id):
        self.calls.append(comment_id)
        if self.error:
            raise self.error
        return {"success": True}

    def get(self, path, **params):
        if params.get("fields") != "hidden":
            raise AssertionError("only a hidden check is allowed")
        return {"id": path, "hidden": self.hidden}


class HumanDecisionTests(unittest.TestCase):
    def sheet(self, rows):
        sheet = FakeSheets()
        sheet.tabs[REVIEW_TAB]["rows"].extend(rows)
        return sheet

    def test_dropdown_is_added_without_changing_existing_cells(self):
        sheet = self.sheet([
            review_row(1, "18087079217429612", "숨김", "2026-11-11 15:48 KST 숨김 완료"),
            review_row(30, "18000000000000030", "숨김", ""),
        ])
        before = [list(row) for row in sheet.tabs[REVIEW_TAB]["rows"]]
        apply_human_decisions(sheet, HumanGraph(), hide_enabled=True)
        self.assertEqual(sheet.tabs[REVIEW_TAB]["rows"], before)
        rule = sheet.validations[0]["rule"]["condition"]
        self.assertEqual([item["userEnteredValue"] for item in rule["values"]], ["숨김", "유지"])
        area = sheet.validations[0]["range"]
        self.assertEqual(area["startRowIndex"], 2)
        self.assertEqual(area["startColumnIndex"], 11)
        self.assertEqual(len(sheet.validations), 1)

    def test_hide_runs_once_keep_does_not_call_the_api_and_manual_rows_stay(self):
        moment = dt.datetime(2026, 11, 12, 8, 0, tzinfo=dt.timezone.utc)
        sheet = self.sheet([
            review_row(1, "18087079217429612", "숨김", "2026-11-11 15:48 KST 숨김 완료"),
            review_row(2, "18077060762319401", "유지", ""),
            review_row(30, "18000000000000030", "숨김", ""),
            review_row(38, "18111760985229472", "숨김", "2026-11-12 17:05 KST 자동 감지"),
            review_row(39, "17936497245410609", "유지", "2026-11-12 17:05 KST 자동 감지"),
        ])
        graph = HumanGraph()
        apply_human_decisions(sheet, graph, now=moment, hide_enabled=True)
        self.assertEqual(graph.calls, ["18111760985229472"])
        memo_rows = [request["updateCells"]["range"]["startRowIndex"]
                     for body in sheet.updates for request in body["requests"] if "updateCells" in request]
        self.assertIn(5, memo_rows)
        self.assertNotIn(2, memo_rows)
        rows = {row[6]: row for row in sheet.data(REVIEW_TAB)}
        self.assertEqual(rows["18087079217429612"][12], "2026-11-11 15:48 KST 숨김 완료")
        self.assertEqual(rows["18077060762319401"][12], "")
        self.assertEqual(rows["18000000000000030"][12], "")
        self.assertEqual(rows["18111760985229472"][11], "숨김")
        self.assertIn("숨김 완료", rows["18111760985229472"][12])
        self.assertIn("유지 확정", rows["17936497245410609"][12])
        apply_human_decisions(sheet, graph, now=moment, hide_enabled=True)
        self.assertEqual(graph.calls, ["18111760985229472"])

    def test_flag_off_writes_a_trial_status_and_does_not_hide(self):
        sheet = self.sheet([review_row(38, "18111760985229472", "숨김", "2026-11-12 17:05 KST 자동 감지")])
        graph = HumanGraph()
        with mock.patch.dict("os.environ", {"SNS_HUMAN_DECISION_HIDE": "", "SNS_MODERATION_HIDE": "1"}, clear=False):
            apply_human_decisions(sheet, graph, now=dt.datetime(2026, 11, 12, 8, 0, tzinfo=dt.timezone.utc))
        self.assertEqual(graph.calls, [])
        self.assertIn("숨김 예정(시험)", sheet.data(REVIEW_TAB)[-1][12])

    def test_deleted_comment_stops_and_a_temporary_error_retries_only_three_times(self):
        moment = dt.datetime(2026, 11, 12, 8, 0, tzinfo=dt.timezone.utc)
        sheet = self.sheet([review_row(38, "18111760985229472", "숨김", "2026-11-12 17:05 KST 자동 감지")])
        gone = HumanGraph(error=GraphError(400, 100))
        apply_human_decisions(sheet, gone, now=moment, hide_enabled=True)
        self.assertIn("숨김 실패 (중단)", sheet.data(REVIEW_TAB)[-1][12])
        apply_human_decisions(sheet, gone, now=moment, hide_enabled=True)
        self.assertEqual(gone.calls, ["18111760985229472"])
        sheet = self.sheet([review_row(38, "18111760985229472", "숨김", "2026-11-12 17:05 KST 사람 판단 대기")])
        flaky = HumanGraph(error=GraphError(500, 1))
        for _ in range(4):
            apply_human_decisions(sheet, flaky, now=moment, hide_enabled=True)
        self.assertEqual(len(flaky.calls), 3)
        self.assertIn("숨김 실패 (3/3)", sheet.data(REVIEW_TAB)[-1][12])


if __name__ == "__main__":
    unittest.main()
