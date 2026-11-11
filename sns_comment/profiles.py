"""Account settings, with isolated defaults and compatibility for the first account."""

import copy
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile

from .api import PilotError
from .storage import now, run_lock


ROOT = Path(__file__).resolve().parent.parent


def username(value):
    if not isinstance(value, str):
        raise PilotError("Instagram @아이디를 지정해주세요.")
    value = value.removeprefix("@").lower()
    if not re.fullmatch(r"[a-z0-9_][a-z0-9_.]{0,29}", value) or value.endswith(".") or ".." in value:
        raise PilotError("Instagram @아이디 형식을 확인해주세요.")
    return value


def token_env(name):
    return "SNS_IG_ACCESS_TOKEN_" + username(name).replace(".", "_").upper()


def credential_key(profile, name, primary=None):
    key = profile.get("meta_credential")
    if key is None:
        return token_env(name), name, "account:" + name
    if key == "primary":
        return "SNS_IG_ACCESS_TOKEN", primary, "legacy"
    if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", key):
        raise PilotError("공유 Meta 인증 이름은 영문 소문자·숫자·밑줄로 지정해주세요.")
    return "SNS_IG_ACCESS_TOKEN_" + key.upper(), "connection:" + key, "connection:" + key


def read_settings(path=None):
    try:
        value = json.loads(Path(path or ROOT / "config.json").read_text())
        validate(value)
        return value
    except OSError:
        raise PilotError("계정 설정 파일을 읽지 못했습니다. 경로와 실행 사용자의 읽기 권한을 확인해주세요.") from None
    except (ValueError, TypeError, KeyError):
        raise PilotError("계정 설정 파일의 형식을 확인해주세요.") from None


def validate(settings):
    if not isinstance(settings, dict):
        raise PilotError("계정 설정은 JSON 객체여야 합니다.")
    primary = username(settings.get("expected_username"))
    accounts = settings.get("accounts", {})
    if not isinstance(accounts, dict):
        raise PilotError("accounts 설정은 계정별 JSON 객체여야 합니다.")
    canonical = settings.get('workflow', {}).get('google_sheets', {})
    if settings.get('workflow', {}).get('enabled') and canonical.get('spreadsheet_id'):
        for target in [settings.get('google_sheets', {}), *[p.get('google_sheets', {}) for p in accounts.values() if isinstance(p, dict)]]:
            if target.get('spreadsheet_id') and target['spreadsheet_id'] != canonical['spreadsheet_id']:
                raise PilotError('댓글 시트는 workflow.google_sheets의 기준 파일 하나만 사용할 수 있습니다.')
    ids, names, environments = set(), {primary}, {"SNS_IG_ACCESS_TOKEN": "legacy"}
    primary_id = settings.get("account_id")
    if primary_id is not None:
        if not isinstance(primary_id, str) or not primary_id.isdigit():
            raise PilotError("Instagram 계정 ID는 숫자 문자열이어야 합니다.")
        ids.add(primary_id)
    for key, profile in accounts.items():
        name = username(key)
        if name != key or name in names or not isinstance(profile, dict):
            raise PilotError("계정 설정 키가 중복되었거나 형식이 올바르지 않습니다.")
        # Keep profile overrides deliberately narrow: no embedded credentials,
        # arbitrary paths, or authentication-mode overrides.
        if set(profile) - {"account_id", "enabled", "post_url", "collection", "google_sheets", "verified_at", "meta_credential", "meta_auth"}:
            raise PilotError("추가 계정에는 ID·활성 상태·수집·시트 설정만 저장할 수 있습니다.")
        if not isinstance(profile.get("enabled", False), bool):
            raise PilotError("계정 활성 상태는 true 또는 false여야 합니다.")
        if profile.get("meta_auth", "facebook_login") not in {"facebook_login", "instagram_login"}:
            raise PilotError("계정 인증 방식은 facebook_login 또는 instagram_login이어야 합니다.")
        if profile.get("meta_auth") == "instagram_login" and profile.get("meta_credential") is not None:
            raise PilotError("Instagram 로그인 토큰은 계정별로 등록하며 공유 인증으로 묶지 않습니다.")
        identifier = profile.get("account_id")
        if identifier is not None:
            if not isinstance(identifier, str) or not identifier.isdigit() or identifier in ids:
                raise PilotError("계정 ID 형식 또는 중복을 확인해주세요.")
            ids.add(identifier)
        if profile.get("enabled") and identifier is None:
            raise PilotError("활성 계정에는 검증된 Instagram 계정 ID가 필요합니다.")
        env, _, credential = credential_key(profile, name, primary)
        if env in environments and environments[env] != credential:
            raise PilotError("계정별 인증 환경변수 이름이 겹칩니다.")
        names.add(name)
        environments[env] = credential
        for field in ("collection", "google_sheets"):
            if field in profile and not isinstance(profile[field], dict):
                raise PilotError("계정별 수집·시트 설정 형식을 확인해주세요.")
    if username(settings.get("default_account", primary)) not in names:
        raise PilotError("기본 계정이 등록되어 있지 않습니다.")


