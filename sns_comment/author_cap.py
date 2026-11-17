"""Hide the 3rd and later comment by the same author on one post.

D12 is active only when the loaded rulebook contains it as a hide rule.
Unchecking 사용 in Notion drops the row, so the cap does not run.
K3 still keeps an official account's first two comments. D11 still hides an
exact repeat that is only the second comment.
"""

from .api import GraphError, PilotError, RequestBudgetExceeded, next_cursor, page_data
from .moderation import (
    HIDE_LABEL, _apply_hide, _has_written_at, _hidden_check, _human_kept, _put, _queue_sheet,
    _rate_limited, _record, decision_name, flush_sheet_log, plain,
)


CAP_RULE = "D12"
CAP_LIMIT = 2
INDEX_NAME = "moderation/author-posts/{account}.json"
SWEEP_NAME = "moderation/author-cap/{account}.json"
MEDIA_FIELDS = "id,permalink,timestamp"
COMMENT_FIELDS = "id,text,timestamp,username,from,hidden"


def author_cap_enabled(book):
    """True only when Notion (or the fallback file) still lists D12 as 숨김."""
    if not isinstance(book, dict):
        return False
    rule = next((item for item in book.get("rules") or []
                 if isinstance(item, dict) and item.get("id") == CAP_RULE), None)
    if not rule or rule.get("label") not in {"삭제", "숨김"}:
        return False
    if CAP_RULE in set(book.get("abolished") or []):
        return False
    hide_ids = book.get("hide_rule_ids")
    return not isinstance(hide_ids, list) or CAP_RULE in hide_ids


def author_key(comment):
    """One identity per person. from.id wins over a changed username."""
    author_id = comment.get("author_id") if isinstance(comment, dict) else None
    if isinstance(author_id, str) and author_id.strip():
        return "id:" + author_id.strip()
    username = None
    if isinstance(comment, dict):
        username = comment.get("username") if isinstance(comment.get("username"), str) else comment.get("author_username")
    if isinstance(username, str) and username.strip():
        return "name:" + plain(username)
    return ""


def post_slot(comment):
    media = str(comment.get("media_id") or "") if isinstance(comment, dict) else ""
    key = author_key(comment)
    if not media or not media.isdigit() or not key:
        return ""
    return media + "\t" + key


def _stamp(comment):
    written = comment.get("written_at") if isinstance(comment, dict) else ""
    identifier = comment.get("id") or comment.get("comment_id") or ""
    if not isinstance(written, str) or not written.strip() or not isinstance(identifier, str) or not identifier:
        return None
    stamped = {"id": identifier, "written_at": written}
    if isinstance(comment.get("text"), str):
        stamped["text"] = comment["text"]
    return stamped


def author_ranks(comments, earlier=None):
    """1-based position of each comment among that author's comments on one post."""
    groups = {}
    for comment in comments or []:
        slot = post_slot(comment)
        stamped = _stamp(comment)
        if not slot or not stamped:
            continue
        groups.setdefault(slot, [])
        if all(item["id"] != stamped["id"] for item in groups[slot]):
            groups[slot].append(stamped)
    for slot, items in (earlier or {}).items():
        if not isinstance(items, list):
            continue
        groups.setdefault(slot, [])
        for item in items:
            stamped = _stamp(item)
            if stamped and all(current["id"] != stamped["id"] for current in groups[slot]):
                groups[slot].append(stamped)
    ranks = {}
    for items in groups.values():
        items.sort(key=lambda item: (item["written_at"], item["id"]))
        for index, item in enumerate(items, start=1):
            ranks[item["id"]] = index
    return ranks


def cap_choice():
    return {"label": HIDE_LABEL, "rule_id": CAP_RULE, "confidence": "high",
            "reason": "한 게시물에 같은 작성자의 3번째 댓글입니다.", "source": "guard"}


def load_author_posts(state, account_id):
    current, generation = state.json_get(INDEX_NAME.format(account=account_id), {"entries": {}})
    if not isinstance(current, dict) or not isinstance(current.get("entries"), dict):
        current = {"entries": {}}
    return current, generation


