"""Small Streamable HTTP client for the official Notion MCP endpoint."""

import json
import re
import urllib.error
import urllib.request

from .api import NoRedirect, PilotError, https_context


class BearerCredentials:
    """A supplied MCP token: no browser login, refresh, or persistent storage."""

    def __init__(self, token):
        if not isinstance(token, str) or not token or any(not 33 <= ord(c) <= 126 for c in token):
            raise PilotError("공백 없는 Notion MCP 토큰을 숨김 입력으로 입력해주세요.")
        self._token = token

    def token(self):
        return self._token

    def refresh(self):
        raise PilotError("Notion MCP 토큰이 거부되었거나 만료되었습니다. 발급받은 토큰과 접근 권한을 확인해주세요.")


class McpToolError(PilotError):
    """Report recognized error codes without echoing remote messages or secrets."""

    def __init__(self, tool, result):
        self.tool = tool
        self.code = None
        self.status = None
        codes = {"object_not_found", "restricted_resource", "unauthorized", "validation_error",
                 "rate_limited", "internal_server_error", "service_unavailable", "conflict_error",
                 "invalid_token", "token_expired", "permission_denied", "forbidden"}
        candidates = [result.get("structuredContent")]
        for item in result.get("content", []):
            if item.get("type") != "text":
                continue
            raw = item.get("text", "")
            try:
                candidates.append(json.loads(raw))
            except (ValueError, TypeError):
                for code in codes:
                    if isinstance(raw, str) and re.search(r"\b" + code + r"\b", raw):
                        self.code = code
        for value in candidates:
            if not isinstance(value, dict):
                continue
            for data in (value, value.get("error")):
                if not isinstance(data, dict):
                    continue
                if isinstance(data.get("code"), str) and data["code"] in codes:
                    self.code = data["code"]
                status = data.get("status")
                if type(status) is int and 400 <= status <= 599:
                    self.status = status
        detail = []
        if self.code:
            detail.append(self.code)
        if self.status:
            detail.append("HTTP " + str(self.status))
        message = "Notion MCP 도구 실패: " + tool
        super().__init__(message + (" (" + ", ".join(detail) + ")" if detail else " (상세 코드 미제공)"))


class McpClient:
    def __init__(self, credentials, opener=None):
        self.credentials = credentials
        self.opener = opener or urllib.request.build_opener(NoRedirect(),urllib.request.HTTPSHandler(context=https_context()))
        self.session = None
        self.protocol = "2025-06-18"
        self.counter = 0
        self.tools = {}
        result = self.rpc("initialize",{"protocolVersion":self.protocol,"capabilities":{},
            "clientInfo":{"name":"MADEONE SNS Comments Pipeline","version":"1.0.0"}})
        self.protocol = result.get("protocolVersion",self.protocol)
        self.rpc("notifications/initialized",{},notification=True)
        cursor = None
        while True:
            result = self.rpc("tools/list",{"cursor":cursor} if cursor else {})
            self.tools.update({item["name"]:item for item in result["tools"]})
            following = result.get("nextCursor")
            if not following:
                break
            if cursor == following:
                raise PilotError("Notion MCP 도구 목록 커서 오류입니다.")
            cursor = following

    def rpc(self, method, params, notification=False, refreshed=False):
        self.counter += 1
        request_id = self.counter
        payload = {"jsonrpc":"2.0","method":method,"params":params}
        if not notification:
            payload["id"] = request_id
        headers = {"Authorization":"Bearer "+self.credentials.token(),"Content-Type":"application/json",
                   "Accept":"application/json, text/event-stream","MCP-Protocol-Version":self.protocol}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        request = urllib.request.Request("https://mcp.notion.com/mcp",data=json.dumps(payload).encode(),headers=headers,method="POST")
        try:
            with self.opener.open(request,timeout=90) as response:
                if response.headers.get("Mcp-Session-Id"):
                    self.session = response.headers["Mcp-Session-Id"]
                if notification and response.status in (202,204):
                    return {}
                if "text/event-stream" in response.headers.get("Content-Type",""):
                    data, raw_size = [],0
                    message = None
                    for line in response:
                        raw_size += len(line)
                        if raw_size > 5_000_000:
                            raise PilotError("Notion MCP 응답이 처리 한도를 초과했습니다.")
                        line = line.decode("utf-8").rstrip("\r\n")
                        if line.startswith("data:"):
                            data.append(line[5:].lstrip())
                        elif not line and data:
                            event = json.loads("\n".join(data))
                            data = []
                            if event.get("id") == request_id:
                                message = event
                                break
                    if message is None:
                        raise PilotError("Notion MCP 응답이 중단되었습니다.")
                else:
                    raw = response.read()
                    message = json.loads(raw) if raw else {}
            if message.get("error"):
                raise PilotError("Notion MCP 요청 오류. 코드 "+str(message["error"].get("code")))
            return message.get("result",{})
        except urllib.error.HTTPError as error:
            if error.code == 401 and not refreshed:
                self.credentials.refresh()
                return self.rpc(method,params,notification,refreshed=True)
            raise PilotError(f"Notion MCP HTTP {error.code}. 연결·권한 확인 필요.") from None
        except (OSError,ValueError,TypeError):
            raise PilotError("Notion MCP 통신 실패. 쓰기 결과 확인 후 재실행 필요.") from None

    def name(self, suffix):
        matches = [name for name in self.tools if name == suffix or name == "notion-"+suffix]
        if len(matches) != 1:
            raise PilotError("Notion MCP 필수 도구를 확인하지 못했습니다: "+suffix)
        return matches[0]

    def call(self, suffix, arguments):
        name = self.name(suffix)
        result = self.rpc("tools/call",{"name":name,"arguments":arguments})
        if result.get("isError"):
            raise McpToolError(suffix, result)
        if result.get("structuredContent") is not None:
            return result["structuredContent"]
        values = [item["text"] for item in result.get("content",[]) if item.get("type") == "text"]
        if len(values) == 1:
            try:
                return json.loads(values[0])
            except ValueError:
                return {"text":values[0]}
        return {"text":"\n".join(values)}
