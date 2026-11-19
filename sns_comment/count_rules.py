"""Apply Notion rows that carry a 조건 종류. Rows without one stay content rules.

A bad condition skips that row and logs a warning. The job keeps running.
Count rules include official accounts. K3 still applies only when no count rule matched.

같은 대상 짧은 시간 몰림
----------------------
Each comment is normalized before a target is read. The text is casefolded.
An @mention is taken out first: dots and underscores are removed, then a trailing
"official" is removed, so @x.in_official, X:IN, xin, and Xin🙌 all become xin.
Emoji, punctuation, and symbols (including : . _ !) are deleted rather than
turned into spaces, so X:IN stays one token. Spaces separate tokens. Filler
words (please, pls, plz, bring, its, time, for, and, we, need, this, invite,
yes, omg) and the leftover word "official" are not targets. A one-letter
piece is dropped. "x in" with a space is not joined into xin.

An empty 대상 column counts every extracted target. A filled 대상 column counts
only those targets, after the same normalization.

The rule is per post. For one target on one post, comments are ordered by
timestamp, then id. A sliding window is the comments whose newest timestamp is
at most 시간(분) minutes after the oldest (the boundary is included). When that
window holds more than 숫자 comments, every comment after the first 숫자 in
that window is hidden. A later comment is kept again once every window that
contains it is back to 숫자 or fewer. The first 숫자 comments of a burst stay
even if a later overlapping window also covers them. Replies on the same post
count. Different authors count, including an official account, and the wording
does not have to match beyond the target. A comment with no timestamp or no
target is not part of a window. 숫자 defaults to 10 and 시간(분) defaults to
60 when the cell is empty. This condition does not accept 계정 전체.

특정 단어 포함
--------------
Each 대상 entry is a word to block. The comment text is folded first: NFKD without
combining marks and format characters, then NFKC and casefold, so full-width
ｘ:ｉｎ, bold or circled letters, and Cyrillic look-alikes become plain letters.
An entry made only of ASCII letters and digits (x:in, @x.in_official, xin) is
matched loosely: its letters and digits are read in order, each may repeat
(XINNN), and up to 8 spaces, punctuation, symbols, or emoji may sit between them
(X : IN, X🔥I.N). The match must stand on its own: a letter or digit right before
or after it, or a handle continuing after an underscore (xin_yi), stops it, so
maxine, xinyi, and @xin_yi are not matched, while xin_official, @x.in_official,
#xin, and "XIN이" are. Hangul between the letters also stops it. Any other entry
(for example Korean) is a plain substring test of the casefolded text, as before.
Official-account comments (is_owner = 1) are never matched by this kind.

An entry written as 계정:토론토 (or 계정:toronto, or an account id) does not block
a word. It limits the whole row to that account: the row applies only to comments
of that account. The account is known by its id, its handle, a handle piece such
as toronto, or the Korean label 토론토. If the account is unknown, a row with a
계정 limit matches nothing.
"""

import datetime as dt
import re
import unicodedata
from collections import deque

from .moderation import HIDE_LABEL, KEEP_LABEL, REVIEW_LABEL, _decision, emoji_only, plain


AUTHOR_COUNT = "author_count"
REPEAT_TEXT = "repeat_text"
KEYWORD = "keyword"
AUTHOR_ID = "author_id"
BURST = "burst"
KINDS = {
    "같은 작성자 댓글 수 초과": AUTHOR_COUNT,
    "같은 작성자 같은 글 반복": REPEAT_TEXT,
    "특정 단어 포함": KEYWORD,
    "특정 작성자 아이디": AUTHOR_ID,
    "같은 대상 짧은 시간 몰림": BURST,
}
SCOPES = {"게시물별": "post", "계정 전체": "account"}
FILLER = frozenset({
    "please", "pls", "plz", "bring", "its", "time", "for", "and",
    "we", "need", "this", "invite", "yes", "omg", "official",
})
DEFAULT_BURST_COUNT = 10
DEFAULT_BURST_MINUTES = 60


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


def _canonical_piece(raw):
    piece = "".join(ch for ch in raw.casefold() if ch.isalnum())
    if piece.endswith("official") and len(piece) > len("official"):
        piece = piece[: -len("official")]
    if len(piece) < 2 or piece in FILLER:
        return ""
    return piece


_MENTION = re.compile(r"@([0-9a-z._]+)")


def comment_targets(text):
    """Artist-like tokens and @mentions, folded so a handle and a name can match."""
    if text is None:
        return []
    if isinstance(text, list):
        found = []
        for item in text:
            for token in comment_targets(item):
                if token not in found:
                    found.append(token)
        return found
    if not isinstance(text, str):
        text = str(text)
    if not text.strip():
        return []
    folded = unicodedata.normalize("NFC", text).casefold()
    folded = folded.replace(",", " ").replace("、", " ").replace("\n", " ")
    found = []

    def add(token):
        if token and token not in found:
            found.append(token)

    for match in _MENTION.finditer(folded):
        add(_canonical_piece(match.group(1)))
    rest = _MENTION.sub(" ", folded)
    cleaned = []
    for ch in rest:
        if ch.isalnum():
            cleaned.append(ch)
        elif ch.isspace():
            cleaned.append(" ")
    for word in "".join(cleaned).split():
        add(_canonical_piece(word))
    return found


