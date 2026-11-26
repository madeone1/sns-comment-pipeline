"""Caption rulebook sync: grounded edits only, and no moderation lease."""

import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import unittest
from unittest import mock

from sns_comment.api import PilotError
from sns_comment.cloud_state import CAPTION_POLICY_LEASE, MODERATION_LEASE, CloudError
from sns_comment.mcp import McpToolError
from sns_comment.caption_policy import (
    PAGE_ID, PAGE_URL, PROTECTED_SOURCE, UNCHANGED_LINE, AccessError, PolicyError,
    keep_findings, korean_report, parse_research, plan_updates, research, run,
)
from sns_comment import caption_policy
from sns_comment.monthly_cost import workload


NOW = dt.datetime(2026, 12, 1, 1, 0, tzinfo=dt.timezone.utc)
TODAY = dt.date(2026, 12, 1)
TITLE = "✍🏻 워터밤 캡션 작성 규정집 | 실무 가이드"
HASHTAG = "③ 해시태그: 게시 목적에 맞게 최대 5개. Instagram hashtags: at most 5, chosen for the post's purpose."
REELS_URL = "https://about.instagram.com/blog/announcements/reels-caption-limit"
AD_URL = "https://www.facebook.com/business/help/ad-copy-update"
OFFICIAL_CAP_URL = "https://help.instagram.com/999000111"
TRADE_URL = "https://www.socialmediatoday.com/news/instagram-hashtag-count"


def page_text(extra=""):
    body = """<callout icon="💡" color="blue_bg">
담당: SNS
목표: 캡션 품질
적용: 인스타그램 게시물
버전: WB-CAPTION-2.5
진행 상태: 사용 중
개정일: 2026-11-26
변경 근거: CEO 요청 https://help.instagram.com/351460621611097
</callout>

# 1. 목적
게시 목적에 맞는 문장만 쓴다.

# 2. 범위
인스타그램 피드와 릴스 캡션에 적용한다.

# 3. 캡션 작성 순서
① 후크
② 본문
""" + HASHTAG + """
④ 위치
⑤ 링크
⑥ 점검

# 4. 말투
구어체로 쓴다.

# 5. 금지
금지어를 본문에 넣지 않는다.

# 6. AI 작성 지침
AI 초안은 사람이 고친 뒤 게시한다.

# 7. 게시 전 확인사항
광고 문구는 메타 광고 정책을 확인한다.

# 8. 진행 결과 정리하기

| 날짜 | 버전 | 변경 | 근거 |
| --- | --- | --- | --- |
| 2026-11-01 | WB-CAPTION-2.4 | 초안 | https://help.instagram.com/351460621611097 |
| 2026-11-26 | WB-CAPTION-2.5 | 해시태그 최대 5개 | https://help.instagram.com/351460621611097 |
""" + extra
    props = json.dumps({
        "이름": TITLE, "버전": "WB-CAPTION-2.5",
        "마지막 수정": "2026-11-26", "마지막 수정자": "봉재",
    }, ensure_ascii=False)
    return (
        '<page url="' + PAGE_URL + '">\n<properties>\n' + props + '\n</properties>\n<content>\n'
        + body + '\n</content>\n</page>'
    )


def fetched(text):
    return {"id": PAGE_ID, "url": PAGE_URL, "title": TITLE, "text": text}


def finding(url, claim, new_text, topic="reels", day="2026-11-20", action="add", old="", section=None, title="공식 안내"):
    return {
        "topic": topic, "claim": claim, "source_url": url, "source_date": day,
        "source_title": title, "section": section, "action": action,
        "new_text": new_text, "old_snippet": old,
    }


def grounded(domain, url):
    return {"groundingChunks": [{"web": {"domain": domain, "uri": url, "title": "source"}}]}


class MemoryState:
    def __init__(self, busy=False):
        self.names = []
        self.seconds = []
        self.busy = busy

    @contextlib.contextmanager
    def lease(self, seconds=3900, name="state/lease.json"):
        self.names.append(name)
        self.seconds.append(seconds)
        if self.busy:
            raise CloudError("다른 클라우드 실행이 진행 중입니다")
        yield


