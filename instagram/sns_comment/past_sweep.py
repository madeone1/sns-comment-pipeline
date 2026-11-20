"""On-demand past-comment sweep for one Cloud Run execution.

Hides only, and only when --apply or SNS_PAST_SWEEP_APPLY=apply is set.
Official-account comments and a human 유지 are left as they are. Progress is
stored at moderation/past-sweep/{account}.json so a timeout or a Meta 429
continues on the next execution. The sweep uses the moderation lease and
stops before the next :00 or :30 slot so the 30-minute job can take it.
"""

import argparse
import copy
import datetime as dt
import os
from pathlib import Path
import re
import signal
import sys
import time

from .api import GraphError, PilotError
from .author_cap import _hide_pending, comments_from_stored, posts_for_accounts
from .cloud_state import MODERATION_LEASE, CloudError, CloudState
from .connection_registry import REGISTRY_OBJECT, initial_registry, runtime_configs
from .count_rules import active_rules, structural_decisions
from .moderation import HIDE_LABEL, REVIEW_LABEL, _human_kept, decision_name, flush_sheet_log, policy_sha
from .moderation_job import account_clients, log, open_sheet
from .profiles import read_settings
from .rulebook_source import resolve_rulebook, rulebook_source


ROOT = Path(__file__).resolve().parent.parent
JOB_NAME = "sns-comments-past-sweep"
CHECKPOINT = "moderation/past-sweep/{account}.json"
YIELD_SECONDS = 120
MAX_CHUNK_SECONDS = 480
LEASE_SLACK = 45
MIN_WORK_SECONDS = 30
OFFICIAL_HANDLES = {
    "waterbomb_official",
    "waterbomb_seoul_official",
    "waterbomb_busan_official",
    "waterbomb_sokcho_official",
    "waterbomb_toronto_official",
}
_FIELDS = ("id", "account_id", "media_id", "parent_id", "username", "author_id", "is_owner",
           "text", "written_at", "permalink", "already_hidden", "_rule_id", "_reason", "_label")
_CITIES = {
    "서울": "seoul", "seoul": "seoul", "대표": "seoul", "대표(공식)": "seoul",
    "부산": "busan", "busan": "busan",
    "속초": "sokcho", "sokcho": "sokcho",
    "토론토": "toronto", "toronto": "toronto",
}


def _blank_counts():
    return {"scanned": 0, "within_limit": 0, "would_hide": 0, "already": 0,
            "human_kept": 0, "human_review": 0, "official_skipped": 0,
            "hidden": 0, "hide_failed": 0}


def seconds_until_slot(now):
    """Seconds until the next minute-0 or minute-30. The moderation job starts then."""
    moment = now if now.tzinfo else now.replace(tzinfo=dt.timezone.utc)
    into = moment.minute * 60 + moment.second + moment.microsecond / 1_000_000
    half = 30 * 60
    return half - (into % half) or half


def parse_rule_ids(text):
    """None means every active count rule. '+' is accepted because gcloud splits on commas."""
    if text is None:
        return None
    raw = str(text).strip()
    if not raw or raw.casefold() in {"all", "전체", "*"}:
        return None
    found = []
    for part in re.split(r"[,+\s]+", raw):
        item = part.strip().upper()
        if item and item not in found:
            found.append(item)
    return found or None


def _rule_key(rule_ids):
    if not rule_ids:
        return ["*"]
    return sorted(rule_ids)


def city_of(config):
    region = str(config.get("region_label") or "")
    username = str(config.get("expected_username") or "").lstrip("@").casefold()
    pieces = set(username.split("_"))
    if region == "부산" or "busan" in pieces:
        return "busan"
    if region == "속초" or "sokcho" in pieces:
        return "sokcho"
    if region == "토론토" or "toronto" in pieces:
        return "toronto"
    if region in {"서울", "대표", "대표(공식)"} or username in {"waterbomb_official", "waterbomb_seoul_official"}:
        return "seoul"
    return ""


def select_sweep_account(configs, account):
    """One config for 서울/부산/속초/토론토, a handle, or an account id. Empty when it is not one account."""
    if not isinstance(account, str) or not account.strip():
        return []
    token = account.strip()
    city = _CITIES.get(token) or _CITIES.get(token.casefold())
    if city:
        return [config for config in configs if city_of(config) == city]
    folded = token.lstrip("@").casefold()
    exact = []
    for config in configs:
        username = str(config.get("expected_username") or "").lstrip("@").casefold()
        if str(config.get("account_id") or "") == token or username == folded:
            exact.append(config)
    return exact