_ACCOUNT_PREFIX = re.compile(r"^\s*계정\s*[:：]\s*(.+?)\s*$")
_ALIASES = {"토론토": "toronto", "toronto": "토론토"}
_LOOK_ALIKES = str.maketrans({"\u0445": "x", "\u0456": "i", "\u0131": "i", "\u04cf": "i", "\u0578": "n"})
_LOOSE_GAP = r"[^a-z0-9\u1100-\u11ff\u3130-\u318f\uac00-\ud7a3]{0,8}"
_LOOSE_CACHE = {}


def fold_text(text):
    """Make look-alike spellings of the same letters equal: full-width, bold, circled, accented, zero-width."""
    if text is None:
        return ""
    raw = unicodedata.normalize("NFKD", str(text))
    kept = "".join(ch for ch in raw if unicodedata.category(ch) not in {"Mn", "Cf"})
    return unicodedata.normalize("NFKC", kept).casefold().translate(_LOOK_ALIKES)


def _ascii_word(value):
    folded = fold_text(value)
    letters = [ch for ch in folded if ch.isalnum()]
    if letters and all(ch.isascii() for ch in letters):
        return "".join(letters)
    return ""


def loose_pattern(word):
    """Regex for one blocked word: letters in order, repeats and symbol gaps allowed, standing alone."""
    letters = _ascii_word(word)
    if not letters:
        return None
    pattern = _LOOSE_CACHE.get(letters)
    if pattern is None:
        body = _LOOSE_GAP.join(re.escape(ch) + "+" for ch in letters)
        pattern = re.compile(r"(?<![a-z0-9])(?<![a-z0-9]_)" + body + r"(?![a-z0-9])(?!_(?!official)[a-z0-9])")
        _LOOSE_CACHE[letters] = pattern
    return pattern


def keyword_hit(text, targets):
    if not isinstance(text, str) or not text.strip():
        return False
    folded = None
    for target in targets or []:
        pattern = loose_pattern(target)
        if pattern is not None:
            if folded is None:
                folded = fold_text(text)
            if pattern.search(folded):
                return True
        elif target and target in plain(text):
            return True
    return False


def keyword_targets(value):
    """Return (words, accounts). 계정:xxx entries limit the row; every other entry is a word."""
    if value is None:
        return [], []
    if isinstance(value, list):
        words, accounts = [], []
        for item in value:
            more_words, more_accounts = keyword_targets(item)
            words += [w for w in more_words if w not in words]
            accounts += [a for a in more_accounts if a not in accounts]
        return words, accounts
    words, accounts = [], []
    text = value if isinstance(value, str) else str(value)
    for part in text.replace("、", ",").replace("\n", ",").split(","):
        found = _ACCOUNT_PREFIX.match(unicodedata.normalize("NFC", part))
        if found:
            token = fold_text(found.group(1)).strip().lstrip("@")
            if token and token not in accounts:
                accounts.append(token)
            continue
        item = unicodedata.normalize("NFC", part).strip()
        if not item:
            continue
        item = fold_text(item).strip() if _ascii_word(item) else plain(item)
        if item and item not in words:
            words.append(item)
    return words, accounts


def account_allowed(rule, account):
    wanted = rule.get("accounts") or []
    if not wanted:
        return True
    if not isinstance(account, dict):
        return False
    username = fold_text(account.get("username") or "").lstrip("@")
    have = {str(account.get("id") or ""), username, fold_text(account.get("region") or "").strip()}
    have.update(piece for piece in username.split("_") if piece)
    have.update(_ALIASES[item] for item in list(have) if item in _ALIASES)
    have.discard("")
    return any(token in have for token in wanted)


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
    if kind == KEYWORD:
        words, accounts = keyword_targets(row.get("대상") if isinstance(row, dict) else None)
        if not words:
            raise ConditionSkip(code, code + " 대상이 없어 그 행은 건너뜁니다.")
        return {"kind": kind, "threshold": threshold, "scope": scope or "post", "targets": words,
                "accounts": accounts}
    if kind == BURST:
        if threshold is None:
            raw_number = row.get("숫자") if isinstance(row, dict) else None
            blank = raw_number is None or isinstance(raw_number, bool) or (
                isinstance(raw_number, str) and not raw_number.strip())
            if not blank:
                raise ConditionSkip(code, code + " 숫자가 없어 그 행은 건너뜁니다.")
            threshold = DEFAULT_BURST_COUNT
        elif threshold < 1:
            raise ConditionSkip(code, code + " 숫자가 없어 그 행은 건너뜁니다.")
        minutes = _whole_number(row.get("시간(분)") if isinstance(row, dict) else None)
        raw_minutes = row.get("시간(분)") if isinstance(row, dict) else None
        minutes_blank = raw_minutes is None or isinstance(raw_minutes, bool) or (
            isinstance(raw_minutes, str) and not raw_minutes.strip())
        if minutes is None and not minutes_blank:
            raise ConditionSkip(code, code + " 시간(분)이 없어 그 행은 건너뜁니다.")
        if minutes is not None and minutes < 1:
            raise ConditionSkip(code, code + " 시간(분)이 없어 그 행은 건너뜁니다.")
        if scope_label and not scope:
            raise ConditionSkip(code, code + " 범위가 없어 그 행은 건너뜁니다.")
        if scope == "account":
            raise ConditionSkip(code, code + " 범위는 게시물별만 쓸 수 있어 그 행은 건너뜁니다.")
        listed = comment_targets(row.get("대상") if isinstance(row, dict) else None)
        raw_targets = row.get("대상") if isinstance(row, dict) else None
        if parse_targets(raw_targets) and not listed:
            raise ConditionSkip(code, code + " 대상을 읽지 못해 그 행은 건너뜁니다.")
        return {"kind": kind, "threshold": threshold, "scope": "post",
                "targets": listed, "minutes": minutes if minutes is not None else DEFAULT_BURST_MINUTES}
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


