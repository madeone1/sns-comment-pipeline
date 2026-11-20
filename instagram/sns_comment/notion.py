"""Scoped Notion data API client; only the dated SNS Comments section is written."""

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from .api import NoRedirect, PilotError, https_context


class NotionError(PilotError):
    pass


def rich(text, link=None):
    result = []
    for start in range(0, len(str(text)), 1800):
        item = {"type": "text", "text": {"content": str(text)[start:start + 1800]}}
        if link:
            item["text"]["link"] = {"url": link}
        result.append(item)
    return result


def block(kind, text, **attributes):
    return {"object": "block", "type": kind, kind: {"rich_text": rich(text), **attributes}}


def plain(item):
    kind = item.get("type")
    return "".join(r.get("plain_text", r.get("text", {}).get("content", ""))
                   for r in item.get(kind, {}).get("rich_text", []))


class NotionClient:
    def __init__(self, token, source_id, opener=None):
        if not token or any(c.isspace() for c in token):
            raise NotionError("클라우드용 Notion 인증 정보를 등록해야 합니다.")
        self._token = token
        self.source_id = source_id
        self.opener = opener or urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=https_context()))

    def request(self, method, path, payload=None):
        if path.startswith(("http:", "https:")) or ".." in path:
            raise NotionError("허용되지 않은 Notion API 경로입니다.")
        req = urllib.request.Request("https://api.notion.com/v1/" + path,
            data=json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None,
            headers={"Authorization": "Bearer " + self._token, "Content-Type": "application/json", "Notion-Version": "2026-03-11"},
            method=method)
        try:
            # Respect Notion's sustained request limit. Writes are never blindly
            # retried after an ambiguous transport failure.
            time.sleep(0.35)
            with self.opener.open(req, timeout=40) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise NotionError(f"Notion API 실패 (HTTP {error.code}). 접근 권한·요청 상태 확인 필요.") from None
        except (OSError, ValueError):
            raise NotionError("Notion 통신 실패. 쓰기 결과 확인 후 재실행이 필요합니다.") from None

    def children(self, parent):
        rows, cursor = [], None
        while True:
            query = {"page_size": 100}
            if cursor:
                query["start_cursor"] = cursor
            value = self.request("GET", f"blocks/{parent}/children?" + urllib.parse.urlencode(query))
            rows.extend(value["results"])
            if not value.get("has_more"):
                return rows
            following = value.get("next_cursor")
            if not following or following == cursor:
                raise NotionError("Notion 페이지 목록 커서가 정상적이지 않습니다.")
            cursor = following

    def daily_page(self, day):
        result = self.request("POST", f"data_sources/{self.source_id}/query", {
            "filter": {"and": [{"property": "유형", "select": {"equals": "오늘 업무"}},
                                  {"property": "이벤트 시간", "date": {"equals": day}}]}, "page_size": 100})
        if result.get("has_more") or len(result["results"]) != 1:
            raise NotionError(f"{day} 일일 업무 페이지가 없거나 여러 개입니다. 자동 생성·임의 선택하지 않습니다.")
        page = result["results"][0]
        value = page.get("properties", {}).get("이벤트 시간", {}).get("date") or {}
        if str(value.get("start", ""))[:10] != day:
            raise NotionError("일일 업무 날짜가 보고 날짜와 일치하지 않습니다.")
        return page

    def rules(self, page_id):
        page = self.request("GET", "pages/" + page_id)
        lines, visited = [], set()

        def walk(parent, depth=0):
            if depth > 12 or parent in visited:
                raise NotionError("규정집 본문을 완전하게 읽지 못했습니다.")
            visited.add(parent)
            for item in self.children(parent):
                kind = item["type"]
                # Proposals and examples are reference children, not adopted rules.
                if kind in {"child_page", "child_database"}:
                    continue
                if kind == "table_row":
                    lines.append(" | ".join("".join(x.get("plain_text", x.get("text", {}).get("content", "")) for x in cell)
                                             for cell in item[kind]["cells"]))
                else:
                    lines.append(plain(item))
                if item.get("has_children"):
                    walk(item["id"], depth + 1)

        walk(page_id)
        content = "\n".join(lines).strip()
        if not content or "SNS" not in content and "댓글" not in content:
            raise NotionError("규정집 내용을 확인하지 못했습니다.")
        titles = [v.get("title", []) for v in page.get("properties", {}).values() if v.get("type") == "title"]
        title = " ".join(x.get("plain_text",x.get("text",{}).get("content","")) for values in titles for x in values)
        version = re.search(r"\bv\d+(?:\.\d+)+", title + "\n" + content)
        if not version:
            raise NotionError("적용할 규정집 버전을 확인하지 못했습니다.")
        return {"page_id": page_id, "version": version.group(), "last_edited_at": page["last_edited_time"], "text": content}

    def publish(self, day, sections):
        """One owned callout in SNS Comments. Other blocks/pages are untouched."""
        page = self.daily_page(day)
        rows = self.children(page["id"])
        headings = [i for i, item in enumerate(rows) if item.get("type") == "heading_3" and plain(item).strip() == "SNS Comments"]
        if len(headings) != 1:
            raise NotionError("일일 업무의 SNS Comments 제목이 없거나 중복입니다.")
        index = headings[0]
        stop = len(rows)
        for i in range(index + 1, len(rows)):
            if rows[i]["type"] in {"heading_1", "heading_2", "heading_3", "divider"}:
                stop = i
                break
        label = f"인스타그램 댓글 자동 보고 · {day} 08시 · @waterbomb_official"
        owned = [b for b in rows[index + 1:stop] if b["type"] == "callout" and plain(b).strip() == label]
        if len(owned) > 1:
            raise NotionError("자동 보고 블록이 중복되어 확인이 필요합니다.")
        if not owned:
            created = self.request("PATCH", f"blocks/{page['id']}/children", {
                "children": [block("callout", label, icon={"type": "emoji", "emoji": "📊"})],
                "position": {"type": "after_block", "after_block": {"id": rows[index]["id"]}},
                "notifications": {"mode": "silent"}})
            container = created["results"][0]
        else:
            container = owned[0]
        if len(sections) > 100:
            raise NotionError("보고서 최상위 블록 한도를 초과했습니다.")
        # Only our dated callout is regenerated. The original daily task,
        # fixed rules, manual notes outside it and child pages are preserved.
        for item in self.children(container["id"]):
            self.request("PATCH", "blocks/" + item["id"], {"archived": True})
        if sections:
            self.request("PATCH", f"blocks/{container['id']}/children", {"children": sections, "notifications": {"mode": "silent"}})
        return {"page_id": page["id"], "page_url": page.get("url"), "container_id": container["id"], "day": day}
