"""Verified comment notifications add individual media to the activity watchlist.

This is an ingestion function, not an installed HTTP server or a Meta
subscription. Notification delivery must be connected and tested separately.
"""

import hashlib
import hmac
import json
import re

from .api import PilotError
from .storage import now


def accept_comment_webhook(body, signature, app_secret, account_id, store):
    if not isinstance(body, bytes) or len(body) > 1_000_000 or not app_secret:
        raise PilotError("댓글 알림 본문·서명 설정을 확인해주세요.")
    if not isinstance(signature, str) or not re.fullmatch(r"sha256=[0-9a-fA-F]{64}", signature):
        raise PilotError("댓글 알림 서명을 확인해야 합니다.")
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature[7:].lower()):
        raise PilotError("댓글 알림 서명이 일치하지 않습니다.")
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise PilotError("댓글 알림 JSON 형식이 올바르지 않습니다.") from None
    if not isinstance(payload, dict) or payload.get("object") != "instagram" or not isinstance(payload.get("entry"), list):
        raise PilotError("Instagram 댓글 알림 형식을 확인해야 합니다.")
    media_ids = set()
    for entry in payload["entry"]:
        if not isinstance(entry, dict) or entry.get("id") != account_id:
            continue
        changes = entry.get("changes", [])
        if not isinstance(changes, list):
            raise PilotError("댓글 알림 변경 목록이 올바르지 않습니다.")
        for change in changes:
            if not isinstance(change, dict) or change.get("field") != "comments":
                continue
            value = change.get("value")
            media = value.get("media") if isinstance(value, dict) else None
            media_id = media.get("id") if isinstance(media, dict) else None
            if not isinstance(media_id, str) or not media_id.isdigit():
                raise PilotError("댓글 알림의 게시물 ID를 확인해야 합니다.")
            media_ids.add(media_id)
    at = now()
    # Arrival is an activity signal, not proof of the comment's written time.
    # The collector later reads that timestamp before counting in a report.
    for media_id in media_ids:
        store.track_media(account_id, media_id, at, "verified_webhook")
    return {"tracked_media": sorted(media_ids), "signal_basis": "verified_notification_received_at",
            "comment_body_saved": False, "instagram_modified": False}
