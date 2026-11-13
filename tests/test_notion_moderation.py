import copy
import datetime as dt
import os
import unittest
from unittest import mock

from sns_comment.api import PilotError
from sns_comment.human_decision import set_decision_if_blank
from sns_comment.moderation_sheet import HIDE_TAB, REVIEW_TAB
from sns_comment.notion_mcp import NoDailyPage
from sns_comment.notion_moderation import (
    DATABASE_TITLE, HIDE_URL, REVIEW_URL, hidden_yesterday, notion_decisions_enabled,
    pending_reviews, prepare_notion_decisions, publish_daily_summary, pull_decisions,
    push_results, sheet_memo_status, toggle_title,
)
from tests.test_moderation import FakeSheets, MemoryState


PARENT = "4120761a512d817fae8ff53edda31b9e"
DAY = "2026-11-13"
NOW = dt.datetime(2026, 11, 13, 0, 0, tzinfo=dt.timezone.utc)
SOURCE = "collection://aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
DB_URL = "https://app.notion.com/p/dbdbdbdbdbdbdbdbdbdbdbdbdbdbdbdb"
SETTINGS = {"moderation": {"notion": {"parent_page_id": PARENT}}, "notion": {"daily_data_source_id": "source"}}


def hide_row(account, comment_id, status, note):
    return ["1", account, "2026-11-12 09:00", "someone", "본문", "https://www.instagram.com/p/a/",
            comment_id, "", "D3", "이유", "높음", status, note]


def review_row(comment_id, decision, memo, text="애매한 댓글"):
    return ["1", "서울 댓글", "2026-11-12 09:00", "person", text, "https://www.instagram.com/p/a/",
            comment_id, "", "H1", "확신 낮음", "낮음", decision, memo]


class FakeNotion:
    def __init__(self):
        self.pages = {PARENT: "부모 페이지"}
        self.rows = []
        self.calls = []

    def call(self, name, args):
        self.calls.append((name, copy.deepcopy(args)))
        if name == "fetch":
            return {"id": args["id"], "text": self.pages.get(args["id"], "")}
        if name == "create-database":
            self.pages[PARENT] += '\n<database url="' + DB_URL + '" data-source-url="' + SOURCE + '">' + DATABASE_TITLE + "</database>"
            return {"text": self.pages[PARENT]}
        if name == "query-data-sources":
            params = args["data"].get("params") or []
            query = args["data"]["query"]
            rows = list(self.rows)
            if 'WHERE "댓글 ID"' in query:
                rows = [row for row in rows if row.get("댓글 ID") == params[0]]
            elif 'WHERE "결정"' in query:
                rows = [row for row in rows if row.get("결정") in params]
            return {"results": rows}
        if name == "create-pages":
            for page in args["pages"]:
                row = dict(page["properties"])
                row["url"] = "https://app.notion.com/p/" + row["댓글 ID"]
                self.rows.append(row)
            return {}
        if name == "update-page":
            if args["command"] == "insert_content":
                self.pages[args["page_id"]] = self.pages.get(args["page_id"], "") + "\n" + args["content"]
                return {}
            if args["command"] == "update_properties":
                for row in self.rows:
                    if row.get("url") == args["page_id"]:
                        row["상태"] = args["properties"]["상태"]
                return {}
            raise AssertionError(args["command"])
        raise AssertionError(name)


