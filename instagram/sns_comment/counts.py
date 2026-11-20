"""Count published media counters without fetching comment/reply bodies."""

import argparse
import json
import os
from pathlib import Path
import sys
import sqlite3
import time
import uuid

from .__main__ import ROOT, positive, read_config
from .api import GraphClient, PilotError, next_cursor, page_data
from .collector import Collector
from .storage import now
from . import profiles
from .credentials import interactive_meta_token


def count_media(client, owner, account_id=None, progress=None):
    started, request_start, began = now(), client.request_count, time.monotonic()
    media, seen, after, complete, verified, error = {}, set(), None, False, False, None
    try:
        account_id = Collector(client, None, owner).account(account_id)
        verified = True
        while True:
            if after in seen:
                raise PilotError("게시물 목록 커서가 반복되어 전체 합계를 확정하지 않습니다.")
            seen.add(after)
            params = {"fields": "id,comments_count,timestamp", "limit": 50}
            if after is not None:
                params["after"] = after
            payload = client.get(account_id + "/media", **params)
            items, following = page_data(payload), next_cursor(payload)
            if any(not isinstance(item.get("id"), str) or not item["id"].isdigit() for item in items):
                raise PilotError("게시물 ID를 확인할 수 없어 해당 목록 페이지는 집계하지 않습니다.")
            if following is not None and following in seen:
                raise PilotError("게시물 목록 커서가 반복되어 해당 페이지는 집계하지 않습니다.")
            for item in items:
                count = item.get("comments_count")
                valid = isinstance(count, int) and not isinstance(count, bool) and count >= 0
                media[item["id"]] = {"id": item["id"], "comments_count": count if valid else None,
                                     "timestamp": item.get("timestamp")}
            if progress:
                progress(len(media))
            if following is None:
                complete = True
                break
            after = following
    except PilotError as failure:
        error = str(failure)
    except KeyboardInterrupt:
        error = "사용자 중단. 현재까지 확인한 카운터만 보존합니다."
    unknown = [item["id"] for item in media.values() if item["comments_count"] is None]
    known_sum = sum(item["comments_count"] for item in media.values() if item["comments_count"] is not None)
    full = verified and complete and not unknown and error is None
    return {"mode": "live_api_media_counters_only", "status": "complete" if full else "partial" if verified else "failed",
            "account_id": account_id, "username": owner.lstrip("@"), "account_verified": verified,
            "started_at": started, "ended_at": now(), "elapsed_seconds": round(time.monotonic() - began, 2),
            "api_requests": client.request_count - request_start, "catalog_complete": complete,
            "media_discovered": len(media), "media_with_counter": len(media) - len(unknown),
            "media_without_counter": unknown, "known_counter_sum": known_sum,
            "total_comments_count": known_sum if full else None,
            "count_basis": "sum_of_api_comments_count_per_accessible_media_all_time",
            "recent_seven_day_count": None, "comment_bodies_requested": False,
            "scope_note": "게시물별 누적 카운터. 최근 7일 작성 건수·실제 조회 가능한 원문 건수와 구분합니다.",
            "error": error, "media": list(media.values())}


def write_result(path, result):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(".media-counts-" + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Instagram 누적 댓글 개수만 조회. 댓글 본문·답글을 읽지 않음")
    parser.add_argument("--max-requests", type=positive, default=200)
    parser.add_argument("--recent", action="store_true", help="로컬 DB에서 최근 7일 게시물·댓글만 집계. API·토큰 입력 없음")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--account", help="등록된 Instagram @아이디")
    parser.add_argument("--config", type=Path)
    args = parser.parse_args(argv)
    code = 1
    try:
        config = read_config(args.config, args.account) if args.config or args.account else read_config()
        directory = profiles.local_directory(config, ROOT)
        args.db = args.db or directory / "comments.sqlite3"
        args.output = args.output or directory / "media-counts.json"
        if args.recent:
            if not config.get("account_id"):
                raise PilotError("이 계정은 아직 연결 검증 전입니다.")
            profiles.check_database(config, args.db)
            from .sheets import read_source
            source = read_source(args.db,config)
            result = {"mode":"cached_recent_posts_and_comments","coverage":source["coverage"],
                      "separate_old_post_tracking":source.get("tracking",{})}
            write_result(args.output,result)
            print(json.dumps(result,ensure_ascii=False,indent=2))
            return 0
        print("댓글 개수만 조회합니다. 게시물별 누적 수이며 최근 7일 작성 건수와 구분합니다.", flush=True)
        profiles.require_enabled(config)
        token = interactive_meta_token(config)
        if not token:
            raise PilotError("로컬 터미널의 숨김 입력란에 Meta 토큰을 입력해주세요.")
        version = os.environ.get("SNS_META_API_VERSION") or config.get("api_version")
        if not version:
            raise PilotError("Meta API 버전을 설정해주세요.")
        client = GraphClient(token, version, max_requests=args.max_requests,
                             api_host=config.get("_meta_api_host", "graph.facebook.com"))
        result = count_media(client, config["expected_username"],
            (None if config.get("_cloud_prefix") else os.environ.get("SNS_IG_ACCOUNT_ID")) or config.get("account_id"),
            progress=lambda count: print(f"개수 조회 진행: 게시물 {count:,}개 확인", flush=True))
        write_result(args.output, result)
        print(json.dumps({k: v for k, v in result.items() if k != "media"}, ensure_ascii=False, indent=2))
        if result["status"] == "complete":
            print(f"누적 댓글 카운터 합계: {result['total_comments_count']:,}건 / 게시물 {result['media_discovered']:,}개")
            code = 0
        else:
            print("전체 합계 미확정입니다. 미제공 카운터·오류를 결과 파일에 기록했습니다.")
        print("결과 파일: " + str(args.output))
    except PilotError as failure:
        print(str(failure), file=sys.stderr)
    except (OSError, KeyError, TypeError, sqlite3.Error):
        print("개수 조회 설정 또는 결과 파일 저장에 실패했습니다.", file=sys.stderr)
    except KeyboardInterrupt:
        print("개수 조회를 중단했습니다.", file=sys.stderr)
        code = 130
    if sys.stdin.isatty():
        try:
            input("결과 확인 후 Enter를 누르면 종료합니다. ")
        except (EOFError, KeyboardInterrupt):
            pass
    return code


if __name__ == "__main__":
    sys.exit(main())