def is_official(comment, owner):
    if not isinstance(comment, dict):
        return False
    if comment.get("is_owner") == 1:
        return True
    username = comment.get("username") if isinstance(comment.get("username"), str) else ""
    if not username and isinstance(comment.get("author_username"), str):
        username = comment["author_username"]
    handle = username.lstrip("@").casefold()
    if not handle:
        return False
    return handle == str(owner or "").lstrip("@").casefold() or handle in OFFICIAL_HANDLES


def book_for_rules(book, rule_ids):
    """Copy of the book limited to the requested rule ids. D12 is omitted unless it was requested."""
    if not rule_ids:
        return book
    wanted = set(rule_ids)
    copied = copy.deepcopy(book)
    copied["conditions"] = [item for item in (copied.get("conditions") or [])
                            if str(item.get("id") or "").upper() in wanted]
    if "D12" not in wanted:
        copied["rules"] = [item for item in (copied.get("rules") or []) if item.get("id") != "D12"]
        hide_ids = copied.get("hide_rule_ids")
        if isinstance(hide_ids, list):
            copied["hide_rule_ids"] = [item for item in hide_ids if item != "D12"]
    return copied


def _snapshot(comment):
    return {key: comment.get(key) for key in _FIELDS}


def _checkpoint_name(account_id):
    return CHECKPOINT.format(account=account_id)


def _load(state, account_id):
    current, generation = state.json_get(_checkpoint_name(account_id), None)
    if not isinstance(current, dict):
        return None, 0
    return current, generation


def _save(state, account_id, checkpoint, generation):
    from .moderation import _put
    return _put(state, _checkpoint_name(account_id), checkpoint, generation)


def _skip_reason(comment, state, account_id, owner):
    if is_official(comment, owner):
        return "official_skipped"
    existing, _generation = state.json_get(decision_name(account_id, comment["id"]))
    if _human_kept(existing):
        return "human_kept"
    if comment.get("already_hidden") or (isinstance(existing, dict) and existing.get("status") == "hidden"):
        return "already"
    return ""


def plan_hides(state, book, config, rule_ids):
    """Decide from comments already stored for this account. Does not call Instagram."""
    account_id = config["account_id"]
    owner = str(config.get("expected_username") or "").lstrip("@")
    narrowed = book_for_rules(book, rule_ids)
    enabled = [item.get("id") for item in active_rules(narrowed)]
    missing = [item for item in (rule_ids or []) if item not in set(enabled)]
    comments = comments_from_stored(state, account_id)
    who = {"id": str(account_id), "username": owner, "region": config.get("region_label")}
    decisions = structural_decisions(comments, posts_for_accounts(state, [account_id]), narrowed, who)
    counts = _blank_counts()
    hides = []
    ordered = sorted(comments, key=lambda row: (str(row.get("written_at") or ""), str(row.get("id") or "")))
    for comment in ordered:
        if not isinstance(comment, dict) or not str(comment.get("id") or "").isdigit():
            continue
        counts["scanned"] += 1
        choice = decisions.get(comment.get("id"))
        reason = _skip_reason(comment, state, account_id, owner)
        if reason == "official_skipped":
            counts["official_skipped"] += 1
            continue
        if not choice or choice.get("label") != HIDE_LABEL:
            if choice and choice.get("label") == REVIEW_LABEL:
                counts["human_review"] += 1
            else:
                counts["within_limit"] += 1
            continue
        if reason:
            counts[reason] += 1
            continue
        counts["would_hide"] += 1
        marked = _snapshot(comment)
        marked["_rule_id"] = choice.get("rule_id") or ""
        marked["_reason"] = choice.get("reason") or ""
        marked["_label"] = choice.get("label") or HIDE_LABEL
        hides.append(marked)
    return hides, counts, enabled, missing


def _result(config, apply, counts, status, done, remaining, rules):
    owner = str(config.get("expected_username") or "").lstrip("@")
    payload = dict(counts)
    payload.update(account=owner, apply=bool(apply), status=status, done=bool(done),
                   remaining=len(remaining), rules=list(rules or []))
    return payload


