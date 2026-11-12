"""Run with python3 -m sns_comment. No third-party packages required."""

import argparse
import getpass
import json
import os
import shlex
from pathlib import Path
import sqlite3
import sys

from .api import GraphClient, PilotError
from .collector import Collector
from .account import AccountCollector, collect_continuously, collection_window
from .parallel import ParallelAccountCollector
from .demo import DemoGraph, AccountDemoGraph
from .storage import Store, run_lock, timestamp
from .sheets import SheetsClient, read_source, sync
from . import profiles
from .credentials import interactive_meta_token


ROOT = Path(__file__).resolve().parent.parent


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("1 이상의 숫자를 입력해주세요.")
    return number


def parser():
    result = argparse.ArgumentParser(description="Instagram 계정의 기간별 댓글·답글 수집·복구")
    commands = result.add_subparsers(dest="command", required=True)
    profiles.configure_parser(commands)
    live = commands.add_parser("run", help="실제 API 수동 수집. 토큰은 로컬에서만 입력")
    live.add_argument("--post-url")
    live.add_argument("--scope", choices=["account", "post"], help="기본은 설정의 계정 단위 수집. post는 진단용")
    live.add_argument("--tracked", action="store_true", help="최근 활동이 확인된 이전 게시물만 별도 수집. 기본 최근 게시물 보고와 분리")
    live.add_argument("--days", type=positive, help="계정 댓글 작성 기간·재조회 일수. 기본 7일")
    live.add_argument("--start", help="포함 시작 시각. --end와 함께 지정")
    live.add_argument("--end", help="미포함 종료 시각. 기본은 가장 최근 한국시간 08시")
    live.add_argument("--resume-scope", help="저장된 기간 수집 ID에서 목록·댓글·답글 처리를 재개")
    live.add_argument("--restart-scope", action="store_true", help="같은 기간을 새로 대조. 기존 원본·수집 이력은 보존")
    live.add_argument("--api-version")
    live.add_argument("--account-id", help="자동 계정 조회가 지원되지 않을 때 검증할 IG 계정 ID")
    live.add_argument("--max-requests", type=positive, default=500)
    live.add_argument("--max-media-pages", type=positive, default=20)
    live.add_argument("--workers",type=positive,help="독립된 댓글·답글 GET 동시 처리 수. 기본 설정값 3, 최대 5")
    live.add_argument("--until-complete", action="store_true", help="같은 메모리 토큰으로 로컬 예산을 나눠 자동 재개. API 오류는 중단")
    live.add_argument("--max-total-requests", type=positive, default=5000, help="연속 수집 전체 요청 상한. 기본 5000회")
    live.add_argument("--sync-sheets", action="store_true", help="수집 성공 뒤 설정된 Google Sheets에 자동 반영")
    live.add_argument("--db", type=Path)
    demo = commands.add_parser("demo", help="네트워크 없이 합성 데이터로 수집·재실행 검증")
    demo.add_argument("--db", type=Path, default=ROOT / ".local/demo.sqlite3")
    offline = commands.add_parser("account-demo", help="다중 게시물·7일 집계·중단 재개를 네트워크 없이 검증")
    offline.add_argument("--db", type=Path, default=ROOT / ".local/account-demo.sqlite3")
    offline.add_argument("--days", type=positive)
    offline.add_argument("--start")
    offline.add_argument("--end")
    offline.add_argument("--resume-scope")
    offline.add_argument("--restart-scope", action="store_true")
    offline.add_argument("--max-requests", type=positive, default=500)
    offline.add_argument("--max-media-pages", type=positive, default=20)
    offline.add_argument("--workers",type=positive,default=1)
    offline.add_argument("--until-complete", action="store_true")
    offline.add_argument("--max-total-requests", type=positive, default=5000)
    sheets = commands.add_parser("sheets", help="원본 DB에서 Google Sheets 내보내기·연결. 원격 파일을 생성하지 않음")
    sheets.add_argument("action", choices=["export", "sync"])
    sheets.add_argument("--db", type=Path)
    sheets.add_argument("--output", type=Path)
    sheets.add_argument("--spreadsheet-id", help="소유 계정을 확인한 마스터 시트 ID")
    sheets.add_argument("--dry-run", action="store_true", help="시트 조회·변경 건수만 확인. 쓰기 없음")
    sheets.add_argument("--as-of", help="집계 기준 시각. 시간대가 있는 ISO 시각")
    sheets.add_argument("--max-batches", type=positive, default=20, help="실행당 최대 쓰기 배치. 미완료는 pending으로 유지")
    tracking = commands.add_parser("tracking", help="최근 활동이 확인된 이전 게시물 추적 목록")
    tracking.add_argument("action", choices=["list", "add"])
    tracking.add_argument("--db", type=Path)
    tracking.add_argument("--media-id")
    tracking.add_argument("--activity-at", help="최근 댓글이 확인된 시각. 시간대 포함 ISO")
    for name in ("status", "comments", "summary"):
        command = commands.add_parser(name)
        command.add_argument("--demo", action="store_true", help="합성 검증 DB 조회")
        command.add_argument("--db", type=Path)
        if name == "comments":
            command.add_argument("--limit", type=positive, default=20)
        if name == "summary":
            command.add_argument("--start", help="포함 시작 시각. 예: 2026-11-03T08:00:00+09:00")
            command.add_argument("--end", help="미포함 종료 시각. 예: 2026-11-04T08:00:00+09:00")
    for command in (live, sheets, tracking, *[commands.choices[name] for name in ("status", "comments", "summary")]):
        command.add_argument("--account", help="등록된 Instagram @아이디")
        command.add_argument("--config", type=Path)
    return result


