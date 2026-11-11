import copy
import datetime as dt
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from sns_comment.daily_merge import (CloudJournal, DEFAULT_POLICY, DailyMerger, Hold, JsonJournal,
                                     MergeApi, checked_policy, cloud_preflight, main, semantic)
from sns_comment.notion import NotionError, block


SOURCE = "1" * 32
MANUAL = "2" * 32
AUTO = "3" * 32
DAY = "2026-11-09"
POLICY = {**DEFAULT_POLICY, "template_sections": ["SNS Comments"]}


def page(identifier, origin):
    return {"id": identifier, "parent": {"type": "data_source_id", "data_source_id": SOURCE},
            "in_trash": False, "created_time": "2026-11-01T00:00:00Z" if identifier == MANUAL else "2026-11-02T00:00:00Z", "last_edited_time": "2026-11-09T00:00:00Z",
            "properties": {"이름": {"type": "title", "title": [{"type": "text", "text": {"content": "Monday"}}]},
                           "유형": {"type": "select", "select": {"name": "오늘 업무"}},
                           "이벤트 시간": {"type": "date", "date": {"start": DAY, "end": None}},
                           "생성 방식": {"type": "select", "select": {"name": origin}},
                           "참석자": {"type": "people", "people": [{"object": "user", "id": "9" * 32}]}}}


class FakeApi(MergeApi):
    def __init__(self):
        self.source_id = SOURCE
        self.pages = {MANUAL: page(MANUAL, "사전 작성"), AUTO: page(AUTO, "정기 자동화")}
        self.blocks = {}
        self.roots = {MANUAL: [], AUTO: []}
        self.counter = 100
        self.writes = []
        self.comments = set()
        self.deny_comments = False
        self.lost_ack = False
        self.fail_append = False
        self.crash_append = False
        self.crash_trash = False
        self.fail_restore = False
        self.after_append = None
        self.after_trash = None
        self.add(MANUAL, block("heading_3", "SNS Comments"))
        self.add(MANUAL, block("paragraph", "Previously entered schedule"))
        self.add(AUTO, block("heading_3", "SNS Comments"))
        self.add(AUTO, block("toggle", "Details", children=[block("quote", "Keep every word"), block("to_do", "Follow up", checked=True)]))

    def add(self, parent, payload):
        self.counter += 1
        identifier = f"{self.counter:032x}"
        kind = payload["type"]
        value = copy.deepcopy(payload[kind])
        children = value.pop("children", [])
        node = {"id": identifier, "type": kind, kind: value, "has_children": bool(children)}
        self.blocks[identifier] = node
        self.roots.setdefault(parent, []).append(identifier)
        self.roots[identifier] = []
        for child in children:
            self.add(identifier, child)
        if parent in self.blocks:
            self.blocks[parent]["has_children"] = True
        return node

    def page(self, identifier):
        return copy.deepcopy(self.pages[identifier])

    def daily_pages(self, start, end):
        return [self.page(key) for key, value in self.pages.items() if not value["in_trash"]]

    def children(self, parent):
        return [copy.deepcopy(self.blocks[key]) for key in self.roots[parent]]

    def no_comments(self, identifier):
        if self.deny_comments:
            raise NotionError("not authorized")
        if identifier in self.comments:
            raise Hold("comments_require_preservation")

    def markdown_hash(self, identifier, allow_empty=False):
        from sns_comment.daily_merge import digest
        return digest([semantic(node) for node in self.tree(identifier)])

    def append(self, parent, payload, position=None):
        self.writes.append(("append", parent))
        if self.fail_append:
            raise NotionError("ambiguous network failure")
        result = self.add(parent, payload)
        if position is not None:
            siblings = self.roots[parent]
            siblings.remove(result["id"])
            index = 0 if position["type"] == "start" else siblings.index(position["after_block"]["id"]) + 1
            siblings.insert(index, result["id"])
        if self.after_append:
            self.after_append(result)
        if self.crash_append:
            self.crash_append = False
            raise KeyboardInterrupt
        if self.lost_ack:
            self.lost_ack = False
            raise NotionError("response lost after successful append")
        return copy.deepcopy(result)

    def update_properties(self, identifier, properties):
        self.writes.append(("properties", identifier))
        for name, value in properties.items():
            self.pages[identifier]["properties"][name].update(copy.deepcopy(value))

    def trash(self, identifier, value):
        self.writes.append(("trash", identifier, value))
        if not value and self.fail_restore:
            raise NotionError("restore failed")
        self.pages[identifier]["in_trash"] = value
        self.pages[identifier]["last_edited_time"] = "2026-11-09T01:00:00Z"
        if value and self.after_trash:
            self.after_trash()
        if value and self.crash_trash:
            self.crash_trash = False
            raise KeyboardInterrupt


class DailyMergeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "journal.json"
        self.api = FakeApi()
        self.journal = JsonJournal(self.path)
        self.merger = DailyMerger(self.api, self.journal, POLICY)

    def merge(self, apply=True, **kwargs):
        return self.merger.merge_pair(MANUAL, AUTO, apply=apply, **kwargs)

    def assert_source_preserved(self):
        self.assertFalse(self.api.pages[AUTO]["in_trash"])
        self.assertFalse(any(write[0] == "trash" and write[2] for write in self.api.writes))

    def test_plan_is_read_only_and_does_not_create_a_journal(self):
        self.assertEqual(self.merge(apply=False)["status"], "ready")
        self.assertEqual(self.api.writes, [])
        self.assertFalse(self.path.exists())

    def test_nested_additions_merge_inline_and_preserve_original_ids_before_trash(self):
        original = self.api.tree(MANUAL)
        source = [semantic(node) for node in self.api.tree(AUTO)]
        result = self.merge()
        self.assertEqual(result["status"], "complete")
        self.assertTrue(self.api.pages[AUTO]["in_trash"])
        self.assertEqual(self.api.tree(MANUAL)[:2], original)
        merged = self.api.tree(MANUAL)
        self.assertEqual(semantic(merged[-1]), source[-1])
        self.assertEqual(sum(node["type"] == "heading_3" for node in merged), 1)
        saved = next(iter(self.journal.data.values()))["source"]["page"]
        self.assertEqual(saved["properties"], self.api.pages[AUTO]["properties"])
        self.assertEqual(self.api.writes[-1], ("trash", AUTO, True))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_rerun_does_not_duplicate_or_retrash(self):
        self.merge(); before = list(self.api.writes)
        self.assertTrue(self.merge()["already_complete"])
        self.assertEqual(self.api.writes, before)
        self.assertEqual(self.merger.run(DAY, DAY, apply=True)["results"], [])

    def test_manual_restore_is_respected(self):
        self.merge(); self.api.pages[AUTO]["in_trash"] = False
        before = list(self.api.writes)
        self.assertEqual(self.merge()["reason"], "source_restored_after_merge")
        self.assertEqual(self.api.writes, before)

    def test_user_selected_oldest_page_rule_does_not_require_origin_property(self):
        self.api.pages[AUTO]["properties"].pop("생성 방식")
        self.assertEqual(self.merge()["status"], "complete")

    def test_equal_oldest_creation_times_are_held(self):
        self.api.pages[AUTO]["created_time"] = self.api.pages[MANUAL]["created_time"]
        result = self.merger.run(DAY, DAY, apply=True)
        self.assertEqual(result["results"][0]["reason"], "oldest_page_not_unique")
        self.assertEqual(self.api.writes, [])

    def test_explicit_ids_cannot_reverse_oldest_page_rule(self):
        result = self.merger.merge_pair(AUTO, MANUAL, apply=True, explicit=True)
        self.assertEqual(result["reason"], "canonical_must_be_created_earlier")
        self.assertEqual(self.api.writes, [])

    def test_wrong_date_source_database_and_page_type_never_write(self):
        edits = [lambda p: p["properties"]["이벤트 시간"]["date"].update(start="2026-11-10"),
                 lambda p: p["parent"].update(data_source_id="f" * 32),
                 lambda p: p["properties"]["유형"]["select"].update(name="회의")]
        for edit in edits:
            with self.subTest(edit=edit):
                self.api.pages[AUTO] = page(AUTO, "정기 자동화"); edit(self.api.pages[AUTO])
                self.assertEqual(self.merge()["status"], "held")
                self.assertEqual(self.api.writes, [])

    def test_child_database_page_synced_block_and_unknown_block_block_all_writes(self):
        for kind in ("child_database", "child_page", "synced_block", "unsupported", "column_list"):
            with self.subTest(kind=kind):
                api = FakeApi(); api.add(AUTO, {"type": kind, kind: {"title": "Keep"}})
                result = DailyMerger(api, JsonJournal(Path(self.temp.name) / kind), POLICY).merge_pair(MANUAL, AUTO, apply=True)
                self.assertEqual(result["reason"], "unsupported_block:" + kind)
                self.assertEqual(api.writes, [])

    def test_hosted_media_relation_and_truncated_properties_block_trash(self):
        for case in ("media", "relation", "truncated"):
            with self.subTest(case=case):
                api = FakeApi()
                if case == "media": api.add(AUTO, {"type": "image", "image": {"type": "file", "file": {"url": "https://temporary.example/file"}}})
                if case == "relation": api.pages[AUTO]["properties"]["Related"] = {"type": "relation", "relation": [{"id": MANUAL}]}
                if case == "truncated": api.pages[AUTO]["properties"]["참석자"]["has_more"] = True
                result = DailyMerger(api, JsonJournal(Path(self.temp.name) / case), POLICY).merge_pair(MANUAL, AUTO, apply=True)
                self.assertEqual(result["status"], "held"); self.assertEqual(api.writes, [])

    def test_comments_or_missing_comment_permission_block_all_writes(self):
        for denied in (False, True):
            self.api.deny_comments = denied
            self.api.comments = set() if denied else {self.api.roots[AUTO][0]}
            self.assertEqual(self.merge()["status"], "held")
            self.assertEqual(self.api.writes, [])

    def test_internal_link_is_held_instead_of_becoming_a_dangling_copy(self):
        self.api.add(AUTO, block("paragraph", "old page", rich_text=[{"type": "text", "text": {"content": "Original", "link": {"url": "https://app.notion.com/" + AUTO}}}]))
        self.assertEqual(self.merge()["reason"], "source_internal_reference")
        self.assert_source_preserved()

    def test_source_edit_during_copy_preserves_source_and_never_trashes(self):
        def edit(_):
            self.api.blocks[self.api.roots[AUTO][0]]["heading_3"]["rich_text"][0]["text"]["content"] = "New human note"
        self.api.after_append = edit
        self.assertEqual(self.merge()["reason"], "source_changed")
        self.assert_source_preserved()

    def test_corrupted_copy_is_detected_by_content_not_only_block_count(self):
        def corrupt(node):
            if node["type"] == "quote":
                node["quote"]["rich_text"][0]["text"]["content"] = "Wrong content"
        self.api.after_append = corrupt
        self.assertEqual(self.merge()["reason"], "copied_content_not_equal")
        self.assert_source_preserved()

    def test_lost_append_ack_is_reconciled_without_duplicate_copy(self):
        self.api.lost_ack = True
        self.assertEqual(self.merge()["status"], "complete")
        self.assertEqual(len(self.api.roots[MANUAL]), 3)

    def test_unknown_append_outcome_is_not_blindly_retried(self):
        self.api.fail_append = True
        self.assertEqual(self.merge()["reason"], "append_outcome_uncertain")
        before = len(self.api.writes)
        self.api.fail_append = False
        self.assertEqual(self.merge()["reason"], "append_outcome_uncertain")
        self.assertEqual(len(self.api.writes), before)
        self.assert_source_preserved()

    def test_process_crash_after_append_resumes_from_durable_intent(self):
        self.api.crash_append = True
        with self.assertRaises(KeyboardInterrupt): self.merge()
        restored = DailyMerger(self.api, JsonJournal(self.path), POLICY)
        self.assertEqual(restored.merge_pair(MANUAL, AUTO, apply=True)["status"], "complete")
        self.assertEqual(len(self.api.roots[MANUAL]), 3)

    def test_post_trash_concurrent_edit_restores_source(self):
        def edit():
            self.api.blocks[self.api.roots[AUTO][0]]["heading_3"]["rich_text"][0]["text"]["content"] = "Arrived during trash request"
        self.api.after_trash = edit
        self.assertEqual(self.merge()["status"], "held")
        self.assertFalse(self.api.pages[AUTO]["in_trash"])
        self.assertEqual(self.api.writes[-2:], [("trash", AUTO, True), ("trash", AUTO, False)])

    def test_malformed_post_trash_response_also_restores_source(self):
        self.api.after_trash=lambda:self.api.pages[AUTO].pop("properties")
        self.assertEqual(self.merge()["status"],"held")
        self.assertFalse(self.api.pages[AUTO]["in_trash"])

    def test_crashed_trash_is_recovered_before_active_page_query(self):
        self.api.crash_trash = True
        with self.assertRaises(KeyboardInterrupt): self.merge()
        self.assertTrue(self.api.pages[AUTO]["in_trash"])
        restored = DailyMerger(self.api, JsonJournal(self.path), POLICY)
        result = restored.run(DAY, DAY, apply=True)
        self.assertEqual(result["status"], "held")
        self.assertFalse(self.api.pages[AUTO]["in_trash"])
        self.assertEqual(self.api.writes[-1], ("trash", AUTO, False))

    def test_failed_restore_remains_recoverable_and_is_not_marked_complete(self):
        self.api.after_trash = lambda: self.api.comments.add(AUTO)
        self.api.fail_restore = True
        self.assertEqual(self.merge()["status"], "held")
        self.assertEqual(next(iter(JsonJournal(self.path).data.values()))["status"], "restore_required")
        self.api.fail_restore = False
        DailyMerger(self.api, JsonJournal(self.path), POLICY).run(DAY, DAY, apply=True)
        self.assertFalse(self.api.pages[AUTO]["in_trash"])

    def test_multiple_automation_pages_merge_into_one_prewritten_page(self):
        extra = "4" * 32
        self.api.pages[extra] = page(extra, "정기 자동화"); self.api.roots[extra] = []
        self.api.add(extra, block("heading_3", "SNS Comments"))
        self.api.add(extra, block("paragraph", "Second automatic page"))
        result = self.merger.run(DAY, DAY, apply=True)
        self.assertEqual([r["status"] for r in result["results"]], ["complete", "complete"])
        self.assertFalse(self.api.pages[MANUAL]["in_trash"])
        self.assertTrue(self.api.pages[AUTO]["in_trash"] and self.api.pages[extra]["in_trash"])

    def test_table_cells_and_nested_lists_are_preserved(self):
        self.api.add(AUTO, block("table", "", table_width=2, has_column_header=True, has_row_header=False,
            children=[{"type": "table_row", "table_row": {"cells": [[{"type":"text","text":{"content":"A"}}], []]}}]))
        # Tables have no rich_text field in the Notion API.
        self.api.blocks[self.api.roots[AUTO][-1]]["table"].pop("rich_text")
        self.assertEqual(self.merge()["status"], "complete")