def select(settings, account=None):
    validate(settings)
    primary = username(settings["expected_username"])
    name = username(account or settings.get("default_account", primary))
    config = copy.deepcopy(settings)
    config.pop("accounts", None)
    config.pop("default_account", None)
    if name == primary:
        config.update(enabled=True, _local_subdir=".local", _cloud_prefix="",
                      _meta_token_env="SNS_IG_ACCESS_TOKEN", _keychain_account=primary)
    else:
        profile = settings.get("accounts", {}).get(name)
        if profile is None:
            raise PilotError("등록되지 않은 계정입니다. accounts list로 확인해주세요.")
        config.pop("post_url", None)
        config.pop("google_sheets", None)
        env, keychain, _ = credential_key(profile, name, primary)
        config.update(account_id=profile.get("account_id"), enabled=profile.get("enabled", False),
                      _local_subdir=f".local/accounts/{name}", _cloud_prefix=f"accounts/{name}",
                      _meta_token_env=env, _keychain_account=keychain)
        config["collection"] = {**config.get("collection", {}), **profile.get("collection", {})}
        for field in ("post_url", "google_sheets", "verified_at"):
            if field in profile:
                config[field] = copy.deepcopy(profile[field])
    config.update(expected_username=name, _account=name)
    if settings.get('workflow', {}).get('enabled') and settings['workflow'].get('google_sheets', {}).get('spreadsheet_id'):
        config['google_sheets'] = copy.deepcopy(settings['workflow']['google_sheets'])
    config["meta_auth"] = settings.get("accounts", {}).get(name, {}).get("meta_auth", "facebook_login")
    config["_meta_api_host"] = "graph.instagram.com" if config["meta_auth"] == "instagram_login" else "graph.facebook.com"
    return config


def load(path=None, account=None):
    return select(read_settings(path), account)


def local_directory(config, root=None):
    return Path(root or ROOT) / config.get("_local_subdir", ".local")


def require_enabled(config):
    if not config.get("enabled", True):
        raise PilotError("이 계정은 아직 연결 검증 전입니다. accounts verify --account로 먼저 확인해주세요.")


def environment_token(config):
    # Reuse requires an explicit shared credential in settings.
    return os.environ.get(config.get("_meta_token_env", "SNS_IG_ACCESS_TOKEN"))


def check_database(config, path):
    """Reject accidental reuse of another account's DB before opening a writer."""
    path = Path(path).resolve()
    identifier = config.get("account_id")
    if not path.exists() or not identifier:
        return
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("media", "comments", "runs", "collection_scopes"):
            if table in tables and connection.execute(
                    f"SELECT 1 FROM {table} WHERE account_id IS NOT NULL AND account_id<>? LIMIT 1", (identifier,)).fetchone():
                raise PilotError("다른 계정의 기록이 있는 DB입니다. 선택 계정의 기본 DB 경로를 사용해주세요.")


def write_settings(path, settings):
    validate(settings)
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".accounts-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(settings, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def connection_failure(name, stage, error):
    """Add context without exposing a remote message or the supplied token."""
    from .api import GraphError
    message = f"@{name} · {stage}: {error}"
    if isinstance(error, GraphError) and (error.code, error.subcode) == (100, 33):
        message += ("\n새 토큰으로 해당 Instagram 계정 객체에 접근하지 못했습니다. "
                    "토큰 발급 방식·자산 할당·Instagram 숫자 ID를 확인해야 합니다. "
                    "토큰 자체의 무효 여부는 이 오류만으로 확정할 수 없습니다.")
    return PilotError(message + "\n기존 토큰과 계정 설정은 교체하지 않았습니다.")


def save_connection_result(config_path, result):
    """Only caller-constructed diagnostic fields; never raw responses or tokens."""
    directory = Path(config_path).parent / ".local"
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix=".connection-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(result, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            os.replace(temporary, directory / "account-connection.json")
        finally:
            Path(temporary).unlink(missing_ok=True)
    except OSError:
        # Failure to write optional diagnostics must not mask the API error.
        pass


