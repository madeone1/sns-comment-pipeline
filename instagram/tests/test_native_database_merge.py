import copy
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock
import urllib.parse

from test_daily_merge import AUTO, DAY, MANUAL, SOURCE, FakeApi, block
from sns_comment.daily_merge import DailyMerger, JsonJournal, DEFAULT_POLICY
from sns_comment.notion import NotionError, plain
from sns_comment.native_database import snapshot


OLD_DB = "a" * 32
NEW_DB = "b" * 32
OLD_DS = "c" * 32
NEW_DS = "d" * 32
OLD_ROW = "e" * 32
NEW_ROW = "f" * 32


class NativeApi(FakeApi):
    preserve_child_databases = True
    allow_template_toggle_buttons = True

    def __init__(self, linked=False):
        super().__init__()
        self.roots[MANUAL] = []; self.roots[AUTO] = []
        self.databases = {}; self.schemas = {}; self.views = {}; self.requests = []
        self.lost_move_ack = False; self.crash_move = False; self.fail_position = False
        self.fail_native_restore = False; self.after_move = None
        for owner, db_id, ds_id, row_id in ((MANUAL, OLD_DB, OLD_DS, OLD_ROW), (AUTO, NEW_DB, NEW_DS, NEW_ROW)):
            self.add(owner, block("heading_3", "Meeting"))
            self.add(owner, block("paragraph", "Meeting guidance"))
            self.blocks[db_id] = {"id": db_id, "type": "child_database", "child_database": {"title": "Meetings"}, "has_children": False}
            self.roots[owner].append(db_id); self.roots[db_id] = []
            self.add(owner, {"type": "divider", "divider": {}})
            self.add(owner, block("heading_3", "SNS Comments"))
            self.add(owner, block("paragraph", "Shared notes"))
            self.add(owner, {"type": "unsupported", "unsupported": {}})
            self.databases[db_id] = {"object": "database", "id": db_id, "parent": {"type": "page_id", "page_id": owner},
                "in_trash": False, "data_sources": [] if linked else [{"id": ds_id, "name": "Meetings"}]}
            self.schemas[ds_id] = {"object": "data_source", "id": ds_id, "properties": {"Name": {"type": "title"}, "Notify": {"type": "button", "button": {}}}}
            view_id = "1" + db_id[1:]
            self.views[view_id] = {"object": "view", "id": view_id, "data_source_id": SOURCE if linked else ds_id,
                                   "name": "Schedule", "type": "table", "configuration": {"wrap_cells": True}, "filter": {"date": {"equals": DAY}}}
            self.pages[row_id] = {"object": "page", "id": row_id, "parent": {"type": "data_source_id", "data_source_id": ds_id},
                                  "properties": {"Name": {"type": "title", "title": [{"type": "text", "text": {"content": "Unique " + row_id}}]}}, "in_trash": False}
            self.roots[row_id] = []
            self.add(row_id, block("paragraph", "Original row details"))

    def daily_pages(self, start, end):
        return [self.page(p) for p in (MANUAL, AUTO) if not self.pages[p]["in_trash"]]

    def request(self, method, path, payload=None):
        self.requests.append((method, path))
        route, _, query = path.partition("?")
        bits = route.split("/")
        if route == "views":
            db_id = urllib.parse.parse_qs(query)["database_id"][0]
            return {"results": [{"id": "1" + db_id[1:]}], "has_more": False}
        if bits[0] == "views":
            return copy.deepcopy(self.views[bits[1]])
        if bits[0] == "data_sources":
            if method == "GET": return copy.deepcopy(self.schemas[bits[1]])
            return {"results": [copy.deepcopy(page) for page in self.pages.values()
                                if page["parent"].get("data_source_id") == bits[1]], "has_more": False}
        if bits[0] == "databases":
            db_id = bits[1]
            if method == "GET":return copy.deepcopy(self.databases[db_id])
            owner = payload["parent"]["page_id"]
            if self.fail_native_restore and owner == AUTO:raise NotionError("restore failed")
            previous = self.databases[db_id]["parent"]["page_id"]
            self.roots[previous].remove(db_id); self.roots[owner].append(db_id)
            self.databases[db_id]["parent"]["page_id"] = owner
            self.writes.append(("move_db", db_id, owner))
            if self.after_move:self.after_move(db_id)
            if self.crash_move:
                self.crash_move = False;raise KeyboardInterrupt
            if self.lost_move_ack:
                self.lost_move_ack = False;raise NotionError("lost move response")
            return copy.deepcopy(self.databases[db_id])
        if bits[0] == "pages" and bits[-1] == "markdown":
            owner = bits[1]
            if method == "GET":
                lines=[]
                for node in self.children(owner):
                    kind=node["type"]
                    if kind=="child_database":lines.append(f'<database url="https://app.notion.com/p/{node["id"]}" inline="true">Meetings</database>')
                    elif kind=="unsupported":lines.append(f'<unknown url="https://app.notion.com/p/{owner}#{node["id"]}" alt="button" />')
                    elif kind=="heading_3":lines.append("### " + plain(node))
                    else:lines.append(plain(node))
                return {"markdown":"\n\n".join(lines),"truncated":False,"unknown_block_ids":[]}
            if self.fail_position:raise NotionError("position update failed")
            insertion=payload["update_content"]["content_updates"][1]["new_str"]
            ids=re.findall(r'url="https://app.notion.com/p/([a-f0-9]+)"',insertion)
            if len(ids)==2:
                anchor, moved=ids
                self.roots[owner].remove(moved)
                self.roots[owner].insert(self.roots[owner].index(anchor)+1,moved)
            else:
                moved=ids[0]
                self.roots[owner].remove(moved)
                anchor=next(n['id'] for n in self.children(owner) if n['type']=='paragraph' and plain(n)=='Meeting guidance')
                index=self.roots[owner].index(anchor)+1
                self.roots[owner].insert(index,moved)
            self.writes.append(("position_db", moved))
            return {"warnings":[],"truncated":False,"unknown_block_ids":[]}
        raise AssertionError((method,path))


class NativeDatabaseMergeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/"journal.json"
        self.api=NativeApi()
        self.policy={**DEFAULT_POLICY,"template_sections":["Meeting","SNS Comments"],
                     "preserve_child_databases":True,"allow_template_toggle_buttons":True}
        self.journal=JsonJournal(self.path)
        self.merger=DailyMerger(self.api,self.journal,self.policy)

    def merge(self):return self.merger.merge_pair(MANUAL,AUTO,True)

    def test_optional_null_paragraph_icon_does_not_block_real_template(self):
        for node in self.api.blocks.values():
            if node['type']=='paragraph':node['paragraph']['icon']=None
        self.assertEqual(self.merge()['status'],'complete')

    def test_native_db_schema_views_rows_and_ids_survive_source_trash(self):
        before=snapshot(self.api,NEW_DB)
        old_ids=list(self.api.roots[MANUAL])
        self.assertEqual(self.merge()["status"],"complete")
        self.assertEqual(snapshot(self.api,NEW_DB)["hash"],before["hash"])
        self.assertEqual(self.api.databases[NEW_DB]["parent"]["page_id"],MANUAL)
        self.assertTrue(self.api.pages[AUTO]["in_trash"])
        self.assertEqual([key for key in self.api.roots[MANUAL] if key in old_ids],old_ids)
        self.assertEqual(self.api.roots[MANUAL].index(NEW_DB),self.api.roots[MANUAL].index(OLD_DB)+1)
        self.assertEqual(self.api.pages[NEW_ROW]["parent"]["data_source_id"],NEW_DS)

    def test_volatile_native_request_ids_do_not_look_like_content_edits(self):
        request=self.api.request
        nonce=[0]
        def with_nonce(method,path,payload=None):
            result=request(method,path,payload)
            if method=='GET':
                nonce[0]+=1;result['request_id']=str(nonce[0])
            return result
        self.api.request=with_nonce
        self.assertEqual(self.merge()['status'],'complete')

    def test_grandparent_metadata_tracks_database_move_without_changing_contents(self):
        request=self.api.request
        def with_parent(method,path,payload=None):
            value=request(method,path,payload)
            if method=='GET' and path.startswith('data_sources/'):
                db_id=OLD_DB if path.endswith(OLD_DS) else NEW_DB
                value['parent']={'type':'database_id','database_id':db_id}
                value['database_parent']=copy.deepcopy(self.api.databases[db_id]['parent'])
            return value
        self.api.request=with_parent
        self.assertEqual(self.merge()['status'],'complete')

    def test_linked_view_moves_without_querying_external_hr_rows(self):
        self.api=NativeApi(linked=True)
        self.merger=DailyMerger(self.api,self.journal,self.policy)
        self.assertEqual(self.merge()["status"],"complete")
        self.assertFalse(any(method=="POST" and "/query" in path for method,path in self.api.requests))

    def test_plan_makes_no_native_moves_or_page_writes(self):
        self.assertEqual(self.merger.merge_pair(MANUAL,AUTO,False)["status"],"ready")
        self.assertEqual(self.api.writes,[]);self.assertFalse(self.path.exists())

    def test_button_exception_keeps_old_button_and_real_created_toggle_content(self):
        self.api.add(AUTO,block("toggle","Individual schedule",children=[block("paragraph","Actual schedule detail")]))
        self.assertEqual(self.merge()["status"],"complete")
        self.assertEqual(sum(n["type"]=="unsupported" for n in self.api.children(MANUAL)),1)
        toggles=[n for n in self.api.tree(MANUAL) if n["type"]=="toggle"]
        self.assertEqual(plain(toggles[0]["_children"][0]),"Actual schedule detail")

    def test_native_database_precedes_template_button_in_same_section(self):
        # Default Notices layout has a DB followed by a button and blank space.
        for identifier in (MANUAL,AUTO):
            header=next(n['id'] for n in self.api.children(identifier) if n['type']=='heading_3' and plain(n)=='SNS Comments')
            self.api.roots[identifier].remove(header)
            self.api.roots[identifier].append(header)
        self.assertEqual(self.merge()['status'],'complete')
        root=self.api.children(MANUAL)
        database_index=next(i for i,n in enumerate(root) if n['id']==NEW_DB)
        button_index=next(i for i,n in enumerate(root) if n['type']=='unsupported')
        self.assertLess(database_index,button_index)

    def test_unidentified_unsupported_block_is_not_treated_as_a_template_button(self):
        original=self.api.template_button_ids
        self.api.template_button_ids=lambda parent:set()
        self.assertEqual(self.merge()["status"],"held")
        self.assertEqual(self.api.writes,[])

    def test_move_ack_loss_reconciles_without_repeating_move(self):
        self.api.lost_move_ack=True
        self.assertEqual(self.merge()["status"],"complete")
        self.assertEqual(sum(w[0]=="move_db" and w[2]==MANUAL for w in self.api.writes),1)

    def test_crash_after_move_resumes_from_durable_native_intent(self):
        self.api.crash_move=True
        with self.assertRaises(KeyboardInterrupt):self.merge()
        self.assertEqual(self.api.databases[NEW_DB]["parent"]["page_id"],MANUAL)
        restored=DailyMerger(self.api,JsonJournal(self.path),self.policy)
        self.assertEqual(restored.merge_pair(MANUAL,AUTO,True)["status"],"complete")
        self.assertEqual(sum(w[0]=="move_db" and w[2]==MANUAL for w in self.api.writes),1)

    def test_failed_position_restores_native_db_and_preserves_source(self):
        self.api.fail_position=True
        self.assertEqual(self.merge()["status"],"held")
        self.assertFalse(self.api.pages[AUTO]["in_trash"])
        self.assertEqual(self.api.databases[NEW_DB]["parent"]["page_id"],AUTO)

    def test_native_edit_after_trash_restores_source_and_all_new_row_information(self):
        def edit():self.api.pages[NEW_ROW]["properties"]["Name"]["title"][0]["text"]["content"]="Human edit during trash"
        self.api.after_trash=edit
        self.assertEqual(self.merge()["status"],"held")
        self.assertFalse(self.api.pages[AUTO]["in_trash"])
        self.assertEqual(self.api.databases[NEW_DB]["parent"]["page_id"],AUTO)
        self.assertEqual(self.api.pages[NEW_ROW]["properties"]["Name"]["title"][0]["text"]["content"],"Human edit during trash")

    def test_native_row_unknown_container_children_are_in_preservation_fingerprint(self):
        tab=self.api.add(NEW_ROW,{'type':'tab','tab':{'children':[block('paragraph','Nested native row detail')]}})
        before=snapshot(self.api,NEW_DB)['hash']
        child=self.api.roots[tab['id']][0]
        self.api.blocks[child]['paragraph']['rich_text'][0]['text']['content']='Updated nested detail'
        self.assertNotEqual(snapshot(self.api,NEW_DB)['hash'],before)

    def test_native_restore_failure_is_durable_and_retried_before_query(self):
        self.api.fail_position=True;self.api.fail_native_restore=True
        self.assertEqual(self.merge()["status"],"held")
        self.assertEqual(next(iter(JsonJournal(self.path).data.values()))["status"],"restore_required")
        self.api.fail_native_restore=False;self.api.fail_position=False
        restored=DailyMerger(self.api,JsonJournal(self.path),self.policy)
        self.assertEqual(restored.run(DAY,DAY,True)["status"],"held")
        self.assertEqual(self.api.databases[NEW_DB]["parent"]["page_id"],AUTO)

    def test_incomplete_native_query_blocks_all_writes(self):
        original=self.api.request
        def request(method,path,payload=None):
            value=original(method,path,payload)
            if method=="POST" and path.endswith('/query'):value['request_status']={'type':'incomplete'}
            return value
        self.api.request=request
        self.assertEqual(self.merge()["status"],"held")
        self.assertEqual(self.api.writes,[])

    def test_trashed_child_api_404_requires_full_markdown_readback(self):
        import sns_comment.daily_merge as module
        actual=module.source_snapshot
        def read(api,identifier,record=None):
            if api.pages[identifier]['in_trash']:raise NotionError('Notion API 실패 (HTTP 404).')
            return actual(api,identifier,record)
        with mock.patch.object(module,'source_snapshot',side_effect=read):
            self.assertEqual(self.merge()['status'],'complete')
        self.assertTrue(self.api.pages[AUTO]['in_trash'])

    def test_changed_trashed_markdown_restores_source_and_native_db(self):
        import sns_comment.daily_merge as module
        actual=module.source_snapshot
        def read(api,identifier,record=None):
            if api.pages[identifier]['in_trash']:raise NotionError('Notion API 실패 (HTTP 404).')
            return actual(api,identifier,record)
        self.api.after_trash=lambda:self.api.add(AUTO,block('paragraph','Late source content'))
        with mock.patch.object(module,'source_snapshot',side_effect=read):
            self.assertEqual(self.merge()['status'],'held')
        self.assertFalse(self.api.pages[AUTO]['in_trash'])
        self.assertEqual(self.api.databases[NEW_DB]['parent']['page_id'],AUTO)
        self.assertTrue(any(plain(n)=='Late source content' for n in self.api.children(AUTO)))

    def test_empty_trashed_response_is_logged_not_used_as_a_source_hash(self):
        import sns_comment.daily_merge as module
        actual=module.source_snapshot;markdown=self.api.markdown_hash
        def read(api,identifier,record=None):
            if api.pages[identifier]['in_trash']:raise NotionError('Notion API 실패 (HTTP 404).')
            return actual(api,identifier,record)
        self.api.markdown_hash=lambda identifier,allow_empty=False:None if allow_empty else markdown(identifier)
        with mock.patch.object(module,'source_snapshot',side_effect=read):
            self.assertEqual(self.merge()['status'],'complete')
        record=next(iter(self.journal.data.values()))
        self.assertEqual(record['post_trash_source_readback'],'unavailable_in_trash')
        self.assertEqual(record['verified_source_hash'],record['source']['hash'])

    def test_empty_active_markdown_cannot_establish_a_pre_trash_proof(self):
        from sns_comment.daily_merge import MergeApi,Hold
        api=MergeApi('synthetic',SOURCE)
        with mock.patch.object(api,'request',return_value={'markdown':'','truncated':False,'unknown_block_ids':[]}),self.assertRaises(Hold):
            api.markdown_hash(AUTO)

    def test_button_exception_cannot_be_spoofed_by_code_or_escaped_markup(self):
        from sns_comment.daily_merge import MergeApi
        api=MergeApi('synthetic',SOURCE)
        tag=f'<unknown url="https://app.notion.com/p/{AUTO}#{NEW_DB}" alt="button" />'
        for markdown in ('```\n'+tag+'\n```','\\'+tag,'Example: '+tag):
            with self.subTest(markdown=markdown),mock.patch.object(api,'request',return_value={'markdown':markdown,'truncated':False,'unknown_block_ids':[]}):
                self.assertEqual(api.template_button_ids(AUTO),set())
        with mock.patch.object(api,'request',return_value={'markdown':tag,'truncated':False,'unknown_block_ids':[]}):
            self.assertEqual(api.template_button_ids(AUTO),{NEW_DB})
