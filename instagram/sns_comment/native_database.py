"""Preserve native databases by identity, including rows, views and controls.

Do not reconstruct schemas, migrate HR rows, or flatten native DBs to text.
Move each distinct source database into the matching daily template section.
"""

import copy
import re
import urllib.parse

from .merge_content import Hold, digest, uuid
from .api import PilotError


def stable(value):
    if isinstance(value, list):
        return [stable(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key == "request_id":
            continue
        if value.get("object") == "data_source" and key == "database_parent":
            # This is the owning DB's *page* parent, changed by our DB move.
            # The data source's direct database_id remains in the fingerprint.
            continue
        if value.get("type") == "file" and key == "file":
            item = copy.deepcopy(item)
            # Signed URL credentials expire; the underlying object path is fixed.
            if item.get("url"):
                parsed = urllib.parse.urlsplit(item["url"])
                item["url"] = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
            item.pop("expiry_time", None)
        if value.get("object") in {"database", "data_source", "page", "block"} and key in {"last_edited_time", "last_edited_by"}:
            continue
        result[key] = stable(item)
    return result


def snapshot(api, identifier, depth=0):
    if depth > 6:
        raise Hold("native_database_depth_limit")
    database = api.request("GET", "databases/" + uuid(identifier))
    if database.get("in_trash"):
        raise Hold("native_database_in_trash")
    views = {}
    for view in api.paginated("views?database_id=" + uuid(identifier)):
        views[uuid(view["id"])] = api.request("GET", "views/" + uuid(view["id"]))
    sources = {}
    # Linked views have no owned data sources. Never query their external rows.
    for source in database.get("data_sources", []):
        key = uuid(source["id"])
        schema = api.request("GET", "data_sources/" + key)
        rows = api.paginated("data_sources/" + key + "/query", method="POST")
        if len(rows) > 200:
            raise Hold("native_database_row_limit")
        values = {}
        for row in rows:
            row_id = uuid(row["id"])
            tree = api.tree(row_id, strict=False, native_depth=depth + 1)
            values[row_id] = {"page": row, "nodes": tree}
        sources[key] = {"schema": schema, "rows": values}
    data = {"database": database, "views": views, "sources": sources}
    normalized = stable(data)
    normalized["database"].pop("parent", None)
    return {"data": data, "hash": digest(normalized)}


def database_tag(markdown, identifier):
    found = [m.group() for m in re.finditer(r'<database\b[^>]*>.*?</database>', markdown, re.S)
             if uuid(identifier) in m.group().replace("-", "").lower()]
    if len(found) != 1:
        raise Hold("native_database_tag_not_unique")
    return found[0]


def restore_contexts(api, parent, identifiers):
    page = api.request("GET", "pages/" + uuid(parent) + "/markdown?markdown_version=v2")
    if page.get("truncated") or page.get("unknown_block_ids"):
        raise Hold("native_database_restore_context_incomplete")
    body = page["markdown"]
    result = {}
    for identifier in identifiers:
        tag = database_tag(body, identifier)
        prefix = body[:body.index(tag)]
        context = None
        for boundary in reversed(list(re.finditer(r"^|\n\n", prefix))):
            candidate = prefix[boundary.end():].strip("\n")
            if len(candidate)>8000:
                break
            if candidate.strip() and '<unknown ' not in candidate and body.count(candidate) == 1:
                context = candidate
                break
        if not context:
            raise Hold("native_database_restore_context_not_unique")
        result[uuid(identifier)] = {"after":context}
    return result


def restore_position(api, identifier, parent, context):
    if context is None:
        return
    page = api.request("GET", "pages/" + uuid(parent) + "/markdown?markdown_version=v2")
    if page.get("truncated") or page.get("unknown_block_ids"):
        raise Hold("native_database_restore_context_incomplete")
    body = page["markdown"]
    moved = database_tag(body, identifier)
    after = isinstance(context,dict)
    marker = context['after'] if after else context
    if body.count(marker) != 1:
        raise Hold("native_database_restore_context_changed")
    if after and body.find(marker)<body.find(moved) and not body[body.find(marker)+len(marker):body.find(moved)].strip():
        return
    if not after and body.find(moved) < body.find(marker) and not body[body.find(moved) + len(moved):body.find(marker)].strip():
        return
    result = api.request("PATCH", "pages/" + uuid(parent) + "/markdown", {
        "type": "update_content", "markdown_version": "v2",
        "update_content": {"content_updates": [{"old_str": moved, "new_str": ""},
                                                 {"old_str": marker, "new_str": marker + "\n\n" + moved if after else moved + marker}]}})
    if result.get("warnings") or result.get("truncated") or result.get("unknown_block_ids"):
        raise Hold("native_database_restore_position_warning")


def place_after(api, identifier, parent, after):
    """Reposition an already moved DB after another native DB in the same section."""
    rows = api.children(parent)
    ids = [uuid(node["id"]) for node in rows]
    if uuid(identifier) not in ids or uuid(after) not in ids:
        raise Hold("native_database_position_not_verified")
    if ids.index(uuid(identifier)) == ids.index(uuid(after)) + 1:
        return
    page = api.request("GET", "pages/" + uuid(parent) + "/markdown?markdown_version=v2")
    if page.get("truncated") or page.get("unknown_block_ids"):
        raise Hold("native_database_markdown_incomplete")
    moved = database_tag(page["markdown"], identifier)
    anchor = database_tag(page["markdown"], after)
    result = api.request("PATCH", "pages/" + uuid(parent) + "/markdown", {
        "type": "update_content", "markdown_version": "v2",
        "update_content": {"content_updates": [{"old_str": moved, "new_str": ""},
                                                 {"old_str": anchor, "new_str": anchor + "\n\n" + moved}]}})
    if result.get("warnings") or result.get("truncated") or result.get("unknown_block_ids"):
        raise Hold("native_database_position_warning")
    following = [uuid(n["id"]) for n in api.children(parent)]
    wanted = [key for key in ids if key != uuid(identifier)]
    wanted.insert(wanted.index(uuid(after)) + 1, uuid(identifier))
    if following != wanted:
        raise Hold("native_database_position_not_verified")


def parent_id(api, identifier):
    parent = api.request("GET", "databases/" + uuid(identifier)).get("parent", {})
    if parent.get("type") != "page_id":
        raise Hold("native_database_parent_not_a_page")
    return uuid(parent["page_id"])


def apply_move(merger, record, operation, parent, after):
    api = merger.api
    identifier = uuid(operation["node"]["child_database"]["database_id"])
    receipt = record.setdefault("receipts", {}).get(operation["key"])
    if receipt is None:
        if parent_id(api, identifier) != record["source_id"]:
            raise Hold("native_database_source_parent_changed")
        receipt = {"kind": "native_database", "id": identifier, "parent": parent,
                   "original_parent": record["source_id"], "status": "intent"}
        record["receipts"][operation["key"]] = receipt
        merger.journal.save()
        try:
            api.request("PATCH", "databases/" + identifier, {"parent": {"type": "page_id", "page_id": parent}})
        except PilotError:
            # Reconcile an ambiguous response; do not repeat the move.
            pass
    current = parent_id(api, identifier)
    if current != uuid(parent):
        raise Hold("native_database_move_outcome_uncertain")
    receipt["status"] = "moved"
    merger.journal.save()
    place_after(api, identifier, parent, after)
    current = snapshot(api, identifier)
    if current["hash"] != operation["node"]["child_database"]["snapshot_hash"]:
        raise Hold("native_database_content_changed")
    receipt["status"] = "verified"
    merger.journal.save()


def moved_receipts(record):
    return [r for r in record.get("receipts", {}).values() if r.get("kind") == "native_database"]


def rollback(merger, record):
    # This is restoration of our recorded move, not a content rewrite. IDs,
    # views, rows, attachments, controls, comments and human edits stay intact.
    for receipt in reversed(moved_receipts(record)):
        current = parent_id(merger.api, receipt["id"])
        if current == uuid(receipt["original_parent"]):
            receipt["status"] = "restored"
            continue
        if current != uuid(receipt["parent"]):
            raise Hold("native_database_restore_parent_changed")
        receipt["status"] = "restore_required"
        merger.journal.save()
        merger.api.request("PATCH", "databases/" + receipt["id"], {
            "parent": {"type": "page_id", "page_id": receipt["original_parent"]}})
        if parent_id(merger.api, receipt["id"]) != uuid(receipt["original_parent"]):
            raise Hold("native_database_restore_not_verified")
        receipt["status"] = "restored"
        merger.journal.save()
    original = [uuid(node["id"]) for node in record["source"]["nodes"]]
    moved = {uuid(r["id"]) for r in moved_receipts(record)}
    for identifier in original:
        if identifier in moved:
            restore_position(merger.api, identifier, record["source_id"], record["native_restore_contexts"][identifier])
    if moved:
        current = [uuid(node["id"]) for node in merger.api.children(record["source_id"])]
        if [key for key in current if key in original] != original:
            raise Hold("native_database_restore_order_not_verified")