def configure_parser(commands):
    parser = commands.add_parser("accounts", help="계정 목록·추가·실제 연결 검증")
    parser.add_argument("action", choices=["list", "add", "verify", "connect", "diagnose"])
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--account", help="조회·검증할 @아이디")
    parser.add_argument("--username", help="추가할 @아이디")
    parser.add_argument("--account-id", help="확인된 숫자 Instagram 계정 ID")
    parser.add_argument("--credential", help="추가 계정들이 함께 사용할 Meta 인증 이름")
    parser.add_argument("--meta-auth", choices=["facebook_login", "instagram_login"], help="추가 계정의 Meta 인증 방식")


def manage(args):
    if args.action == "diagnose":
        return diagnose_shared(args)
    if args.action == "connect":
        return connect_instagram(args) if args.account else connect(args)
    if args.action == "list":
        settings = read_settings(args.config)
        primary = username(settings["expected_username"])
        names = [primary, *settings.get("accounts", {})]
        if args.account:
            names = [username(args.account)]
        rows = []
        for name in names:
            config = select(settings, name)
            rows.append({"username": name, "account_id": config.get("account_id"), "enabled": config["enabled"],
                         "meta_token_env": config["_meta_token_env"], "local_directory": config["_local_subdir"],
                         "cloud_prefix": config["_cloud_prefix"], "notion_auth": config.get("notion", {}).get("auth_mode", "mcp_oauth")})
            rows[-1].update(meta_auth=config["meta_auth"], api_host=config["_meta_api_host"])
        return {"default_account": settings.get("default_account", primary), "accounts": rows}
    with run_lock(args.config):
        settings = read_settings(args.config)
        if args.action == "add":
            if args.account or not args.username:
                raise PilotError("accounts add에는 --username을 지정해주세요.")
            name = username(args.username)
            if name == username(settings["expected_username"]) or name in settings.get("accounts", {}):
                raise PilotError("이미 등록된 계정입니다. 기존 설정을 덮어쓰지 않습니다.")
            settings.setdefault("accounts", {})[name] = {"account_id": args.account_id, "enabled": False}
            if args.credential:
                settings["accounts"][name]["meta_credential"] = args.credential
            if args.meta_auth:
                settings["accounts"][name]["meta_auth"] = args.meta_auth
            write_settings(args.config, settings)
            return {"status": "added_pending_verification", "username": name, "enabled": False,
                    "meta_token_env": select(settings, name)["_meta_token_env"], "live_instagram_called": False}
        if not args.account or args.username:
            raise PilotError("accounts verify에는 --account를 지정해주세요.")
        from .collector import Collector
        from .api import GraphClient
        from .credentials import interactive_meta_token
        config = select(settings, args.account)
        identifier = args.account_id or config.get("account_id")
        if config["_cloud_prefix"] == "" and args.account_id and args.account_id != config.get("account_id"):
            raise PilotError("기존 운영 계정의 ID는 연결 검증 명령으로 변경하지 않습니다.")
        client = GraphClient(interactive_meta_token(config), config["api_version"], max_requests=20,
                             api_host=config["_meta_api_host"])
        try:
            identifier = Collector(client, None, config["expected_username"]).account(identifier)
        except PilotError as error:
            raise connection_failure(config["expected_username"], "계정 ID 확인", error) from None
        if config["_cloud_prefix"]:
            settings["accounts"][config["_account"]].update(account_id=identifier, enabled=True, verified_at=now())
            write_settings(args.config, settings)
        return {"status": "account_verified", "username": config["_account"], "account_id": identifier,
                "enabled": True, "live_instagram_called": True, "comments_collected": False}