class SheetCounts(unittest.TestCase):
    def test_yesterday_hide_counts_are_by_account_and_ignore_other_rows(self):
        rows = [
            ["안내"], ["머리글"],
            hide_row("서울 댓글", "1", "숨김 완료", "2026-11-12 18:00 KST 자동 감지"),
            hide_row("부산 댓글", "2", "숨김 완료", "2026-11-12 19:00 KST 자동 감지"),
            hide_row("속초 댓글", "3", "숨겼을 것(시험)", "2026-11-12 19:00 KST 자동 감지"),
            hide_row("토론토 댓글", "4", "숨김 완료", "2026-11-11 19:00 KST 자동 감지"),
            hide_row("서울 댓글", "5", "숨김 실패", "2026-11-12 19:00 KST 자동 감지"),
        ]
        counted = hidden_yesterday(rows, "2026-11-12")
        self.assertEqual(counted["total"], 2)
        self.assertEqual(counted["by_account"], {"서울": 1, "부산": 1, "속초": 0, "토론토": 0})

    def test_pending_reviews_are_blank_choices_with_an_automation_note(self):
        rows = [
            ["안내"], ["머리글"],
            review_row("10", "숨김", "2026-11-11 15:48 KST 숨김 완료"),
            review_row("11", "유지", ""),
            review_row("12", "", "2026-11-13 09:00 KST 자동 감지"),
            review_row("13", "", "사람이 적은 메모"),
        ]
        pending = pending_reviews(rows)
        self.assertEqual([item["comment_id"] for item in pending], ["12"])

    def test_status_text_uses_the_sheet_memo(self):
        self.assertEqual(sheet_memo_status("2026-11-13 09:12 KST 숨김 완료", NOW), "숨김 완료 09:12")
        self.assertEqual(sheet_memo_status("2026-11-13 09:12 KST 유지 확정", NOW), "유지 확정")
        self.assertEqual(sheet_memo_status("2026-11-13 09:12 KST 숨김 실패 (1/3): 실패", NOW), "숨김 실패 (1/3)")


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.notion = FakeNotion()
        self.state = MemoryState()
        self.sheet = FakeSheets()
        self.sheet.tabs[HIDE_TAB]["rows"].append(hide_row("서울 댓글", "81", "숨김 완료", "2026-11-12 18:02 KST 자동 감지"))
        self.sheet.tabs[REVIEW_TAB]["rows"].append(review_row("82", "", "2026-11-13 08:10 KST 자동 감지", text="확인할 댓글"))
        self.ready = True
        self.waits = []

    def finder(self, day):
        self.assertEqual(day, DAY)
        if not self.ready:
            raise NoDailyPage("아직 없습니다")
        return {"id": "daily-page", "text": self.notion.pages.get("daily-page", "오늘 업무"), "truncated": False}

    def publish(self, attempts=1):
        return publish_daily_summary(SETTINGS, self.state, self.sheet, self.notion, self.finder, now=NOW,
                                     attempts=attempts, wait_seconds=300, sleeper=self.waits.append)

    def test_toggle_is_appended_once_with_counts_and_the_decision_table(self):
        result = self.publish()
        self.assertEqual(result["status"], "inserted")
        self.assertEqual(result["hidden"], 1)
        self.assertEqual(result["added"], 1)
        inserted = [args for name, args in self.notion.calls if name == "update-page" and args["command"] == "insert_content"]
        self.assertEqual(len(inserted), 1)
        self.assertEqual(inserted[0]["position"], {"type": "end"})
        content = inserted[0]["content"]
        self.assertIn(toggle_title(DAY), content)
        self.assertIn("<b>인스타 댓글 관리 (2026-11-13)</b>", content)
        self.assertIn("어제 숨긴 댓글 1건", content)
        self.assertIn("사람 판단 필요 1건", content)
        self.assertIn("- 서울 1건", content)
        self.assertIn("- 토론토 0건", content)
        self.assertIn(HIDE_URL, content)
        self.assertIn(REVIEW_URL, content)
        self.assertIn('inline="true">' + DATABASE_TITLE, content)
        self.assertNotIn("replace_content", content)
        again = self.publish()
        self.assertEqual(again["status"], "unchanged")
        self.assertEqual(sum(1 for name, args in self.notion.calls if name == "update-page" and args["command"] == "insert_content"), 1)
        self.assertEqual(sum(1 for name, _args in self.notion.calls if name == "create-database"), 1)
        self.assertEqual(len(self.notion.rows), 1)
        self.assertEqual(self.notion.rows[0]["댓글 ID"], "82")
        self.assertNotIn("결정", self.notion.rows[0])

    def test_missing_daily_page_retries_and_does_not_create_a_page(self):
        self.ready = False
        result = self.publish(attempts=3)
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(self.waits, [300, 300])
        self.assertFalse(any(name in {"update-page", "create-pages", "create-database"} for name, _args in self.notion.calls))

    def test_retry_then_finds_the_page(self):
        calls = {"count": 0}

        def finder(day):
            calls["count"] += 1
            if calls["count"] < 3:
                raise NoDailyPage("아직 없습니다")
            return self.finder(day)

        self.ready = True
        result = publish_daily_summary(SETTINGS, self.state, self.sheet, self.notion, finder, now=NOW,
                                       attempts=3, wait_seconds=300, sleeper=self.waits.append)
        self.assertEqual(result["status"], "inserted")
        self.assertEqual(self.waits, [300, 300])