def remember_author_posts(state, comments):
    """Remember every seen comment so a later run can count past the current batch."""
    grouped = {}
    for comment in comments or []:
        if not isinstance(comment, dict):
            continue
        account_id = comment.get("account_id")
        slot = post_slot(comment)
        stamped = _stamp(comment)
        if not isinstance(account_id, str) or not slot or not stamped:
            continue
        grouped.setdefault(account_id, {}).setdefault(slot, []).append(stamped)
    for account_id, slots in grouped.items():
        current, generation = load_author_posts(state, account_id)
        entries = current["entries"]
        changed = False
        for slot, items in slots.items():
            stored = list(entries.get(slot) or [])
            seen = {item.get("id") for item in stored if isinstance(item, dict)}
            for item in items:
                if item["id"] in seen:
                    continue
                stored.append(item)
                seen.add(item["id"])
                changed = True
            if changed:
                entries[slot] = stored
        if changed:
            current["entries"] = entries
            _put(state, INDEX_NAME.format(account=account_id), current, generation)


def posts_for_accounts(state, account_ids, extra=None):
    found = {}
    for account_id in account_ids:
        current, _generation = load_author_posts(state, account_id)
        for slot, items in current["entries"].items():
            found.setdefault(slot, [])
            found[slot].extend(item for item in items if isinstance(item, dict))
    for comment in extra or []:
        slot = post_slot(comment)
        stamped = _stamp(comment)
        if slot and stamped:
            found.setdefault(slot, []).append(stamped)
    return found


def _blank_sweep():
    return {"done": False, "media_after": "", "media": [], "media_id": "", "permalink": "",
            "comment_after": "", "comments": [], "reply_parent": "", "reply_after": "",
            "collected": [], "pending_hides": [], "counts": _blank_counts()}


def _blank_counts():
    return {"scanned": 0, "within_limit": 0, "would_hide": 0, "already": 0,
            "human_kept": 0, "human_review": 0, "hidden": 0, "hide_failed": 0}


def _comment(item, account_id, owner, media_id, parent_id, permalink):
    from .moderation_job import comment_from_payload
    return comment_from_payload(item, account_id, owner, media_id, parent_id=parent_id, permalink=permalink)


def _fetch_page(client, path, fields, after):
    params = {"fields": fields, "limit": 50}
    if after:
        params["after"] = after
    try:
        payload = client.get(path, **params)
    except (GraphError, RequestBudgetExceeded) as error:
        if isinstance(error, RequestBudgetExceeded) or _rate_limited(error):
            raise _Pause() from None
        raise
    return page_data(payload), next_cursor(payload)


class _Pause(Exception):
    pass


