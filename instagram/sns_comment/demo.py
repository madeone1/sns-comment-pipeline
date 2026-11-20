"""Synthetic API only; this module never makes network requests."""

import copy

from .api import PilotError


def comment(comment_id, text, written_at, username="demo_audience", author_id="9001", replies=None):
    result = {"id": comment_id, "text": text, "timestamp": written_at, "username": username,
              "from": {"id": author_id, "username": username}, "like_count": 0}
    if replies is not None:
        result["replies"] = replies
    return result


class DemoGraph:
    """Two parent pages, two reply pages, an old parent, and an official reply."""

    def __init__(self):
        self.request_count = 0
        self.responses = {
            ("me/assigned_instagram_accounts", None): {"data": [{"id": "100", "username": "waterbomb_official"}]},
            ("100", None): {"id": "100", "username": "waterbomb_official"},
            ("100/media", None): {"data": [{"id": "200", "permalink": "https://www.instagram.com/reel/DfCKxEYlZVv/",
                                           "caption": "합성 검증용 게시물. 실제 Instagram 자료가 아닙니다.", "timestamp": "2025-01-01T00:00:00Z"}]},
            ("200/comments", None): {"data": [comment("300", "[합성] 오래된 부모 댓글", "2025-01-01T00:00:00Z", replies={
                "data": [comment("400", "[합성] 새 답글", "2026-11-04T00:10:00Z")],
                "paging": {"next": "https://graph.facebook.com/300/replies?after=reply2"}})],
                "paging": {"next": "https://graph.facebook.com/200/comments?after=page2"}},
            ("300/replies", "reply2"): {"data": [comment("401", "[합성] 공식 계정 답글", "2026-11-04T00:20:00Z", "waterbomb_official", "9999")]},
            ("200/comments", "page2"): {"data": [
                comment("301", "[합성] 같은 문구", "2026-11-03T23:10:00Z", replies={"data": []}),
                comment("302", "[합성] 같은 문구", "2026-11-03T23:20:00Z", "demo_other", "9002", replies={"data": []})]},
        }

    def get(self, path, **params):
        self.request_count += 1
        key = (path, params.get("after"))
        if key not in self.responses:
            raise PilotError("합성 API에 요청한 응답이 없습니다.")
        value = self.responses[key]
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)


class AccountDemoGraph(DemoGraph):
    """Multiple catalog pages and old/new media, with seven-day boundary cases."""

    def __init__(self):
        super().__init__()
        self.responses[("100/media", None)]["paging"] = {"next": "https://graph.facebook.com/100/media?after=media2"}
        self.responses[("100/media", "media2")] = {"data": [
            {"id": "201", "permalink": "https://www.instagram.com/p/SyntheticNew/", "timestamp": "2026-11-03T00:00:00Z"},
            {"id": "202", "permalink": "https://www.instagram.com/p/SyntheticOld/", "timestamp": "2024-01-01T00:00:00Z"},
        ]}
        self.responses[("201/comments", None)] = {"data": [comment("500", "[합성] 다른 게시물 댓글", "2026-11-04T01:00:00Z",
            replies={"data": [comment("501", "[합성] 기간 시작 포함", "2026-10-28T23:00:00Z"),
                              comment("502", "[합성] 기간 종료 미포함", "2026-11-04T23:00:00Z")]})]}
        self.responses[("202/comments", None)] = {"data": []}
