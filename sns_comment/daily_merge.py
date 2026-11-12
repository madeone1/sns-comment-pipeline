"""Merge newer daily-page information into the oldest page's template sections.

Keep old page/block IDs, deduplicate identical template content, verify every
addition, then soft-trash the newer page. Unsupported content is a hold.
"""

import argparse
import base64
import copy
import datetime as dt
import json
import os
from pathlib import Path
import re
import tempfile
import urllib.parse

from .api import PilotError
from .notion import NotionClient, NotionError
from .merge_content import Hold, SUPPORTED, create_payload, digest, external_media, page_values, semantic, uuid
from .profiles import read_settings
from .storage import run_lock


def daily_date(page, source_id):
    parent = page.get("parent", {})
    if parent.get("type") != "data_source_id" or uuid(parent.get("data_source_id")) != uuid(source_id):
        raise Hold("page_outside_daily_data_source")
    props = page.get("properties", {})
    if (props.get("유형", {}).get("select") or {}).get("name") != "오늘 업무":
        raise Hold("not_a_daily_work_page")
    value = props.get("이벤트 시간", {}).get("date") or {}
    start = value.get("start", "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start) or value.get("end"):
        raise Hold("ambiguous_daily_date")
    dt.date.fromisoformat(start)
    return start


def created_at(page):
    try:
        value = dt.datetime.fromisoformat(page["created_time"].replace("Z", "+00:00"))
        if value.utcoffset() is None:
            raise ValueError
        return value
    except (KeyError, TypeError, ValueError, AttributeError):
        raise Hold("page_creation_time_not_verified") from None


class MergeApi(NotionClient):
    preserve_child_databases = False
    allow_template_toggle_buttons = False

    def markdown_hash(self, identifier, allow_empty=False):
        value = self.request("GET", "pages/" + uuid(identifier) + "/markdown?markdown_version=v2")
        if value.get("truncated") or value.get("unknown_block_ids") or not isinstance(value.get("markdown"), str):
            raise Hold("source_markdown_incomplete")
        if not value["markdown"].strip():
            if allow_empty:
                return None
            raise Hold("source_markdown_empty")
        return digest(value["markdown"])

    def paginated(self, path, method="GET"):
        rows, cursor, seen = [], None, set()
        while True:
            payload = {"page_size": 100}
            if cursor:
                payload["start_cursor"] = cursor
            result = self.request(method, path + ("&" if "?" in path else "?") + urllib.parse.urlencode(payload)) if method == "GET" else self.request(method, path, payload)
            self.complete_response(result)
            rows.extend(result["results"])
            if not result.get("has_more"):
                return rows
            cursor = result.get("next_cursor")
            if not cursor or cursor in seen or len(rows) > 2000:
                raise Hold("native_database_pagination_incomplete")
            seen.add(cursor)

    def template_button_ids(self, parent):
        value = self.request("GET", "pages/" + uuid(parent) + "/markdown?markdown_version=v2")
        if value.get("truncated") or value.get("unknown_block_ids"):
            raise Hold("template_button_identification_incomplete")
        result = set()
        fence = None
        markers = []
        for line in value.get("markdown", "").splitlines():
            boundary = re.match(r"^\s*(`{3,}|~{3,})", line)
            if boundary:
                token = boundary.group(1)
                if fence is None:
                    fence = token
                elif token[0] == fence[0] and len(token) >= len(fence):
                    fence = None
                continue
            marker = re.fullmatch(r'<unknown\b[^>]*\balt="button"[^>]*/>', line.strip())
            if fence is None and marker:
                markers.append(marker.group())
        for marker in markers:
            match = re.search(r'url="([^"]+)"', marker)
            if not match:
                continue
            parsed = urllib.parse.urlsplit(match.group(1))
            if parsed.hostname not in {"app.notion.com", "www.notion.so", "notion.so"}:
                continue
            if parsed.path.replace("-", "").rstrip("/").split("/")[-1] == uuid(parent):
                result.add(uuid(parsed.fragment))
        return result

    def verify_connection(self, expected_name):
        if expected_name is None:
            return
        identity = self.request("GET", "users/me")
        if identity.get("type") != "bot" or identity.get("name", "").strip() != expected_name:
            raise Hold("merge_connection_identity_mismatch")

    def page(self, identifier):
        return self.request("GET", "pages/" + uuid(identifier))

    def daily_pages(self, start, end):
        rows, cursor, seen = [], None, set()
        while True:
            payload = {"page_size": 100, "filter": {"and": [
                {"property": "유형", "select": {"equals": "오늘 업무"}},
                {"property": "이벤트 시간", "date": {"on_or_after": start}},
                {"property": "이벤트 시간", "date": {"on_or_before": end}}]}}
            if cursor:
                payload["start_cursor"] = cursor
            result = self.request("POST", f"data_sources/{self.source_id}/query", payload)
            self.complete_response(result)
            rows.extend(result["results"])
            if not result.get("has_more"):
                return rows
            cursor = result.get("next_cursor")
            if not cursor or cursor in seen or len(rows) > 2000:
                raise Hold("daily_page_query_incomplete")
            seen.add(cursor)

    @staticmethod
    def complete_response(result):
        if not isinstance(result.get("results"), list) or result.get("truncated") or result.get("request_status", {}).get("type", "complete") != "complete":
            raise Hold("incomplete_api_response")

    def children(self, parent):
        rows, cursor, seen = [], None, set()
        while True:
            query = {"page_size": 100}
            if cursor:
                query["start_cursor"] = cursor
            result = self.request("GET", f"blocks/{uuid(parent)}/children?" + urllib.parse.urlencode(query))
            self.complete_response(result)
            rows.extend(result["results"])
            if not result.get("has_more"):
                return rows
            cursor = result.get("next_cursor")
            if not cursor or cursor in seen or len(rows) > 3000:
                raise Hold("block_query_incomplete")
            seen.add(cursor)

    def tree(self, parent, strict=True, native_depth=0):
        seen = set()
        button_ids = None
        def walk(identifier, depth=0):
            nonlocal button_ids
            if depth > 20 or len(seen) > 3000:
                raise Hold("page_tree_limit")
            result = []
            for item in self.children(identifier):
                key = uuid(item["id"])
                if key in seen:
                    raise Hold("repeated_block_id")
                seen.add(key)
                item = copy.deepcopy(item)
                kind = item.get("type")
                native = kind == "child_database" and self.preserve_child_databases
                if kind == "unsupported" and self.allow_template_toggle_buttons:
                    if button_ids is None:
                        button_ids = self.template_button_ids(parent)
                    if key in button_ids and not item.get("has_children"):
                        item["_template_toggle_button"] = True
                approved_button = item.get("_template_toggle_button") is True
                if strict and kind not in SUPPORTED and not native and not approved_button:
                    raise Hold("unsupported_block:" + str(kind))
                if native:
                    from .native_database import snapshot
                    item["_native_database"] = snapshot(self, key, native_depth)
                descend = item.get("has_children") and (kind in SUPPORTED or not strict and kind != "child_database")
                if not strict and kind == "child_page":
                    descend = True
                item["_children"] = walk(key, depth + 1) if descend else []
                result.append(item)
            return result
        return walk(uuid(parent))

    def no_comments(self, identifier):
        result = self.request("GET", "comments?" + urllib.parse.urlencode({"block_id": identifier, "page_size": 1}))
        self.complete_response(result)
        if result["results"] or result.get("has_more"):
            raise Hold("comments_require_preservation")

    def append(self, parent, payload, position=None):
        body = {"children": [payload], "notifications": {"mode": "silent"}}
        if position is not None:
            body["position"] = position
        result = self.request("PATCH", f"blocks/{uuid(parent)}/children", {
            **body})
        if len(result.get("results", [])) != 1:
            raise Hold("append_response_not_verified")
        return result["results"][0]

    def update_properties(self, identifier, properties):
        return self.request("PATCH", "pages/" + uuid(identifier), {"properties": properties})

    def trash(self, identifier, value):
        return self.request("PATCH", "pages/" + uuid(identifier), {"in_trash": value})


def all_nodes(nodes):
    for node in nodes:
        yield node
        yield from all_nodes(node.get("_children", []))


def source_snapshot(api, identifier, record=None):
    page = api.page(identifier)
    external_media(page.get("icon")); external_media(page.get("cover"))
    for prop in page["properties"].values():
        kind = prop.get("type")
        if kind in {"relation", "rollup", "formula", "unique_id", "verification", "button", "place"}:
            raise Hold("property_requires_preservation:" + str(kind))
        if prop.get("has_more") or kind in {"people", "title", "rich_text"} and len(prop.get(kind, [])) >= 25:
            raise Hold("property_pagination_not_verified")
        if kind == "files":
            for item in prop.get(kind, []):
                external_media(item)
        if kind not in {"title", "rich_text", "number", "select", "status", "multi_select", "date", "people",
                        "files", "checkbox", "url", "email", "phone_number", "created_time", "created_by",
                        "last_edited_time", "last_edited_by"}:
            raise Hold("unsupported_property:" + str(kind))
    nodes = api.tree(identifier)
    if record:
        from .native_database import moved_receipts, parent_id, snapshot
        moved = {uuid(r["id"]): r for r in moved_receipts(record) if r.get("status") in {"moved", "verified"}}
        if moved:
            actual = iter(nodes)
            restored = []
            for original in record["source"]["nodes"]:
                key = uuid(original["id"])
                if key in moved:
                    if parent_id(api, key) != uuid(record["canonical_id"]):
                        raise Hold("native_database_target_parent_changed")
                    original = copy.deepcopy(original)
                    original["_native_database"] = snapshot(api, key)
                    restored.append(original)
                else:
                    current = next(actual, None)
                    if current is None:
                        raise Hold("source_changed")
                    restored.append(current)
            nodes = [*restored, *actual]
    normalized = [semantic(node) for node in nodes]
    own_ids = {uuid(identifier), *[uuid(node["id"]) for node in all_nodes(nodes) if node["type"] != "child_database"]}
    def links(value):
        if isinstance(value, dict):
            if value.get("type") in {"page", "database"} and isinstance(value.get(value["type"]), dict):
                if value[value["type"]].get("id", "").replace("-", "").lower() in own_ids:
                    raise Hold("source_internal_reference")
            for key, item in value.items():
                if key == "url" and isinstance(item, str):
                    parsed = urllib.parse.urlsplit(item)
                    if parsed.hostname in {"notion.so", "www.notion.so", "app.notion.com", "www.notion.com"}:
                        compact = item.replace("-", "").lower()
                        if any(key in compact for key in own_ids):
                            raise Hold("source_internal_reference")
                links(item)
        elif isinstance(value, list):
            for item in value: links(item)
    links(normalized); links(page["properties"])
    for key in [uuid(identifier), *[uuid(node["id"]) for node in all_nodes(nodes) if node["type"] != "child_database"]]:
        api.no_comments(key)
    for node in all_nodes(normalized):
        if node["type"] not in {"child_database", "unsupported"}:
            create_payload(node)  # Validate before the first write.
    return {"page": page, "nodes": nodes, "semantic": normalized,
            "hash": digest({"page": page_values(page), "body": normalized})}


def baseline(api, identifier):
    page = api.page(identifier)
    return {"page": page_values(page), "nodes": api.tree(identifier, strict=False)}


class JsonJournal:
    """An atomic, private local journal; caller holds its single-writer lock."""
    def __init__(self, path):
        self.path = Path(path)
        self.data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(prefix=".merge-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self.data, stream, ensure_ascii=False)
                stream.flush(); os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)


class CloudJournal:
    """Generation-guarded state; caller holds the existing global cloud lease."""
    def __init__(self, state):
        self.state = state
        self.data, self.generation = state.json_get("state/daily-merge.json", {})

    def save(self):
        self.generation = self.state.json_put("state/daily-merge.json", self.data, self.generation)


class DailyMerger:
    def __init__(self, api, journal, policy):
        self.api, self.journal, self.policy = api, journal, policy

    def preflight(self, canonical_id, source_id, explicit=False):
        canonical_id, source_id = uuid(canonical_id), uuid(source_id)
        if canonical_id == source_id:
            raise Hold("same_page_pair")
        canonical, source = self.api.page(canonical_id), self.api.page(source_id)
        day = daily_date(canonical, self.api.source_id)
        if daily_date(source, self.api.source_id) != day:
            raise Hold("different_daily_dates")
        if canonical.get("in_trash") or source.get("in_trash"):
            raise Hold("page_already_in_trash")
        if created_at(canonical) >= created_at(source):
            raise Hold("canonical_must_be_created_earlier")
        if any(source.get(key) is not None and source.get(key) != canonical.get(key) for key in ("icon", "cover")):
            raise Hold("conflicting_page_icon_or_cover")
        snapshot = source_snapshot(self.api, source_id)
        if snapshot["page"].get("last_edited_time") != source.get("last_edited_time"):
            raise Hold("source_changed_during_read")
        initial = baseline(self.api, canonical_id)
        from .template_merge import property_plan, template_plan
        planned = template_plan(canonical_id, initial["nodes"], snapshot["nodes"], self.policy["template_sections"])
        native_refs = {uuid(n["id"]) for n in initial["nodes"] if n["type"] == "child_database"}
        for operation in planned["operations"]:
            if operation["node"]["type"] == "child_database":
                anchor = operation["after"]
                anchor = anchor if anchor and anchor.startswith("inline/") else uuid(anchor)
                if uuid(operation["parent"]) != canonical_id or anchor not in native_refs:
                    raise Hold("native_database_anchor_requires_review")
                native_refs.add(operation["key"])
        properties = property_plan(canonical, snapshot["page"], "생성 방식")
        native_ids = [op["node"]["child_database"]["database_id"] for op in planned["operations"] if op["node"]["type"] == "child_database"]
        from .native_database import restore_contexts
        contexts = restore_contexts(self.api, source_id, native_ids) if native_ids else {}
        if self.api.page(canonical_id).get("last_edited_time") != canonical.get("last_edited_time"):
            raise Hold("canonical_changed_during_read")
        return {"day": day, "data_source_id": self.api.source_id, "canonical_id": canonical_id, "source_id": source_id,
                "source": snapshot, "baseline": initial, "explicit": explicit,
                "merge_strategy": "template_inline_v2", "inline_plan": planned, "property_updates": properties,
                "native_restore_contexts": contexts,
                "preserve_child_databases": self.api.preserve_child_databases,
                "allow_template_toggle_buttons": self.api.allow_template_toggle_buttons}

    def summary(self, record):
        result = {key: record.get(key) for key in ("day", "canonical_id", "source_id", "status", "reason")}
        if record.get("merge_strategy") == "template_inline_v2":
            result.update(merge_strategy="template_inline_v2", added_blocks=len(record["inline_plan"]["operations"]),
                          updated_properties=len(record["property_updates"]))
        return result

    def append_once(self, record, key, parent, node, position=None):
        receipts = record.setdefault("receipts", {})
        expected = copy.deepcopy(node)
        if node["type"] != "table":
            expected["_children"] = []
        receipt = receipts.get(key)
        if receipt is None:
            existing = self.api.children(parent)
            receipt = receipts[key] = {"parent": parent, "before": [n["id"] for n in existing],
                                      "expected": expected, "position": position, "status": "intent"}
            self.journal.save()  # Persist intent BEFORE any remote mutation.
            try:
                result = self.api.append(parent, create_payload(node), position=position)
                receipt["id"] = result["id"]
                receipt["status"] = "created"
                self.journal.save()
            except PilotError:
                # Never issue a second append after an ambiguous response.
                receipt["status"] = "uncertain"
                self.journal.save()
        if not receipt.get("id"):
            candidates = []
            for item in self.api.tree(parent, strict=False):
                if item["id"] not in receipt["before"]:
                    try:
                        if semantic(item) == expected:
                            candidates.append(item)
                    except Hold:
                        pass
            if len(candidates) != 1:
                raise Hold("append_outcome_uncertain")
            receipt.update(id=candidates[0]["id"], status="recovered")
            self.journal.save()
        return receipt["id"]

    def verify(self, record):
        from .template_merge import properties_match, verify_expected
        try:
            source = source_snapshot(self.api, record["source_id"], record)
            if source["hash"] != record["source"]["hash"]:
                raise Hold("source_changed")
        except NotionError as error:
            # Notion hides trashed block children and can return empty Markdown.
            # Never treat that empty response as a second source content proof.
            # Require the durable full pre-trash proof; recheck the actual target
            # and every moved native object below, recording this API limitation.
            page = self.api.page(record["source_id"])
            if "HTTP 404" not in str(error) or not page.get("in_trash") or not record.get("source_markdown_hash_before_trash"):
                raise
            if page_values(page) != page_values(record["source"]["page"]):
                raise Hold("source_properties_changed")
            after_hash = self.api.markdown_hash(record["source_id"], allow_empty=True)
            if after_hash is not None and after_hash != record["source_markdown_hash_before_trash"]:
                raise Hold("source_changed_after_trash")
            if after_hash is None:
                if record.get("status") != "trashing" or record.get("verified_source_hash") != record["source"]["hash"]:
                    raise Hold("durable_pre_trash_source_proof_required")
                record["post_trash_source_readback"] = "unavailable_in_trash"
            else:
                record["post_trash_source_readback"] = "complete_markdown"
            source = {"page":page, "verification":record["post_trash_source_readback"]}
        target = baseline(self.api, record["canonical_id"])
        initial = record["baseline"]
        if not properties_match(self.api.page(record["canonical_id"]), initial["page"], record["property_updates"]):
            raise Hold("canonical_properties_changed")
        verify_expected(record["inline_plan"]["expected_nodes"], target["nodes"], record.get("receipts", {}))
        if self.api.page(record["canonical_id"]).get("in_trash"):
            raise Hold("canonical_page_in_trash")
        return source

    def apply_properties(self, record):
        from .template_merge import properties_match
        updates = record["property_updates"]
        if not updates:
            return
        if record.get("property_intent"):
            if not properties_match(self.api.page(record["canonical_id"]), record["baseline"]["page"], updates):
                raise Hold("property_update_outcome_uncertain")
            return
        if not properties_match(self.api.page(record["canonical_id"]), record["baseline"]["page"], {}):
            raise Hold("canonical_properties_changed")
        record["property_intent"] = True
        self.journal.save()
        try:
            self.api.update_properties(record["canonical_id"], updates)
        except PilotError:
            pass  # Reconcile by readback; never resend an ambiguous write.
        if not properties_match(self.api.page(record["canonical_id"]), record["baseline"]["page"], updates):
            raise Hold("property_update_outcome_uncertain")

    def apply_inline(self, record):
        def resolve(reference):
            if reference is None:
                return None
            if reference.startswith("inline/"):
                identifier = record.get("receipts", {}).get(reference, {}).get("id")
                if not identifier:
                    raise Hold("inline_parent_or_anchor_not_verified")
                return identifier
            return reference
        for operation in record["inline_plan"]["operations"]:
            after = resolve(operation["after"])
            position = {"type": "after_block", "after_block": {"id": after}} if after else {"type": "start"}
            if operation["node"]["type"] == "child_database":
                from .native_database import apply_move
                apply_move(self, record, operation, resolve(operation["parent"]), after)
            else:
                self.append_once(record, operation["key"], resolve(operation["parent"]), operation["node"], position)
        self.apply_properties(record)

    def restore(self, record):
        record.update(status="restore_required", reason="post_trash_verification_failed")
        try:
            self.journal.save()
        except (PilotError, OSError):
            pass  # The durable pre-trash intent already exists; restore first.
        self.api.trash(record["source_id"], False)
        if self.api.page(record["source_id"]).get("in_trash"):
            raise Hold("source_restore_not_verified")
        from .native_database import rollback
        rollback(self, record)
        record["status"] = "held"
        self.journal.save()

    def merge_pair(self, canonical_id, source_id, apply=False, explicit=False):
        canonical_id, source_id = uuid(canonical_id), uuid(source_id)
        key = digest([self.api.source_id, canonical_id, source_id])
        record = self.journal.data.get(key)
        try:
            if record and record["status"] == "complete":
                if not self.api.page(source_id).get("in_trash"):
                    return {**self.summary(record), "status": "held", "reason": "source_restored_after_merge"}
                return {**self.summary(record), "already_complete": True}
            if record and record["status"] in {"trashing", "restore_required"}:
                if not apply:
                    return {**self.summary(record), "reason": "recovery_required"}
                self.restore(record)
                return self.summary(record)
            if record and record.get("merge_strategy") != "template_inline_v2":
                raise Hold("legacy_merge_journal_requires_review")
            if record and apply:
                from .native_database import moved_receipts, parent_id
                for receipt in moved_receipts(record):
                    if receipt["status"] == "intent":
                        if parent_id(self.api, receipt["id"]) != uuid(record["canonical_id"]):
                            raise Hold("native_database_move_outcome_uncertain")
                        receipt["status"] = "moved"
                        self.journal.save()
            if record is None:
                record = self.preflight(canonical_id, source_id, explicit)
                record["status"] = "ready"
            if not apply:
                return self.summary(record)
            # A resumable operation never incorporates new source edits silently.
            current = source_snapshot(self.api, source_id, record)
            if current["hash"] != record["source"]["hash"]:
                raise Hold("source_changed")
            if not record.get("receipts") and not record.get("property_intent"):
                current_baseline = baseline(self.api, canonical_id)
                from .template_merge import properties_match, verify_expected
                if not properties_match(self.api.page(canonical_id), record["baseline"]["page"], {}):
                    raise Hold("canonical_changed_before_copy")
                verify_expected(record["baseline"]["nodes"], current_baseline["nodes"], {})
            self.journal.data[key] = record
            record["status"] = "copying"
            self.journal.save()
            self.apply_inline(record)
            source = self.verify(record)
            from .native_database import moved_receipts
            latest = self.api.page(source_id) if moved_receipts(record) else record["source"]["page"]
            if source["page"].get("last_edited_time") != latest.get("last_edited_time"):
                raise Hold("source_edited_before_trash")
            record["source_markdown_hash_before_trash"] = self.api.markdown_hash(source_id)
            if self.api.page(source_id).get("last_edited_time") != source["page"].get("last_edited_time"):
                raise Hold("source_edited_before_trash")
            record.update(status="copy_verified", verified_source_hash=source["hash"])
            self.journal.save()
            record["status"] = "trashing"
            self.journal.save()
            try:
                self.api.trash(source_id, True)
                self.verify(record)
                if not self.api.page(source_id).get("in_trash"):
                    raise Hold("trash_not_verified")
            except (PilotError, OSError, ValueError, TypeError, KeyError) as post_error:
                record['post_trash_error'] = str(post_error) if isinstance(post_error,Hold) else type(post_error).__name__ + (':' + str(post_error.http_status) if isinstance(post_error,NotionError) and hasattr(post_error,'http_status') else '')
                self.restore(record)
                return self.summary(record)
            record.update(status="complete", reason=None)
            self.journal.save()
            return self.summary(record)
        except (PilotError, OSError, ValueError, TypeError, KeyError) as error:
            reason = str(error) if isinstance(error, Hold) else "api_or_permission_not_verified"
            if record:
                from .native_database import moved_receipts
                if moved_receipts(record):
                    try:
                        self.restore(record)
                    except (PilotError, OSError, ValueError, TypeError, KeyError) as restore_error:
                        record["status"] = "restore_required"
                        record["restore_error"] = str(restore_error) if isinstance(restore_error, Hold) else "native_restore_api_check_required"
                record["reason"] = reason
                # Preserve trash/recovery intent across a failed recovery write.
                if record.get("status") not in {"trashing", "restore_required"}:
                    record["status"] = "held"
                if key in self.journal.data:
                    self.journal.save()
            return {"day": record.get("day") if record else None, "canonical_id": canonical_id,
                    "source_id": source_id, "status": "held", "reason": reason,
                    "error_type":type(error).__name__,
                    "api_http_status":int(re.search(r"HTTP (\d{3})",str(error)).group(1)) if isinstance(error,NotionError) and re.search(r"HTTP (\d{3})",str(error)) else None}

    def run(self, start, end, apply=False):
        groups, results, recovered = {}, [], set()
        # A crashed trash request can hide the source from a normal DB query.
        # Recover these intents BEFORE querying active duplicates.
        for record in self.journal.data.values():
            if record.get("status") in {"trashing", "restore_required"}:
                if record.get("data_source_id") != self.api.source_id:
                    raise Hold("journal_data_source_mismatch")
                if apply:
                    self.restore(record)
                results.append({**self.summary(record), "status": "held", "reason": "trash_intent_recovered" if apply else "recovery_required"})
                recovered.add(record["source_id"])
        for page in self.api.daily_pages(start, end):
            if not page.get("in_trash"):
                groups.setdefault(daily_date(page, self.api.source_id), []).append(page)
        for day, pages in sorted(groups.items()):
            if len(pages) < 2:
                continue
            try:
                pages.sort(key=lambda p: (created_at(p), uuid(p["id"])))
                if created_at(pages[0]) == created_at(pages[1]):
                    raise Hold("oldest_page_not_unique")
            except Hold as error:
                results.append({"day": day, "status": "held", "reason": str(error), "page_ids": [p["id"] for p in pages]})
                continue
            for source in pages[1:]:
                if uuid(source["id"]) in recovered:
                    continue
                results.append(self.merge_pair(pages[0]["id"], source["id"], apply))
        return {"mode": "apply" if apply else "plan", "results": results,
                "scanned_pages":sum(len(pages) for pages in groups.values()),
                "single_page_dates":sum(len(pages)==1 for pages in groups.values()),
                "duplicate_dates":sum(len(pages)>1 for pages in groups.values()),
                "merged_pages":sum(r["status"]=="complete" and not r.get("already_complete") for r in results),
                "status": "held" if any(r["status"] == "held" for r in results) else "complete"}


DEFAULT_POLICY = {"enabled": False, "apply": False, "token_secret": None,
                  "preserve_child_databases": False, "allow_template_toggle_buttons": False,
                  "lookback_days": 14, "lookahead_days": 35, "expected_connection_name": None,
                  "template_sections": ["TOP 3", "Meeting", "Develop", "Contents schedule", "Info", "SNS Comments", "Notices (Individual Schedules)"]}


def checked_policy(value):
    policy = {**DEFAULT_POLICY, **value}
    if any(type(policy[key]) is not bool for key in ("enabled", "apply", "preserve_child_databases", "allow_template_toggle_buttons")):
        raise Hold("invalid_merge_feature_flag")
    sections = policy["template_sections"]
    if not isinstance(sections, list) or not sections or any(not isinstance(s, str) or not s.strip() or s != s.strip() for s in sections) or len(set(sections)) != len(sections):
        raise Hold("invalid_template_sections")
    if policy["expected_connection_name"] is not None and (not isinstance(policy["expected_connection_name"],str) or not policy["expected_connection_name"].strip()):
        raise Hold("invalid_connection_name")
    if any(type(policy[key]) is not int or not 0 <= policy[key] <= 90 for key in ("lookback_days", "lookahead_days")) or policy["lookback_days"] + policy["lookahead_days"] > 90:
        raise Hold("invalid_merge_date_range")
    return policy


def secret_token(transport, resource):
    if not re.fullmatch(r"projects/[a-zA-Z0-9-]+/secrets/[a-zA-Z0-9_-]+", resource or ""):
        raise Hold("invalid_merge_token_secret")
    response = transport.json("GET", "https://secretmanager.googleapis.com/v1/" + resource + "/versions/latest:access")
    return base64.b64decode(response["payload"]["data"], validate=True).decode().strip()


def cloud_preflight(notion_config, state, instant):
    """Called once, inside the existing global lease and before SNS reports.

    Disabled by default. Registering a token alone never enables writes.
    """
    policy = checked_policy(notion_config.get("daily_merge", {}))
    if not policy["enabled"]:
        return {"status": "disabled"}
    token = secret_token(state.transport, policy["token_secret"])
    api = MergeApi(token, notion_config["daily_data_source_id"])
    api.preserve_child_databases = policy["preserve_child_databases"]
    api.allow_template_toggle_buttons = policy["allow_template_toggle_buttons"]
    api.verify_connection(policy["expected_connection_name"])
    today = instant.astimezone(dt.timezone(dt.timedelta(hours=9))).date()
    start = (today - dt.timedelta(days=policy["lookback_days"])).isoformat()
    end = (today + dt.timedelta(days=policy["lookahead_days"])).isoformat()
    return DailyMerger(api, CloudJournal(state), policy).run(start, end, apply=policy["apply"])


def main(argv=None):
    parser = argparse.ArgumentParser(description="일일 업무 중복 병합. 기본 plan은 Notion을 변경하지 않습니다.")
    parser.add_argument("mode", choices=["plan", "apply"], nargs="?", default="plan")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--canonical-page", help="기준 이전 생성 페이지 ID")
    parser.add_argument("--newer-page", "--automatic-page", dest="newer_page", help="추가 내용을 읽을 새 페이지 ID. --canonical-page와 함께 지정")
    parser.add_argument("--journal", type=Path, default=Path(".local/notion-daily-merge/journal.json"))
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        settings = read_settings(args.config)
        policy = checked_policy(settings.get("notion", {}).get("daily_merge", {}))
        token = os.environ.get("NOTION_DAILY_MERGE_TOKEN")
        if not token:
            raise Hold("merge_token_not_configured")
        api = MergeApi(token, settings["notion"]["daily_data_source_id"])
        api.preserve_child_databases = policy["preserve_child_databases"]
        api.allow_template_toggle_buttons = policy["allow_template_toggle_buttons"]
        api.verify_connection(policy["expected_connection_name"])
        with run_lock(args.journal):
            merger = DailyMerger(api, JsonJournal(args.journal), policy)
            if args.canonical_page or args.newer_page:
                if not args.canonical_page or not args.newer_page:
                    raise Hold("both_page_ids_required")
                result = merger.merge_pair(args.canonical_page, args.newer_page, args.mode == "apply", explicit=True)
            else:
                start, end = dt.date.fromisoformat(args.start), dt.date.fromisoformat(args.end)
                if end < start or (end - start).days > 90:
                    raise Hold("invalid_merge_date_range")
                result = merger.run(start.isoformat(), end.isoformat(), args.mode == "apply")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return int(result["status"] == "held")
    except (PilotError, OSError, ValueError, TypeError, KeyError):
        print(json.dumps({"status": "held", "reason": "merge_token_not_configured" if not os.environ.get("NOTION_DAILY_MERGE_TOKEN") else "merge_configuration_or_api_check_required"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
