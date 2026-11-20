"""Conservative, versioned screening; counts and raw IDs are never discarded."""

import collections
import hashlib
import json
import unicodedata


VERSION = "2026-11-09-conservative-reactions-v1"
# Only recognizable reaction glyphs are cheap aggregate candidates. Unknown
# symbols, words, names, numbers and mixed text remain available for analysis.
REACTIONS = frozenset("❤♥🔥👏😍🥰😘💕💖💗💓💞💘💝💙💚💛💜🧡🤍🤎🖤🙌👍✨🎉💯😂🤣😁😆🤩🥳🤗💋🎊⭐🌟")
DECORATIONS = frozenset("\ufe0e\ufe0f\u200d\u20e3")


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def classify(row):
    if row.get("is_owner") == 1:
        return {"group": "official", "reason": "공식 계정 작성분", "pattern": ""}
    text = row.get("text")
    if text is None or not str(text).strip():
        return {"group": "hold", "reason": "본문 미제공" if text is None else "빈 본문·문맥 확인 필요", "pattern": ""}
    normalized = unicodedata.normalize("NFC", str(text))
    glyphs = [c for c in normalized if not c.isspace() and c not in DECORATIONS and not 0x1F3FB <= ord(c) <= 0x1F3FF]
    if glyphs and all(c in REACTIONS for c in glyphs):
        return {"group": "simple", "reason": "구체적 내용 없는 이모지 반응", "pattern": "".join(sorted(set(glyphs)))}
    return {"group": "analysis", "reason": "내용·의도·문맥 분석 대상", "pattern": ""}


def simple_annotation(row):
    return {"id": row["id"], "category": "콘텐츠 반응", "intent": "판단 보류", "sentiment": "판단 보류",
            "tags": ["단순 반응", "개별 AI 분석 생략"], "reason": "이모지 반응량으로 집계. 구체적 의견·감정·스팸 여부는 확정하지 않음.",
            "processing_group": "simple", "screening_version": VERSION}


def hold_annotation(row):
    return {'id':row['id'],'category':'기타','intent':'판단 보류','sentiment':'판단 보류',
            'tags':['본문·문맥 확인 필요'],'reason':classify(row)['reason'],
            'processing_group':'hold','screening_version':VERSION}


def summarize(rows):
    groups = {name: [] for name in ("simple", "analysis", "hold", "official")}
    buckets = collections.defaultdict(list)
    seen = set()
    for row in rows:
        if row["id"] in seen:
            raise ValueError("댓글 ID가 중복된 선별 입력입니다.")
        seen.add(row["id"])
        decision = classify(row)
        groups[decision["group"]].append(row)
        if decision["group"] == "simple":
            buckets[row.get("media_id", ""), decision["pattern"]].append(row)
    reactions = []
    for (media_id, pattern), values in sorted(buckets.items()):
        reactions.append({"media_id": media_id, "pattern": pattern, "comments": len(values),
                          "known_authors": len({r["author_id"] for r in values if r.get("author_id")}),
                          "unknown_authors": sum(not r.get("author_id") for r in values)})
    counts = {name: len(values) for name, values in groups.items()}
    return {"version": VERSION, "counts": counts, "reaction_groups": reactions,
            "comment_ids": {name: [r["id"] for r in values] for name, values in groups.items()}}


def persist(connection, account_id, rows):
    connection.execute("""CREATE TABLE IF NOT EXISTS comment_screenings (
        account_id TEXT NOT NULL, comment_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
        policy_version TEXT NOT NULL, processing_group TEXT NOT NULL, reason TEXT NOT NULL,
        PRIMARY KEY(account_id,comment_id,payload_hash,policy_version))""")
    with connection:
        for row in rows:
            value = classify(row)
            connection.execute("INSERT OR REPLACE INTO comment_screenings VALUES(?,?,?,?,?,?)",
                               (account_id, row["id"], row.get("payload_hash", fingerprint(row)), VERSION, value["group"], value["reason"]))
    return summarize(rows)