def connect(args):
    """Verify every member before replacing a shared credential or enabling accounts."""
    from .api import GraphClient, GraphError, RequestBudgetExceeded
    from .collector import Collector
    from .credentials import prompt_meta_token, SERVICE
    from .notion_oauth import save_local, read_local
    import sys

    if args.account or args.username or args.account_id or not args.credential:
        raise PilotError("accounts connect에는 --credential 인증이름만 지정해주세요.")
    with run_lock(args.config):
        settings = read_settings(args.config)
        primary = username(settings["expected_username"])
        names = [name for name, profile in settings.get("accounts", {}).items()
                 if profile.get("meta_credential") == args.credential]
        if args.credential == "primary":
            names.insert(0, primary)
        if not names:
            raise PilotError("해당 인증 연결에 등록된 계정이 없습니다.")
        token = prompt_meta_token(" · ".join("@" + name for name in names))
        client = GraphClient(token, settings["api_version"], max_requests=100)
        verified = []
        for name in names:
            config = select(settings, name)
            identifier = config.get("account_id")
            collector = Collector(client, None, name)
            stage = "계정 자동 탐색" if identifier is None else "계정 ID 확인"
            print(f"연결 확인: @{name} · {stage}", file=sys.stderr, flush=True)
            try:
                if identifier is None:
                    # Some company system-user tokens do not expose the assigned
                    # edge. Ask for an explicit ID, then verify it with a real GET.
                    try:
                        identifier = collector.account()
                    except PilotError as error:
                        if isinstance(error, RequestBudgetExceeded) or (isinstance(error, GraphError) and
                                (error.status in (401, 403, 429) or error.code in (4, 10, 17, 32, 190, 200, 613))):
                            raise
                        if not sys.stdin.isatty():
                            raise PilotError(f"@{name}의 숫자 Instagram 계정 ID를 설정한 뒤 다시 연결해주세요.") from None
                        identifier = input(f"@{name} 자동 탐색 불가. Meta에 표시된 숫자 Instagram 계정 ID: ").strip()
                stage = "계정 ID 확인"
                identifier = collector.account(identifier)
            except PilotError as error:
                result = {"status": "verification_failed", "at": now(), "failed_account": name,
                          "failed_stage": stage, "account_id": identifier if isinstance(identifier, str) and identifier.isdigit() else None,
                          "verified_accounts": verified, "api_host": "graph.facebook.com", "api_version": settings["api_version"],
                          "token_replaced": False, "settings_updated": False}
                if isinstance(error, GraphError):
                    result.update(http_status=error.status, code=error.code, subcode=error.subcode)
                save_connection_result(args.config, result)
                raise connection_failure(name, stage, error) from None
            if name != primary:
                settings["accounts"][name].update(account_id=identifier, enabled=True, verified_at=now())
            verified.append({"username": name, "account_id": identifier})
        validate(settings)
        # Config remains untouched on any account mismatch. Save with the same
        # Keychain API as the existing connection; no tokens in argv or files.
        config = select(settings, names[0])
        options = {"keychain_service": SERVICE, "keychain_account": config["_keychain_account"], "json_value": False}
        save_local(token, **options)
        if read_local(**options) != token:
            raise PilotError("인증 저장값이 일치하지 않아 계정을 활성화하지 않았습니다.")
        write_settings(args.config, settings)
        save_connection_result(args.config, {"status": "connected", "at": now(), "verified_accounts": verified,
                                            "token_replaced": True, "settings_updated": True})
        return {"status": "connected", "accounts": verified, "token_displayed": False,
                "comments_collected": False, "cloud_credentials_updated": False}


def connect_instagram(args):
    """Bind an Instagram Login token only to its verified account and API ID."""
    from .api import GraphClient, GraphError
    from .collector import Collector
    from .credentials import prompt_meta_token, SERVICE
    from .notion_oauth import read_local, save_local
    import sys

    if args.credential or args.username or args.account_id:
        raise PilotError("Instagram 계정 연결에는 --account만 지정해주세요.")
    with run_lock(args.config):
        settings = read_settings(args.config)
        config = select(settings, args.account)
        if config["meta_auth"] != "instagram_login" or not config["_cloud_prefix"]:
            raise PilotError("이 명령은 Instagram 로그인 방식의 추가 계정용입니다. 기존 운영 연결은 유지합니다.")
        name = config["_account"]
        token = prompt_meta_token("@" + name + " 계정 행에서 생성한")
        print(f"연결 확인: @{name} · Instagram 로그인 계정 확인", file=sys.stderr, flush=True)
        try:
            client = GraphClient(token, config["api_version"], max_requests=5, api_host="graph.instagram.com")
            identifier = Collector(client, None, name).account(config.get("account_id"))
        except PilotError as error:
            report = {"status": "verification_failed", "at": now(), "failed_account": name,
                      "failed_stage": "Instagram 로그인 계정 확인", "api_host": "graph.instagram.com",
                      "token_replaced": False, "settings_updated": False}
            if isinstance(error, GraphError):
                report.update(http_status=error.status, code=error.code, subcode=error.subcode)
            save_connection_result(args.config, report)
            raise connection_failure(name, "Instagram 로그인 계정 확인", error) from None
        settings["accounts"][name].update(account_id=identifier, enabled=True, verified_at=now())
        validate(settings)
        options = {"keychain_service": SERVICE, "keychain_account": config["_keychain_account"], "json_value": False}
        save_local(token, **options)
        if read_local(**options) != token:
            raise PilotError("인증 저장값이 일치하지 않아 계정을 활성화하지 않았습니다.")
        write_settings(args.config, settings)
        result = {"status": "connected", "username": name, "account_id": identifier,
                  "meta_auth": "instagram_login", "api_host": "graph.instagram.com", "token_displayed": False,
                  "comments_collected": False, "cloud_credentials_updated": False}
        save_connection_result(args.config, {**result, "at": now(), "token_replaced": True, "settings_updated": True})
        return result