class Notion:
    def __init__(self, text=None, fail_fetch=None, fail_comment=False):
        self.text = text if text is not None else page_text()
        self.calls = []
        self.fail_fetch = fail_fetch
        self.fail_comment = fail_comment

    def call(self, name, args):
        self.calls.append((name, args))
        if name == "fetch":
            if self.fail_fetch is not None and args.get("id") == PAGE_URL:
                raise self.fail_fetch
            return fetched(self.text)
        if name == "update-page" and args.get("command") == "update_content":
            updated = self.text
            for change in args["content_updates"]:
                if updated.count(change["old_str"]) != 1:
                    return {"warnings": ["miss"]}
                updated = updated.replace(change["old_str"], change["new_str"], 1)
            self.text = updated
            return {}
        if name == "create-comment" and self.fail_comment:
            raise PilotError("Notion MCP 필수 도구를 확인하지 못했습니다: create-comment")
        return {}


def commands(notion, command):
    return [args for name, args in notion.calls if name == "update-page" and args.get("command") == command]


class KeepTests(unittest.TestCase):
    def test_trade_press_with_a_matching_grounded_domain_is_kept(self):
        raw = finding(TRADE_URL, "업계 기사가 릴스 캡션 길이를 전했다.", "릴스 캡션은 첫 줄에 핵심을 둔다.", topic="reels")
        kept, dropped = keep_findings([raw], grounded("socialmediatoday.com", TRADE_URL), dt.date(2026, 10, 27), TODAY)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["kind"], "trade")
        self.assertEqual(dropped, [])

    def test_a_url_missing_from_grounding_is_discarded(self):
        raw = finding(REELS_URL, "릴스 캡션 제한이 바뀌었다.", "릴스 캡션은 125자 안에서 쓴다.")
        kept, dropped = keep_findings([raw], grounded("example.com", "https://example.com/post"), dt.date(2026, 10, 27), TODAY)
        self.assertEqual(kept, [])
        self.assertEqual(dropped[0]["reason"], "unsourced")

    def test_a_google_search_redirect_does_not_ground_an_official_url(self):
        raw = finding(REELS_URL, "릴스 캡션 제한이 바뀌었다.", "릴스 캡션은 125자 안에서 쓴다.")
        metadata = grounded("google.com", "https://www.google.com/search?q=reels")
        kept, _dropped = keep_findings([raw], metadata, dt.date(2026, 10, 27), TODAY)
        self.assertEqual(kept, [])

    def test_dates_outside_the_window_are_discarded(self):
        raw = finding(REELS_URL, "릴스 캡션 제한이 바뀌었다.", "릴스 캡션은 125자 안에서 쓴다.", day="2026-09-01")
        kept, dropped = keep_findings([raw], grounded("about.instagram.com", REELS_URL), dt.date(2026, 10, 27), TODAY)
        self.assertEqual(kept, [])
        self.assertEqual(dropped[0]["reason"], "outside_window")

    def test_vendor_blogs_are_not_trade_sources(self):
        rows = [
            finding("https://later.com/blog/instagram-captions", "래터 블로그", "래터 블로그의 캡션 조언이다."),
            finding("https://blog.hootsuite.com/instagram-hashtags", "후트스위트 블로그", "후트스위트 블로그의 해시태그 조언이다."),
            finding("https://www.hootsuite.com/resources/instagram", "후트스위트 자료", "후트스위트 자료의 캡션 조언이다."),
            finding("https://sproutsocial.com/insights/instagram-captions", "스프라우트 블로그", "스프라우트 블로그의 캡션 조언이다."),
            finding("https://buffer.com/resources/instagram-captions", "버퍼 블로그", "버퍼 블로그의 캡션 조언이다."),
            finding("https://metricool.com/instagram-hashtags", "메트리쿨 블로그", "메트리쿨 블로그의 해시태그 조언이다."),
            finding("https://www.socialmediaexaminer.com/instagram-captions", "소셜 미디어 이그재미너", "소셜 미디어 이그재미너의 캡션 조언이다."),
        ]
        domains = (
            "later.com", "blog.hootsuite.com", "hootsuite.com", "sproutsocial.com",
            "buffer.com", "metricool.com", "socialmediaexaminer.com",
        )
        metadata = {"groundingChunks": [{"web": {"domain": domain, "uri": row["source_url"]}} for domain, row in zip(domains, rows)]}
        kept, dropped = keep_findings(rows, metadata, dt.date(2026, 10, 27), TODAY)
        self.assertEqual(kept, [])
        self.assertEqual(len(dropped), len(rows))
        self.assertTrue(all(item["reason"] == "unsourced" for item in dropped))

    def test_a_homepage_and_an_unknown_host_are_discarded(self):
        rows = [
            finding("https://about.instagram.com/", "본문", "본문 문장입니다."),
            finding("https://random.example/post", "본문", "본문 문장입니다."),
        ]
        kept, dropped = keep_findings(rows, grounded("about.instagram.com", "https://about.instagram.com/blog/post"), dt.date(2026, 10, 27), TODAY)
        self.assertEqual(kept, [])
        self.assertEqual(len(dropped), 2)


