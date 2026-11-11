import copy
from pathlib import Path
import unittest
from unittest import mock

import test_daily_merge as fixtures
from test_daily_merge import AUTO, DAY, MANUAL, POLICY, DailyMerger, FakeApi, JsonJournal, block
from sns_comment.daily_merge import Hold, checked_policy, semantic
from sns_comment.notion import NotionError, plain


class TemplateMergeTests(unittest.TestCase):
    """Real preservation cases for one visible page with common template headings."""
    merge = fixtures.DailyMergeTests.merge
    assert_source_preserved = fixtures.DailyMergeTests.assert_source_preserved

    def setUp(self):
        fixtures.DailyMergeTests.setUp(self)
        self.api = FakeApi()
        self.api.roots[MANUAL] = []; self.api.roots[AUTO] = []
        self.policy = {**POLICY, "template_sections": ["Develop", "SNS Comments"]}
        self.merger = DailyMerger(self.api, self.journal, self.policy)
        for identifier in (MANUAL, AUTO):
            self.api.add(identifier, block("heading_3", "Develop"))
            self.api.add(identifier, block("to_do", "Shared task", checked=False))
            self.api.add(identifier, {"type": "divider", "divider": {}})
            self.api.add(identifier, block("heading_3", "SNS Comments"))
            self.api.add(identifier, block("paragraph", "Shared comment"))

    def body(self):
        return self.api.tree(MANUAL)

    def test_zero_or_one_page_per_date_skips_all_body_checks_and_writes(self):
        old = copy.deepcopy(self.api.pages[MANUAL])
        newer = copy.deepcopy(self.api.pages[AUTO])
        newer["properties"]["이벤트 시간"]["date"]["start"] = "2026-11-10"
        # A single real template can contain unsupported DBs and should not
        # enter preservation checks at all. Creation order is irrelevant here.
        old.pop("created_time")
        self.api.add(MANUAL, {"type": "child_database", "child_database": {"title": "Meeting"}})
        for pages in ([], [old], [old, newer]):
            with self.subTest(page_count=len(pages)), \
                    mock.patch.object(self.api, "daily_pages", return_value=pages) as query, \
                    mock.patch.object(self.api, "page", side_effect=AssertionError("unexpected page read")), \
                    mock.patch.object(self.api, "children", side_effect=AssertionError("unexpected body read")), \
                    mock.patch.object(self.api, "no_comments", side_effect=AssertionError("unexpected comment read")):
                result = self.merger.run(DAY, "2026-11-10", apply=True)
                self.assertEqual(result, {"mode": "apply", "results": [], "status": "complete",
                                          "scanned_pages":len(pages),"single_page_dates":len(pages),"duplicate_dates":0,"merged_pages":0})
                query.assert_called_once_with(DAY, "2026-11-10")
                self.assertEqual(self.api.writes, [])
                self.assertFalse(self.path.exists())

    def test_new_information_is_inside_matching_sections_without_archive_toggle(self):
        self.api.add(AUTO, block("paragraph", "New comment"))
        self.api.add(MANUAL, block("paragraph", "Old personal note"))
        # The new task belongs before Develop's divider, not at the page bottom.
        added = self.api.add(AUTO, block("to_do", "New task", checked=True))
        self.api.roots[AUTO].remove(added["id"])
        self.api.roots[AUTO].insert(2, added["id"])
        previous_ids = [n["id"] for n in self.body()]
        self.assertEqual(self.merge()["status"], "complete")
        self.assertEqual([plain(n) for n in self.body()], ["Develop", "Shared task", "New task", "", "SNS Comments",
                                                        "Shared comment", "Old personal note", "New comment"])
        self.assertEqual([n["id"] for n in self.body() if n["id"] in previous_ids], previous_ids)
        self.assertFalse(any(n["type"] in {"toggle", "code"} for n in self.body()))

    def test_identical_pages_only_trash_new_page_without_copying_template(self):
        original = self.body()
        self.assertEqual(self.merge()["status"], "complete")
        self.assertEqual(self.body(), original)
        self.assertEqual(self.api.writes, [("trash", AUTO, True)])

    def test_read_only_plan_describes_additions_without_writes(self):
        self.api.add(AUTO, block("paragraph", "New information"))
        result = self.merge(apply=False)
        self.assertEqual(result["added_blocks"], 1)
        self.assertEqual(result["merge_strategy"], "template_inline_v2")
        self.assertEqual(self.api.writes, [])
        self.assertFalse(self.path.exists())

    def test_nested_container_keeps_old_id_and_unions_child_information(self):
        old = self.api.add(MANUAL, block("bulleted_list_item", "Campaign", children=[block("paragraph", "Existing detail")]))
        self.api.add(AUTO, block("bulleted_list_item", "Campaign", children=[block("paragraph", "Existing detail"), block("paragraph", "Extra detail")]))
        old_child = self.api.roots[old["id"]][0]
        self.assertEqual(self.merge()["status"], "complete")
        containers = [n for n in self.body() if plain(n) == "Campaign"]
        self.assertEqual(len(containers), 1)
        self.assertEqual(containers[0]["id"], old["id"])
        self.assertEqual([plain(n) for n in containers[0]["_children"]], ["Existing detail", "Extra detail"])
        self.assertEqual(containers[0]["_children"][0]["id"], old_child)

    def test_insert_between_shared_items_preserves_source_and_target_order(self):
        self.api.add(MANUAL, block("paragraph", "Ending"))
        self.api.add(AUTO, block("paragraph", "Inserted"))
        self.api.add(AUTO, block("paragraph", "Ending"))
        self.assertEqual(self.merge()["status"], "complete")
        self.assertEqual([plain(n) for n in self.body()][-3:], ["Shared comment", "Inserted", "Ending"])

    def test_empty_new_placeholders_do_not_duplicate_scaffolding(self):
        self.api.add(AUTO, block("paragraph", ""))
        self.api.add(AUTO, block("to_do", "", checked=False))
        self.assertEqual(self.merge()["status"], "complete")
        self.assertEqual(self.api.writes, [("trash", AUTO, True)])

    def test_intentional_repeated_content_keeps_maximum_occurrence_count(self):
        self.api.add(AUTO, block("paragraph", "Shared comment"))
        self.assertEqual(self.merge()["status"], "complete")
        self.assertEqual(sum(plain(n) == "Shared comment" for n in self.body()), 2)

    def test_different_template_missing_or_duplicate_heading_is_held_before_write(self):
        for mode in ("missing", "duplicate"):
            with self.subTest(mode=mode):
                api = copy.deepcopy(self.api)
                if mode == "missing":
                    api.roots[AUTO].pop(0)
                else:
                    api.add(AUTO, block("heading_3", "Develop"))
                result = DailyMerger(api, JsonJournal(Path(self.temp.name)/mode), self.policy).merge_pair(MANUAL, AUTO, True)
                self.assertEqual(result["status"], "held")
                self.assertEqual(api.writes, [])

    def test_non_template_report_heading_stays_with_its_original_section(self):
        report = self.api.add(AUTO, block("heading_3", "Ticket report", is_toggleable=True, children=[block("paragraph", "New sales")]))
        self.api.roots[AUTO].remove(report["id"]); self.api.roots[AUTO].insert(2, report["id"])
        self.assertEqual(self.merge()["status"], "complete")
        self.assertEqual([plain(n) for n in self.body()][:5], ["Develop", "Shared task", "Ticket report", "", "SNS Comments"])

    def test_changed_check_state_is_held_instead_of_overwriting_old_task(self):
        self.api.blocks[self.api.roots[AUTO][1]]["to_do"]["checked"] = True
        self.assertEqual(self.merge()["reason"], "conflicting_task_state")
        self.assertEqual(self.api.writes, [])

    def test_participants_and_tags_are_unioned_without_overwriting_existing_people(self):
        self.api.pages[AUTO]["properties"]["참석자"]["people"].append({"id": "8"*32})
        for identifier in (MANUAL, AUTO):
            self.api.pages[identifier]["properties"]["태그"] = {"type": "multi_select", "multi_select": [{"id": "tag1", "name": "Existing"}]}
        self.api.pages[AUTO]["properties"]["태그"]["multi_select"].append({"id": "tag2", "name": "New"})
        result = self.merge()
        self.assertEqual(result["status"], "complete")
        props = self.api.pages[MANUAL]["properties"]
        self.assertEqual([u["id"] for u in props["참석자"]["people"]], ["9"*32, "8"*32])
        self.assertEqual([u["id"] for u in props["태그"]["multi_select"]], ["tag1", "tag2"])
        self.assertEqual(self.api.writes, [("properties", MANUAL), ("trash", AUTO, True)])

    def test_new_scalar_information_fills_empty_field(self):
        for identifier in (MANUAL, AUTO):
            self.api.pages[identifier]["properties"]["URL"] = {"type": "url", "url": None}
        self.api.pages[AUTO]["properties"]["URL"]["url"] = "https://example.com/agenda"
        self.assertEqual(self.merge()["status"], "complete")
        self.assertEqual(self.api.pages[MANUAL]["properties"]["URL"]["url"], "https://example.com/agenda")

    def test_older_page_extra_participants_do_not_trigger_redundant_property_write(self):
        self.api.pages[MANUAL]["properties"]["참석자"]["people"].append({"id": "8"*32})
        self.assertEqual(self.merge()["status"], "complete")
        self.assertEqual(self.api.writes, [("trash", AUTO, True)])

    def test_all_seven_real_template_sections_merge_in_one_visible_page(self):
        from sns_comment.daily_merge import DEFAULT_POLICY
        api = FakeApi()
        api.roots[MANUAL] = []; api.roots[AUTO] = []
        for section in DEFAULT_POLICY["template_sections"]:
            for identifier in (MANUAL, AUTO):
                api.add(identifier, block("heading_3", section))
                api.add(identifier, block("paragraph", "Common " + section))
            api.add(AUTO, block("paragraph", "New " + section))
            for identifier in (MANUAL, AUTO):
                api.add(identifier, {"type": "divider", "divider": {}})
        result = DailyMerger(api, JsonJournal(Path(self.temp.name)/"seven-sections.json"), DEFAULT_POLICY).merge_pair(MANUAL, AUTO, True)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["added_blocks"], 7)
        visible = [plain(node) for node in api.tree(MANUAL)]
        self.assertEqual(visible, [part for section in DEFAULT_POLICY["template_sections"] for part in (section, "Common " + section, "New " + section, "")])

    def test_conflicting_scalar_properties_hold_before_copy(self):
        self.api.pages[AUTO]["properties"]["이름"]["title"][0]["text"]["content"] = "Different title"
        self.api.add(AUTO, block("paragraph", "New note"))
        self.assertEqual(self.merge()["reason"], "conflicting_page_property")
        self.assertEqual(self.api.writes, [])

    def test_property_ack_loss_reconciles_and_does_not_repeat_write(self):
        self.api.pages[AUTO]["properties"]["참석자"]["people"].append({"id": "8"*32})
        original = self.api.update_properties
        def update(*args):
            original(*args)
            raise NotionError("response lost")
        self.api.update_properties = update
        self.assertEqual(self.merge()["status"], "complete")
        self.assertEqual(sum(w[0] == "properties" for w in self.api.writes), 1)

    def test_crash_after_property_write_resumes_without_repeating_update(self):
        self.api.pages[AUTO]["properties"]["참석자"]["people"].append({"id": "8"*32})
        original = self.api.update_properties
        def update(*args):
            original(*args)
            raise KeyboardInterrupt
        self.api.update_properties = update
        with self.assertRaises(KeyboardInterrupt):
            self.merge()
        self.api.update_properties = original
        restored = DailyMerger(self.api, JsonJournal(self.path), self.policy)
        self.assertEqual(restored.merge_pair(MANUAL, AUTO, True)["status"], "complete")
        self.assertEqual(sum(w[0] == "properties" for w in self.api.writes), 1)

    def test_unrelated_canonical_edit_is_detected_and_source_kept(self):
        self.api.add(AUTO, block("paragraph", "Extra"))
        self.api.after_append = lambda _: self.api.add(MANUAL, block("paragraph", "Concurrent note"))
        self.assertEqual(self.merge()["status"], "held")
        self.assert_source_preserved()

    def test_oldest_selection_ignores_legacy_origin_labels(self):
        self.api.pages[MANUAL]["properties"]["생성 방식"]["select"]["name"] = "정기 자동화"
        self.api.pages[AUTO]["properties"]["생성 방식"]["select"]["name"] = "사전 작성"
        result = self.merger.run(DAY, DAY, True)
        self.assertEqual(result["results"][0]["canonical_id"], MANUAL)
        self.assertFalse(self.api.pages[MANUAL]["in_trash"])
        self.assertTrue(self.api.pages[AUTO]["in_trash"])

    def test_oldest_selection_requires_valid_creation_times(self):
        self.api.pages[AUTO]["created_time"] = "bad date"
        self.assertEqual(self.merger.run(DAY, DAY, True)["results"][0]["reason"], "page_creation_time_not_verified")
        self.assertEqual(self.api.writes, [])

    def test_table_rows_are_merged_in_one_existing_table(self):
        def table(rows):
            return {"type": "table", "table": {"table_width": 1, "has_column_header": True, "has_row_header": False,
                "children": [{"type": "table_row", "table_row": {"cells": [[{"type": "text", "text": {"content": row}}]]}} for row in rows]}}
        old = self.api.add(MANUAL, table(["Header", "Old row"]))
        self.api.add(AUTO, table(["Header", "Old row", "New row"]))
        self.assertEqual(self.merge()["status"], "complete")
        tables = [n for n in self.body() if n["type"] == "table"]
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0]["id"], old["id"])
        self.assertEqual(len(tables[0]["_children"]), 3)

    def test_changed_table_header_is_held_before_any_write(self):
        def table(header):
            return {"type": "table", "table": {"table_width": 1, "has_column_header": True, "has_row_header": False,
                "children": [{"type": "table_row", "table_row": {"cells": [[{"type": "text", "text": {"content": header}}]]}}]}}
        self.api.add(MANUAL, table("Old")); self.api.add(AUTO, table("New"))
        self.assertEqual(self.merge()["reason"], "table_header_conflict")
        self.assertEqual(self.api.writes, [])

    def test_legacy_appendix_journal_is_not_resumed_under_new_strategy(self):
        from sns_comment.daily_merge import digest
        key = digest([self.api.source_id, MANUAL, AUTO])
        self.journal.data[key] = {"status": "copying", "day": DAY, "canonical_id": MANUAL, "source_id": AUTO}
        self.journal.save()
        self.assertEqual(self.merge()["reason"], "legacy_merge_journal_requires_review")
        self.assertEqual(self.api.writes, [])

    def test_invalid_template_configuration_cannot_run(self):
        for sections in ([], "SNS Comments", [""], ["SNS Comments", "SNS Comments"], [" SNS Comments"]):
            with self.subTest(sections=sections), self.assertRaises(Hold):
                checked_policy({"template_sections": sections})
