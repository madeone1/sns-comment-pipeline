import contextlib
import copy
import datetime as dt
import importlib.util
import os
import threading
import unittest
from pathlib import Path
from unittest import mock

from sns_comment.api import PilotError
from sns_comment.cloud_state import SHEET_HIDE_LEASE, CloudError
from sns_comment.human_decision import apply_human_decisions, set_decision_if_blank
from sns_comment.moderation_sheet import HIDE_TAB, REVIEW_TAB, sync_sheet
from sns_comment.notion_mcp import NoDailyPage
from sns_comment.notion_moderation import (
    DATABASE_TITLE, SHEET_URL, _open_locked, hidden_yesterday, notion_decisions_enabled, parse_database,
    coverage_period, fill_written_dates, layout_updates, rewrite_today_toggle, run_notion_summary, toggle_updates,
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


DAILY_PAGE = """### Meeting

회의

### Develop

<toggle heading="h3">
📊 티켓 판매 인사이트 보고서 (2026-11-13)

판매
</toggle>

<toggle heading="h3">
📣 마케팅 성과 보고서 (2026-11-13)

마케팅
</toggle>

### Info

안내
"""


class FakeNotion:
    def __init__(self):
        self.pages = {PARENT: "부모 페이지", "daily-page": DAILY_PAGE}
        self.rows = []
        self.calls = []
        self.create_response = None

    def call(self, name, args):
        self.calls.append((name, copy.deepcopy(args)))
        if name == "fetch":
            if args["id"] == DB_URL:
                return {"id": args["id"], "text": '<data-source url="' + SOURCE + '"/>'}
            return {"id": args["id"], "text": self.pages.get(args["id"], "")}
        if name == "create-database":
            self.pages[PARENT] = self.pages.get(PARENT, "") + '\n<database url="' + DB_URL + '" inline="false">' + DATABASE_TITLE + "</database>\n"
            if self.create_response is not None:
                return self.create_response
            return {"url": DB_URL, "id": "11aabf27-f671-4bd7-ac4f-c437a7e5ef65",
                    "data_sources": [{"id": SOURCE.replace("collection://", "")}]}
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
            if args["command"] == "update_content":
                text = self.pages.get(args["page_id"], "")
                for change in args["content_updates"]:
                    old, new = change["old_str"], change["new_str"]
                    if text.count(old) != 1:
                        raise AssertionError("update is not unique")
                    text = text.replace(old, new, 1)
                self.pages[args["page_id"]] = text
                return {}
            if args["command"] == "update_properties":
                for row in self.rows:
                    if row.get("url") == args["page_id"]:
                        row.update(args["properties"])
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
        self.sheet.tabs[REVIEW_TAB]["rows"].append(review_row("82", "", "2026-11-12 08:10 KST 자동 감지", text="확인할 댓글"))
        self.ready = True
        self.waits = []

    def finder(self, day):
        self.assertEqual(day, DAY)
        if not self.ready:
            raise NoDailyPage("아직 없습니다")
        return {"id": "daily-page", "text": self.notion.pages["daily-page"], "truncated": False}

    def publish(self, attempts=1):
        return publish_daily_summary(SETTINGS, self.state, self.sheet, self.notion, self.finder, now=NOW,
                                     attempts=attempts, wait_seconds=300, sleeper=self.waits.append)

    def test_toggle_is_appended_once_with_counts_and_the_decision_table(self):
        result = self.publish()
        self.assertEqual(result["status"], "inserted")
        self.assertEqual(result["hidden"], 1)
        self.assertEqual(result["added"], 1)
        edited = [args for name, args in self.notion.calls if name == "update-page" and args["command"] == "update_content"]
        self.assertEqual(len(edited), 1)
        self.assertNotIn("insert_content", [args["command"] for name, args in self.notion.calls if name == "update-page"])
        text = self.notion.pages["daily-page"]
        self.assertLess(text.index("📣 마케팅 성과 보고서"), text.index(toggle_title(DAY)))
        self.assertLess(text.index(toggle_title(DAY)), text.index("### Info"))
        self.assertIn("### Meeting", text)
        self.assertIn('heading="h3"', text)
        self.assertIn("<b>인스타 댓글 관리 (2026-11-13)</b>", text)
        self.assertIn("어제 숨긴 댓글 1건 (서울 1 · 부산 0 · 속초 0 · 토론토 0)", text)
        self.assertIn("사람 판단 필요 1건 — 아래 표에서 숨김/유지를 고르세요", text)
        self.assertNotIn("- 서울", text)
        self.assertIn("[📄 규정집 시트 열기](" + SHEET_URL + ")", text)
        self.assertNotIn("#gid=", text)
        self.assertNotIn("<mention", text)
        self.assertNotIn("<database", self.notion.pages["daily-page"].split(toggle_title(DAY))[1].split("### Info")[0])
        self.assertNotIn("replace_content", [args["command"] for name, args in self.notion.calls if name == "update-page"])
        again = self.publish()
        self.assertEqual(again["status"], "unchanged")
        self.assertEqual(sum(1 for name, args in self.notion.calls if name == "update-page" and args["command"] == "update_content"), 1)
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

    def test_json_create_response_without_a_database_tag_parses(self):
        parsed = parse_database({
            "url": "https://app.notion.com/p/11aabf27f6714bd7ac4fc437a7e5ef65",
            "id": "11aabf27-f671-4bd7-ac4f-c437a7e5ef65",
            "data_sources": [{"id": "01e6d1b9-96b7-4234-9965-ab1229bbf794"}],
        })
        self.assertEqual(parsed["url"], "https://app.notion.com/p/11aabf27f6714bd7ac4fc437a7e5ef65")
        self.assertEqual(parsed["data_source_url"], "collection://01e6d1b9-96b7-4234-9965-ab1229bbf794")
        with self.assertRaises(PilotError):
            parse_database({"text": "created"})

    def test_existing_database_is_reused_and_moved_into_the_develop_toggle(self):
        page = DAILY_PAGE + '\n<database url="' + DB_URL + '" inline="false">' + DATABASE_TITLE + "</database>\n"
        self.notion.pages[PARENT] = page

        def finder(_day):
            return {"id": PARENT, "text": self.notion.pages[PARENT], "truncated": False}

        result = publish_daily_summary(SETTINGS, self.state, self.sheet, self.notion, finder, now=NOW,
                                       attempts=1, wait_seconds=0, sleeper=self.waits.append)
        self.assertEqual(result["status"], "inserted")
        self.assertFalse(any(name == "create-database" for name, _args in self.notion.calls))
        text = self.notion.pages[PARENT]
        self.assertLess(text.index("📣 마케팅 성과 보고서"), text.index(toggle_title(DAY)))
        self.assertLess(text.index(toggle_title(DAY)), text.index("### Info"))
        self.assertEqual(text.count("<database"), 1)
        self.assertIn('inline="false"', text)
        self.assertNotIn("<mention", text)
        start = text.index(toggle_title(DAY))
        self.assertLess(text.index("<database", start), text.index("</toggle>", start))
        self.assertIn("### Meeting", text)
        saved, _generation = self.state.json_get("moderation/notion-decision-database.json")
        self.assertEqual(saved["data_source_url"], SOURCE)
        again = publish_daily_summary(SETTINGS, self.state, self.sheet, self.notion, finder, now=NOW,
                                      attempts=1, wait_seconds=0, sleeper=self.waits.append)
        self.assertEqual(again["status"], "unchanged")
        self.assertFalse(any(name == "create-database" for name, _args in self.notion.calls))

    def test_fragile_page_inserts_only_the_toggle_and_leaves_other_blocks(self):
        other = "https://app.notion.com/p/otherotherotherotherotherotheroo"
        page = """### Develop

<toggle heading="h3">
📊 티켓 판매 인사이트 보고서 (2026-11-13)

판매
</toggle>

<toggle heading="h3">
📣 마케팅 성과 보고서 (2026-11-13)

마케팅
</toggle>

<unknown alt="button" />

![사진](notion-file-block://abc)

<database url=\"""" + other + """\" inline="true">다른 표</database>

<database url=\"""" + DB_URL + """\" data-source-url=\"""" + SOURCE + """\" inline="false">""" + DATABASE_TITLE + """</database>
"""
        self.notion.pages[PARENT] = page

        def finder(_day):
            return {"id": PARENT, "text": self.notion.pages[PARENT], "truncated": False}

        result = publish_daily_summary(SETTINGS, self.state, self.sheet, self.notion, finder, now=NOW,
                                       attempts=1, wait_seconds=0, sleeper=self.waits.append)
        self.assertEqual(result["status"], "inserted")
        self.assertFalse(any(name == "create-database" for name, _args in self.notion.calls))
        edited = [args for name, args in self.notion.calls if name == "update-page" and args["command"] == "update_content"]
        changes = edited[0]["content_updates"]
        placed = [change for change in changes if "마케팅" in change["old_str"]]
        moved = [change for change in changes if DB_URL in change["old_str"]]
        self.assertEqual(len(placed), 1)
        self.assertEqual(len(moved), 1)
        self.assertNotIn("<unknown", placed[0]["old_str"])
        self.assertNotIn("notion-file-block", placed[0]["old_str"])
        self.assertNotIn("<database", placed[0]["old_str"])
        self.assertNotIn("<unknown", moved[0]["old_str"])
        self.assertNotIn(other, moved[0]["old_str"])
        text = self.notion.pages[PARENT]
        self.assertLess(text.index("📣 마케팅 성과 보고서"), text.index(toggle_title(DAY)))
        self.assertLess(text.index(toggle_title(DAY)), text.index("<unknown"))
        self.assertIn('<unknown alt="button" />', text)
        self.assertIn("notion-file-block://abc", text)
        self.assertIn('inline="false">' + DATABASE_TITLE, text)
        self.assertEqual(text.count("<database"), 2)
        self.assertEqual(text.count(other), 1)
        self.assertNotIn("<mention", text)
        start = text.index(toggle_title(DAY))
        self.assertLess(text.index(DB_URL, start), text.index("</toggle>", start))

    def test_real_develop_page_anchors_after_the_last_report_toggle(self):
        fixture = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "notion_daily_2026_11_13_develop.md"
        page = fixture.read_text(encoding="utf-8")
        database = {"url": "https://app.notion.com/p/11aabf27f6714bd7ac4fc437a7e5ef65",
                    "data_source_url": "collection://01e6d1b9-96b7-4234-9965-ab1229bbf794"}
        counts = {"total": 2, "by_account": {"서울": 2, "부산": 0, "속초": 0, "토론토": 0}}
        updates = toggle_updates(page, DAY, counts, 1, database)
        placed = [change for change in updates if "### Contents schedule" in change["old_str"]]
        moved = [change for change in updates if "11aabf27" in change["old_str"]]
        self.assertEqual(len(placed), 1)
        self.assertEqual(len(moved), 1)
        old, new = placed[0]["old_str"], placed[0]["new_str"]
        self.assertNotIn("notion-file-block", old)
        self.assertNotIn("<unknown", old)
        self.assertNotIn("<database", old)
        self.assertNotIn("<image", old)
        self.assertIn("---", old)
        self.assertIn("### Contents schedule", old)
        self.assertEqual(page.count(old), 1)
        self.assertNotIn("<unknown", moved[0]["old_str"])
        self.assertNotIn("notion-file-block", moved[0]["old_str"])
        edited = page
        for change in updates:
            self.assertEqual(edited.count(change["old_str"]), 1)
            edited = edited.replace(change["old_str"], change["new_str"], 1)
        self.assertLess(edited.index("📈 유튜브 구독자"), edited.index(toggle_title(DAY)))
        self.assertLess(edited.index(toggle_title(DAY)), edited.index("\n---\n\n### Contents schedule"))
        self.assertNotIn("사람 판단 표:", edited)
        self.assertNotIn('type="database">' + DATABASE_TITLE, edited)
        self.assertEqual(edited.count("<database"), page.count("<database"))
        start = edited.index(toggle_title(DAY))
        self.assertLess(edited.index("11aabf27", start), edited.index("</toggle>", start))
        self.assertIn('alt="button"', edited)
        self.assertEqual(page.count("notion-file-block"), edited.count("notion-file-block"))

    def test_rewrite_puts_one_table_in_todays_toggle_and_preview_does_not_write(self):
        hidden = {"total": 0, "by_account": {"서울": 0, "부산": 0, "속초": 0, "토론토": 0},
                  "start": "2026-11-12", "end": "2026-11-12"}
        old = """<toggle heading="h3">
💬 <b>인스타 댓글 관리 (2026-11-13)</b>

어제 숨긴 댓글 0건

- 서울 0건

[숨김 대상 시트](https://docs.google.com/spreadsheets/d/1496AQpUgceCZCNaahpJ89_SSMbiO6nmOtwFi6u4QzMY/edit#gid=123926565)

사람 판단 표: <mention url=\"""" + DB_URL + """\" type="database">인스타 댓글 사람 판단</mention>
</toggle>"""
        page = "### Develop\n\n" + old + "\n\n<unknown alt=\"button\" />\n\n<database url=\"" + DB_URL + "\" inline=\"false\">" + DATABASE_TITLE + "</database>\n### Info\n"
        database = {"url": DB_URL, "data_source_url": SOURCE}
        updates = layout_updates(page, DAY, hidden, 1, database, replace_existing=True)
        text = page
        for change in updates:
            self.assertEqual(text.count(change["old_str"]), 1)
            self.assertNotIn("<unknown", change["old_str"])
            text = text.replace(change["old_str"], change["new_str"], 1)
        self.assertEqual(text.count("<database"), 1)
        self.assertEqual(text.count("<mention"), 0)
        self.assertIn("어제 숨긴 댓글 0건 (서울 0 · 부산 0 · 속초 0 · 토론토 0)", text)
        self.assertIn("사람 판단 필요 1건 — 아래 표에서 숨김/유지를 고르세요", text)
        self.assertIn("[📄 규정집 시트 열기](" + SHEET_URL + ")", text)
        self.assertNotIn("#gid=", text)
        start = text.index(toggle_title(DAY))
        self.assertLess(text.index("<database", start), text.index("</toggle>", start))
        self.assertIn('<unknown alt="button" />', text)
        self.assertIn("### Info", text)
        self.assertEqual(layout_updates(text, DAY, hidden, 1, database, replace_existing=True), [])
        none = {"total": 0, "by_account": hidden["by_account"], "start": "2026-11-12", "end": "2026-11-12"}
        quiet = layout_updates(text, DAY, none, 0, database, replace_existing=True)
        self.assertEqual(len(quiet), 1)
        self.assertIn("사람 판단 필요 없음", quiet[0]["new_str"])
        other = "https://app.notion.com/p/otherotherotherotherotherotheroo"
        untouched = '### Develop\n\n안내\n\n<database url="' + other + '" inline="true">다른 표</database>\n'
        kept = layout_updates(untouched, DAY, hidden, 0, database, replace_existing=True)
        edited = untouched
        for change in kept:
            edited = edited.replace(change["old_str"], change["new_str"], 1)
        self.assertIn(other, edited)
        self.assertEqual(edited.count("<database"), 1)
        self.notion.pages["daily-page"] = page

        def finder(_day):
            return {"id": "daily-page", "text": self.notion.pages["daily-page"], "truncated": False}

        preview = rewrite_today_toggle(SETTINGS, self.state, self.sheet, self.notion, finder, now=NOW, apply=False)
        self.assertEqual(preview["status"], "preview")
        self.assertEqual(self.notion.pages["daily-page"], page)
        self.assertFalse(any(name == "update-page" and args.get("command") == "update_content"
                             for name, args in self.notion.calls))
        written = rewrite_today_toggle(SETTINGS, self.state, self.sheet, self.notion, finder, now=NOW, apply=True)
        self.assertEqual(written["status"], "rewritten")
        self.assertEqual(self.notion.pages["daily-page"].count("<database"), 1)
        self.assertEqual(self.notion.pages["daily-page"].count("<mention"), 0)

    def test_weekend_skips_without_touching_notion(self):
        for day in (dt.datetime(2026, 11, 14, 0, 0, tzinfo=dt.timezone.utc),
                    dt.datetime(2026, 11, 15, 0, 0, tzinfo=dt.timezone.utc)):
            with mock.patch.dict(os.environ, {"SNS_NOTION_DECISIONS": "1"}), \
                    mock.patch("sns_comment.notion_moderation.open_notion") as opened:
                result = run_notion_summary(SETTINGS, MemoryState(), now=day)
            self.assertEqual(result["status"], "weekend_skip")
            opened.assert_not_called()
            self.assertFalse(any(name == "update-page" for name, _args in self.notion.calls))

    def test_monday_covers_friday_through_sunday_after_the_last_summary(self):
        self.state.json_put("moderation/notion-summary-coverage.json", {"covered_through": "2026-11-12"})
        monday = dt.datetime(2026, 11, 16, 0, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(coverage_period(self.state, monday), ("2026-11-13", "2026-11-15"))
        self.sheet.tabs[HIDE_TAB]["rows"].extend([
            hide_row("서울 댓글", "501", "숨김 완료", "2026-11-13 18:00 KST 자동 감지"),
            hide_row("부산 댓글", "502", "숨김 완료", "2026-11-14 12:00 KST 자동 감지"),
            hide_row("속초 댓글", "503", "숨김 완료", "2026-11-15 09:00 KST 자동 감지"),
            hide_row("토론토 댓글", "504", "숨김 완료", "2026-11-12 09:00 KST 자동 감지"),
        ])
        self.sheet.tabs[REVIEW_TAB]["rows"].extend([
            review_row("601", "", "2026-11-14 10:00 KST 자동 감지"),
            review_row("602", "", "2026-11-01 10:00 KST 자동 감지"),
        ])

        def finder(day):
            self.assertEqual(day, "2026-11-16")
            return {"id": "daily-page", "text": self.notion.pages["daily-page"], "truncated": False}

        result = publish_daily_summary(SETTINGS, self.state, self.sheet, self.notion, finder, now=monday,
                                       attempts=1, wait_seconds=0, sleeper=self.waits.append)
        self.assertEqual(result["status"], "inserted")
        self.assertEqual(result["start"], "2026-11-13")
        self.assertEqual(result["end"], "2026-11-15")
        text = self.notion.pages["daily-page"]
        self.assertIn("11/13(금)~11/15(일) 숨긴 댓글 3건 (서울 1 · 부산 1 · 속초 1 · 토론토 0)", text)
        self.assertNotIn("- 서울", text)
        added = [row["댓글 ID"] for row in self.notion.rows]
        self.assertIn("601", added)
        self.assertNotIn("602", added)
        saved, _generation = self.state.json_get("moderation/notion-summary-coverage.json")
        self.assertEqual(saved["covered_through"], "2026-11-15")
        tuesday = dt.datetime(2026, 11, 17, 0, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(coverage_period(self.state, tuesday), ("2026-11-16", "2026-11-16"))
        capped = MemoryState()
        capped.json_put("moderation/notion-summary-coverage.json", {"covered_through": "2026-10-01"})
        self.assertEqual(coverage_period(capped, NOW), ("2026-11-06", "2026-11-12"))

    def test_blank_notion_written_date_is_copied_from_the_sheet(self):
        from sns_comment.notion_moderation import fill_written_dates
        comment = "17936497245410609"
        self.sheet.tabs[REVIEW_TAB]["rows"].append(review_row(comment, "", "2026-11-12 17:05 KST 자동 감지"))
        self.notion.rows.append({"url": "https://app.notion.com/p/" + comment, "댓글 ID": comment,
                                 "결정": "", "상태": "사람 판단 대기", "작성일(KST)": ""})
        database = {"url": DB_URL, "data_source_url": SOURCE}
        filled = fill_written_dates(self.sheet, self.notion, database)
        self.assertEqual(filled, [comment])
        self.assertEqual(self.notion.rows[-1]["작성일(KST)"], "2026-11-12 09:00")
        filled_again = fill_written_dates(self.sheet, self.notion, database)
        self.assertEqual(filled_again, [])

    def test_unparsed_create_response_reuses_the_database_instead_of_creating_another(self):
        self.notion.create_response = {"text": "created"}
        result = self.publish()
        self.assertEqual(result["status"], "inserted")
        self.assertEqual(sum(1 for name, _args in self.notion.calls if name == "create-database"), 1)
        self.assertEqual(self.state.json_get("moderation/notion-decision-database.json")[0]["url"], DB_URL)


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


class LockState:
    def __init__(self):
        self.locks = {}
        self.names = []

    @contextlib.contextmanager
    def lease(self, seconds=3900, name="state/lease.json"):
        self.names.append(name)
        lock = self.locks.setdefault(name, threading.Lock())
        if not lock.acquire(timeout=3):
            raise CloudError("다른 클라우드 실행이 진행 중입니다")
        try:
            yield
        finally:
            lock.release()


def sheet_record(comment_id, written):
    return {"status": "hidden", "comment_id": comment_id, "region": "서울", "author_username": "a",
            "text": "댓글", "permalink": "https://www.instagram.com/p/a/", "rule_id": "D3", "reason": "이유",
            "confidence": "high", "written_at": written, "updated_at": written, "hidden_check": "hidden=true"}


class ParallelSheetTests(unittest.TestCase):
    def test_two_inserts_keep_unique_numbers_and_do_not_use_the_daily_lock(self):
        sheet = FakeSheets()
        state = LockState()
        barrier = threading.Barrier(2)
        errors = []

        def write(comment_id, written):
            try:
                barrier.wait(timeout=3)
                sync_sheet(sheet, [sheet_record(comment_id, written)], state=state)
            except Exception as error:
                errors.append(error)

        threads = [threading.Thread(target=write, args=pair) for pair in (
            ("41001", "2026-11-13T01:00:00+00:00"), ("41002", "2026-11-13T02:00:00+00:00"))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        data = sheet.data(HIDE_TAB)
        self.assertEqual(sorted(row[6] for row in data), ["41001", "41002", "99999"])
        self.assertEqual(sorted(row[0] for row in data), ["12", "13", "14"])
        self.assertEqual([row for row in data if row[6] == "99999"], [sheet.original[HIDE_TAB][2]])
        self.assertIn(SHEET_HIDE_LEASE, state.names)
        self.assertNotIn("state/lease.json", state.names)

    def test_memo_is_written_to_the_row_found_after_an_insert(self):
        note = ["안내"] + [""] * 12
        header = ["번호", "계정/탭", "댓글 작성일(KST)", "작성자", "댓글 원문", "게시물 URL", "댓글 ID",
                  "원댓글 ID", "규칙 ID", "판정 이유", "확신도", "담당자 결정", "메모"]
        target = review_row("77", "유지", "2026-11-13 08:00 KST 자동 감지")
        extra = review_row("76", "", "2026-11-13 08:05 KST 자동 감지")
        reads = {"n": 0}

        def read(_client, _title):
            reads["n"] += 1
            if reads["n"] == 1:
                return [note, header, target]
            return [note, header, extra, target]

        class Graph:
            def hide_comment(self, _comment_id):
                raise AssertionError("유지는 숨기지 않습니다")

        sheet = FakeSheets()
        with mock.patch("sns_comment.human_decision._read_rows", side_effect=read):
            result = apply_human_decisions(sheet, Graph(), now=NOW, hide_enabled=False)
        self.assertEqual(result["updates"], 1)
        written = [request["updateCells"]["range"]["startRowIndex"]
                   for body in sheet.updates for request in body["requests"] if "updateCells" in request]
        self.assertEqual(written, [3])

    def test_notion_refresh_retries_its_own_lock_and_not_the_daily_lock(self):
        state = LockState()
        state.n = 0
        original = state.lease

        @contextlib.contextmanager
        def flaky(seconds=3900, name="state/lease.json"):
            state.n += 1
            if state.n < 3:
                raise CloudError("다른 클라우드 실행이 진행 중입니다")
            with original(seconds, name):
                yield

        state.lease = flaky
        waits = []
        with mock.patch("sns_comment.notion_moderation.open_notion", return_value=("mcp", "reader")):
            opened = _open_locked(SETTINGS, state, attempts=4, wait_seconds=9, sleeper=waits.append)
        self.assertEqual(opened, ("mcp", "reader"))
        self.assertEqual(waits, [9, 9])
        self.assertIn("state/notion-oauth-lease.json", state.names)
        self.assertNotIn("state/lease.json", state.names)

    def test_scheduler_invoker_check_does_not_treat_an_empty_policy_as_bound(self):
        path = Path(__file__).resolve().parents[1] / "scripts" / "token_connection_release.py"
        sys_path = list(__import__("sys").path)
        __import__("sys").path.insert(0, str(path.parent))
        spec = importlib.util.spec_from_file_location("token_connection_release", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        __import__("sys").path[:] = sys_path
        member = module.SCHEDULER
        self.assertFalse(module.scheduler_invoker_bound({"bindings": []}, member))
        self.assertTrue(module.scheduler_invoker_bound(
            {"bindings": [{"role": "roles/run.invoker", "members": [member]}]}, member))
        self.assertFalse(module.scheduler_invoker_bound(
            {"bindings": [{"role": "roles/run.developer", "members": [member]}]}, member))


if __name__ == "__main__":
    unittest.main()