def _collect_media(client, account_id, owner, checkpoint):
    """Read one post's comments and replies. A rate limit leaves the cursor in place."""
    if checkpoint.get("pending_hides") or checkpoint.get("done") or checkpoint.get("ready"):
        return checkpoint
    while True:
        if not checkpoint.get("media_id"):
            if not checkpoint.get("media"):
                if checkpoint.get("media_seen") and not checkpoint.get("media_after"):
                    checkpoint["done"] = True
                    return checkpoint
                items, nxt = _fetch_page(client, account_id + "/media", MEDIA_FIELDS,
                                         checkpoint.get("media_after") or None)
                checkpoint["media_seen"] = True
                checkpoint["media"] = [{"id": item.get("id"), "permalink": item.get("permalink")}
                                       for item in items if isinstance(item.get("id"), str) and item["id"].isdigit()]
                checkpoint["media_after"] = nxt or ""
                if not checkpoint["media"]:
                    if not nxt:
                        checkpoint["done"] = True
                    return checkpoint
            current = checkpoint["media"].pop(0)
            checkpoint["media_id"] = current["id"]
            checkpoint["permalink"] = current.get("permalink") or ""
            checkpoint["comments"] = []
            checkpoint["comment_after"] = ""
            checkpoint["need_comments"] = True
            checkpoint["collected"] = []
            checkpoint["reply_parent"] = ""
            checkpoint["reply_after"] = ""
        if checkpoint.get("reply_parent"):
            items, nxt = _fetch_page(client, checkpoint["reply_parent"] + "/replies", COMMENT_FIELDS,
                                     checkpoint.get("reply_after") or None)
            for item in items:
                checkpoint["collected"].append(_comment(
                    item, account_id, owner, checkpoint["media_id"], checkpoint["reply_parent"],
                    checkpoint.get("permalink")))
            if nxt:
                checkpoint["reply_after"] = nxt
                continue
            checkpoint["reply_parent"] = ""
            checkpoint["reply_after"] = ""
        if checkpoint.get("need_comments") or checkpoint.get("comment_after") or checkpoint.get("comments"):
            if not checkpoint.get("comments"):
                items, nxt = _fetch_page(client, checkpoint["media_id"] + "/comments", COMMENT_FIELDS,
                                         checkpoint.get("comment_after") or None)
                checkpoint["comments"] = items
                checkpoint["comment_after"] = nxt or ""
                checkpoint["need_comments"] = False
            if checkpoint.get("comments"):
                item = checkpoint["comments"].pop(0)
                parent = _comment(item, account_id, owner, checkpoint["media_id"], None, checkpoint.get("permalink"))
                checkpoint["collected"].append(parent)
                if isinstance(parent.get("id"), str) and parent["id"].isdigit():
                    checkpoint["reply_parent"] = parent["id"]
                    checkpoint["reply_after"] = ""
                continue
            if checkpoint.get("comment_after"):
                continue
        checkpoint["ready"] = True
        return checkpoint


def _judge_account(checkpoint, state, client, sheet, book, account_id, owner, region, apply):
    from .count_rules import HIDE_LABEL, REVIEW_LABEL, structural_decisions
    comments = [item for item in checkpoint.get("all") or [] if isinstance(item, dict)]
    decisions = structural_decisions(comments, posts_for_accounts(state, [account_id]), book)
    remember_author_posts(state, comments)
    counts = checkpoint["counts"]
    hides, reviews = [], []
    for comment in comments:
        counts["scanned"] += 1
        choice = decisions.get(comment.get("id"))
        if not choice:
            counts["within_limit"] += 1
            continue
        existing, _generation = state.json_get(decision_name(account_id, comment["id"]))
        if _human_kept(existing):
            counts["human_kept"] += 1
            continue
        if choice["label"] == HIDE_LABEL and (comment.get("already_hidden") or
                                              (isinstance(existing, dict) and existing.get("status") == "hidden")):
            counts["already"] += 1
            continue
        marked = {**comment, "_rule_id": choice["rule_id"], "_reason": choice["reason"], "_label": choice["label"]}
        if choice["label"] == HIDE_LABEL:
            counts["would_hide"] += 1
            hides.append(marked)
        elif choice["label"] == REVIEW_LABEL:
            counts["human_review"] += 1
            reviews.append(marked)
        else:
            counts["within_limit"] += 1
    checkpoint["judged"] = True
    if not apply:
        return False
    checkpoint["pending_hides"] = hides
    checkpoint["pending_reviews"] = reviews
    _write_reviews(checkpoint, state, sheet, book, account_id, owner, region)
    return _hide_pending(checkpoint, state, client, sheet, book, account_id, owner, region)


def _write_reviews(checkpoint, state, sheet, book, account_id, owner, region):
    from .moderation import REVIEW_LABEL, policy_sha
    sha = policy_sha(book)
    for comment in checkpoint.get("pending_reviews") or []:
        existing, generation = state.json_get(decision_name(account_id, comment["id"]))
        choice = {"label": REVIEW_LABEL, "rule_id": comment.get("_rule_id") or "",
                  "reason": comment.get("_reason") or "", "confidence": "high", "source": "guard"}
        record = _record(comment, choice, owner, region, book, sha, "human_review", 0, extra={},
                         existing=existing if isinstance(existing, dict) else None)
        _put(state, decision_name(account_id, comment["id"]), record, generation)
        _queue_sheet(state, [record] if _has_written_at(record) else [])
        if sheet is not None:
            try:
                flush_sheet_log(state, sheet)
            except PilotError:
                pass
    checkpoint["pending_reviews"] = []