def _whole_number(value):
    if isinstance(value, bool) or value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def active_rules(book):
    """Condition rows plus the existing D12 row until its 조건 종류 is filled in."""
    from .author_cap import author_cap_enabled
    rules = []
    for item in book.get("conditions") or []:
        if isinstance(item, dict) and item.get("kind") in set(KINDS.values()):
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


def _matches(rule, comment, comments, earlier, account=None):
    if not isinstance(comment, dict) or not comment.get("written_at"):
        return False
    kind = rule.get("kind")
    if kind == KEYWORD:
        if comment.get("is_owner") == 1 or not account_allowed(rule, account):
            return False
        return keyword_hit(str(comment.get("text") or ""), rule.get("targets") or [])
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


def _instant(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _burst_rows(comments, earlier):
    rows = {}

    def add(item, media):
        if not isinstance(item, dict) or not media:
            return
        identifier = str(item.get("id") or item.get("comment_id") or "")
        when = _instant(item.get("written_at") or "")
        if not identifier or when is None:
            return
        text = item.get("text") if isinstance(item.get("text"), str) else ""
        current = rows.get(identifier)
        if current is not None and current["text"] and not text:
            return
        rows[identifier] = {"id": identifier, "media": media, "written_at": when,
                            "text": text, "sort": (when.isoformat(), identifier)}

    for item in comments or []:
        add(item, _media(item))
    for slot, entries in (earlier or {}).items():
        media, _, _author = str(slot).partition("\t")
        if not media.isdigit():
            continue
        for item in entries or []:
            add(item, media)
    return list(rows.values())


def burst_hidden_ids(comments, earlier, rule):
    """Ids that fall after the first 숫자 comments in an over-full time window."""
    allowed = set(rule.get("targets") or [])
    minutes = float(rule.get("minutes") if rule.get("minutes") is not None else DEFAULT_BURST_MINUTES)
    threshold = int(rule.get("threshold") if rule.get("threshold") is not None else DEFAULT_BURST_COUNT)
    if minutes < 1 or threshold < 1:
        return set()
    grouped = {}
    for row in _burst_rows(comments, earlier):
        for target in comment_targets(row["text"]):
            if allowed and target not in allowed:
                continue
            grouped.setdefault((row["media"], target), []).append(row)
    hidden = set()
    span = minutes * 60
    for rows in grouped.values():
        rows.sort(key=lambda item: item["sort"])
        window = deque()
        for row in rows:
            window.append(row)
            while window and (row["written_at"] - window[0]["written_at"]).total_seconds() > span:
                window.popleft()
            if len(window) > threshold:
                for extra in list(window)[threshold:]:
                    hidden.add(extra["id"])
    return hidden


def structural_decisions(comments, earlier, book, account=None):
    """First matching condition rule, in 순서. Later rules do not override it.

    account is {"id", "username", "region"} of the account the comments belong to. It is only
    read by rows that carry a 계정 limit."""
    rules = active_rules(book)
    if not rules:
        return {}
    prepared = []
    for rule in rules:
        if rule.get("kind") == BURST:
            prepared.append((rule, burst_hidden_ids(comments, earlier, rule)))
        else:
            prepared.append((rule, None))
    decided = {}
    ordered = sorted(comments or [], key=lambda row: (str(row.get("written_at") or ""), str(row.get("id") or "")))
    for comment in ordered:
        if not isinstance(comment, dict) or not comment.get("id"):
            continue
        for rule, hidden in prepared:
            if hidden is not None:
                matched = str(comment.get("id")) in hidden
            else:
                try:
                    matched = _matches(rule, comment, ordered, earlier, account)
                except ValueError:
                    matched = False
            if matched:
                decided[comment["id"]] = _choice(rule)
                break
    return decided