def run_chunk(state, client, sheet, book, config, rule_ids, apply, deadline, limit=None, stop=None):
    """Hide a bounded batch. A saved checkpoint is resumed when the rule set and apply flag match."""
    account_id = config["account_id"]
    owner = str(config.get("expected_username") or "").lstrip("@")
    key = _rule_key(rule_ids)
    stored, generation = _load(state, account_id)
    if not apply:
        _hides, counts, enabled, missing = plan_hides(state, book, config, rule_ids)
        if missing:
            log("past_sweep_rule_inactive", account=owner, rules=missing,
                message="요청한 규칙이 켜져 있지 않아 그 규칙은 적용하지 않습니다.")
        return _result(config, False, counts, "dry_run", True, [], enabled)
    resume = (isinstance(stored, dict) and stored.get("apply") is True
              and stored.get("rules") == key and not stored.get("done")
              and isinstance(stored.get("remaining"), list) and stored.get("remaining"))
    if resume:
        checkpoint = stored
        checkpoint["counts"] = {**_blank_counts(), **(stored.get("counts") or {})}
        enabled = list(stored.get("enabled") or key)
    else:
        hides, counts, enabled, missing = plan_hides(state, book, config, rule_ids)
        if missing:
            log("past_sweep_rule_inactive", account=owner, rules=missing,
                message="요청한 규칙이 켜져 있지 않아 그 규칙은 적용하지 않습니다.")
        checkpoint = {"version": 1, "account_id": account_id, "rules": key, "apply": True,
                      "enabled": enabled, "remaining": hides, "counts": counts, "done": False}
        generation = _save(state, account_id, checkpoint, generation)
    if client is None:
        log("past_sweep_failed", account=owner, error="숨김을 실행할 계정 연결이 없습니다.")
        return _result(config, True, checkpoint["counts"], "failed", False, checkpoint["remaining"], enabled)
    status = "complete"
    while checkpoint["remaining"]:
        if stop and stop.get("stop"):
            status = "paused"
            break
        if time.monotonic() >= deadline or (limit is not None and limit <= 0):
            status = "paused"
            break
        comment = checkpoint["remaining"][0]
        reason = _skip_reason(comment, state, account_id, owner)
        if reason:
            checkpoint["counts"][reason] = int(checkpoint["counts"].get(reason) or 0) + 1
            if reason == "human_kept" and checkpoint["counts"].get("would_hide"):
                checkpoint["counts"]["would_hide"] -= 1
            checkpoint["remaining"] = checkpoint["remaining"][1:]
            generation = _save(state, account_id, checkpoint, generation)
            continue
        working = {"pending_hides": [comment], "counts": checkpoint["counts"]}
        try:
            paused = _hide_pending(working, state, client, sheet, book, account_id, owner,
                                   config.get("region_label"))
        except GraphError as error:
            from .moderation import _rate_limited
            if not _rate_limited(error):
                raise
            paused = True
        checkpoint["counts"] = working["counts"]
        if paused:
            status = "rate_limited"
            generation = _save(state, account_id, checkpoint, generation)
            break
        checkpoint["remaining"] = checkpoint["remaining"][1:]
        if limit is not None:
            limit -= 1
        generation = _save(state, account_id, checkpoint, generation)
    else:
        status = "complete"
    checkpoint["done"] = status == "complete" and not checkpoint["remaining"]
    generation = _save(state, account_id, checkpoint, generation)
    if apply and sheet is not None:
        try:
            flush_sheet_log(state, sheet)
        except PilotError as error:
            log("past_sweep_sheet_retry", account=owner, error=str(error),
                message="숨김 기록은 대기열에 남았고 다음 실행에서 시트에 다시 씁니다.")
    return _result(config, apply, checkpoint["counts"], status, checkpoint["done"], checkpoint["remaining"], enabled)


def _busy(error):
    text = str(error)
    return "진행 중" in text or "잠금" in text


def _bind_stop():
    flag = {"stop": False}

    def handle(signum, frame):
        flag["stop"] = True

    previous = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, handle)
    return flag, previous


def guarded_run(state, client, sheet, book, config, rule_ids, apply, now=None, limit=None):
    """Take the moderation lease for one short chunk, or leave it for the 30-minute job."""
    moment = now or dt.datetime.now(dt.timezone.utc)
    wait = seconds_until_slot(moment)
    work = min(MAX_CHUNK_SECONDS, wait - YIELD_SECONDS)
    owner = str(config.get("expected_username") or "").lstrip("@")
    if work < MIN_WORK_SECONDS:
        log("past_sweep_yielded", message="skipped: moderation window", account=owner,
            seconds_until_slot=round(wait, 1))
        return _result(config, apply, _blank_counts(), "yielded", False, [], _rule_key(rule_ids))
    lease_seconds = min(int(work) + LEASE_SLACK, max(MIN_WORK_SECONDS, int(wait - 15)))
    try:
        with state.lease(lease_seconds, MODERATION_LEASE):
            flag, previous = _bind_stop()
            try:
                if apply and sheet is not None:
                    try:
                        flush_sheet_log(state, sheet)
                    except PilotError as error:
                        log("past_sweep_sheet_retry", account=owner, error=str(error))
                return run_chunk(state, client, sheet, book, config, rule_ids, apply,
                                 deadline=time.monotonic() + work, limit=limit, stop=flag)
            finally:
                signal.signal(signal.SIGTERM, previous)
    except CloudError as error:
        if _busy(error):
            log("past_sweep_skipped", message="skipped: lease busy", account=owner, reason=str(error))
            return _result(config, apply, _blank_counts(), "skipped", False, [], _rule_key(rule_ids))
        raise


