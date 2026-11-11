"""Read-only test of a supplied Notion MCP token against pipeline destinations."""

import argparse
import datetime as dt
import getpass
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import warnings

from .api import PilotError
from .mcp import BearerCredentials, McpClient, McpToolError
from .notion_mcp import body


ROOT = Path(__file__).resolve().parent.parent
TOKEN_ENV = "SNS_NOTION_MCP_TOKEN"


def input_token():
    if TOKEN_ENV in os.environ:
        return BearerCredentials(os.environ[TOKEN_ENV]).token()
    if not sys.stdin.isatty():
        raise PilotError("test-notion-token.command를 열어 토큰을 숨김 입력해주세요.")
    # Never let getpass fall back to echoed input when a terminal is unavailable.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            token = getpass.getpass("Notion MCP 토큰 (화면·파일에 표시하지 않음): ").strip()
        except getpass.GetPassWarning:
            raise PilotError("안전한 숨김 입력을 사용할 수 없습니다. 터미널에서 직접 실행해주세요.") from None
    return BearerCredentials(token).token()


def probe(token, config, day=None, diagnose=False):
    """Only read tools are called; write permission is distinct from a write test."""
    if day:
        try:
            dt.date.fromisoformat(day)
        except ValueError:
            raise PilotError("조회 날짜를 YYYY-MM-DD 형식으로 입력해주세요.") from None
    notion = config["notion"]
    client = McpClient(BearerCredentials(token))
    access_map = client.call("get-tool-access", {})
    access = access_map.get("current_tool_access", {})
    statuses = {name: access.get(name, {}).get("status", "unknown")
                for name in ("fetch", "query_data_sources", "update_page")}
    allowed = {"available", "available_with_limit"}
    result = {
        "status": "connection_verified",
        "auth_mode": "mcp_token",
        "mcp_connected": True,
        "endpoint": "https://mcp.notion.com/mcp",
        "protocol": client.protocol,
        "tool_count": len(client.tools),
        "tool_access": statuses,
        "rules_read": False,
        "daily_data_source_read": False,
        "report_tools_authorized": all(status in allowed for status in statuses.values()),
        "checks": [],
        "write_tested": False,
        "writes_performed": 0,
        "token_persisted": False,
    }

    def check(target, operation, entries=None):
        entries = result["checks"] if entries is None else entries
        try:
            value = operation()
            entries.append({"target": target, "status": "passed"})
            return value
        except PilotError as error:
            entry = {"target": target, "status": "failed", "error": str(error)}
            if isinstance(error, McpToolError):
                entry.update({"error_code": error.code, "http_status": error.status})
            entries.append(entry)
            return None

    if any(name in client.tools for name in ("get-self", "notion-get-self")):
        identity = check("connected_workspace", lambda: client.call("get-self", {}))
        if isinstance(identity, dict) and isinstance(identity.get("workspace"), dict):
            workspace = identity["workspace"]
            result["workspace"] = {key: workspace[key] for key in ("id", "name")
                                   if isinstance(workspace.get(key), str)}
        if isinstance(identity, dict) and isinstance(identity.get("user"), dict):
            user = identity["user"]
            result["connection_identity"] = {key: user[key] for key in ("type", "id", "name")
                                             if isinstance(user.get(key), str)}

    def fetch(identifier):
        if statuses["fetch"] not in allowed:
            raise PilotError("현재 토큰의 페이지 조회 도구 사용 권한을 확인해야 합니다.")
        page = client.call("fetch", {"id": identifier})
        if not isinstance(page, dict) or not page.get("text"):
            raise PilotError("Notion 문서 응답을 확인하지 못했습니다.")
        return page

    def read_rules():
        page = fetch(notion["rules_page_id"])
        if page.get("truncated") or page.get("unknown_block_count", 0):
            raise PilotError("규정집 전체를 읽지 못했습니다.")
        text = re.sub(r"<page\b[^>]*>.*?</page>", "", body(page), flags=re.S)
        version = re.search(r"\bv\d+(?:\.\d+)+", page.get("title", "") + "\n" + text)
        if not version or not page.get("page_last_edited_at"):
            raise PilotError("규정 버전·최종 수정 시각을 확인하지 못했습니다.")
        return version.group()

    version = check("rules_page", read_rules)
    if version:
        result.update({"rules_read": True, "rules_version": version})

    def read_schema():
        schema = fetch("collection://" + notion["daily_data_source_id"])
        required = ['"date:이벤트 시간:start"', '"유형"', 'url TEXT']
        if any(column not in schema["text"] for column in required):
            raise PilotError("일일 업무 데이터베이스의 날짜·유형 속성을 확인해야 합니다.")
        return True

    if check("daily_data_source", read_schema):
        result["daily_data_source_read"] = True
    if day:
        from .notion_mcp import NotionMcpClient
        page = check("daily_page", lambda: NotionMcpClient(client, notion["daily_data_source_id"]).daily_page(day))
        if page:
            result.update({"daily_page_read": True, "daily_page_id": page["id"], "day": day})

    if diagnose:
        # Compare the same token and session across documented read operations.
        # Never change sharing, tool grants, configured IDs, or page contents.
        details = {"reference_checks": [], "searches": [], "tool_schemas": {}}
        result["diagnostics"] = details
        for suffix in ("fetch", "search", "get-self"):
            names = [name for name in client.tools if name in {suffix, "notion-" + suffix}]
            if len(names) == 1:
                details["tool_schemas"][suffix] = client.tools[names[0]].get("inputSchema", {})

        def compare_reference(label, identifier):
            entries = details["reference_checks"]
            page = check(label, lambda: fetch(identifier), entries)
            entries[-1]["reference"] = identifier
            if page:
                entries[-1].update({
                    "title": page.get("title"),
                    "data_source_ids": sorted(set(re.findall(
                        r"collection://([0-9a-fA-F-]{36})", page["text"]))),
                })

        import uuid
        compact = uuid.UUID(notion["rules_page_id"]).hex
        compare_reference("rules_notion_url", "https://www.notion.so/" + compact)
        compare_reference("rules_app_url", "https://app.notion.com/p/" + compact)
        database_id = notion.get("daily_database_page_id")
        if database_id:
            compact_database = uuid.UUID(database_id).hex
            compare_reference("daily_database_id", database_id)
            compare_reference("daily_database_url", "https://www.notion.so/" + compact_database)

        names = [name for name in client.tools if name in {"search", "notion-search"}]
        search_access = access.get("search", {})
        if len(names) == 1 and search_access.get("status") in allowed:
            for query in ("인스타그램 관객 댓글 수집", "일일 업무"):
                response = check("search_" + query, lambda q=query: client.call("search", {"query": q}), details["searches"])
                entry = details["searches"][-1]
                if isinstance(response, dict):
                    rows = response.get("results", [])
                    if isinstance(rows, list):
                        entry["results"] = [
                            {key: row[key] for key in ("id", "title", "url") if isinstance(row.get(key), str)}
                            for row in rows[:5] if isinstance(row, dict)
                        ]
                        entry["returned_count"] = len(rows)
                    entry["response_keys"] = sorted(response)
                    entry["has_more"] = bool(response.get("has_more"))
        else:
            details["search_status"] = search_access.get("status", "not_exposed")
    failed = any(entry["status"] == "failed" for entry in result["checks"])
    result["status"] = "target_access_failed" if failed else "read_verified"
    result["report_ready"] = not failed and result["report_tools_authorized"]
    return result