class ResearchParseTests(unittest.TestCase):
    def test_fenced_json_and_grounded_request_shape(self):
        payload = {"findings": [finding(REELS_URL, "릴스 캡션 제한이 바뀌었다.", "릴스 캡션은 125자 안에서 쓴다.")]}
        self.assertEqual(len(parse_research("```json\n" + json.dumps(payload) + "\n```")), 1)
        transport = mock.Mock()
        transport.json.return_value = {
            "candidates": [{
                "finishReason": "STOP",
                "content": {"parts": [
                    {"thought": True, "text": "not json"},
                    {"text": json.dumps(payload, ensure_ascii=False)},
                ]},
                "groundingMetadata": grounded("about.instagram.com", REELS_URL),
            }],
        }
        raw, metadata = research(transport, "waterbomb-sns-pipeline", "gemini-3.8-flash", dt.date(2026, 10, 27), TODAY)
        self.assertEqual(raw[0]["source_url"], REELS_URL)
        self.assertIn("groundingChunks", metadata)
        body = transport.json.call_args.args[2]
        self.assertEqual(list(body["tools"][0]), ["googleSearch"])
        self.assertNotIn("responseMimeType", body["generationConfig"])
        self.assertIn("2026-10-27", body["contents"][0]["parts"][0]["text"])
        self.assertEqual(transport.json.call_args.kwargs["timeout"], 180)
        self.assertIn("/locations/global/", transport.json.call_args.args[1])

    def test_a_partial_model_response_does_not_become_an_empty_month(self):
        transport = mock.Mock()
        transport.json.return_value = {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "{}"}]}}]}
        with self.assertRaises(PolicyError):
            research(transport, "waterbomb-sns-pipeline", "gemini-3.8-flash", dt.date(2026, 10, 27), TODAY)


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.page = caption_policy.parse_fetch(fetched(page_text()))

    def test_two_findings_bump_the_minor_version_once_and_keep_history(self):
        rows = [
            finding(REELS_URL, "릴스 캡션은 첫 줄이 잘린다.", "릴스 캡션은 첫 줄에 핵심을 둔다.", topic="reels", section=3),
            finding(AD_URL, "광고 문구에 가격 표기 기준이 늘었다.", "광고 문구의 가격은 결제 금액과 같게 쓴다.", topic="ad_copy", day="2026-11-18", section=7),
        ]
        kept, _dropped = keep_findings(rows, {"groundingChunks": [
            {"web": {"domain": "about.instagram.com", "uri": REELS_URL}},
            {"web": {"domain": "facebook.com", "uri": AD_URL}},
        ]}, dt.date(2026, 10, 27), TODAY)
        plan = plan_updates(self.page, kept, [], TODAY)
        self.assertEqual(plan["version"], "WB-CAPTION-2.5")
        self.assertEqual(plan["next_version"], "WB-CAPTION-2.6")
        preview = self.page["content"]
        for change in plan["content_updates"]:
            self.assertEqual(preview.count(change["old_str"]), 1)
            preview = preview.replace(change["old_str"], change["new_str"], 1)
        self.assertIn("WB-CAPTION-2.6", preview)
        self.assertIn("WB-CAPTION-2.4", preview)
        self.assertIn(HASHTAG, preview)
        self.assertIn(PROTECTED_SOURCE, preview)
        self.assertIn("업데이트: 2026-12-01 | 요청자: 메타 정책 월간 점검 | 반영: 캡션 규정 동기화 | 이번 변경:", preview)
        self.assertIn("릴스 캡션은 첫 줄에 핵심을 둔다.", preview)
        self.assertIn("자동 점검", preview)
        self.assertEqual(plan["properties"]["버전"], "WB-CAPTION-2.6")
        self.assertEqual(plan["properties"]["마지막 수정자"], "캡션 규정 동기화")
        self.assertEqual(plan["properties"]["마지막 수정"], "2026-12-01")
        self.assertTrue(all(set(change) == {"old_str", "new_str"} for change in plan["content_updates"]))

    def test_trade_press_is_reported_and_does_not_join_an_official_edit(self):
        official = finding(REELS_URL, "릴스 캡션은 첫 줄이 잘린다.", "릴스 캡션은 첫 줄에 핵심을 둔다.", section=3)
        trade = finding(
            "https://www.adweek.com/social-marketing/instagram-caption-change",
            "애드위크가 캡션 길이 변화를 전했다.", "애드위크가 전한 캡션 길이 변화다.",
            topic="captions", day="2026-11-19",
        )
        kept, _dropped = keep_findings([official, trade], {"groundingChunks": [
            {"web": {"domain": "about.instagram.com", "uri": REELS_URL}},
            {"web": {"domain": "adweek.com", "uri": trade["source_url"]}},
        ]}, dt.date(2026, 10, 27), TODAY)
        plan = plan_updates(self.page, kept, [], TODAY)
        preview = self.page["content"]
        for change in plan["content_updates"]:
            preview = preview.replace(change["old_str"], change["new_str"], 1)
        self.assertIn("릴스 캡션은 첫 줄에 핵심을 둔다.", preview)
        self.assertNotIn("애드위크가 전한 캡션 길이", preview)
        self.assertIn("업계 매체 보도", plan["report"])
        self.assertIn("https://adweek.com/social-marketing/instagram-caption-change", plan["report"])
        self.assertIn("2026-11-19", plan["report"])
        self.assertEqual(plan["next_version"], "WB-CAPTION-2.6")

    def test_trade_press_cannot_change_the_five_hashtag_rule(self):
        raw = finding(
            TRADE_URL, "해시태그 최대 30개까지 쓸 수 있다.", "해시태그는 최대 30개까지 쓴다.",
            topic="hashtags", day="2026-11-27", old=HASHTAG, action="revise",
        )
        kept, _dropped = keep_findings([raw], grounded("socialmediatoday.com", TRADE_URL), dt.date(2026, 10, 27), TODAY)
        plan = plan_updates(self.page, kept, [], TODAY)
        self.assertEqual(plan["content_updates"], [])
        self.assertEqual(plan["applied"], [])
        self.assertIn(UNCHANGED_LINE, plan["report"])
        self.assertIn("업계 매체 보도", plan["report"])
        self.assertIn("https://socialmediatoday.com/news/instagram-hashtag-count", plan["report"])
        self.assertIn("2026-11-27", plan["report"])
        self.assertNotIn("최대 30개", self.page["content"])

    def test_a_later_official_source_can_replace_the_hashtag_sentence(self):
        raw = finding(
            OFFICIAL_CAP_URL, "고객센터가 해시태그 최대 30개로 바꿨다.",
            "해시태그는 게시 목적에 맞게 최대 30개까지 고른다.",
            topic="hashtags", day="2026-11-27", old=HASHTAG, action="revise", section=3,
        )
        kept, _dropped = keep_findings([raw], grounded("help.instagram.com", OFFICIAL_CAP_URL), dt.date(2026, 10, 27), TODAY)
        plan = plan_updates(self.page, kept, [], TODAY)
        preview = self.page["content"]
        for change in plan["content_updates"]:
            preview = preview.replace(change["old_str"], change["new_str"], 1)
        self.assertNotIn("at most 5", preview.split("# 4.")[0])
        self.assertIn("최대 30개", preview)
        self.assertIn(PROTECTED_SOURCE, preview)
        self.assertIn("WB-CAPTION-2.4", preview)

    def test_the_same_help_article_on_the_ceo_date_does_not_move_the_cap(self):
        raw = finding(
            PROTECTED_SOURCE, "해시태그 최대 30개라는 해석", "해시태그는 최대 30개까지 쓴다.",
            topic="hashtags", day="2026-11-26", old=HASHTAG, action="revise",
        )
        kept, _dropped = keep_findings([raw], grounded("help.instagram.com", PROTECTED_SOURCE), dt.date(2026, 10, 22), dt.date(2026, 11, 26))
        plan = plan_updates(self.page, kept, [], dt.date(2026, 11, 26))
        self.assertEqual(plan["content_updates"], [])
        self.assertEqual(plan["skipped"][0]["reason"], "protected_hashtag_rule")

    def test_a_non_official_source_cannot_delete_an_existing_rule(self):
        sentence = "금지어를 본문에 넣지 않는다."
        raw = finding(
            TRADE_URL, "금지어 규칙을 뺐다.", "금지어 규칙을 뺀다.",
            topic="captions", action="remove", old=sentence, section=5,
        )
        kept, _dropped = keep_findings([raw], grounded("socialmediatoday.com", TRADE_URL), dt.date(2026, 10, 27), TODAY)
        plan = plan_updates(self.page, kept, [], TODAY)
        self.assertEqual(plan["content_updates"], [])
        self.assertEqual(plan["applied"], [])
        self.assertIn("업계 매체 보도", plan["report"])
        self.assertIn("https://socialmediatoday.com/news/instagram-hashtag-count", plan["report"])
        self.assertIn(sentence, self.page["content"])

    def test_text_already_in_the_page_is_not_edited(self):
        sentence = "AI 초안은 사람이 고친 뒤 게시한다."
        raw = finding(REELS_URL, sentence, sentence, topic="captions", section=6)
        kept, _dropped = keep_findings([raw], grounded("about.instagram.com", REELS_URL), dt.date(2026, 10, 27), TODAY)
        plan = plan_updates(self.page, kept, [], TODAY)
        self.assertEqual(plan["content_updates"], [])
        self.assertEqual(plan["skipped"][0]["reason"], "already_present")
        self.assertIn(UNCHANGED_LINE, plan["report"])

    def test_missing_sections_block_a_write(self):
        text = page_text().replace("# 4. 말투\n구어체로 쓴다.\n\n", "")
        page = caption_policy.parse_fetch(fetched(text))
        raw = finding(REELS_URL, "릴스 캡션은 첫 줄이 잘린다.", "릴스 캡션은 첫 줄에 핵심을 둔다.", section=3)
        kept, _dropped = keep_findings([raw], grounded("about.instagram.com", REELS_URL), dt.date(2026, 10, 27), TODAY)
        with self.assertRaises(PolicyError):
            plan_updates(page, kept, [], TODAY)

    def test_an_html_changelog_row_is_appended(self):
        table = "<table><tr><td>날짜</td><td>버전</td><td>변경</td><td>근거</td></tr></table>"
        old, new = caption_policy._append_html(table, TODAY, "WB-CAPTION-2.6", "릴스 (자동 점검)", [REELS_URL])
        self.assertIn("WB-CAPTION-2.6", new)
        self.assertIn(old.split("</table>")[0], new)


class RunTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"GOOGLE_CLOUD_PROJECT": "waterbomb-sns-pipeline"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _run(self, notion, researcher, dry_run=False, state=None):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = run(NOW, mock.Mock(), notion=notion, dry_run=dry_run, researcher=researcher,
                       state=state or MemoryState())
        return code, output.getvalue(), notion

    def test_a_real_change_is_written_in_place_and_reported(self):
        notion = Notion()
        raw = finding(REELS_URL, "릴스 캡션은 첫 줄이 잘린다.", "릴스 캡션은 첫 줄에 핵심을 둔다.", section=3)

        def researcher(start, end):
            return [raw], grounded("about.instagram.com", REELS_URL)

        code, logs, notion = self._run(notion, researcher)
        self.assertEqual(code, 0)
        self.assertIn("caption_policy_done", logs)
        self.assertIn('"outcome": "updated"', logs)
        edits = commands(notion, "update_content")
        self.assertEqual(len(edits), 1)
        self.assertNotIn("replace_content", [args.get("command") for _name, args in notion.calls])
        props = commands(notion, "update_properties")
        self.assertEqual(props[0]["properties"]["버전"], "WB-CAPTION-2.6")
        comments = [args for name, args in notion.calls if name == "create-comment"]
        self.assertIn(REELS_URL, comments[0]["markdown"])
        self.assertIn("WB-CAPTION-2.6", notion.text)
        self.assertIn(HASHTAG, notion.text)
        self.assertNotIn(MODERATION_LEASE, json.dumps([name for name, _args in notion.calls]))

    def test_the_job_takes_only_the_caption_lease(self):
        state = MemoryState()
        notion = Notion()

        def researcher(start, end):
            return [], {}

        code, logs, _notion = self._run(notion, researcher, state=state)
        self.assertEqual(code, 0)
        self.assertEqual(state.names, [CAPTION_POLICY_LEASE])
        self.assertNotIn(MODERATION_LEASE, state.names)
        self.assertLessEqual(state.seconds[0], 960)
        self.assertIn(UNCHANGED_LINE, logs)
        self.assertEqual(commands(notion, "update_content"), [])

    def test_a_busy_caption_lease_defers_without_calling_notion(self):
        notion = Notion()
        called = {"n": 0}

        def researcher(start, end):
            called["n"] += 1
            return [], {}

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = run(NOW, mock.Mock(), notion=notion, researcher=researcher, state=MemoryState(busy=True))
        self.assertEqual(code, 0)
        self.assertIn("caption_policy_deferred", output.getvalue())
        self.assertEqual(notion.calls, [])
        self.assertEqual(called["n"], 0)

    def test_dry_run_writes_nothing(self):
        notion = Notion()
        raw = finding(REELS_URL, "릴스 캡션은 첫 줄이 잘린다.", "릴스 캡션은 첫 줄에 핵심을 둔다.", section=3)

        def researcher(start, end):
            return [raw], grounded("about.instagram.com", REELS_URL)

        code, logs, notion = self._run(notion, researcher, dry_run=True)
        self.assertEqual(code, 0)
        self.assertIn("미리보기입니다", logs)
        self.assertIn('"outcome": "dry_run"', logs)
        self.assertEqual([name for name, _args in notion.calls if name != "fetch"], [])
        self.assertNotIn("WB-CAPTION-2.6", notion.text)

    def test_trade_only_report_does_not_edit_the_rulebook(self):
        notion = Notion()
        raw = finding(TRADE_URL, "업계 기사가 릴스 캡션 길이를 전했다.", "릴스 캡션은 첫 줄에 핵심을 둔다.", day="2026-11-20")

        def researcher(start, end):
            return [raw], grounded("socialmediatoday.com", TRADE_URL)

        code, logs, notion = self._run(notion, researcher)
        self.assertEqual(code, 0)
        self.assertIn('"outcome": "unchanged"', logs)
        self.assertIn('"reported": 1', logs)
        self.assertEqual(commands(notion, "update_content"), [])
        self.assertEqual(commands(notion, "update_properties"), [])
        comment = [args for name, args in notion.calls if name == "create-comment"][0]["markdown"]
        self.assertIn(UNCHANGED_LINE, comment)
        self.assertIn("업계 매체 보도", comment)
        self.assertIn("https://socialmediatoday.com/news/instagram-hashtag-count", comment)
        self.assertIn("2026-11-20", comment)
        self.assertNotIn("WB-CAPTION-2.6", notion.text)

    def test_no_change_posts_the_exact_line_and_does_not_edit_rules(self):
        notion = Notion()

        def researcher(start, end):
            return [], {}

        code, logs, notion = self._run(notion, researcher)
        self.assertEqual(code, 0)
        self.assertIn(UNCHANGED_LINE, logs)
        comment = [args for name, args in notion.calls if name == "create-comment"][0]
        self.assertIn(UNCHANGED_LINE, comment["markdown"])
        self.assertEqual(commands(notion, "update_content"), [])
        self.assertEqual(commands(notion, "update_properties"), [])

    def test_missing_comment_tool_uses_a_toggle_without_bumping_the_version(self):
        notion = Notion(fail_comment=True)

        def researcher(start, end):
            return [], {}

        code, logs, notion = self._run(notion, researcher)
        self.assertEqual(code, 0)
        self.assertIn('"report_channel": "toggle"', logs)
        inserted = commands(notion, "insert_content")
        self.assertEqual(len(inserted), 1)
        self.assertIn(UNCHANGED_LINE, inserted[0]["content"])
        self.assertIn("메타 정책 월간 점검", inserted[0]["content"])
        self.assertEqual(commands(notion, "update_content"), [])
        self.assertNotIn("WB-CAPTION-2.6", notion.text)

    def test_access_denied_names_the_share_step_and_skips_research(self):
        notion = Notion(fail_fetch=McpToolError("fetch", {"structuredContent": {"code": "object_not_found", "status": 404}}))
        called = {"n": 0}

        def researcher(start, end):
            called["n"] += 1
            return [], {}

        with self.assertRaises(AccessError) as caught:
            self._run(notion, researcher)
        self.assertIn("MADEONE SNS Comments Pipeline", str(caught.exception))
        self.assertIn(PAGE_URL, str(caught.exception))
        self.assertEqual(called["n"], 0)
        self.assertEqual(commands(notion, "update_content"), [])

    def test_a_stale_property_after_a_written_row_is_repaired_without_a_new_version(self):
        extra = "| 2026-12-01 | WB-CAPTION-2.6 | 릴스 (자동 점검) | " + REELS_URL + " |\n"
        notion = Notion(page_text(extra))

        def researcher(start, end):
            return [], {}

        code, logs, notion = self._run(notion, researcher)
        self.assertEqual(code, 0)
        self.assertIn('"outcome": "repaired"', logs)
        self.assertEqual(commands(notion, "update_content"), [])
        props = commands(notion, "update_properties")[0]["properties"]
        self.assertEqual(props["마지막 수정"], "2026-12-01")
        self.assertNotIn("버전", props)

    def test_caption_job_cost_counts_as_instagram(self):
        self.assertEqual(workload({"resource_name": "sns-comments-caption-policy"}), "instagram")

    def test_source_does_not_take_the_moderation_lease(self):
        source = Path(caption_policy.__file__).read_text(encoding="utf-8")
        self.assertNotIn("MODERATION_LEASE", source)
        self.assertNotIn("state/lease.json", source)
        self.assertNotIn("state/moderation-lease.json", source)
        self.assertIn("state/caption-policy-lease.json", source)

    def test_runbook_has_the_schedule_and_manual_commands(self):
        text = (Path(__file__).resolve().parents[1] / "CLOUD.md").read_text(encoding="utf-8")
        self.assertIn("sns-comments-caption-policy", text)
        self.assertIn("--schedule='0 10 1 * *'", text)
        self.assertIn("--time-zone=Asia/Seoul", text)
        self.assertIn("SNS_CAPTION_POLICY_DRY_RUN=1", text)
        self.assertIn("caption_policy_done", text)
        self.assertIn("https://console.cloud.google.com/cloudscheduler?project=waterbomb-sns-pipeline", text)
        self.assertIn("https://console.cloud.google.com/run/jobs/details/asia-northeast3/sns-comments-caption-policy/executions?project=waterbomb-sns-pipeline", text)
        self.assertIn("업계 매체 보도", text)
        self.assertIn("gcloud run jobs update sns-comments-caption-policy", text)
        self.assertIn("--project=waterbomb-sns-pipeline", text)
        self.assertIn("python3 scripts/token_connection_release.py prepare", text)
        self.assertIn("python3 scripts/token_connection_release.py build", text)
        scheduler = text.split("gcloud scheduler jobs create http sns-comments-caption-policy", 1)[1].split("```", 1)[0]
        self.assertNotIn("SNS_CAPTION_POLICY_DRY_RUN", scheduler)


