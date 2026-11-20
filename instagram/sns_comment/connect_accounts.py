"""Interactive shared-account connection entry point for Finder."""

import sys

from .__main__ import main as cli
from .profiles import ROOT, read_settings
from .api import PilotError
from pathlib import Path
import argparse


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description="설정된 Instagram 계정 연결")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--diagnose", action="store_true", help="묶음 토큰 접근만 진단. 인증·설정 저장 없음")
    parsed = parser.parse_args(args)
    code = 1
    try:
        settings = read_settings(parsed.config)
        shared = any(profile.get("meta_credential") == "primary" for profile in settings.get("accounts", {}).values())
        if parsed.diagnose:
            code = cli(["accounts", "diagnose", "--credential", "primary", "--config", str(parsed.config)])
            names = []
        elif shared:
            # Explicit grouped authorization takes precedence. A screen label
            # must never force the user's shared token into per-account login.
            code = cli(["accounts", "connect", "--credential", "primary", "--config", str(parsed.config)])
            names = []
        else:
            names = [name for name, profile in settings.get("accounts", {}).items()
                     if profile.get("meta_auth") == "instagram_login"]
        if not names and not shared and not parsed.diagnose:
            raise PilotError("Instagram 로그인 방식의 추가 계정이 없습니다.")
        for name in names:
            code = cli(["accounts", "connect", "--account", name, "--config", str(parsed.config)])
            if code:
                break
    except PilotError as error:
        print(str(error), file=sys.stderr)
    label = "묶음 토큰 접근 진단" if parsed.diagnose else "계정 연결 검증"
    print(label + (" 완료." if code == 0 else " 확인 필요. 위 안내를 확인해주세요."), flush=True)
    if sys.stdin.isatty():
        try:
            input("확인 후 Enter를 누르면 종료합니다. ")
        except (EOFError, KeyboardInterrupt):
            pass
    return code


if __name__ == "__main__":
    sys.exit(main())
