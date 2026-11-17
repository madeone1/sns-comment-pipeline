"""Apply Notion rows that carry a 조건 종류. Rows without one stay content rules.

A bad condition skips that row and logs a warning. The job keeps running.
Count rules include official accounts. K3 still applies only when no count rule matched.
"""

from .moderation import HIDE_LABEL, KEEP_LABEL, REVIEW_LABEL, _decision, emoji_only, plain


AUTHOR_COUNT = "author_count"
REPEAT_TEXT = "repeat_text"
KEYWORD = "keyword"
AUTHOR_ID = "author_id"
KINDS = {
    "같은 작성자 댓글 수 초과": AUTHOR_COUNT,
    "같은 작성자 같은 글 반복": REPEAT_TEXT,
    "특정 단어 포함": KEYWORD,
    "특정 작성자 아이디": AUTHOR_ID,
}
SCOPES = {"게시물별": "post", "계정 전체": "account"}


class ConditionSkip(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(message)


def parse_targets(value):
    if value is None:
        return []
    if isinstance(value, list):
        parts = []
        for item in value:
            parts.extend(parse_targets(item))
        return parts
    text = value if isinstance(value, str) else str(value)
    found = []
    for part in text.replace("、", ",").replace("\n", ",").split(","):
        item = plain(part)
        if item and item not in found:
            found.append(item)
    return found


def parse_condition(row, code):
    """Return a condition dict, None for a normal content rule, or raise ConditionSkip."""
    raw = row.get("조건 종류") if isinstance(row, dict) else None
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    label = raw.strip() if isinstance(raw, str) else str(raw).strip()
    kind = KINDS.get(label)
    if not kind:
        raise ConditionSkip(code, code + " 조건 종류를 알 수 없어 그 행은 건너뜁니다: " + label)
    number = row.get("숫자") if isinstance(row, dict) else None
    threshold = None
    if isinstance(number, bool) or number is None or (isinstance(number, str) and not number.strip()):
        threshold = None
    else:
        try:
            threshold = int(float(number))
        except (TypeError, ValueError):
            threshold = None
    scope_raw = row.get("범위") if isinstance(row, dict) else None
    scope_label = scope_raw.strip() if isinstance(scope_raw, str) else ""
    scope = SCOPES.get(scope_label, "")
    targets = parse_targets(row.get("대상") if isinstance(row, dict) else None)
    if kind in {AUTHOR_COUNT, REPEAT_TEXT}:
        if threshold is None or threshold < 1:
            raise ConditionSkip(code, code + " 숫자가 없어 그 행은 건너뜁니다.")
        if kind == AUTHOR_COUNT and not scope:
            raise ConditionSkip(code, code + " 범위가 없어 그 행은 건너뜁니다.")
        if kind == REPEAT_TEXT and not scope:
            scope = "post"
    elif not targets:
        raise ConditionSkip(code, code + " 대상이 없어 그 행은 건너뜁니다.")
    return {"kind": kind, "threshold": threshold, "scope": scope or "post", "targets": targets}


def active_rules(book):
    """Condition rows plus the existing D12 row until its 조건 종류 is filled in."""
    from .author_cap import author_cap_enabled
    rules = []
    for item in book.get("conditions") or []:
        if isinstance(item, dict) and item.get("kind") in {AUTHOR_COUNT, REPEAT_TEXT, KEYWORD, AUTHOR_ID}:
            rules.append(dict(item))
    known = {item.get("id") for item in rules}
    if author_cap_enabled(book) and "D12" not in known:
        rules.append({"id": "D12", "label": "삭제", "kind": AUTHOR_COUNT, "threshold": 2,
                      "scope": "post", "targets": [], "order": 12,
                      "reason": "한 게시물에 같은 작성자의 3번째 댓글입니다."})
    rules.sort(key=lambda item: (item.get("order") if isinstance(item.get("order"), (int, float)) else 10000,
                                str(item.get("id") or "")))
    return rules


def repeat_rule_configured(book):
    return any(item.get("kind") == REPEAT_TEXT for item in active_rules(book))


def _author(comment):
    from .author_cap import author_key
    return author_key(comment)


def _media(comment):
    media = str(comment.get("media_id") or "") if isinstance(comment, dict) else ""
    return media if media.isdigit() else ""


def _identity(comment):
    return (_author(comment), _media(comment), str(comment.get("written_at") or ""), str(comment.get("id") or ""))


def _pool(comment, comments, earlier, scope, text=None):
    author = _author(comment)
    media = _media(comment)
    rows = []
    seen = set()

    def add(identifier, written, item_author, item_media, item_text):
        if not identifier or not isinstance(written, str) or not written or identifier in seen:
            return
        if item_author != author:
            return
        if scope == "post" and item_media != media:
            return
        if text is not None and plain(str(item_text or "")) != text:
            return
        seen.add(identifier)
        rows.append((written, identifier))

    for item in comments or []:
        if isinstance(item, dict):
            add(str(item.get("id") or ""), item.get("written_at") or "", _author(item), _media(item), item.get("text"))
    for slot, entries in (earlier or {}).items():
        slot_media, _, slot_author = str(slot).partition("\t")
        for item in entries or []:
            if not isinstance(item, dict):
                continue
            add(str(item.get("id") or item.get("comment_id") or ""), item.get("written_at") or "",
                slot_author, slot_media if slot_media.isdigit() else "", item.get("text"))
    rows.sort()
    return [identifier for _written, identifier in rows]


def _matches(rule, comment, comments, earlier):
    if not isinstance(comment, dict) or not comment.get("written_at"):
        return False
    kind = rule.get("kind")
    if kind == KEYWORD:
        body = plain(str(comment.get("text") or ""))
        return any(target and target in body for target in rule.get("targets") or [])
    if kind == AUTHOR_ID:
        author = plain(_author(comment).split(":", 1)[-1])
        username = plain(str(comment.get("username") or comment.get("author_username") or ""))
        targets = set(rule.get("targets") or [])
        return author in targets or username in targets
    if not _author(comment):
        return False
    if kind == AUTHOR_COUNT:
        if rule.get("scope") == "post" and not _media(comment):
            return False
        rank = _pool(comment, comments, earlier, rule.get("scope") or "post").index(str(comment.get("id"))) + 1
        return rank > int(rule.get("threshold") or 0)
    if kind == REPEAT_TEXT:
        text = plain(str(comment.get("text") or ""))
        if not text or emoji_only(str(comment.get("text") or "")):
            return False
        if rule.get("scope") == "post" and not _media(comment):
            return False
        rank = _pool(comment, comments, earlier, rule.get("scope") or "post", text=text).index(str(comment.get("id"))) + 1
        return rank >= int(rule.get("threshold") or 2)
    return False


def _choice(rule):
    label = rule.get("label")
    if label in {"삭제", "숨김"}:
        mapped = HIDE_LABEL
    elif label in {"사람판단", "사람 판단"}:
        mapped = REVIEW_LABEL
    else:
        mapped = KEEP_LABEL
    reason = rule.get("reason") or rule.get("name") or rule.get("id") or "조건 규칙"
    return _decision(mapped, rule.get("id") or "", str(reason)[:80], "high", "guard")


def structural_decisions(comments, earlier, book):
    """First matching condition rule, in 순서. Later rules do not override it."""
    rules = active_rules(book)
    if not rules:
        return {}
    decided = {}
    ordered = sorted(comments or [], key=lambda row: (str(row.get("written_at") or ""), str(row.get("id") or "")))
    for comment in ordered:
        if not isinstance(comment, dict) or not comment.get("id"):
            continue
        for rule in rules:
            try:
                matched = _matches(rule, comment, ordered, earlier)
            except ValueError:
                matched = False
            if matched:
                decided[comment["id"]] = _choice(rule)
                break
    return decided
