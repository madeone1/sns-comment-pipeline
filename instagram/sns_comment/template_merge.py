"""Add new information inside matching template sections, preserving old IDs."""

import copy
import collections
import difflib

from .merge_content import Hold, digest, page_values, rich_text, semantic, uuid


def text(node):
    kind = node["type"]
    return "".join(item.get("plain_text", item.get("text", {}).get("content", ""))
                   for item in node.get(kind, {}).get("rich_text", []))


def empty_placeholder(node):
    kind = node["type"]
    value = semantic(node)
    return (kind in {"paragraph", "bulleted_list_item", "numbered_list_item", "to_do"}
            and not value["_children"] and not value[kind]["rich_text"]
            and value[kind].get("color", "default") == "default"
            and not value[kind].get("checked", False))


def identity(node):
    value = semantic(node)
    # Containers with the same own content share the existing parent block.
    # Tables are merged by row only when their table settings also agree.
    value["_children"] = []
    return digest(value)


def value_of(prop):
    kind = prop.get("type")
    value = copy.deepcopy(prop.get(kind))
    if kind in {"people", "multi_select"}:
        value = sorted(item.get("id", item.get("name")) for item in value or [])
    if kind in {"select", "status"} and value:
        value = value.get("id", value.get("name"))
    if kind in {"title", "rich_text"}:
        value = rich_text(value or [])
    return {"type": kind, "value": value}


def property_plan(canonical, source, origin_property=None):
    """Fill empty fields and union people/tags; never replace conflicting data."""
    updates = {}
    metadata = {"created_time", "created_by", "last_edited_time", "last_edited_by"}
    for name, source_prop in source["properties"].items():
        kind = source_prop["type"]
        if kind in metadata or name == origin_property:
            continue
        target = canonical["properties"].get(name)
        if target is None or target.get("type") != kind:
            raise Hold("template_property_schema_mismatch")
        if value_of(target) == value_of(source_prop):
            continue
        if kind in {"people", "multi_select"}:
            result = copy.deepcopy(target.get(kind) or [])
            keys = {item.get("id", item.get("name")) for item in result}
            for item in source_prop.get(kind) or []:
                key = item.get("id", item.get("name"))
                if key not in keys:
                    result.append(copy.deepcopy(item)); keys.add(key)
            if len(result) == len(target.get(kind) or []):
                continue
            if len(result) > 100:
                raise Hold("merged_property_limit")
            updates[name] = {kind: [{"id": item["id"]} if item.get("id") else {"name": item["name"]} for item in result]}
        elif source_prop.get(kind) is None or source_prop.get(kind) in ([], ""):
            continue
        elif target.get(kind) is None or target.get(kind) in ([], ""):
            if kind in {"title", "rich_text"}:
                value = rich_text(source_prop[kind])
            elif kind in {"select", "status"}:
                value = {"id": source_prop[kind]["id"]} if source_prop[kind].get("id") else {"name": source_prop[kind]["name"]}
            else:
                value = copy.deepcopy(source_prop[kind])
            updates[name] = {kind: value}
        else:
            raise Hold("conflicting_page_property")
    return updates


def properties_match(page, initial, updates):
    """Compare writable values semantically, keeping unrelated old properties."""
    current = page_values(page)
    expected = copy.deepcopy(initial)
    for name, change in updates.items():
        expected["properties"][name].update(copy.deepcopy(change))
    return (all(current[key] == expected[key] for key in ("id", "parent", "icon", "cover"))
            and set(current["properties"]) == set(expected["properties"])
            and all(value_of(current["properties"][name]) == value_of(prop)
                    for name, prop in expected["properties"].items()))


