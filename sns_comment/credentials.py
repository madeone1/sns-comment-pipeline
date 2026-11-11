"""Meta credential supply for unattended runs; no token in files or argv."""

import argparse
import getpass
import json
import os
from pathlib import Path
import subprocess
import sys
import warnings

from .api import GraphClient, PilotError
from .notion_oauth import read_local,save_local
from .profiles import environment_token, load
from .collector import Collector


SERVICE = "kr.madeone.sns-comment.meta.v2"
ACCOUNT = "waterbomb_official"


def get_token(kind="meta", config=None):
    if kind not in {"meta", "notion"}:
        raise PilotError("지원하지 않는 인증 종류입니다.")
    service = SERVICE if kind == "meta" else "kr.madeone.sns-comment.notion"
    label = "Meta" if kind == "meta" else "Notion"
    config = config or {}
    account = config.get("_keychain_account", ACCOUNT) if kind == "meta" else ACCOUNT
    token = environment_token(config) if kind == "meta" else os.environ.get("SNS_NOTION_TOKEN")
    if not token:
        if sys.platform != "darwin":
            raise PilotError("SNS_IG_ACCESS_TOKEN을 자동 실행 환경의 비밀 저장소에 등록해야 합니다.")
        try:
            token=read_local(keychain_service=service,keychain_account=account,json_value=False).strip()
        except (OSError,PilotError):
            raise PilotError("Meta 토큰 키체인을 읽지 못했습니다. 로그인·접근 허용 상태를 확인해주세요.") from None
    if not token or any(c.isspace() for c in token):
        raise PilotError("등록된 Meta 토큰 형식을 확인해주세요.")
    return token


def get_meta_token(config=None):
    return get_token("meta", config)


def prompt_meta_token(label):
    if not sys.stdin.isatty():
        raise PilotError("Meta 토큰 등록은 로컬 터미널의 숨김 입력을 사용해주세요.")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            value = getpass.getpass(f"{label} Meta 토큰(숨김 입력): ").strip()
        except (getpass.GetPassWarning, EOFError):
            raise PilotError("숨김 입력이 가능한 터미널에서 실행해주세요.") from None
    if not value or any(c.isspace() for c in value):
        raise PilotError("공백 없는 Meta 토큰을 입력해주세요.")
    return value


def interactive_meta_token(config):
    value = environment_token(config)
    if value:
        return value
    if sys.platform == "darwin":
        try:
            return get_meta_token(config)
        except PilotError:
            pass
    if sys.stdin.isatty():
        return prompt_meta_token("@" + config["expected_username"])
    return get_meta_token(config)


def verify_meta_token(token, config=None):
    config = config or load()
    client = GraphClient(token, config["api_version"], max_requests=2,
                         api_host=config.get("_meta_api_host", "graph.facebook.com"))
    Collector(client, None, config["expected_username"]).account(config.get("account_id"))
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description="자동 수집용 Meta 토큰의 macOS 키체인 등록")
    parser.add_argument("action", choices=["setup", "check"])
    parser.add_argument("--kind", choices=["meta", "notion"], default="meta")
    parser.add_argument("--account", help="Meta 인증을 등록할 @아이디")
    parser.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.kind == "notion" and (args.account or args.config):
            raise PilotError("Notion은 기존 MCP OAuth 연결을 사용합니다. 계정별 토큰을 등록하지 않습니다.")
        config = load(args.config, args.account) if args.account or args.config else None
        account = config["_keychain_account"] if config else ACCOUNT
        if args.action == "check":
            get_token(args.kind, config)
            print(json.dumps({"credential_available": True, "token_displayed": False}))
            return 0
        if sys.platform != "darwin" or not sys.stdin.isatty():
            raise PilotError("setup-automation.command를 터미널에서 직접 열어주세요.")
        label = "Meta" if args.kind == "meta" else "Notion"
        service = SERVICE if args.kind == "meta" else "kr.madeone.sns-comment.notion"
        print(f"클라우드 초기 연결에 사용할 {label} 토큰을 macOS 키체인에 저장합니다.", flush=True)
        token=getpass.getpass(f"{label} 액세스 토큰(화면에 표시하지 않음): ").strip()
        if not token or any(c.isspace() for c in token):
            raise PilotError("공백 없는 토큰 값을 붙여넣어주세요.")
        if args.kind=="meta":
            verify_meta_token(token, config) if config else verify_meta_token(token)
        # Python getpass has no native getpass(3) 128-character cap. Write the
        # complete bytes through Security.framework, never command arguments.
        save_local(token,keychain_service=service,keychain_account=account,json_value=False)
        if get_token(args.kind, config)!=token:
            raise PilotError("입력값과 저장값이 일치하지 않아 등록 완료로 표시하지 않습니다.")
        print("계정 조회 검증과 전체 토큰 저장을 완료했습니다. 이제 창을 닫아도 됩니다.")
        return 0
    except PilotError as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
