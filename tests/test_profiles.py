import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import urllib.parse

from sns_comment import profiles, credentials, cloud, launcher, connect_accounts
from sns_comment.__main__ import main
from sns_comment.api import PilotError, GraphError
from sns_comment.cloud_state import CloudState
from sns_comment.demo import DemoGraph
from sns_comment.storage import Store


BUSAN = "waterbomb_busan_official"
SOKCHO = "waterbomb_sokcho_official"
TOKEN = "synthetic-meta-" + "x" * 600
BASE = {"expected_username": "waterbomb_official", "account_id": "10", "api_version": "v26.0",
        "post_url": "https://www.instagram.com/reel/DfCKxEYlZVv/",
        "collection": {"mode": "account", "media_scope": "recent_posts", "workers": 1},
        "google_sheets": {"spreadsheet_id": "original-sheet"},
        "notion": {"auth_mode": "mcp_oauth", "daily_data_source_id": "source", "rules_page_id": "rules"}}


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "config.json"
        self.settings = copy.deepcopy(BASE)
        self.settings["accounts"] = {
            BUSAN: {"account_id": "100", "enabled": False, "meta_credential": "primary"},
            SOKCHO: {"account_id": "101", "enabled": False, "meta_credential": "primary"}}
        self.save()

    def tearDown(self):
        self.temp.cleanup()

    def save(self):
        self.path.write_text(json.dumps(self.settings))

    def cli(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main([*args, "--config", str(self.path)])
        self.assertNotIn(TOKEN, output.getvalue() + errors.getvalue())
        return code, output.getvalue(), errors.getvalue()

    def test_old_settings_keep_original_storage_and_credentials(self):
        config = profiles.select(BASE)
        self.assertEqual(config["account_id"], "10")
        self.assertEqual(config["_local_subdir"], ".local")
        self.assertEqual(config["_cloud_prefix"], "")
        self.assertEqual(config["_meta_token_env"], "SNS_IG_ACCESS_TOKEN")
        self.assertEqual(config["_keychain_account"], "waterbomb_official")

    def test_workflow_uses_one_canonical_workbook_for_every_account(self):
        settings=copy.deepcopy(self.settings)
        settings['workflow']={'enabled':True,'google_sheets':{'spreadsheet_id':'original-sheet','owner_email':'syan@madeone.kr'}}
        settings.pop('google_sheets')
        for account in ('waterbomb_official',BUSAN,SOKCHO):
            self.assertEqual(profiles.select(settings,account)['google_sheets'],settings['workflow']['google_sheets'])
        settings['accounts'][BUSAN]['google_sheets']={'spreadsheet_id':'another-sheet'}
        with self.assertRaisesRegex(PilotError,'기준 파일 하나'):profiles.select(settings,BUSAN)

    def test_extra_accounts_have_separate_storage_and_shared_notion_oauth(self):
        config = profiles.load(self.path, "@" + BUSAN)
        self.assertEqual(config["_local_subdir"], ".local/accounts/" + BUSAN)
        self.assertEqual(config["_cloud_prefix"], "accounts/" + BUSAN)
        self.assertEqual(config["notion"], BASE["notion"])
        self.assertNotIn("post_url", config)
        self.assertNotIn("google_sheets", config)
        self.assertEqual(profiles.load(self.path)["account_id"], "10")

    def test_explicit_shared_credential_uses_original_keychain_for_all_three(self):
        configs = [profiles.load(self.path, name) for name in ("waterbomb_official", BUSAN, SOKCHO)]
        self.assertEqual({c["_meta_token_env"] for c in configs}, {"SNS_IG_ACCESS_TOKEN"})
        self.assertEqual({c["_keychain_account"] for c in configs}, {"waterbomb_official"})

    def test_independent_account_never_falls_back_to_original_token(self):
        self.settings["accounts"][BUSAN].pop("meta_credential")
        config = profiles.select(self.settings, BUSAN)
        with mock.patch.dict(os.environ, {"SNS_IG_ACCESS_TOKEN": TOKEN}, clear=True), \
                mock.patch.object(credentials.sys, "platform", "linux"):
            self.assertIsNone(profiles.environment_token(config))
            with self.assertRaises(PilotError):
                credentials.get_meta_token(config)

    def test_independent_keychain_is_account_specific(self):
        self.settings["accounts"][BUSAN].pop("meta_credential")
        config = profiles.select(self.settings, BUSAN)
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(credentials.sys, "platform", "darwin"), \
                mock.patch.object(credentials, "read_local", return_value=TOKEN) as read:
            self.assertEqual(credentials.get_meta_token(config), TOKEN)
            self.assertEqual(read.call_args.kwargs["keychain_account"], BUSAN)

    def test_add_is_disabled_and_does_not_call_network(self):
        with mock.patch("sns_comment.api.GraphClient") as graph:
            code, output, _ = self.cli("accounts", "add", "--username", "@another_official")
            self.assertEqual(code, 0)
            graph.assert_not_called()
        self.assertFalse(json.loads(output)["enabled"])
        self.assertEqual(profiles.read_settings(self.path)["account_id"], "10")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_duplicate_add_does_not_overwrite_any_settings(self):
        before = self.path.read_bytes()
        code, _, _ = self.cli("accounts", "add", "--username", BUSAN)
        self.assertEqual(code, 1)
        self.assertEqual(self.path.read_bytes(), before)

    def test_pending_account_blocks_collection_and_sheet_authentication(self):
        with mock.patch("sns_comment.__main__.GraphClient") as graph, mock.patch("sns_comment.__main__.sheets_client") as sheets:
            code, _, _ = self.cli("run", "--account", BUSAN, "--sync-sheets")
            self.assertEqual(code, 1)
            graph.assert_not_called()
            sheets.assert_not_called()

    def test_wrong_account_verification_keeps_disabled_settings(self):
        before = self.path.read_bytes()
        graph = mock.Mock()
        graph.get.return_value = {"id": "100", "username": SOKCHO}
        with mock.patch("sns_comment.credentials.interactive_meta_token", return_value=TOKEN), \
                mock.patch("sns_comment.api.GraphClient", return_value=graph):
            self.assertEqual(self.cli("accounts", "verify", "--account", BUSAN)[0], 1)
        self.assertEqual(self.path.read_bytes(), before)

    def test_verified_account_is_enabled_after_real_account_check(self):
        graph = mock.Mock()
        graph.get.return_value = {"id": "100", "username": BUSAN}
        with mock.patch("sns_comment.credentials.interactive_meta_token", return_value=TOKEN), \
                mock.patch("sns_comment.api.GraphClient", return_value=graph):
            self.assertEqual(self.cli("accounts", "verify", "--account", BUSAN)[0], 0)
        profile = profiles.read_settings(self.path)["accounts"][BUSAN]
        self.assertTrue(profile["enabled"])
        self.assertIn("verified_at", profile)
        graph.get.assert_called_once_with("100", fields="id,username")

    def test_shared_connection_failure_never_replaces_token_or_enables_accounts(self):
        before = self.path.read_bytes()
        with mock.patch("sns_comment.credentials.prompt_meta_token", return_value=TOKEN), \
                mock.patch("sns_comment.api.GraphClient"), \
                mock.patch("sns_comment.collector.Collector.account", side_effect=["10", PilotError("wrong account")]), \
                mock.patch("sns_comment.notion_oauth.save_local") as save:
            self.assertEqual(self.cli("accounts", "connect", "--credential", "primary")[0], 1)
            save.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)

    def test_finder_keeps_explicit_grouped_authorization_instead_of_forcing_separate_tokens(self):
        with mock.patch.object(connect_accounts, "cli", return_value=0) as cli, \
                mock.patch("sys.stdin.isatty", return_value=False), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(connect_accounts.main(["--config", str(self.path)]), 0)
        cli.assert_called_once_with(["accounts", "connect", "--credential", "primary", "--config", str(self.path)])

    def test_grouped_token_diagnosis_distinguishes_valid_identity_from_inaccessible_assets(self):
        before = self.path.read_bytes()
        graph = mock.Mock()
        def get(path, **params):
            if path == "me":
                return {"id": "123"}
            if path == "me/permissions":
                return {"data": [{"permission": "instagram_basic", "status": "granted"},
                                 {"permission": "instagram_manage_comments", "status": "granted"}]}
            if path in {"me/assigned_instagram_accounts", "me/accounts"}:
                return {"data": []}
            raise GraphError(400, 100, 33)
        graph.get.side_effect = get
        with mock.patch("sns_comment.credentials.prompt_meta_token", return_value=TOKEN), \
                mock.patch("sns_comment.api.GraphClient", return_value=graph), \
                mock.patch("sns_comment.notion_oauth.save_local") as save:
            code, output, _ = self.cli("accounts", "diagnose", "--credential", "primary")
        self.assertEqual(code, 1)
        report = json.loads(output)
        self.assertEqual(report["checks"]["principal"]["status"], "readable")
        self.assertEqual(report["checks"]["permissions"]["permissions"][1]["permission"], "instagram_manage_comments")
        self.assertEqual(report["accounts"][0]["subcode"], 33)
        self.assertFalse(report["settings_updated"])
        save.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)

    def test_grouped_diagnosis_discovers_three_selected_accounts_without_replacing_token(self):
        for profile in self.settings["accounts"].values():
            profile["account_id"] = None
        self.save()
        before = self.path.read_bytes()
        graph = mock.Mock()
        names = {"10": "waterbomb_official", "100": BUSAN, "101": SOKCHO}
        def get(path, **params):
            if path == "me":
                return {"id": "123"}
            if path == "me/permissions":
                return {"data": []}
            if path == "me/assigned_instagram_accounts":
                return {"data": [{"id": identifier, "username": name} for identifier, name in names.items()]}
            if path == "me/accounts":
                raise GraphError(400, 100)
            return {"id": path, "username": names[path]}
        graph.get.side_effect = get
        with mock.patch("sns_comment.credentials.prompt_meta_token", return_value=TOKEN), \
                mock.patch("sns_comment.api.GraphClient", return_value=graph), \
                mock.patch("sns_comment.notion_oauth.save_local") as save:
            code, output, _ = self.cli("accounts", "diagnose", "--credential", "primary")
        self.assertEqual(code, 0)
        report = json.loads(output)
        self.assertEqual(report["status"], "account_access_verified")
        self.assertEqual(len(report["accounts"]), 3)
        save.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertNotIn(TOKEN, (self.root / ".local/account-connection.json").read_text())

    def test_unsupported_account_error_names_account_stage_and_preserves_token(self):
        before = self.path.read_bytes()
        with mock.patch("sns_comment.credentials.prompt_meta_token", return_value=TOKEN), \
                mock.patch("sns_comment.api.GraphClient"), \
                mock.patch("sns_comment.collector.Collector.account", side_effect=GraphError(400, 100, 33)), \
                mock.patch("sns_comment.notion_oauth.save_local") as save:
            code, output, error = self.cli("accounts", "connect", "--credential", "primary")
        self.assertEqual(code, 1)
        self.assertIn("@waterbomb_official", error)
        self.assertIn("계정 ID 확인", error)
        self.assertIn("code=100, subcode=33", error)
        save.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)
        path = self.root / ".local/account-connection.json"
        result = json.loads(path.read_text())
        self.assertEqual(result["failed_account"], "waterbomb_official")
        self.assertEqual(result["code"], 100)
        self.assertFalse(result["token_replaced"])
        self.assertNotIn(TOKEN, path.read_text() + output + error)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_later_account_failure_reports_earlier_verification_without_enabling(self):
        before = self.path.read_bytes()
        with mock.patch("sns_comment.credentials.prompt_meta_token", return_value=TOKEN), \
                mock.patch("sns_comment.api.GraphClient"), \
                mock.patch("sns_comment.collector.Collector.account", side_effect=["10", "100", GraphError(400, 100, 33)]), \
                mock.patch("sns_comment.notion_oauth.save_local") as save:
            code, _, error = self.cli("accounts", "connect", "--credential", "primary")
        self.assertEqual(code, 1)
        self.assertIn("@" + SOKCHO, error)
        report = json.loads((self.root / ".local/account-connection.json").read_text())
        self.assertEqual([row["username"] for row in report["verified_accounts"]], ["waterbomb_official", BUSAN])
        save.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)

    def test_shared_connection_validates_every_account_before_saving_token(self):
        events = []
        def verify(identifier):
            events.append(identifier)
            return identifier
        def save(token, **options):
            self.assertEqual(token, TOKEN)
            self.assertEqual(events, ["10", "100", "101"])
            self.assertEqual(options["keychain_account"], "waterbomb_official")
        with mock.patch("sns_comment.credentials.prompt_meta_token", return_value=TOKEN), \
                mock.patch("sns_comment.api.GraphClient"), \
                mock.patch("sns_comment.collector.Collector.account", side_effect=verify), \
                mock.patch("sns_comment.notion_oauth.save_local", side_effect=save), \
                mock.patch("sns_comment.notion_oauth.read_local", return_value=TOKEN):
            code, output, _ = self.cli("accounts", "connect", "--credential", "primary")
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(output)["accounts"]), 3)
        self.assertTrue(all(p["enabled"] for p in profiles.read_settings(self.path)["accounts"].values()))
        self.assertNotIn(TOKEN, self.path.read_text())

    def test_system_user_without_account_listing_can_verify_explicit_numeric_ids(self):
        for profile in self.settings["accounts"].values():
            profile["account_id"] = None
        self.save()
        graph = mock.Mock()
        def get(path, **params):
            if path == "me/assigned_instagram_accounts":
                return {"data": []}
            return {"id": path, "username": {"10": "waterbomb_official", "100": BUSAN, "101": SOKCHO}[path]}
        graph.get.side_effect = get
        with mock.patch("sns_comment.credentials.prompt_meta_token", return_value=TOKEN), \
                mock.patch("sns_comment.api.GraphClient", return_value=graph), \
                mock.patch("sys.stdin.isatty", return_value=True), mock.patch("builtins.input", side_effect=["100", "101"]), \
                mock.patch("sns_comment.notion_oauth.save_local"), \
                mock.patch("sns_comment.notion_oauth.read_local", return_value=TOKEN):
            self.assertEqual(self.cli("accounts", "connect", "--credential", "primary")[0], 0)
        self.assertEqual(profiles.load(self.path, SOKCHO)["account_id"], "101")

    def test_expired_token_during_discovery_does_not_prompt_for_account_id_or_save(self):
        self.settings["accounts"][BUSAN]["account_id"] = None
        self.save()
        before = self.path.read_bytes()
        with mock.patch("sns_comment.credentials.prompt_meta_token", return_value=TOKEN), \
                mock.patch("sns_comment.api.GraphClient"), \
                mock.patch("sns_comment.collector.Collector.account", side_effect=["10", GraphError(400, 190)]), \
                mock.patch("builtins.input") as prompt, mock.patch("sns_comment.notion_oauth.save_local") as save:
            self.assertEqual(self.cli("accounts", "connect", "--credential", "primary")[0], 1)
            prompt.assert_not_called()
            save.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)

    def test_wrong_database_is_rejected_before_network_or_writes(self):
        self.settings["accounts"][BUSAN]["enabled"] = True
        self.save()
        path = self.root / "original.sqlite3"
        store = Store(path)
        run = store.start_run("https://www.instagram.com/waterbomb_official/")
        store.stage(run, "catalog", account_id="10")
        store.close()
        before = path.read_bytes()
        with mock.patch("sns_comment.__main__.GraphClient") as graph:
            self.assertEqual(self.cli("run", "--account", BUSAN, "--db", str(path))[0], 1)
            graph.assert_not_called()
        self.assertEqual(path.read_bytes(), before)

    def test_keychain_readback_failure_does_not_enable_new_accounts(self):
        before = self.path.read_bytes()
        with mock.patch("sns_comment.credentials.prompt_meta_token", return_value=TOKEN), \
                mock.patch("sns_comment.api.GraphClient"), \
                mock.patch("sns_comment.collector.Collector.account", side_effect=lambda identifier: identifier), \
                mock.patch("sns_comment.notion_oauth.save_local"), \
                mock.patch("sns_comment.notion_oauth.read_local", return_value="incomplete"):
            self.assertEqual(self.cli("accounts", "connect", "--credential", "primary")[0], 1)
        self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_duplicate_ids_and_embedded_tokens_are_rejected(self):
        for patch in ({"account_id": "10"}, {"token": TOKEN}, {"enabled": "yes"}, {"meta_credential": "../escape"}):
            settings = copy.deepcopy(self.settings)
            settings["accounts"][BUSAN].update(patch)
            with self.subTest(patch=list(patch)), self.assertRaises(PilotError):
                profiles.validate(settings)

    def test_unknown_account_and_path_names_are_rejected(self):
        for name in ("missing_official", "../escape", "user/name", "@", "user..name"):
            with self.subTest(name=name), self.assertRaises(PilotError):
                profiles.select(self.settings, name)

    def test_live_cli_uses_selected_database_token_and_resume_account(self):
        self.settings["accounts"][BUSAN]["enabled"] = True
        self.save()
        graph = DemoGraph()
        graph.responses[("100", None)]["username"] = BUSAN
        with mock.patch("sns_comment.__main__.ROOT", self.root), \
                mock.patch("sns_comment.__main__.GraphClient", return_value=graph), \
                mock.patch.dict(os.environ, {"SNS_IG_ACCESS_TOKEN": TOKEN, "SNS_IG_ACCOUNT_ID": "10"}, clear=True):
            code, output, error = self.cli("run", "--account", BUSAN)
        self.assertEqual(code, 0, error)
        result = json.loads(output)
        self.assertEqual(result["account_id"], "100")
        self.assertIn("--account " + BUSAN, result["resume_command"])
        self.assertIn("--config ", result["resume_command"])
        self.assertTrue((self.root / ".local/accounts" / BUSAN / "comments.sqlite3").is_file())
        self.assertFalse((self.root / ".local/comments.sqlite3").exists())

    def test_cloud_state_namespaces_reads_uploads_and_compare_guards(self):
        transport = mock.Mock()
        transport.request.return_value = b'{"generation":"9"}'
        state = CloudState("private", transport, prefix="accounts/" + BUSAN)
        self.assertEqual(state.put("state/reports.json", b"{}", generation=4), 9)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(transport.request.call_args.args[1]).query)
        self.assertEqual(query["name"], ["accounts/" + BUSAN + "/state/reports.json"])
        self.assertEqual(query["ifGenerationMatch"], ["4"])
        self.assertIn(urllib.parse.quote("accounts/" + BUSAN + "/state/comments.sqlite3", safe=""), state.url("state/comments.sqlite3"))
        self.assertIn("/o/state%2Freports.json", CloudState("private", transport).url("state/reports.json"))

    def test_cloud_new_account_keeps_global_oauth_lease_and_profile_state(self):
        self.settings["accounts"][BUSAN]["enabled"] = True
        self.save()
        states = [mock.MagicMock(), mock.MagicMock()]
        with mock.patch.dict(os.environ, {"GOOGLE_CLOUD_PROJECT": "synthetic", "SNS_STATE_BUCKET": "private",
                                         "SNS_NOTION_OAUTH_SECRET": "existing-oauth"}, clear=True), \
                mock.patch.object(cloud, "CloudState", side_effect=states) as factory, \
                mock.patch.object(cloud, "cloud_credentials") as oauth, \
                mock.patch.object(cloud, "McpClient"), mock.patch.object(cloud, "NotionMcpClient") as notion, \
                mock.patch.object(cloud, "Analyzer"), mock.patch.object(cloud, "execute", return_value={"status":"complete"}) as execute, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cloud.main(["--config", str(self.path), "--account", BUSAN]), 0)
        states[0].lease.assert_called_once()
        states[1].lease.assert_not_called()
        self.assertEqual(factory.call_args.kwargs["prefix"], "accounts/" + BUSAN)
        oauth.assert_called_once_with(states[0].transport, "existing-oauth")
        self.assertEqual(notion.call_args.kwargs["username"], BUSAN)
        self.assertIs(execute.call_args.args[1], states[1])

    def test_scheduled_accounts_are_explicit_unique_and_enabled(self):
        self.settings["cloud"]={"scheduled_accounts":["waterbomb_official",BUSAN,SOKCHO]}
        for profile in self.settings["accounts"].values():
            profile["enabled"]=True
        self.assertEqual([c["expected_username"] for c in cloud.scheduled_configs(self.settings)],
                         ["waterbomb_official",BUSAN,SOKCHO])
        for names in ([],[BUSAN,BUSAN],["missing"]):
            broken=copy.deepcopy(self.settings);broken["cloud"]["scheduled_accounts"]=names
            with self.subTest(names=names),self.assertRaises(PilotError):cloud.scheduled_configs(broken)
        self.settings["accounts"][BUSAN]["enabled"]=False
        with self.assertRaises(PilotError):cloud.scheduled_configs(self.settings)

    def test_scheduled_pipeline_isolates_accounts_and_continues_after_one_failure(self):
        self.settings["cloud"]={"scheduled_accounts":["waterbomb_official",BUSAN,SOKCHO]}
        for profile in self.settings["accounts"].values():profile["enabled"]=True
        self.save()
        environment={"GOOGLE_CLOUD_PROJECT":"synthetic","SNS_STATE_BUCKET":"private",
                     "SNS_NOTION_OAUTH_SECRET":"existing-oauth"}
        for fail in (False,True):
            with self.subTest(fail=fail):
                states=[mock.MagicMock() for _ in range(3)]
                states[0].json_get.return_value=(None,0)
                seen=[]
                def execute(config,state,notion,analyzer,**kwargs):
                    seen.append((config["expected_username"],state,kwargs["instant"]))
                    if fail and config["expected_username"]=="waterbomb_official":raise PilotError("synthetic report error")
                    return {"status":"complete","report_status":"published"}
                with mock.patch.dict(os.environ,environment,clear=True), \
                        mock.patch.object(cloud,"CloudState",side_effect=states), \
                        mock.patch.object(cloud,"cloud_credentials") as oauth, \
                        mock.patch.object(cloud,"McpClient") as mcp, \
                        mock.patch.object(cloud,"NotionMcpClient"),mock.patch.object(cloud,"Analyzer"), \
                        mock.patch.object(cloud,"execute",side_effect=execute),contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cloud.main(["--config",str(self.path),"--all-accounts"]),int(fail))
                self.assertEqual([name for name,_,_ in seen],["waterbomb_official",BUSAN,SOKCHO])
                self.assertEqual([state for _,state,_ in seen],states)
                self.assertEqual(len({instant for _,_,instant in seen}),1)
                oauth.return_value.refresh.assert_called_once();mcp.assert_called_once()
                states[0].lease.assert_called_once()
                for state in states[1:]:state.lease.assert_not_called()
                summary=states[0].json_put.call_args.args[1]
                self.assertEqual([r["status"] for r in summary["accounts"]],["failed" if fail else "complete","complete","complete"])

    def test_launcher_writes_account_scoped_metadata(self):
        with mock.patch.object(launcher, "ROOT", self.root), mock.patch.object(launcher, "collect_main", return_value=0), \
                mock.patch("sys.stdin.isatty", return_value=False), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(launcher.main(["--account", BUSAN, "--config", str(self.path)]), 0)
        self.assertTrue((self.root / ".local/accounts" / BUSAN / "last-launch.json").exists())
        self.assertFalse((self.root / ".local/last-launch.json").exists())


if __name__ == "__main__":
    unittest.main()