def _hide_pending(checkpoint, state, client, sheet, book, account_id, owner, region):
    from .moderation import policy_sha
    sha = policy_sha(book)
    counts = checkpoint["counts"]
    remaining = []
    paused = False
    for comment in checkpoint.get("pending_hides") or []:
        if paused:
            remaining.append(comment)
            continue
        existing, generation = state.json_get(decision_name(account_id, comment["id"]))
        choice = {"label": HIDE_LABEL, "rule_id": comment.get("_rule_id") or CAP_RULE,
                  "reason": comment.get("_reason") or cap_choice()["reason"],
                  "confidence": "high", "source": "guard"}
        status, extra = "hidden", {}
        attempts = int((existing or {}).get("hide_attempts") or 0) + 1 if isinstance(existing, dict) else 1
        try:
            _apply_hide(client, comment["id"])
            extra["hidden_check"] = _hidden_check(client, comment["id"])
            counts["hidden"] += 1
        except GraphError as error:
            if _rate_limited(error):
                paused = True
                remaining.append(comment)
                continue
            status = "hide_failed"
            extra["error"] = "숨김 요청 실패"
            counts["hide_failed"] += 1
        except PilotError as error:
            status = "hide_failed"
            extra["error"] = str(error)
            counts["hide_failed"] += 1
        if status == "hide_failed" and paused:
            continue
        record = _record(comment, choice, owner, region, book, sha, status, attempts, extra=extra,
                         existing=existing if isinstance(existing, dict) else None)
        _put(state, decision_name(account_id, comment["id"]), record, generation)
        _queue_sheet(state, [record] if _has_written_at(record) else [])
        if sheet is not None:
            try:
                flush_sheet_log(state, sheet)
            except PilotError:
                pass
    checkpoint["pending_hides"] = remaining
    return paused


def sweep_account_cap(config, state, client, settings, sheet, book, apply):
    account_id = config["account_id"]
    owner = config["expected_username"].lstrip("@")
    name = SWEEP_NAME.format(account=account_id)
    checkpoint, generation = state.json_get(name, None)
    if not isinstance(checkpoint, dict):
        checkpoint = _blank_sweep()
        generation = 0
    checkpoint.setdefault("counts", _blank_counts())
    paused = False
    try:
        while not checkpoint.get("done") or checkpoint.get("pending_hides"):
            if checkpoint.get("pending_hides"):
                paused = _hide_pending(checkpoint, state, client, sheet, book, account_id, owner,
                                       config.get("region_label"))
                if paused:
                    break
                continue
            checkpoint = _collect_media(client, account_id, owner, checkpoint)
            if checkpoint.get("ready"):
                checkpoint.setdefault("all", []).extend(checkpoint.get("collected") or [])
                checkpoint["collected"] = []
                checkpoint["media_id"] = ""
                checkpoint["ready"] = False
                continue
            if checkpoint.get("done") and not checkpoint.get("judged"):
                paused = _judge_account(checkpoint, state, client, sheet, book, account_id, owner,
                                        config.get("region_label"), apply)
                if paused:
                    break
                continue
            if checkpoint.get("done"):
                break
    except _Pause:
        paused = True
    _put(state, name, checkpoint, generation)
    counts = dict(checkpoint.get("counts") or _blank_counts())
    counts.update(account=owner, apply=apply, done=bool(checkpoint.get("done")) and not paused,
                  status="rate_limited" if paused else "complete")
    return counts


def sweep_author_cap(configs, state, clients, settings, sheet, book, apply):
    results = []
    for config in configs:
        from .moderation_sheet import account_tab
        client = clients.get(account_tab({"region": config.get("region_label") or "",
                                           "account_username": config.get("expected_username") or ""}))
        if client is None:
            results.append({"account": config.get("expected_username"), "status": "failed",
                            "error": "계정 토큰을 찾지 못했습니다.", **_blank_counts()})
            continue
        results.append(sweep_account_cap(config, state, client, settings, sheet, book, apply))
    return {"apply": apply, "accounts": results}