def read_config(path=None, account=None):
    return profiles.load(path or ROOT / "config.json", account)


def sheets_client(config, spreadsheet_id=None):
    sheet_id = spreadsheet_id or config.get("google_sheets", {}).get("spreadsheet_id")
    if not sheet_id:
        raise PilotError("마스터 시트 ID를 지정해주세요. 새 파일은 자동 생성하지 않습니다.")
    token = os.environ.get("SNS_GOOGLE_ACCESS_TOKEN")
    if not token and sys.stdin.isatty():
        token = getpass.getpass("Google 액세스 토큰(화면에 표시·저장하지 않음): ").strip()
    return sheet_id, SheetsClient(token, sheet_id)


def main(argv=None):
    os.umask(0o077)
    args = parser().parse_args(argv)
    store = None
    try:
        if args.command == "accounts":
            result = profiles.manage(args)
            emit(result)
            return 1 if args.action == "diagnose" and result["status"] == "needs_attention" else 0
        selected = getattr(args, "account", None)
        config_path = getattr(args, "config", None)
        config = read_config(config_path, selected) if selected or config_path else read_config()
        directory = profiles.local_directory(config, ROOT)
        if args.command in {"run", "tracking", "sheets"}:
            profiles.require_enabled(config)
            args.db = args.db or directory / "comments.sqlite3"
            profiles.check_database(config, args.db)
        if args.command == "sheets":
            args.output = args.output or directory / "sheets-source.json"
        if args.command == "tracking":
            with run_lock(args.db):
                store = Store(args.db)
                account_id = config.get("account_id")
                start, end = collection_window(config.get("collection", {}).get("lookback_days", 7))
                if args.action == "add":
                    if not args.media_id or not args.activity_at:
                        raise PilotError("추가할 게시물 ID와 활동 확인 시각을 지정해주세요.")
                    owned = store.db.execute("SELECT 1 FROM media WHERE account_id=? AND id=?", (account_id,args.media_id)).fetchone()
                    if not owned:
                        raise PilotError("현재 계정의 확인된 게시물만 수동 추적 목록에 추가할 수 있습니다.")
                    store.track_media(account_id,args.media_id,args.activity_at)
                targets = store.recent_targets(account_id,start,end)
                emit({"policy": "recent_posts_and_separate_activity_watchlist",
                      "recent_posts": [{"id":r["id"],"permalink":r["permalink"]} for r in targets if r["selection_reason"]=="recent_post"],
                      "tracked_old_posts": [{"id":r["id"],"permalink":r["permalink"]} for r in targets if r["selection_reason"]=="tracked_activity"],
                      "activity_detection": "known_comments_or_manual_or_verified_events",
                      "webhook_connected": False})
        elif args.command == "sheets":
            as_of = None
            if args.as_of:
                import datetime as dt
                as_of = dt.datetime.fromisoformat(timestamp(args.as_of))
            source = read_source(args.db, config, as_of=as_of)
            if args.action == "export":
                args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                args.output.write_text(json.dumps(source, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                os.chmod(args.output, 0o600)
                emit({"status": "exported_locally", "output": str(args.output),
                      "rows": {name: len(rows) - 1 for name, rows in source["tabs"].items()},
                      "google_sheets_written": False})
            else:
                sheet_id, client = sheets_client(config, args.spreadsheet_id)
                result = sync(source, client, directory / ("sheets-" + sheet_id + ".json"),
                              dry_run=args.dry_run, max_batches=args.max_batches)
                emit(result)
                if result["status"] == "pending":
                    return 1
        elif args.command in {"run", "demo", "account-demo"}:
            media_policy = ("tracked_activity" if getattr(args,"tracked",False) else config.get("collection", {}).get("media_scope", "recent_posts")) if args.command == "run" else "all_accessible"
            discovery_pages = config.get("collection", {}).get("discovery_pages", 1)
            workers=(args.workers or config.get("collection",{}).get("workers",3)) if args.command=="run" else getattr(args,"workers",1)
            if not isinstance(workers,int) or isinstance(workers,bool) or not 1<=workers<=5:
                raise PilotError("동시 조회 수는 1~5개로 지정해주세요.")
            if args.command != "demo" and args.resume_scope and (args.days or args.start or args.end or args.restart_scope):
                # Resume uses the recorded period and exact job ID. New dates
                # or a fresh scan are separate requests, never silently ignored.
                raise PilotError("재개할 때는 --resume-scope와 저장 DB만 지정해주세요. 기간 변경·새 대조와 함께 쓸 수 없습니다.")
            mode = (args.scope or ("post" if args.post_url else config.get("collection", {}).get("mode", "post"))) if args.command == "run" else ("account" if args.command == "account-demo" else "post")
            if args.command == "run" and ((mode == "account" and args.post_url) or
                    (mode == "post" and (args.days or args.start or args.end or args.resume_scope or args.restart_scope or args.until_complete or args.tracked))):
                raise PilotError("계정 기간 수집과 게시물 1개 진단 옵션을 함께 사용할 수 없습니다.")
            sheet_connection = sheets_client(config) if args.command == "run" and args.sync_sheets else None
            if args.command in {"demo", "account-demo"}:
                client = AccountDemoGraph() if args.command == "account-demo" else DemoGraph()
                post_url = config["post_url"]
                account_id = "100" if args.command == "account-demo" else None
                owner = "waterbomb_official"
            else:
                version = args.api_version or os.environ.get("SNS_META_API_VERSION") or config.get("api_version")
                if not version and sys.stdin.isatty():
                    version = input("Meta 앱에서 사용하는 API 버전(v숫자.0): ").strip()
                if not version:
                    raise PilotError("API 버전을 입력해야 실행할 수 있습니다. Meta 도구의 버전 선택값을 확인해주세요.")
                token = interactive_meta_token(config)
                if not token:
                    raise PilotError("액세스 토큰이 필요합니다. 실행 파일을 터미널에서 열어 로컬 입력해주세요.")
                client = GraphClient(token, version, max_requests=args.max_requests,
                                     api_host=config.get("_meta_api_host", "graph.facebook.com"))
                post_url = args.post_url or config.get("post_url")
                if mode == "post" and not post_url:
                    raise PilotError("이 계정의 게시물 진단에는 --post-url을 지정해주세요.")
                account_id = args.account_id or (None if config.get("_cloud_prefix") else os.environ.get("SNS_IG_ACCOUNT_ID")) or config.get("account_id")
                if config.get("_cloud_prefix") and account_id != config.get("account_id"):
                    raise PilotError("선택한 계정과 계정 ID 재정의 값이 다릅니다.")
                owner = config["expected_username"]
            with run_lock(args.db):
                store = Store(args.db)
                window = None
                if mode == "account":
                    if args.resume_scope:
                        prior = store.scope(args.resume_scope)
                        if prior["owner"] != owner.lstrip("@") or (account_id and prior["account_id"] != account_id):
                            raise PilotError("재개할 기간 수집의 계정이 현재 설정과 다릅니다.")
                        window = prior["window_start"], prior["window_end"]
                        account_id = prior["account_id"]
                    else:
                        window = collection_window(args.days or config.get("collection", {}).get("lookback_days", 7),
                                                   args.start, args.end)
                    if args.until_complete:
                        def progress(value):
                            print("수집 진행: " + json.dumps(value, ensure_ascii=False), file=sys.stderr, flush=True)
                        result = collect_continuously(client, store, owner, *window, account_id=account_id,
                            restart=args.restart_scope, resume_scope_id=args.resume_scope, max_requests=args.max_requests,
                            max_media_pages=args.max_media_pages, max_total_requests=args.max_total_requests, progress=progress,
                            media_policy=media_policy,discovery_pages=discovery_pages,workers=workers)
                    else:
                        collector_class=ParallelAccountCollector if workers>1 else AccountCollector
                        extra={"workers":workers} if workers>1 else {}
                        collector = collector_class(client, store, owner, max_media_pages=args.max_media_pages,
                                                     max_requests=args.max_requests,media_policy=media_policy,discovery_pages=discovery_pages,**extra)
                        result = collector.collect_account(*window, account_id=account_id, restart=args.restart_scope,
                                                           resume_scope_id=args.resume_scope)
                    result["resume_command"] = ("python3 -m sns_comment " + ("account-demo" if args.command == "account-demo" else "run")
                        + " --db " + shlex.quote(str(args.db.resolve())) + " --resume-scope " + result["scope_id"]
                        + (" --until-complete --max-total-requests " + str(args.max_total_requests) if args.until_complete else ""))
                    result["resume_command"] += " --workers " + str(workers)
                    if args.command == "run":
                        result["resume_command"] += " --account " + shlex.quote(config["expected_username"])
                        if config_path:
                            result["resume_command"] += " --config " + shlex.quote(str(config_path.resolve()))
                    if media_policy == "tracked_activity":
                        result["resume_command"] += " --tracked"
                else:
                    collector = Collector(client, store, owner, max_media_pages=getattr(args, "max_media_pages", 20))
                    result = collector.collect(post_url, account_id)
                if args.command in {"demo", "account-demo"}:
                    result["simulated_requests"] = result.pop("api_requests")
                    result["mode"] = "offline_synthetic_account_demo" if args.command == "account-demo" else "offline_synthetic_demo"
                    result["live_instagram_called"] = False
                else:
                    result["mode"] = "live_api_account_window" if mode == "account" else "live_api_manual_pilot"
                    if result["status"] != "complete":
                        emit(result)
                        return 1
                    if sheet_connection is not None:
                        sheet_id, sheet_client = sheet_connection
                        try:
                            result["sheets_sync"] = sync(read_source(args.db, config, collection_period=window), sheet_client,
                                directory / ("sheets-" + sheet_id + ".json"))
                        except PilotError:
                            result["sheets_sync"] = {"status": "pending", "collection_preserved": True,
                                "retry_command": "python3 -m sns_comment sheets sync --account "
                                    + shlex.quote(config["expected_username"]) + " --db " + shlex.quote(str(args.db.resolve()))
                                    + (" --config " + shlex.quote(str(config_path.resolve())) if config_path else "")}
                            emit(result)
                            raise
                emit(result)
                if result["status"] != "complete":
                    return 1
                if result.get("sheets_sync", {}).get("status") == "pending":
                    return 1
        else:
            path = args.db or (ROOT / ".local/demo.sqlite3" if args.demo else directory / "comments.sqlite3")
            if not path.is_file():
                raise PilotError("아직 저장된 DB가 없습니다. 실제 수집 또는 demo를 먼저 실행해주세요.")
            store = Store(path)
            account_filter = config.get("account_id") if not args.demo else None
            if not args.demo and config.get("_cloud_prefix") and not account_filter:
                raise PilotError("이 계정은 아직 연결 검증 전입니다.")
            if args.command == "status":
                emit({"database": str(path), "runs": store.runs(account_filter), "account_scopes": store.account_scopes(account_filter)})
            elif args.command == "comments":
                emit(store.comments(min(args.limit, 1000), account_filter))
            else:
                if bool(args.start) != bool(args.end):
                    raise PilotError("기간 집계에는 --start와 --end를 함께 입력해주세요.")
                start = timestamp(args.start) if args.start else None
                end = timestamp(args.end) if args.end else None
                if start and start >= end:
                    raise PilotError("집계 종료 시각은 시작 시각보다 나중이어야 합니다.")
                emit({"basis": "written_at", "start_inclusive_utc": start, "end_exclusive_utc": end,
                      "counts": store.summary(account_filter, start=start, end=end), "analysis": "not_implemented"})
        return 0
    except PilotError as error:
        print(str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("실행을 중단했습니다. 저장된 원본은 유지됩니다.", file=sys.stderr)
        return 130
    except (sqlite3.Error, OSError, KeyError, TypeError):
        print("로컬 저장소 또는 설정 처리에 실패했습니다. 파일 권한과 설정 형식을 확인해주세요.", file=sys.stderr)
        return 1
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    sys.exit(main())