class MainTests(unittest.TestCase):
    def test_missing_bucket_fails_before_a_run(self):
        with mock.patch.dict(os.environ, {"SNS_STATE_BUCKET": "", "GOOGLE_CLOUD_PROJECT": "waterbomb-sns-pipeline"}):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = caption_policy.main([])
        self.assertEqual(code, 1)
        self.assertIn("caption_policy_failed", output.getvalue())

    def test_access_denied_is_its_own_log_line(self):
        with mock.patch.dict(os.environ, {"SNS_STATE_BUCKET": "waterbomb-sns-pipeline-state", "GOOGLE_CLOUD_PROJECT": "waterbomb-sns-pipeline"}):
            with mock.patch("sns_comment.caption_policy.run", side_effect=AccessError("공유가 필요합니다")):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = caption_policy.main([])
        self.assertEqual(code, 1)
        self.assertIn("caption_policy_access_denied", output.getvalue())
        self.assertNotIn("caption_policy_failed", output.getvalue())

    def test_dry_run_flag_and_env(self):
        self.assertTrue(caption_policy.dry_run_requested(True))
        with mock.patch.dict(os.environ, {"SNS_CAPTION_POLICY_DRY_RUN": "1"}):
            self.assertTrue(caption_policy.dry_run_requested(False))
        with mock.patch.dict(os.environ, {"SNS_CAPTION_POLICY_DRY_RUN": ""}):
            self.assertFalse(caption_policy.dry_run_requested(False))


if __name__ == "__main__":
    unittest.main()
