"""Classify a durable comment queue. Hiding stays off unless configured.

The webhook receiver is unchanged: it still stores IDs only. This job reads those IDs
and also polls recent comments, so an account without a webhook (토론토) is still covered.
It checkpoints comment and reply pages before classification, keeps overflow, and
never deletes webhook evidence or writes the Meta secret.
"""

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import sys
import time

from .api import GraphClient, GraphError, PilotError, page_data
from .cloud_state import CloudError, CloudState
from .connection_registry import REGISTRY_OBJECT, initial_registry, runtime_configs
from .moderation import (VertexModerator, _rate_limited, backfill_sheet, comments_waiting_to_hide,
                         flush_sheet_log, hiding_enabled, load_rulebook, moderate, policy_sha)
from .profiles import read_settings
from .storage import timestamp
from .token_connections import STATUS_OBJECT, read_token, source_secret


ROOT = Path(__file__).resolve().parent.parent


def log(event, **values):
    print(json.dumps({"event": event, **values}, ensure_ascii=False), flush=True)


def comment_from_payload(item, account_id, owner, media_id, parent_id=None, permalink=None):
    author = item.get("from") if isinstance(item.get("from"), dict) else {}
    username = item.get("username") if isinstance(item.get("username"), str) else author.get("username")
    author_id = author.get("id") if isinstance(author.get("id"), str) and author.get("id").strip() else None
    if isinstance(username, str) and username.casefold() == owner.casefold():
        is_owner = 1
    elif isinstance(username, str):
        is_owner = 0
    else:
        is_owner = None
    written = None
    if isinstance(item.get("timestamp"), str):
        try:
            written = timestamp(item["timestamp"])
        except PilotError:
            written = None
    link = permalink if isinstance(permalink, str) else None
    return {"id": item.get("id"), "account_id": account_id, "media_id": media_id, "parent_id": parent_id,
            "username": username if isinstance(username, str) else None, "author_id": author_id, "is_owner": is_owner,
            "text": item.get("text") if isinstance(item.get("text"), str) else None, "written_at": written,
            "permalink": link, "already_hidden": item.get("hidden") is True}


def sweep_account(config, state, client, model, settings, now, sheet_client=None):
    from .moderation_polling import Polling
    account_id = config["account_id"]
    owner = config["expected_username"].lstrip("@")
    mod = settings.get("moderation") or {}
    polling = Polling(state, account_id, owner, mod, now, comment_from_payload)
    deadline = time.monotonic() + int(mod.get('poll_seconds') or 90)
    polling.webhooks(client, deadline=deadline)
    if not polling.blocked:
        polling.peek(client, deadline)
    if not polling.blocked:
        polling.scan(client, 'recent', int(mod.get('recent_pages_per_run') or 16), deadline)
    if not polling.blocked:
        polling.scan(client, 'catalog', int(mod.get('catalog_pages_per_run') or 8), deadline)
    queued = polling.pending(int(mod.get('processing_batch_size') or 40))
    chosen = list(queued)
    seen = {row['id'] for row in chosen}
    if hiding_enabled(settings):
        book = load_rulebook(mod.get("policy_version"))
        for comment in comments_waiting_to_hide(state, account_id, policy_sha(book)):
            if comment["id"] in seen:
                continue
            seen.add(comment["id"])
            chosen.append(comment)
    result = moderate(chosen, state, settings, model, client=client, account_username=owner,
                      region=config.get("region_label"), sheet_client=sheet_client)
    polling.acknowledge(queued)
    result['coverage'] = polling.coverage()
    if polling.error:
        result['status'] = 'rate_limited' if _rate_limited(polling.error) else 'pending'
        result['poll_error'] = str(polling.error)
    elif (result['status'] == 'complete' and (result['coverage']['queued'] or
          not result['coverage']['recent_complete'] or not result['coverage']['catalog_complete'])):
        result['status'] = 'pending'
    log('moderation_coverage', account=owner, status=result['status'], **result['coverage'])
    result["account"] = owner
    result["seen"] = len(seen)
    return result