class DecisionSyncTests(unittest.TestCase):
    def setUp(self):
        self.sheet = FakeSheets()
        self.notion = FakeNotion()
        self.database = {"url": DB_URL, "data_source_url": SOURCE}
        self.sheet.tabs[REVIEW_TAB]["rows"].extend([
            review_row("91", "", "2026-11-13 08:00 KST 자동 감지"),
            review_row("92", "유지", "2026-11-13 08:00 KST 자동 감지"),
            review_row("93", "", "2026-11-13 08:00 KST 자동 감지"),
        ])

    def test_notion_choice_fills_a_blank_sheet_cell_and_does_not_replace_one(self):
        self.notion.rows = [
            {"url": "https://app.notion.com/p/91", "댓글 ID": "91", "결정": "숨김", "상태": "사람 판단 대기"},
            {"url": "https://app.notion.com/p/92", "댓글 ID": "92", "결정": "숨김", "상태": "사람 판단 대기"},
            {"url": "https://app.notion.com/p/93", "댓글 ID": "93", "결정": "유지", "상태": ""},
        ]
        outcomes = {item["comment_id"]: item["result"] for item in pull_decisions(self.sheet, self.notion, self.database)}
        self.assertEqual(outcomes["91"], "written")
        self.assertEqual(outcomes["92"], "conflict")
        self.assertEqual(outcomes["93"], "written")
        rows = {row[6]: row for row in self.sheet.data(REVIEW_TAB)}
        self.assertEqual(rows["91"][11], "숨김")
        self.assertEqual(rows["92"][11], "유지")
        self.assertEqual(rows["92"][12], "2026-11-13 08:00 KST 자동 감지")
        self.assertEqual(rows["93"][11], "유지")
        self.assertEqual(self.notion.rows[1]["상태"], "시트와 다름: 시트는 유지")
        self.assertFalse(any(name == "update-page" and args.get("command") == "replace_content" for name, args in self.notion.calls))

    def test_sheet_result_is_written_back_and_new_pending_rows_are_added_once(self):
        self.sheet.tabs[REVIEW_TAB]["rows"][2][11] = "숨김"
        self.sheet.tabs[REVIEW_TAB]["rows"][2][12] = "2026-11-13 09:20 KST 숨김 완료"
        self.notion.rows = [{"url": "https://app.notion.com/p/91", "댓글 ID": "91", "결정": "숨김", "상태": "사람 판단 대기"}]
        updated = push_results(self.sheet, self.notion, self.database, now=NOW)
        self.assertEqual(updated, [{"comment_id": "91", "status": "숨김 완료 09:20"}])
        from sns_comment.notion_moderation import add_pending
        added = add_pending(self.notion, self.database, pending_reviews(self.sheet.tabs[REVIEW_TAB]["rows"]))
        self.assertEqual([item["comment_id"] for item in added], ["93"])
        again = add_pending(self.notion, self.database, pending_reviews(self.sheet.tabs[REVIEW_TAB]["rows"]))
        self.assertEqual(again, [])
        self.assertEqual(sum(1 for name, _args in self.notion.calls if name == "create-pages"), 1)

    def test_direct_sheet_write_refuses_to_replace_a_choice(self):
        self.assertEqual(set_decision_if_blank(self.sheet, "92", "숨김")["result"], "conflict")
        self.assertEqual(self.sheet.data(REVIEW_TAB)[1][11], "유지")

    def test_flag_off_does_not_open_notion(self):
        with mock.patch.dict(os.environ, {"SNS_NOTION_DECISIONS": "0"}), \
             mock.patch("sns_comment.notion_moderation.open_notion") as opened:
            self.assertFalse(notion_decisions_enabled())
            self.assertIsNone(prepare_notion_decisions(SETTINGS, MemoryState(), self.sheet))
            opened.assert_not_called()

    def test_notion_failure_does_not_raise_into_the_job(self):
        with mock.patch.dict(os.environ, {"SNS_NOTION_DECISIONS": "1"}), \
             mock.patch("sns_comment.notion_moderation.open_notion", side_effect=PilotError("권한 없음")):
            self.assertIsNone(prepare_notion_decisions(SETTINGS, MemoryState(), self.sheet))


if __name__ == "__main__":
    unittest.main()