def _messages():
    return {
        "dry_run": "미리보기입니다. 댓글을 숨기지 않았습니다.",
        "complete": "이번 계정 점검을 마쳤습니다.",
        "paused": "시간이 되어 잠시 멈췄습니다. 같은 명령으로 다시 실행하면 이어서 합니다.",
        "rate_limited": "인스타 호출 제한으로 멈췄습니다. 같은 명령으로 다시 실행하면 이어서 합니다.",
        "yielded": "곧 30분 댓글 관리가 시작되어 이번 실행은 시작하지 않았습니다.",
        "skipped": "댓글 관리 작업이 잠금을 쓰는 중이라 이번 실행은 바로 물러났습니다.",
        "failed": "점검을 끝내지 못했습니다.",
    }


def _client_for(config, clients):
    from .moderation_sheet import account_tab
    if not isinstance(clients, dict):
        return None
    return clients.get(account_tab({
        "region": config.get("region_label") or "",
        "account_username": config.get("expected_username") or ""}))


def _load_book(settings, state):
    notion_mcp = None
    if rulebook_source(settings) == "notion":
        try:
            from .notion_moderation import _open_locked
            opened = _open_locked(settings, state)
            notion_mcp = opened[0] if opened else None
        except PilotError as error:
            log("past_sweep_rulebook_unavailable", error=str(error))
    return resolve_rulebook(settings, state, notion_mcp)


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="저장된 과거 댓글에 개수·단어 규칙을 다시 적용합니다. 기본은 미리보기입니다.")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--account", help="서울, 부산, 속초, 토론토 중 하나. 핸들이나 계정 아이디도 됩니다.")
    parser.add_argument("--rules", help="적용할 규칙. 예: D12 또는 D11+D14. 비우면 켜져 있는 개수·단어 규칙 전부.")
    parser.add_argument("--apply", action="store_true", help="이 플래그가 있을 때만 실제로 숨깁니다.")
    args = parser.parse_args(argv)
    apply = bool(args.apply) or os.environ.get("SNS_PAST_SWEEP_APPLY", "").strip() == "apply"
    account = args.account if isinstance(args.account, str) and args.account.strip() else os.environ.get("SNS_PAST_SWEEP_ACCOUNT", "")
    rules_text = args.rules if args.rules is not None else os.environ.get("SNS_PAST_SWEEP_RULES", "")
    rule_ids = parse_rule_ids(rules_text)
    try:
        settings = read_settings(args.config)
        state = CloudState(os.environ["SNS_STATE_BUCKET"])
        if not isinstance(account, str) or not account.strip():
            log("past_sweep_account", severity="WARNING",
                message="계정을 지정해주세요. 서울, 부산, 속초, 토론토 중 하나입니다. 숨기지 않았습니다.")
            return 0
        registry, _generation = state.json_get(REGISTRY_OBJECT)
        if not isinstance(registry, dict):
            registry = initial_registry(settings)
        configs = [config for config in runtime_configs(settings, registry) if config.get("account_id")]
        chosen = select_sweep_account(configs, account)
        if len(chosen) != 1:
            log("past_sweep_account", severity="WARNING", account=account,
                message="해당 계정을 하나만 찾지 못해 과거 댓글을 검사하지 않습니다.")
            return 0
        config = chosen[0]
        book = _load_book(settings, state)
        narrowed = book_for_rules(book, rule_ids)
        if not active_rules(narrowed):
            log("past_sweep_rules", severity="WARNING", rules=rule_ids or [],
                message="사용 중인 개수·단어 규칙이 없어 과거 댓글을 검사하지 않습니다.")
            return 0
        sheet = None
        client = None
        if apply:
            sheet = open_sheet(settings, state)
            if sheet is None:
                log("past_sweep_sheet_retry", account=config.get("expected_username"),
                    message="시트를 지금 열지 못했습니다. 숨김 기록은 대기열에 남기고 다음 실행에서 다시 씁니다.")
            client = _client_for(config, account_clients(settings, state))
            if client is None:
                log("past_sweep_failed", severity="ERROR", account=config.get("expected_username"),
                    error="계정 토큰을 찾지 못했습니다.")
                return 1
        result = guarded_run(state, client, sheet, book, config, rule_ids, apply)
        log("past_sweep_finished", message=_messages().get(result["status"], ""),
            policy=policy_sha(book), **{key: value for key, value in result.items() if key != "accounts"})
        return 1 if result["status"] == "failed" else 0
    except PilotError as error:
        log("past_sweep_failed", severity="ERROR", error=str(error))
        return 1
    except KeyError:
        log("past_sweep_failed", severity="ERROR", error="클라우드 환경 변수를 확인해주세요.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