class TemplatePlan:
    def __init__(self, canonical_id, canonical_nodes, sections):
        self.canonical_id = canonical_id
        self.expected = copy.deepcopy(canonical_nodes)
        self.operations = []
        self.sections = sections

    def clone(self, source, parent, after):
        op_id = "inline/" + str(len(self.operations))
        node = copy.deepcopy(semantic(source))
        node["_ref"] = op_id
        self.operations.append({"key": op_id, "parent": parent, "after": after,
                                "node": copy.deepcopy(semantic(source))})
        if node["type"] not in {"table", "child_database"}:
            # Children are created independently so every append has a receipt.
            children, node["_children"] = node["_children"], []
            previous = None
            for child in children:
                created = self.clone(child, op_id, previous)
                node["_children"].append(created)
                previous = created["_ref"]
        return node

    @staticmethod
    def ref(node):
        return node.get("id", node.get("_ref"))

    def merge_list(self, target, incoming, parent, preceding=None):
        # Empty template placeholders contain no new information. Old ones stay.
        incoming = [node for node in incoming if not node.get("_template_toggle_button") and not empty_placeholder(node)]
        original = list(target)
        for new in incoming:
            if new["type"] != "to_do":
                continue
            new_value = semantic(new); new_value["to_do"].pop("checked")
            for old in original:
                if old["type"] != "to_do":
                    continue
                old_value = semantic(old); old_value["to_do"].pop("checked")
                if new_value == old_value and new["to_do"].get("checked", False) != old["to_do"].get("checked", False):
                    raise Hold("conflicting_task_state")
        target_keys, source_keys = [identity(n) for n in original], [identity(n) for n in incoming]
        matcher = difflib.SequenceMatcher(a=target_keys, b=source_keys, autojunk=False)
        extras = collections.Counter(source_keys) - collections.Counter(target_keys)
        offset = 0
        for tag, first, stop, source_first, source_stop in matcher.get_opcodes():
            if tag == "equal":
                for old, new in zip(original[first:stop], incoming[source_first:source_stop]):
                    if semantic(old) == semantic(new):
                        continue
                    if old["type"] == "table":
                        if old["table"].get("has_column_header") and semantic(old["_children"][0]) != semantic(new["_children"][0]):
                            raise Hold("table_header_conflict")
                    self.merge_list(old.setdefault("_children", []), new.get("_children", []), self.ref(old))
                    if old["type"] == "table" and len(old["_children"]) > 100:
                        raise Hold("unsupported_table_shape")
                continue
            if tag == "delete":
                continue  # Keep all previously entered information.
            position = stop + offset
            after = self.ref(target[position - 1]) if position else preceding
            for source in incoming[source_first:source_stop]:
                # Reordered existing blocks are ambiguous, not new information.
                key = identity(source)
                if key in target_keys:
                    if not extras[key]:
                        raise Hold("section_order_or_duplicate_conflict")
                    extras[key] -= 1
                if source["type"] == "child_database":
                    slots = [i for i, node in enumerate(target) if node["type"] == "child_database"]
                    if not slots:
                        raise Hold("native_database_anchor_requires_review")
                    position = slots[-1] + 1
                    after = self.ref(target[position - 1])
                node = self.clone(source, parent, after)
                target.insert(position, node)
                after = self.ref(node)
                position += 1; offset += 1
        return target

    def split(self, nodes):
        expected = set(self.sections)
        sections, order, start, label = {}, [], 0, None
        for index, node in enumerate(nodes):
            heading = text(node).strip() if node["type"] == "heading_3" else None
            if heading not in expected:
                continue
            if heading in sections or heading in order:
                raise Hold("duplicate_template_heading")
            sections[label] = (start, index)
            order.append(heading)
            label, start = heading, index + 1
        sections[label] = (start, len(nodes))
        if order != self.sections:
            raise Hold("template_sections_mismatch")
        return sections

    def build(self, source_nodes):
        source_sections = self.split(source_nodes)
        target_sections = self.split(self.expected)
        # Work from the end so earlier section indices stay valid.
        for label in reversed([None, *self.sections]):
            start, end = target_sections[label]
            source_start, source_end = source_sections[label]
            old_body = self.expected[start:end]
            new_body = source_nodes[source_start:source_end]
            if label is not None:
                old_heading, new_heading = self.expected[start - 1], source_nodes[source_start - 1]
                if identity(old_heading) != identity(new_heading):
                    raise Hold("template_heading_format_conflict")
                if old_heading.get("_children") or new_heading.get("_children"):
                    raise Hold("template_heading_children_require_review")
            # Keep the old section separator at its end, after new information.
            tail = []
            while old_body and (old_body[-1]["type"] == "divider" or empty_placeholder(old_body[-1])):
                tail.insert(0, old_body.pop())
            while new_body and (new_body[-1]["type"] == "divider" or empty_placeholder(new_body[-1])):
                new_body = new_body[:-1]
            preceding = self.ref(self.expected[start - 1]) if start else None
            self.merge_list(old_body, new_body, self.canonical_id, preceding)
            self.expected[start:end] = [*old_body, *tail]
        return {"operations": self.operations, "expected_nodes": self.expected}


def template_plan(canonical_id, canonical_nodes, source_nodes, sections):
    return TemplatePlan(canonical_id, canonical_nodes, sections).build(source_nodes)


def verify_expected(expected, actual, receipts):
    """Check every old ID and all final content/order, including nested inserts."""
    if len(expected) != len(actual):
        raise Hold("copied_content_not_equal")
    for wanted, found in zip(expected, actual):
        identifier = wanted.get("id")
        if wanted.get("_ref"):
            identifier = receipts.get(wanted["_ref"], {}).get("id")
        if identifier and uuid(identifier) != uuid(found["id"]):
            raise Hold("canonical_original_content_changed")
        # Atomic table children receive server IDs; compare their whole content.
        own = semantic(wanted); own["_children"] = []
        other = semantic(found); other["_children"] = []
        if own != other:
            raise Hold("copied_content_not_equal")
        if wanted["type"] == "table" and wanted.get("_ref"):
            if semantic(wanted) != semantic(found):
                raise Hold("copied_content_not_equal")
        else:
            verify_expected(wanted.get("_children", []), found.get("_children", []), receipts)