def sweep(configs, state, client, model, settings, now=None, sheet_client=None):
    moment = now or dt.datetime.now(dt.timezone.utc)
    if configs:
        # Rotate the starting account so a shared request budget cannot starve one account.
        offset = (int(moment.timestamp()) // 1800) % len(configs)
        configs = configs[offset:] + configs[:offset]
    results = []
    for config in configs:
        try:
            results.append(sweep_account(config, state, client, model, settings, moment, sheet_client=sheet_client))
        except PilotError as error:
            results.append({"account": config.get("expected_username"), "status": "failed", "error": str(error),
                            "classified": 0, "would_hide": 0, "hidden": 0, "human_review": 0})
    status = "complete"
    if any(item.get("status") == "failed" for item in results):
        status = "failed"
    elif any(item.get("status") == "rate_limited" for item in results):
        status = "rate_limited"
    elif any(item.get("status") == "pending" for item in results):
        status = "pending"
    return {"status": status, "accounts": results,
            "classified": sum(item.get("classified") or 0 for item in results),
            "would_hide": sum(item.get("would_hide") or 0 for item in results),
            "hidden": sum(item.get("hidden") or 0 for item in results),
            "human_review": sum(item.get("human_review") or 0 for item in results)}


def _reference(settings, configs):
    versions = {config.get("_web_secret_version") for config in configs}
    versions.discard(None)
    if len(versions) == 1:
        return versions.pop()
    return source_secret(settings) + "/versions/latest"


def open_sheet(settings, state):
    """Sheets token is the pipeline service account. The file is moderation.sheet.spreadsheet_id."""
    spreadsheet_id = ((settings.get("moderation") or {}).get("sheet") or {}).get("spreadsheet_id")
    auth = (settings.get("workflow") or {}).get("google_sheets") or {}
    if not isinstance(spreadsheet_id, str) or not spreadsheet_id or auth.get("enabled") is not True:
        return None
    try:
        from .google_access import access_token
        from .sheets import SheetsClient
        return SheetsClient(access_token(auth, state.transport), spreadsheet_id)
    except PilotError as error:
        log("moderation_sheet_unavailable", error=str(error))
        return None


def account_clients(settings, state):
    """One Graph client per rulebook account label, using that account's token."""
    from .moderation_sheet import account_tab
    registry, _generation = state.json_get(REGISTRY_OBJECT)
    if not isinstance(registry, dict):
        registry = initial_registry(settings)
    configs = [config for config in runtime_configs(settings, registry) if config.get("account_id")]
    opened, by_label, tokens = {}, {}, []
    try:
        for config in configs:
            label = account_tab({"region": config.get("region_label") or "",
                                 "account_username": config.get("expected_username") or ""})
            reference = config.get("_credential_secret_version") or (source_secret(settings) + "/versions/latest")
            if reference not in opened:
                token, _name = read_token(state.transport, reference)
                tokens.append(token)
                opened[reference] = GraphClient(token, settings["api_version"], max_requests=80,
                                                api_host=config.get("_meta_api_host", "graph.facebook.com"))
            by_label[label] = opened[reference]
        return by_label
    finally:
        for token in tokens:
            del token


def comment_time_lookup(clients):
    """GET /{comment-id}?fields=timestamp only. The account label picks the token."""
    def lookup(account, comment_id):
        client = clients.get(account)
        if client is None:
            raise PilotError("'" + str(account) + "' 계정의 토큰을 찾지 못했습니다.")
        if not isinstance(comment_id, str) or not comment_id.isdigit():
            raise PilotError("댓글 ID 형식을 확인해주세요.")
        try:
            item = client.get(comment_id, fields="timestamp")
        except GraphError as error:
            if _rate_limited(error):
                raise PilotError("API 호출 제한") from None
            raise PilotError("댓글 작성 시각을 읽지 못했습니다.") from None
        raw = item.get("timestamp") if isinstance(item, dict) else None
        if not isinstance(raw, str) or not raw.strip():
            raise PilotError("댓글 작성 시각을 읽지 못했습니다.")
        return timestamp(raw.strip())
    return lookup


def fill_notion_written_dates(settings, state, sheet):
    """Copy freshly written sheet dates into blank Notion 작성일 cells. Does not overwrite a value."""
    from .notion_moderation import _open_locked, ensure_database, fill_written_dates, notion_decisions_enabled
    if not notion_decisions_enabled():
        return []
    opened = _open_locked(settings, state)
    if opened is None:
        return []
    mcp, _reader = opened
    database = ensure_database(mcp, state, settings)
    return fill_written_dates(sheet, mcp, database)


def run(settings, state, project, now=None):
    registry, _ = state.json_get(REGISTRY_OBJECT)
    if not isinstance(registry, dict):
        registry = initial_registry(settings)
    configs = [config for config in runtime_configs(settings, registry) if config.get("account_id")]
    if not configs:
        log("moderation_idle", reason="활성 계정 없음")
        return {"status": "complete", "classified": 0, "would_hide": 0, "hidden": 0, "human_review": 0}
    token, _name = read_token(state.transport, _reference(settings, configs))
    try:
        client = GraphClient(token, settings["api_version"], max_requests=400,
                             api_host=configs[0].get("_meta_api_host", "graph.facebook.com"))
        model = VertexModerator(state.transport, project, settings.get("cloud", {}).get("ai_model", "gemini-3.8-flash"))
        sheet = open_sheet(settings, state)
        result = sweep(configs, state, client, model, settings, now=now, sheet_client=sheet)
        if sheet is not None:
            try:
                flush_sheet_log(state, sheet)
            except PilotError as error:
                log("moderation_sheet_failed", error=str(error))
            notion = None
            try:
                from .notion_moderation import complete_notion_decisions, prepare_notion_decisions
                notion = prepare_notion_decisions(settings, state, sheet)
            except PilotError as error:
                log("notion_decision_unavailable", error=str(error))
            try:
                from .human_decision import apply_human_decisions
                decisions = apply_human_decisions(sheet, client, state=state)
                log("human_decision_scan", updates=decisions["updates"], planned=len(decisions["plans"]))
            except PilotError as error:
                log("human_decision_failed", error=str(error))
            if notion is not None:
                complete_notion_decisions(notion, sheet, now=now)
        revision = registry.get('token_control', {}).get('revision')
        if revision:
            status, generation = state.json_get(STATUS_OBJECT, {})
            if status.get('revision') == revision:
                ready = result['status'] not in {'failed', 'rate_limited'}
                state.json_put(STATUS_OBJECT, {**status,
                    'status': 'ready' if ready else 'sync_pending',
                    'stage': '댓글 관리 작업에서 계정 연결 확인' if ready else '댓글 관리 재시도 대기',
                    'moderation_status': result['status'],
                    'checked_at': dt.datetime.now(dt.timezone.utc).isoformat()}, generation)
        return result
    finally:
        del token


def log_hide_gate(settings):
    """Say so when the env flag is on but the config mode would ignore it."""
    mode = ((settings.get("moderation") or {}).get("mode") or "dry_run")
    if os.environ.get("SNS_MODERATION_HIDE", "").strip() == "1" and mode != "auto_hide":
        log("moderation_hide_ignored", severity="WARNING", mode=mode,
            message="SNS_MODERATION_HIDE=1 이지만 moderation.mode 가 auto_hide 가 아니라 실제 숨김은 하지 않습니다.")


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="새 댓글을 기준표로 분류합니다. 숨김은 설정이 켜진 경우에만 합니다.")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--backfill-sheet", action="store_true", help="저장된 판단을 기준표 시트에 이어서 붙입니다.")
    parser.add_argument("--notion-summary", action="store_true", help="오늘 일일 업무에 댓글 관리 토글을 붙입니다.")
    parser.add_argument("--rewrite-toggle", action="store_true", help="오늘 댓글 관리 토글을 짧은 배치로 다시 씁니다. 기본은 미리보기입니다.")
    parser.add_argument("--sort-sheet", action="store_true", help="기준표 데이터 행을 댓글 작성일 내림차순으로 정렬합니다. 기본은 미리보기입니다.")
    parser.add_argument("--fill-missing-dates", action="store_true", help="C열이 비어 있는 행의 인스타 작성 시각을 읽고, 반영 시 그 시각으로 다시 정렬합니다. 기본은 미리보기입니다.")
    parser.add_argument("--apply", action="store_true", help="--sort-sheet, --fill-missing-dates, --rewrite-toggle 과 함께 쓰면 반영합니다.")
    args = parser.parse_args(argv)
    try:
        settings = read_settings(args.config)
        if args.fill_missing_dates:
            from .moderation_sheet import fill_missing_dates
            state = CloudState(os.environ["SNS_STATE_BUCKET"])
            sheet = open_sheet(settings, state)
            if sheet is None:
                log("moderation_sheet_unavailable", error="기준표 시트를 열 수 없습니다.")
                return 1
            clients = account_clients(settings, state)
            result = fill_missing_dates(sheet, state, comment_time_lookup(clients), apply=args.apply)
            notion_filled = []
            if args.apply:
                try:
                    notion_filled = fill_notion_written_dates(settings, state, sheet)
                except PilotError as error:
                    log("notion_written_at_unavailable", error=str(error))
            log("sheet_dates_filled", apply=args.apply, tabs=result["rows"], notion_filled=notion_filled)
            return 0
        if args.sort_sheet:
            from .moderation_sheet import sort_tabs
            state = CloudState(os.environ["SNS_STATE_BUCKET"])
            sheet = open_sheet(settings, state)
            if sheet is None:
                log("moderation_sheet_unavailable", error="기준표 시트를 열 수 없습니다.")
                return 1
            result = sort_tabs(sheet, state, apply=args.apply)
            log("sheet_sort", apply=args.apply, tabs=result)
            return 0
        if args.rewrite_toggle:
            from .notion_moderation import _open_locked, rewrite_today_toggle
            state = CloudState(os.environ["SNS_STATE_BUCKET"])
            sheet = open_sheet(settings, state)
            if sheet is None:
                log("moderation_sheet_unavailable", error="기준표 시트를 열 수 없습니다.")
                return 1
            opened = _open_locked(settings, state)
            if opened is None:
                log("notion_summary_deferred", reason="노션 잠금을 열지 못했습니다.")
                return 0
            mcp, reader = opened

            def finder(day):
                page = reader.daily_page(day)
                return {"id": page["id"], "url": page.get("url"), "text": page.get("text") or "",
                        "truncated": page.get("truncated")}

            try:
                result = rewrite_today_toggle(settings, state, sheet, mcp, finder, apply=args.apply)
            except PilotError as error:
                log("notion_toggle_rewrite_failed", error=str(error))
                return 1
            log("notion_toggle_rewrite", status=result["status"], day=result["day"],
                updates=result.get("updates", 0), markdown=result.get("markdown", ""))
            return 0
        if args.notion_summary:
            from .cloud_state import NOTION_SUMMARY_LEASE, CloudError, lease_retry
            from .notion_moderation import run_notion_summary
            state = CloudState(os.environ["SNS_STATE_BUCKET"])
            try:
                with lease_retry(state, 1800, NOTION_SUMMARY_LEASE, attempts=4, wait_seconds=15):
                    result = run_notion_summary(settings, state)
            except CloudError as error:
                if "진행 중" in str(error) or "잠금" in str(error):
                    log("notion_summary_deferred", reason=str(error))
                    return 0
                raise
            except PilotError as error:
                log("notion_summary_failed", error=str(error))
                return 1
            if result.get("status") == "deferred":
                log("notion_summary_deferred", reason="노션 잠금을 여러 번 시도한 뒤에도 열지 못했습니다.")
            log("notion_summary_finished", status=result["status"], day=result.get("day"))
            return 1 if result.get("status") == "failed" else 0
        if args.backfill_sheet:
            state = CloudState(os.environ["SNS_STATE_BUCKET"])
            sheet = open_sheet(settings, state)
            if sheet is None:
                log("moderation_sheet_unavailable", error="기준표 시트를 열 수 없습니다.")
                return 1
            try:
                from .cloud_state import MODERATION_LEASE
                with state.lease(1620, MODERATION_LEASE):
                    result = backfill_sheet(state, sheet)
                log("moderation_sheet_backfill", **result)
                return 0
            except CloudError as error:
                if "진행 중" in str(error) or "잠금" in str(error):
                    log("moderation_deferred", reason=str(error))
                    return 0
                raise
            except PilotError as error:
                log("moderation_sheet_failed", error=str(error))
                return 1
        log_hide_gate(settings)
        if (settings.get("moderation") or {}).get("mode", "dry_run") == "off":
            log("moderation_off")
            return 0
        project = os.environ["GOOGLE_CLOUD_PROJECT"]
        state = CloudState(os.environ["SNS_STATE_BUCKET"])
        try:
            from .cloud_state import MODERATION_LEASE
            with state.lease(1620, MODERATION_LEASE):
                result = run(settings, state, project)
        except CloudError as error:
            if "진행 중" in str(error) or "잠금" in str(error):
                log("moderation_deferred", reason=str(error))
                return 0
            raise
        log("moderation_finished", status=result["status"], classified=result["classified"],
            would_hide=result["would_hide"], hidden=result["hidden"], human_review=result["human_review"])
        return 1 if result["status"] == "failed" else 0
    except PilotError as error:
        log("moderation_failed", severity="ERROR", error=str(error))
        return 1
    except KeyError:
        log("moderation_failed", severity="ERROR", error="클라우드 환경 변수를 확인해주세요.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