def save_result(path, value):
    """Persist a whitelisted diagnostic only, with a private atomic replacement."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".notion-token-test-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None):
    parser = argparse.ArgumentParser(description="전달받은 Notion MCP 토큰으로 연결·보고 대상 조회 테스트")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--day", help="해당 날짜 일일 업무까지 읽기 검증 (YYYY-MM-DD)")
    parser.add_argument("--result", type=Path, default=ROOT / ".local/notion-mcp-token-test.json")
    parser.add_argument("--interactive", action="store_true", help="완료 후 Enter 입력까지 결과 유지")
    parser.add_argument("--diagnose", action="store_true", help="문서 ID·전체 URL·검색·연결 주체를 같은 토큰으로 비교")
    args = parser.parse_args(argv)
    token = None
    try:
        config = json.loads(args.config.read_text())
        print("Notion MCP 토큰 연결과 보고 대상 조회를 검사합니다. 문서를 수정하지 않습니다.", flush=True)
        token = input_token()
        result = probe(token, config, args.day, diagnose=args.diagnose)
        # Even malformed remote metadata must not echo the credential.
        encoded = json.dumps(result, ensure_ascii=False).replace(token, "[REDACTED]")
        result = json.loads(encoded)
        code = 0 if result.get("status") == "read_verified" else 1
    except PilotError as error:
        result = {"status": "connection_failed", "error": str(error).replace(token, "[REDACTED]") if token else str(error),
                  "auth_mode": "mcp_token", "write_tested": False, "writes_performed": 0, "token_persisted": False}
        code = 1
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        result = {"status": "failed", "error": "Notion MCP 설정·응답을 확인해주세요. 인증정보는 출력하지 않습니다.",
                  "auth_mode": "mcp_token", "write_tested": False, "writes_performed": 0, "token_persisted": False}
        code = 1
    except (EOFError, KeyboardInterrupt):
        result = {"status": "cancelled", "write_tested": False, "writes_performed": 0, "token_persisted": False}
        code = 1
    try:
        save_result(args.result, result)
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    except OSError:
        print("테스트 결과 파일을 저장하지 못했습니다. 인증정보는 출력하지 않습니다.", file=sys.stderr)
        code = 1
    if args.interactive and sys.stdin.isatty():
        try:
            input("Enter를 누르면 닫습니다.")
        except (EOFError, KeyboardInterrupt):
            pass
    return code


if __name__ == "__main__":
    sys.exit(main())