def diagnose_shared(args):
    """Inspect a grouped grant and account lookup paths without saving credentials."""
    from .api import GraphClient, GraphError
    from .collector import Collector
    from .credentials import prompt_meta_token

    if args.account or args.username or args.account_id or args.credential != "primary":
        raise PilotError("묶음 연결 진단에는 --credential primary를 지정해주세요.")
    settings = read_settings(args.config)
    primary = username(settings["expected_username"])
    names = [primary, *[name for name, profile in settings.get("accounts", {}).items()
                       if profile.get("meta_credential") == "primary"]]
    token = prompt_meta_token("세 계정에 묶음 발급한")
    client = GraphClient(token, settings["api_version"], max_requests=30)
    report = {"operation": "diagnose", "at": now(), "status": "needs_attention",
              "api_host": "graph.facebook.com", "checks": {}, "accounts": [],
              "token_replaced": False, "settings_updated": False, "comments_collected": False}
    checks, discovered = report["checks"], {}

    def failed(error):
        result = {"status": "failed"}
        if isinstance(error, GraphError):
            result.update(http_status=error.status, code=error.code, subcode=error.subcode)
        return result

    try:
        principal = client.get("me", fields="id")
        if not isinstance(principal.get("id"), str) or not principal["id"].isdigit():
            raise PilotError("토큰 주체 응답을 확인하지 못했습니다.")
        checks["principal"] = {"status": "readable"}
    except PilotError as error:
        checks["principal"] = failed(error)
        save_connection_result(args.config, report)
        return report
    try:
        permissions = []
        for items, _ in Collector(client, None).pages("me/permissions", "permission,status", max_pages=4):
            for item in items:
                permission, status = item.get("permission"), item.get("status")
                if isinstance(permission, str) and re.fullmatch(r"[a-z_]+", permission) and status in {"granted", "declined", "expired"}:
                    permissions.append({"permission": permission, "status": status})
        checks["permissions"] = {"status": "readable", "permissions": permissions}
    except PilotError as error:
        checks["permissions"] = failed(error)
    for label, path, fields in (
            ("assigned_instagram_accounts", "me/assigned_instagram_accounts", "id,username"),
            ("page_instagram_accounts", "me/accounts", "id,instagram_business_account{id,username}")):
        try:
            found = []
            for items, _ in Collector(client, None).pages(path, fields, max_pages=4):
                for item in items:
                    account = item if path.endswith("assigned_instagram_accounts") else item.get("instagram_business_account")
                    if not isinstance(account, dict):
                        continue
                    identifier, name = account.get("id"), account.get("username")
                    if isinstance(identifier, str) and identifier.isdigit() and isinstance(name, str) and name.casefold() in names:
                        found.append({"username": name.casefold(), "account_id": identifier})
                        discovered.setdefault(name.casefold(), set()).add(identifier)
            checks[label] = {"status": "readable", "target_accounts": found}
        except PilotError as error:
            checks[label] = failed(error)
    for name in names:
        config = select(settings, name)
        candidates = discovered.get(name, set())
        identifier = next(iter(candidates)) if len(candidates) == 1 else config.get("account_id") if not candidates else None
        row = {"username": name, "account_id": identifier}
        if identifier is None:
            row["status"] = "account_id_unresolved"
        else:
            try:
                Collector(client, None, name).account(identifier)
                row["status"] = "readable"
                row["matches_configured_id"] = config.get("account_id") in (None, identifier)
            except PilotError as error:
                row.update(failed(error))
        report["accounts"].append(row)
    if all(row["status"] == "readable" for row in report["accounts"]):
        report["status"] = "account_access_verified"
    save_connection_result(args.config, report)
    return report
