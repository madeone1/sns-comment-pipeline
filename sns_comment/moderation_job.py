"""Classify new comments within the polling window. Hiding stays off unless configured.

The webhook receiver is unchanged: it still stores IDs only. This job reads those IDs
and also polls recent comments, so an account without a webhook (토론토) is still covered.
It does not delete pending webhook objects and does not write the Meta secret.
"""

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import sys

from .api import GraphClient, GraphError, PilotError, page_data
from .cloud_state import CloudError, CloudState
from .connection_registry import REGISTRY_OBJECT, initial_registry, runtime_configs
from .moderation import VertexModerator, _rate_limited, backfill_sheet, flush_sheet_log, moderate
from .profiles import read_settings
from .storage import timestamp
from .token_connections import read_token, source_secret


ROOT = Path(__file__).resolve().parent.parent
COMMENT_FIELDS = "id,text,timestamp,username,from,hidden,replies.limit(10){id,text,timestamp,username,from,hidden}"


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


def flatten(payload, account_id, owner, media_id, permalink=None):
    for item in page_data(payload):
        yield comment_from_payload(item, account_id, owner, media_id, permalink=permalink)
        replies = item.get("replies") if isinstance(item, dict) else None
        nested = replies.get("data") if isinstance(replies, dict) else None
        if isinstance(nested, list):
            for reply in nested:
                if isinstance(reply, dict):
                    yield comment_from_payload(reply, account_id, owner, media_id, parent_id=item.get("id"), permalink=permalink)


def attach_permalinks(client, comments):
    needed = sorted({comment.get("media_id") for comment in comments
                     if isinstance(comment.get("media_id"), str) and comment["media_id"].isdigit() and not comment.get("permalink")})
    found = {}
    for media_id in needed:
        try:
            item = client.get(media_id, fields="permalink")
        except GraphError as error:
            if _rate_limited(error):
                break
            continue
        if isinstance(item, dict) and isinstance(item.get("permalink"), str):
            found[media_id] = item["permalink"]
    for comment in comments:
        if not comment.get("permalink"):
            comment["permalink"] = found.get(comment.get("media_id"))
    return comments


def pending_comments(state, client, account_id, owner):
    """Read webhook IDs. Leave the pending objects for the daily job."""
    objects, _ = state.list_objects("webhook/pending/" + account_id + "/", 40)
    found = []
    for obj in objects:
        event, _ = state.json_get(obj["name"])
        if not isinstance(event, dict) or event.get("account_id") != account_id:
            continue
        comment_id = event.get("comment_id")
        if not isinstance(comment_id, str) or not comment_id.isdigit():
            continue
        try:
            item = client.get(comment_id, fields="id,text,timestamp,username,from,hidden")
        except GraphError as error:
            if _rate_limited(error):
                raise
            continue
        comment = comment_from_payload(item, account_id, owner, event.get("media_id"))
        found.append(comment)
    return attach_permalinks(client, found)


def poll_recent(client, account_id, owner, cutoff, media_limit):
    try:
        media = page_data(client.get(account_id + "/media", fields="id,permalink,timestamp", limit=media_limit))
    except GraphError as error:
        if _rate_limited(error):
            return [], "rate_limited"
        raise
    found = []
    for item in media:
        media_id = item.get("id")
        if not isinstance(media_id, str) or not media_id.isdigit():
            continue
        try:
            payload = client.get(media_id + "/comments", fields=COMMENT_FIELDS, limit=25)
            link = item.get("permalink") if isinstance(item.get("permalink"), str) else None
            rows = list(flatten(payload, account_id, owner, media_id, permalink=link))
        except GraphError as error:
            if _rate_limited(error):
                return found, "rate_limited"
            continue
        except PilotError:
            continue
        for comment in rows:
            if not comment.get("written_at"):
                continue
            if dt.datetime.fromisoformat(comment["written_at"]) < cutoff:
                continue
            found.append(comment)
    return attach_permalinks(client, found), None


def sweep_account(config, state, client, model, settings, now, sheet_client=None):
    account_id = config["account_id"]
    owner = config["expected_username"].lstrip("@")
    mod = settings.get("moderation") or {}
    cutoff = now - dt.timedelta(minutes=int(mod.get("max_age_minutes") or 20))
    rate = None
    comments = []
    try:
        comments.extend(pending_comments(state, client, account_id, owner))
    except GraphError as error:
        if not _rate_limited(error):
            raise
        rate = "rate_limited"
    if rate is None:
        polled, rate = poll_recent(client, account_id, owner, cutoff, int(mod.get("media_limit") or 8))
        comments.extend(polled)
    chosen, seen = [], set()
    for comment in sorted(comments, key=lambda row: str(row.get("written_at") or ""), reverse=True):
        if comment.get("id") in seen:
            continue
        seen.add(comment.get("id"))
        chosen.append(comment)
        if len(chosen) >= 40:
            break
    result = moderate(chosen, state, settings, model, client=client, account_username=owner,
                      region=config.get("region_label"), sheet_client=sheet_client)
    if rate:
        result["status"] = "rate_limited"
    result["account"] = owner
    result["seen"] = len(seen)
    return result


def sweep(configs, state, client, model, settings, now=None, sheet_client=None):
    moment = now or dt.datetime.now(dt.timezone.utc)
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
            try:
                from .human_decision import apply_human_decisions
                decisions = apply_human_decisions(sheet, client)
                log("human_decision_scan", updates=decisions["updates"], planned=len(decisions["plans"]))
            except PilotError as error:
                log("human_decision_failed", error=str(error))
        return result
    finally:
        del token


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="새 댓글을 기준표로 분류합니다. 숨김은 설정이 켜진 경우에만 합니다.")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--backfill-sheet", action="store_true", help="저장된 판단을 기준표 시트에 이어서 붙입니다.")
    args = parser.parse_args(argv)
    try:
        settings = read_settings(args.config)
        if args.backfill_sheet:
            state = CloudState(os.environ["SNS_STATE_BUCKET"])
            sheet = open_sheet(settings, state)
            if sheet is None:
                log("moderation_sheet_unavailable", error="기준표 시트를 열 수 없습니다.")
                return 1
            try:
                with state.lease(540, "state/moderation-lease.json"):
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
        if (settings.get("moderation") or {}).get("mode", "dry_run") == "off":
            log("moderation_off")
            return 0
        project = os.environ["GOOGLE_CLOUD_PROJECT"]
        state = CloudState(os.environ["SNS_STATE_BUCKET"])
        try:
            with state.lease(540, "state/moderation-lease.json"):
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