def plain_code(node):
    return "".join(item["text"]["content"] for item in node["code"]["rich_text"])


class MergeConfigurationTests(unittest.TestCase):
    def test_registered_token_must_identify_the_expected_connection(self):
        api=MergeApi("synthetic",SOURCE)
        for identity in ({"type":"person","name":"WSH"},{"type":"bot","name":"Other"}):
            with mock.patch.object(api,"request",return_value=identity),self.assertRaises(Hold):
                api.verify_connection("WSH")
        with mock.patch.object(api,"request",return_value={"type":"bot","name":"WSH"}):
            api.verify_connection("WSH")

    def test_disabled_cloud_feature_never_reads_secret_or_journal(self):
        state=mock.Mock()
        with mock.patch("sns_comment.daily_merge.secret_token") as token:
            self.assertEqual(cloud_preflight({},state,dt.datetime.now(dt.timezone.utc)),{"status":"disabled"})
        token.assert_not_called();state.json_get.assert_not_called();state.json_put.assert_not_called()

    def test_cloud_plan_reads_but_does_not_mutate_notion_or_journal(self):
        state=mock.Mock();state.json_get.return_value=({},0)
        api=FakeApi()
        config={"daily_data_source_id":SOURCE,"daily_merge":{**POLICY,"enabled":True,"apply":False,"token_secret":"projects/example/secrets/wsh"}}
        with mock.patch("sns_comment.daily_merge.secret_token",return_value="synthetic"), \
                mock.patch("sns_comment.daily_merge.MergeApi",return_value=api):
            result=cloud_preflight(config,state,dt.datetime(2026,11,9,tzinfo=dt.timezone.utc))
        self.assertEqual(result["mode"],"plan");self.assertEqual(api.writes,[]);state.json_put.assert_not_called()

    def test_invalid_flags_and_ranges_cannot_enable_mutations(self):
        for policy in ({"enabled":"true"},{"apply":1},
                       {"lookback_days":True},{"lookahead_days":91}):
            with self.subTest(policy=policy),self.assertRaises(Hold):checked_policy(policy)

    def test_incomplete_comment_and_block_responses_are_not_treated_as_empty(self):
        api=MergeApi("synthetic",SOURCE)
        with mock.patch.object(api,"request",return_value={"results":[],"request_status":{"type":"incomplete"}}):
            with self.assertRaises(Hold):api.no_comments(AUTO)
            with self.assertRaises(Hold):api.children(AUTO)

    def test_block_pagination_reads_all_pages_and_repeated_cursor_is_held(self):
        api=MergeApi("synthetic",SOURCE)
        node={"id":"a"*32,"type":"paragraph","paragraph":{"rich_text":[]}}
        responses=[{"results":[node],"has_more":True,"next_cursor":"next"},{"results":[],"has_more":False}]
        with mock.patch.object(api,"request",side_effect=responses) as request:
            self.assertEqual(api.children(AUTO),[node])
            self.assertIn("start_cursor=next",request.call_args.args[1])
        with mock.patch.object(api,"request",return_value=responses[0]):
            with self.assertRaises(Hold):api.children(AUTO)

    def test_token_missing_never_constructs_a_network_client(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("sns_comment.daily_merge.MergeApi") as api, \
                mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(main(["plan", "--start", DAY, "--end", DAY]), 1)
        api.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["reason"], "merge_token_not_configured")

    def test_cloud_journal_uses_generation_guards(self):
        state = mock.Mock(); state.json_get.return_value = ({}, 7); state.json_put.return_value = 8
        journal = CloudJournal(state); journal.data["test"] = {"status": "copying"}; journal.save()
        self.assertEqual(state.json_put.call_args.args[2], 7)
        self.assertEqual(journal.generation, 8)


if __name__ == "__main__": unittest.main()
