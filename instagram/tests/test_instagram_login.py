import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from sns_comment import connect_accounts, profiles
from sns_comment.__main__ import main
from sns_comment.api import GraphClient, GraphError, PilotError
from sns_comment.collector import Collector


BUSAN = "waterbomb_busan_official"
SOKCHO = "waterbomb_sokcho_official"
TOKEN = "synthetic-instagram-token-" + "z" * 600
SETTINGS = {"expected_username": "waterbomb_official", "account_id": "10", "api_version": "v26.0",
            "notion": {"auth_mode": "mcp_oauth"}, "accounts": {
                BUSAN: {"account_id": None, "enabled": False, "meta_auth": "instagram_login"},
                SOKCHO: {"account_id": None, "enabled": False, "meta_auth": "instagram_login"}}}


class InstagramLoginTests(unittest.TestCase):
    def test_instagram_transport_uses_pinned_host_and_header_only_token(self):
        opener = mock.Mock()
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"user_id":"100","username":"waterbomb_busan_official"}'
        opener.open.return_value = response
        client = GraphClient(TOKEN, "v26.0", opener=opener, api_host="graph.instagram.com")
        self.assertEqual(client.get("me", fields="user_id,username")["user_id"], "100")
        request = opener.open.call_args.args[0]
        self.assertTrue(request.full_url.startswith("https://graph.instagram.com/v26.0/me?"))
        self.assertNotIn(TOKEN, request.full_url)
        self.assertEqual(request.get_header("Authorization"), "Bearer " + TOKEN)
        with self.assertRaises(PilotError):
            client.get("me/assigned_instagram_accounts", fields="id,username")
        self.assertEqual(opener.open.call_count, 1)

    def test_arbitrary_hosts_are_rejected_and_legacy_host_does_not_switch(self):
        with self.assertRaises(PilotError):
            GraphClient(TOKEN, "v26.0", api_host="attacker.example")
        client = GraphClient(TOKEN, "v26.0", opener=mock.Mock())
        self.assertEqual(client.api_host, "graph.facebook.com")
        with self.assertRaises(PilotError):
            client.get("me/friends", fields="id")

    def test_instagram_identity_uses_user_id_and_never_facebook_discovery(self):
        client = mock.Mock(api_host="graph.instagram.com")
        client.get.return_value = {"id": "app-scoped-id", "user_id": "100", "username": BUSAN}
        self.assertEqual(Collector(client, None, BUSAN).account(), "100")
        client.get.assert_called_once_with("me", fields="user_id,username")
        with self.assertRaises(PilotError):
            Collector(client, None, BUSAN).account("10")
        with self.assertRaises(PilotError):
            Collector(client, None, SOKCHO).account()

    def test_instagram_profiles_cannot_share_primary_token(self):
        settings = copy.deepcopy(SETTINGS)
        settings["accounts"][BUSAN]["meta_credential"] = "primary"
        with self.assertRaises(PilotError):
            profiles.validate(settings)

    def test_instagram_profile_keeps_legacy_official_connection_unchanged(self):
        official = profiles.select(SETTINGS)
        busan = profiles.select(SETTINGS, BUSAN)
        self.assertEqual(official["_meta_api_host"], "graph.facebook.com")
        self.assertEqual(official["_meta_token_env"], "SNS_IG_ACCESS_TOKEN")
        self.assertEqual(official["_keychain_account"], "waterbomb_official")
        self.assertEqual(busan["_meta_api_host"], "graph.instagram.com")
        self.assertNotEqual(busan["_meta_token_env"], official["_meta_token_env"])
        self.assertEqual(busan["_keychain_account"], BUSAN)
        with mock.patch.dict(os.environ, {"SNS_IG_ACCESS_TOKEN": TOKEN}, clear=True):
            self.assertIsNone(profiles.environment_token(busan))

    def run_connect(self, path, client, token=TOKEN):
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch("sns_comment.credentials.prompt_meta_token", return_value=token), \
                mock.patch("sns_comment.api.GraphClient", return_value=client) as factory, \
                mock.patch("sns_comment.notion_oauth.save_local") as save, \
                mock.patch("sns_comment.notion_oauth.read_local", return_value=token), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(["accounts", "connect", "--account", BUSAN, "--config", str(path)])
        self.assertNotIn(token, output.getvalue() + errors.getvalue())
        self.assertEqual(factory.call_args.kwargs["api_host"], "graph.instagram.com")
        return code, save, output.getvalue(), errors.getvalue()

    def test_account_specific_token_is_saved_only_under_verified_instagram_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text(json.dumps(SETTINGS), encoding="utf-8")
            client = mock.Mock(api_host="graph.instagram.com")
            client.get.return_value = {"user_id": "100", "username": BUSAN}
            code, save, output, _ = self.run_connect(path, client)
            self.assertEqual(code, 0)
            self.assertEqual(save.call_args.kwargs["keychain_account"], BUSAN)
            config = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(config["account_id"], "10")
            self.assertEqual(config["accounts"][BUSAN]["account_id"], "100")
            self.assertTrue(config["accounts"][BUSAN]["enabled"])
            self.assertFalse(config["accounts"][SOKCHO]["enabled"])
            self.assertFalse(json.loads(output)["comments_collected"])
            self.assertNotIn(TOKEN, path.read_text(encoding="utf-8"))

    def test_token_for_another_instagram_account_does_not_replace_credentials_or_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text(json.dumps(SETTINGS), encoding="utf-8")
            before = path.read_bytes()
            client = mock.Mock(api_host="graph.instagram.com")
            client.get.return_value = {"user_id": "10", "username": "waterbomb_official"}
            code, save, _, error = self.run_connect(path, client)
            self.assertEqual(code, 1)
            save.assert_not_called()
            self.assertIn("해당 계정 행에서 생성한 토큰", error)
            self.assertEqual(path.read_bytes(), before)
            report = json.loads((path.parent / ".local/account-connection.json").read_text(encoding="utf-8"))
            self.assertEqual(report["api_host"], "graph.instagram.com")

    def test_instagram_api_error_records_correct_host_and_preserves_existing_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text(json.dumps(SETTINGS), encoding="utf-8")
            before = path.read_bytes()
            client = mock.Mock(api_host="graph.instagram.com")
            client.get.side_effect = GraphError(400, 100, 33)
            code, save, _, error = self.run_connect(path, client)
            self.assertEqual(code, 1)
            save.assert_not_called()
            self.assertIn("@" + BUSAN, error)
            self.assertEqual(path.read_bytes(), before)

    def test_finder_wizard_requests_two_independent_tokens_and_leaves_official_alone(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text(json.dumps(SETTINGS), encoding="utf-8")
            with mock.patch.object(connect_accounts, "cli", return_value=0) as cli, \
                    mock.patch("sys.stdin.isatty", return_value=False), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(connect_accounts.main(["--config", str(path)]), 0)
            self.assertEqual([call.args[0][3] for call in cli.call_args_list], [BUSAN, SOKCHO])
            self.assertTrue(all("--credential" not in call.args[0] for call in cli.call_args_list))


if __name__ == "__main__":
    unittest.main()
