"""Shared Notion content normalization for conservative daily-page merging."""

import copy
import hashlib
import json
import re
import urllib.parse

from .api import PilotError


class Hold(PilotError):
    """A reason code, never remote content or credentials."""

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

def uuid(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F-]{32,36}", value):
        raise Hold("invalid_page_id")
    value = value.replace("-", "").lower()
    if not re.fullmatch(r"[0-9a-f]{32}", value):
        raise Hold("invalid_page_id")
    return value

def rich_text(items):
    result = []
    defaults = dict(bold=False, italic=False, strikethrough=False, underline=False, code=False, color="default")
    for item in items:
        kind = item.get("type", "text")
        if set(item) - {"type", kind, "annotations", "plain_text", "href"}:
            raise Hold("unsupported_rich_text_field")
        value = copy.deepcopy(item.get(kind, {}))
        if kind == "mention":
            mention = value.get("type")
            if mention not in {"user", "page", "database", "date"}:
                raise Hold("unsupported_mention")
            if mention == "date":
                value = {"type": mention, mention: value[mention]}
            else:
                value = {"type": mention, mention: {"id": value[mention]["id"]}}
        elif kind not in {"text", "equation"}:
            raise Hold("unsupported_rich_text")
        if kind == "text":
            value = {"content": value.get("content", ""), "link": value.get("link")}
        annotations = {**defaults, **item.get("annotations", {})}
        if set(annotations) != set(defaults):
            raise Hold("unsupported_annotation")
        result.append({"type": kind, kind: value, "annotations": annotations})
    return result

RICH_BLOCKS = {"paragraph", "heading_1", "heading_2", "heading_3", "heading_4", "quote",
               "toggle", "bulleted_list_item", "numbered_list_item", "to_do", "callout", "code"}

MEDIA = {"image", "video", "audio", "file", "pdf"}

SUPPORTED = RICH_BLOCKS | MEDIA | {"divider", "equation", "bookmark", "embed", "table", "table_row"}

def external_media(value):
    if value is None:
        return None
    if value.get("type") == "emoji":
        return {"type": "emoji", "emoji": value["emoji"]}
    if value.get("type") != "external" or not value.get("external", {}).get("url"):
        raise Hold("hosted_file_or_custom_icon_requires_preservation")
    url = value["external"]["url"]
    host = urllib.parse.urlsplit(url).hostname or ""
    if host.endswith(("amazonaws.com", "notion-static.com", "notion.so", "notion.com")):
        raise Hold("temporary_or_internal_media_url")
    return {"type": "external", "external": {"url": url}}

def semantic(node):
    kind = node.get("type")
    if kind == "unsupported" and node.get("_template_toggle_button") is True:
        return {"type": kind, kind: {"template_toggle_button": True}, "_children": []}
    if kind == "child_database" and node.get("_native_database"):
        return {"type": kind, kind: {"database_id": uuid(node["id"]), "title": node[kind].get("title", ""),
                                     "snapshot_hash": node["_native_database"]["hash"]}, "_children": []}
    # Planned nodes have already been normalized, retaining native object IDs.
    if kind == "child_database" and set(node.get(kind, {})) == {"database_id", "title", "snapshot_hash"}:
        return {"type": kind, kind: copy.deepcopy(node[kind]), "_children": []}
    if kind == "unsupported" and node.get(kind) == {"template_toggle_button": True}:
        return {"type": kind, kind: copy.deepcopy(node[kind]), "_children": []}
    if kind not in SUPPORTED:
        raise Hold("unsupported_block:" + str(kind))
    value = copy.deepcopy(node.get(kind, {}))
    allowed = set()
    data = {}
    if kind in RICH_BLOCKS:
        allowed |= {"rich_text", "color"}
        data["rich_text"] = rich_text(value.get("rich_text", []))
        if kind != "code":
            data["color"] = value.get("color", "default")
        if kind == "paragraph":
            allowed.add("icon")
            # The API can return an optional null paragraph icon. It contains
            # no information; non-null icons are kept explicitly.
            if value.get("icon") is not None:
                data["icon"] = external_media(value["icon"])
        if kind.startswith("heading_"):
            allowed.add("is_toggleable")
            data["is_toggleable"] = value.get("is_toggleable", False)
        if kind == "to_do":
            allowed.add("checked")
            data["checked"] = value.get("checked", False)
        if kind == "callout":
            allowed.add("icon")
            data["icon"] = external_media(value.get("icon"))
        if kind == "code":
            allowed |= {"caption", "language"}
            data.update(caption=rich_text(value.get("caption", [])), language=value.get("language", "plain text"))
    elif kind in MEDIA:
        allowed |= {"type", "external", "caption", "name"}
        data = external_media(value)
        data["caption"] = rich_text(value.get("caption", []))
        if "name" in value:
            data["name"] = value["name"]
    elif kind in {"bookmark", "embed"}:
        allowed |= {"url", "caption"}
        data = {"url": value["url"], "caption": rich_text(value.get("caption", []))}
    elif kind == "equation":
        allowed.add("expression")
        data = {"expression": value["expression"]}
    elif kind == "table":
        allowed |= {"table_width", "has_column_header", "has_row_header"}
        data = {key: value[key] for key in allowed}
    elif kind == "table_row":
        allowed.add("cells")
        data = {"cells": [rich_text(cell) for cell in value["cells"]]}
    if set(value) - allowed:
        raise Hold("unsupported_block_field:" + kind)
    children = [semantic(child) for child in node.get("_children", [])]
    if kind == "table" and (not children or len(children) > 100 or any(c["type"] != "table_row" for c in children)):
        raise Hold("unsupported_table_shape")
    if children and kind in MEDIA | {"code", "equation", "divider", "bookmark", "embed", "table_row"}:
        raise Hold("unexpected_block_children")
    return {"type": kind, kind: data, "_children": children}

def create_payload(node, atomic=False):
    kind = node["type"]
    if kind in {"child_database", "unsupported"}:
        raise Hold("native_object_must_not_be_recreated")
    payload = {"object": "block", "type": kind, kind: copy.deepcopy(node[kind])}
    if atomic or kind == "table":
        if node["_children"]:
            payload[kind]["children"] = [create_payload(child, atomic=True) for child in node["_children"]]
    if len(json.dumps(payload, ensure_ascii=False).encode()) > 450_000:
        raise Hold("block_request_too_large")
    return payload

def page_values(page):
    # Trash changes last-edited metadata. Preserve its original value in the
    # appendix, but compare user content and all other properties independently.
    props = {name: value for name, value in page["properties"].items()
             if value.get("type") not in {"last_edited_time", "last_edited_by"}}
    return {"id": uuid(page["id"]), "parent": page["parent"], "properties": props,
            "icon": page.get("icon"), "cover": page.get("cover")}
