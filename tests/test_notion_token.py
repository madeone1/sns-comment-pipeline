import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import urllib.error

from sns_comment import cloud, notion_token
from sns_comment.api import PilotError
from sns_comment.mcp import BearerCredentials, McpClient, McpToolError


TOKEN = "synthetic-mcp-test-" + "x" * 600
CONFIG = {"notion": {"rules_page_id": "11111111-1111-4111-8111-111111111111",
                     "daily_data_source_id": "22222222-2222-4222-8222-222222222222"}}


class Response:
    def __init__(self, value, status=200):
        self.headers = {"Content-Type": "application/json"}
        self.status = status
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        return json.dumps(self.value).encode()


class Opener:
    def __init__(self, denied=False, reject=False, missing_rules=False, url_rules=False, search_available=True, principal=None):
        self.requests = []
        self.denied, self.reject = denied, reject
        self.missing_rules = missing_rules
        self.url_rules = url_rules
        self.search_available = search_available
        self.principal = principal

    def open(self, request, **kwargs):
        self.requests.append(request)
        if self.reject:
            raise urllib.error.HTTPError(request.full_url, 401, TOKEN, {}, io.BytesIO(TOKEN.encode()))
        message = json.loads(request.data)
        method = message["method"]
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18"}
        elif method == "notifications/initialized":
            return Response({}, 202)
        elif method == "tools/list":
            result = {"tools": [{"name": "notion-" + name, "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}}} for name in ("get-tool-access", "get-self", "fetch", "update-page", "query-data-sources", "search")]}
        elif method == "tools/call":
            name = message["params"]["name"]
            if name == "notion-get-tool-access":
                value = {"current_tool_access": {key: {"status": "available"} for key in ("fetch", "query_data_sources", "update_page")}}
                if self.denied:
                    value["current_tool_access"]["update_page"]["status"] = "unavailable"
                value["current_tool_access"]["search"] = {"status": "available" if self.search_available else "unavailable"}
            elif name == "notion-get-self":
                value = {"workspace": {"id": "33333333-3333-4333-8333-333333333333", "name": "Synthetic test workspace"}}
                if self.principal:
                    value["user"] = self.principal
            elif name == "notion-search":
                if not self.search_available:
                    raise AssertionError("Unavailable search must not be called")
                value = {"results": [{"id": CONFIG["notion"]["rules_page_id"], "title": "Synthetic regulation", "url": "https://www.notion.so/" + CONFIG["notion"]["rules_page_id"].replace("-", "")}], "has_more": False}
            elif name == "notion-fetch":
                identifier = message["params"]["arguments"]["id"]
                if self.missing_rules and not identifier.startswith("collection://") and not (self.url_rules and identifier.startswith("https://")):
                    error = {"status": 404, "code": "object_not_found", "message": "Not accessible " + TOKEN}
                    return Response({"jsonrpc": "2.0", "id": message["id"], "result": {
                        "isError": True, "content": [{"type": "text", "text": json.dumps(error)}]}})
                value = {"text": 'url TEXT, "유형" TEXT, "date:이벤트 시간:start" TEXT'} if identifier.startswith("collection://") else {
                    "text": "Regulation v1.1", "page_last_edited_at": "2026-11-06T00:00:00Z"}
                if "44444444444444448444444444444444" in identifier.replace("-", ""):
                    value = {"title": "Synthetic daily work", "text": '<database><data-source url="collection://' + CONFIG["notion"]["daily_data_source_id"] + '"></data-source></database>'}
            else:
                raise AssertionError("The token probe must never write or run a query without a date")
            result = {"structuredContent": value}
        else:
            raise AssertionError(method)
        return Response({"jsonrpc": "2.0", "id": message["id"], "result": result})


class NotionTokenTests(unittest.TestCase):
    def test_long_bearer_is_complete_and_only_sent_to_official_endpoint_headers(self):
        opener = Opener()
        with mock.patch.object(notion_token, "McpClient", side_effect=lambda credential: McpClient(credential, opener=opener)):
            result = notion_token.probe(TOKEN, CONFIG)
        self.assertEqual(result["status"], "read_verified")
        self.assertTrue(result["rules_read"])
        self.assertTrue(result["daily_data_source_read"])
        self.assertFalse(result["write_tested"])
        for request in opener.requests:
            self.assertEqual(request.full_url, "https://mcp.notion.com/mcp")
            self.assertEqual(request.get_header("Authorization"), "Bearer " + TOKEN)
            self.assertNotIn(TOKEN, request.full_url)
            self.assertNotIn(TOKEN, request.data.decode())
        self.assertNotIn(TOKEN, json.dumps(result))

    def test_rejected_bearer_never_refreshes_or_reads_oauth_keychain(self):
        opener = Opener(reject=True)
        with mock.patch("sns_comment.notion_oauth.oauth_http") as oauth, mock.patch("sns_comment.notion_oauth.read_local") as read, mock.patch("sns_comment.notion_oauth.save_local") as save:
            with self.assertRaises(PilotError) as raised:
                McpClient(BearerCredentials(TOKEN), opener=opener)
            oauth.assert_not_called()
            read.assert_not_called()
            save.assert_not_called()
        self.assertEqual(len(opener.requests), 1)
        self.assertNotIn(TOKEN, str(raised.exception))

    def test_unsupported_report_tools_are_not_reported_as_integration_ready(self):
        opener = Opener(denied=True)
        with mock.patch.object(notion_token, "McpClient", side_effect=lambda credential: McpClient(credential, opener=opener)):
            result = notion_token.probe(TOKEN, CONFIG)
        self.assertTrue(result["mcp_connected"])
        self.assertTrue(result["rules_read"])
        self.assertFalse(result["report_tools_authorized"])
        self.assertFalse(result["report_ready"])

    def test_missing_page_preserves_connection_success_and_checks_other_target(self):
        opener = Opener(missing_rules=True)
        with mock.patch.object(notion_token, "McpClient", side_effect=lambda credential: McpClient(credential, opener=opener)):
            result = notion_token.probe(TOKEN, CONFIG)
        self.assertTrue(result["mcp_connected"])
        self.assertEqual(result["workspace"]["name"], "Synthetic test workspace")
        self.assertEqual(result["status"], "target_access_failed")
        self.assertFalse(result["rules_read"])
        self.assertTrue(result["daily_data_source_read"])
        self.assertFalse(result["report_ready"])
        failure = next(item for item in result["checks"] if item["target"] == "rules_page")
        self.assertEqual(failure["error_code"], "object_not_found")
        self.assertEqual(failure["http_status"], 404)
        self.assertNotIn(TOKEN, json.dumps(result))

    def test_error_detail_retains_only_recognized_codes_and_never_remote_messages(self):
        for value in ({"code": "object_not_found", "status": 404, "message": TOKEN},
                      {"error": {"code": "restricted_resource", "message": TOKEN}},
                      {"code": TOKEN, "status": TOKEN, "message": TOKEN},
                      {"code": {"malformed": TOKEN}}):
            with self.subTest(value=value):
                error = McpToolError("fetch", {"structuredContent": value})
                self.assertNotIn(TOKEN, str(error))
        self.assertEqual(McpToolError("fetch", {"structuredContent": {"code": "object_not_found"}}).code, "object_not_found")

    def test_invalid_date_stops_before_requesting_any_token_authenticated_endpoint(self):
        with mock.patch.object(notion_token, "McpClient") as client:
            with self.assertRaises(PilotError):
                notion_token.probe(TOKEN, CONFIG, day="not-a-date")
            client.assert_not_called()

    def test_diagnostics_compare_full_urls_and_search_with_the_same_token_without_writes(self):
        opener = Opener(missing_rules=True, url_rules=True)
        with mock.patch.object(notion_token, "McpClient", side_effect=lambda credential: McpClient(credential, opener=opener)):
            result = notion_token.probe(TOKEN, CONFIG, diagnose=True)
        self.assertFalse(result["rules_read"])
        self.assertFalse(result["report_ready"])
        details = result["diagnostics"]
        self.assertEqual([item["status"] for item in details["reference_checks"]], ["passed", "passed"])
        self.assertEqual(len(details["searches"]), 2)
        self.assertTrue(all(item["returned_count"] == 1 for item in details["searches"]))
        self.assertEqual(details["tool_schemas"]["fetch"]["properties"]["id"]["type"], "string")
        for request in opener.requests:
            self.assertEqual(request.get_header("Authorization"), "Bearer " + TOKEN)
            message = json.loads(request.data)
            if message["method"] == "tools/call":
                self.assertNotIn(message["params"]["name"], ("notion-update-page", "notion-create-pages"))
        self.assertNotIn(TOKEN, json.dumps(result))

    def test_connected_principal_is_distinguished_from_workspace_without_exporting_email(self):
        opener = Opener(principal={"type": "bot", "id": "55555555-5555-4555-8555-555555555555", "name": "Synthetic bot", "email": TOKEN})
        with mock.patch.object(notion_token, "McpClient", side_effect=lambda credential: McpClient(credential, opener=opener)):
            result = notion_token.probe(TOKEN, CONFIG)
        self.assertEqual(result["connection_identity"]["type"], "bot")
        self.assertNotIn("email", result["connection_identity"])
        self.assertNotIn(TOKEN, json.dumps(result))

    def test_parent_database_comparison_reports_returned_source_without_changing_config(self):
        config = {"notion": {**CONFIG["notion"], "daily_database_page_id": "44444444-4444-4444-8444-444444444444"}}
        before = json.dumps(config, sort_keys=True)
        opener = Opener()
        with mock.patch.object(notion_token, "McpClient", side_effect=lambda credential: McpClient(credential, opener=opener)):
            result = notion_token.probe(TOKEN, config, diagnose=True)
        parents = [item for item in result["diagnostics"]["reference_checks"] if item["target"].startswith("daily_database")]
        self.assertEqual(len(parents), 2)
        for item in parents:
            self.assertEqual(item["data_source_ids"], [CONFIG["notion"]["daily_data_source_id"]])
        self.assertEqual(json.dumps(config, sort_keys=True), before)

    def test_diagnostic_search_respects_reported_tool_availability(self):
        opener = Opener(search_available=False)
        with mock.patch.object(notion_token, "McpClient", side_effect=lambda credential: McpClient(credential, opener=opener)):
            result = notion_token.probe(TOKEN, CONFIG, diagnose=True)
        self.assertEqual(result["diagnostics"]["search_status"], "unavailable")
        self.assertEqual(result["diagnostics"]["searches"], [])

    def test_invalid_header_values_are_rejected(self):
        for value in (None, "", "token\nother", "Bearer token", "token\x7f", "토큰"):
            with self.subTest(value=value), self.assertRaises(PilotError):
                BearerCredentials(value)

    def test_missing_hidden_terminal_fails_without_prompting_or_using_other_credentials(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(notion_token.sys.stdin, "isatty", return_value=False), mock.patch.object(notion_token.getpass, "getpass") as prompt:
            with self.assertRaises(PilotError):
                notion_token.input_token()
            prompt.assert_not_called()

    def test_hidden_input_preserves_long_token(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(notion_token.sys.stdin, "isatty", return_value=True), mock.patch.object(notion_token.getpass, "getpass", return_value=TOKEN):
            self.assertEqual(notion_token.input_token(), TOKEN)

    def test_hidden_input_does_not_fall_back_to_echoed_input(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(notion_token.sys.stdin, "isatty", return_value=True), mock.patch.object(notion_token.getpass, "getpass", side_effect=notion_token.getpass.GetPassWarning("unavailable")):
            with self.assertRaises(PilotError):
                notion_token.input_token()

    def test_cli_persists_only_safe_result_with_private_permissions_even_on_failure(self):
        for failure in (False, True):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                result_path = Path(directory) / "result.json"
                output = io.StringIO()
                outcome = {"status": "read_verified", "token_persisted": False}
                probe = mock.patch.object(notion_token, "probe", side_effect=PilotError("rejected " + TOKEN)) if failure else mock.patch.object(notion_token, "probe", return_value=outcome)
                with mock.patch.dict(os.environ, {notion_token.TOKEN_ENV: TOKEN}, clear=True), probe, contextlib.redirect_stdout(output):
                    code = notion_token.main(["--result", str(result_path)])
                self.assertEqual(code, int(failure))
                self.assertNotIn(TOKEN, output.getvalue())
                self.assertNotIn(TOKEN, result_path.read_text())
                self.assertEqual(result_path.stat().st_mode & 0o777, 0o600)

    def test_cloud_ignores_diagnostic_token_and_keeps_existing_account_oauth(self):
        state = mock.MagicMock()
        output = io.StringIO()
        environment = {"GOOGLE_CLOUD_PROJECT": "synthetic-project", "SNS_STATE_BUCKET": "synthetic-bucket",
                       "SNS_NOTION_OAUTH_SECRET": "projects/synthetic/secrets/notion", notion_token.TOKEN_ENV: TOKEN}
        with mock.patch.dict(os.environ, environment, clear=True), mock.patch.object(cloud, "CloudState", return_value=state), mock.patch.object(cloud, "cloud_credentials") as oauth, mock.patch.object(cloud, "McpClient") as client, mock.patch.object(cloud, "NotionMcpClient"), mock.patch.object(cloud, "Analyzer"), mock.patch.object(cloud, "execute", return_value={"status":"complete"}) as execute, contextlib.redirect_stdout(output):
            self.assertEqual(cloud.main([]), 0)
            oauth.return_value.refresh.assert_called_once()
            self.assertIs(client.call_args.args[0], oauth.return_value)
            execute.assert_called_once()
        self.assertNotIn(TOKEN, output.getvalue())

    def test_empty_explicit_cloud_token_fails_without_oauth_fallback_or_collection(self):
        environment = {"GOOGLE_CLOUD_PROJECT": "synthetic-project", "SNS_STATE_BUCKET": "synthetic-bucket", notion_token.TOKEN_ENV: ""}
        with mock.patch.dict(os.environ, environment, clear=True), mock.patch.object(cloud, "CloudState"), mock.patch.object(cloud, "cloud_credentials") as oauth, mock.patch.object(cloud, "execute") as execute, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cloud.main([]), 1)
            oauth.assert_not_called()
            execute.assert_not_called()

    def test_existing_cloud_oauth_path_still_refreshes_once(self):
        environment = {"GOOGLE_CLOUD_PROJECT": "synthetic-project", "SNS_STATE_BUCKET": "synthetic-bucket", "SNS_NOTION_OAUTH_SECRET": "projects/synthetic/secrets/notion"}
        with mock.patch.dict(os.environ, environment, clear=True), mock.patch.object(cloud, "CloudState"), mock.patch.object(cloud, "cloud_credentials") as oauth, mock.patch.object(cloud, "McpClient"), mock.patch.object(cloud, "NotionMcpClient"), mock.patch.object(cloud, "Analyzer"), mock.patch.object(cloud, "execute", return_value={"status":"complete"}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cloud.main([]), 0)
            oauth.return_value.refresh.assert_called_once()


if __name__ == "__main__":
    unittest.main()
