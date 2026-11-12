"""Finder entry point: own the whole session after replacing the shell.

The shell must not read a changed script after a long collector invocation.
Only safe launch metadata is persisted; credentials remain in collector memory.
"""

import json
import os
from pathlib import Path
import sys
import uuid

from .__main__ import main as collect_main
from .storage import now
from . import profiles


ROOT = Path(__file__).resolve().parent.parent


def write_launch(record, directory=None):
    directory = directory or ROOT / ".local"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = directory / "last-launch.json"
    temporary = directory / (".launch-" + record["id"] + ".tmp")
    try:
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv=None):
    os.umask(0o077)
    args = list(sys.argv[1:] if argv is None else argv)
    record = {"id": uuid.uuid4().hex, "started_at": now(), "ended_at": None,
              "status": "running", "exit_code": None, "credential_storage": "environment_or_account_keychain_or_hidden_input"}
    code = 1
    directory = None
    try:
        import argparse
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--account")
        parser.add_argument("--config", type=Path)
        selected, _ = parser.parse_known_args(args)
        if selected.account or selected.config:
            config = profiles.load(selected.config or ROOT / "config.json", selected.account)
            directory = profiles.local_directory(config, ROOT)
            record["account"] = config["_account"]
        write_launch(record, directory)
        print("SNS 댓글 수집 — 최근 7일 게시물·댓글 · 이전 게시물 활동은 별도 추적", flush=True)
        code = collect_main(["run", "--until-complete", *args])
    except KeyboardInterrupt:
        code = 130
        print("수집을 중단했습니다. 저장된 원본과 진행 위치는 유지됩니다.", file=sys.stderr)
    except SystemExit as error:
        code = error.code if isinstance(error.code, int) else 1
    except Exception:
        print("실행 처리에 실패했습니다. 저장된 수집 상태를 확인해주세요.", file=sys.stderr)
    finally:
        record.update(ended_at=now(), status="complete" if code == 0 else "incomplete", exit_code=code)
        try:
            write_launch(record, directory)
        except OSError:
            print("실행 상태 파일을 저장하지 못했습니다. 수집 DB는 별도로 확인해주세요.", file=sys.stderr)
    if "--help" not in args and "-h" not in args:
        print("수집 완료입니다." if code == 0 else "수집 미완료입니다. 위 결과의 중단 사유와 재개 명령을 확인해주세요.", flush=True)
    if sys.stdin.isatty():
        try:
            input("결과 확인 후 Enter를 누르면 이 실행을 종료합니다. ")
        except (EOFError, KeyboardInterrupt):
            pass
    return code


if __name__ == "__main__":
    sys.exit(main())
