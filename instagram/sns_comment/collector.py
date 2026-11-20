"""Account-scoped single-post collection with explicit partial failure."""

import re
import urllib.parse

from .api import PilotError, next_cursor, page_data


COMMENT_FIELDS = "id,text,timestamp,username,from,like_count"
PARENT_FIELDS = COMMENT_FIELDS + ",replies.limit(50){" + COMMENT_FIELDS + "}"


def post_key(url):
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in {"instagram.com", "www.instagram.com"} or parsed.port:
            raise ValueError
        match = re.fullmatch(r"/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)/?", parsed.path)
        if not match:
            raise ValueError
        return match[1]
    except ValueError:
        raise PilotError("정상적인 https Instagram 게시물 또는 릴스 URL을 입력해주세요.") from None


def canonical_url(url):
    key = post_key(url)
    kind = urllib.parse.urlsplit(url).path.strip("/").split("/")[0]
    kind = "reel" if kind == "reels" else kind
    return f"https://www.instagram.com/{kind}/{key}/"


class Collector:
    def __init__(self, client, store, expected_username="waterbomb_official", max_media_pages=20):
        self.client = client
        self.store = store
        self.owner = expected_username.lstrip("@")
        self.max_media_pages = max_media_pages

    def pages(self, path, fields, initial=None, after=None, max_pages=None):
        seen = set()
        count = 0
        while True:
            if after in seen:
                raise PilotError("페이지 커서가 반복됩니다. 부분 실패로 종료합니다.")
            seen.add(after)
            if max_pages is not None and count >= max_pages:
                raise PilotError("게시물 검색 페이지 상한에 도달했습니다. 검색 범위 안에서 대상 게시물을 찾지 못했습니다.")
            params = {"fields": fields, "limit": 50}
            if after is not None:
                params["after"] = after
            payload = initial if initial is not None else self.client.get(path, **params)
            initial = None
            items = page_data(payload)
            following = next_cursor(payload)
            yield items, following
            count += 1
            if following is None:
                break
            after = following

    def account(self, account_id=None):
        if getattr(self.client, "api_host", None) == "graph.instagram.com":
            # Instagram Login tokens identify one Instagram account directly.
            # Its ID namespace must come from this API, never a Facebook ID.
            account = self.client.get("me", fields="user_id,username")
            identifier = account.get("user_id")
            if (not isinstance(identifier, str) or not identifier.isdigit()
                    or not isinstance(account.get("username"), str)
                    or account["username"].casefold() != self.owner.casefold()
                    or (account_id is not None and account_id != identifier)):
                raise PilotError("Instagram 토큰의 계정이 선택한 @아이디·계정 ID와 다릅니다. 해당 계정 행에서 생성한 토큰을 입력해주세요.")
            return identifier
        if account_id is None:
            matches = []
            for items, _ in self.pages("me/assigned_instagram_accounts", "id,username"):
                matches.extend(item for item in items if isinstance(item.get("username"), str)
                               and item["username"].casefold() == self.owner.casefold())
            ids = {item.get("id") for item in matches}
            if len(ids) != 1:
                raise PilotError("연결 계정 목록에서 대상 계정을 확정하지 못했습니다. 토큰의 자산 할당과 로그인 방식을 확인해주세요.")
            account_id = ids.pop()
        if not isinstance(account_id, str) or not account_id.isdigit():
            raise PilotError("숫자로 된 Instagram 계정 ID를 확인해주세요.")
        account = self.client.get(account_id, fields="id,username")
        if account.get("id") != account_id or not isinstance(account.get("username"), str) or account["username"].casefold() != self.owner.casefold():
            raise PilotError("조회된 계정이 설정된 공식 계정과 일치하지 않습니다. 수집을 중단합니다.")
        return account_id

    def find_media(self, account_id, post_url):
        wanted = post_key(post_url)
        for items, _ in self.pages(f"{account_id}/media", "id,permalink,caption,timestamp", max_pages=self.max_media_pages):
            for item in items:
                if not isinstance(item.get("id"), str) or not item["id"].isdigit():
                    raise PilotError("게시물 응답의 ID 형식이 올바르지 않습니다.")
                if "permalink" not in item:
                    raise PilotError("게시물 응답에 permalink가 없습니다.")
                if post_key(item["permalink"]) == wanted:
                    return item
        raise PilotError("대상 계정의 API 게시물 목록에서 지정한 URL을 찾지 못했습니다. 소유 계정·접근 권한·게시물 상태를 확인해주세요.")

    def collect(self, post_url, account_id=None):
        post_url = canonical_url(post_url)
        self.store.mark_interrupted()
        run_id = self.store.start_run(post_url)
        saved = False
        try:
            account_id = self.account(account_id)
            self.store.stage(run_id, "media", account_id=account_id)
            media = self.find_media(account_id, post_url)
            media_id = media["id"]
            self.store.save_media(account_id, media)
            self.store.stage(run_id, "comments", media_id=media_id)
            for parents, after in self.pages(f"{media_id}/comments", PARENT_FIELDS):
                self.store.save_page(run_id, account_id, media_id, "comments", parents, after, self.owner)
                saved = True
                for parent in parents:
                    edge = f"replies:{parent['id']}"
                    self.store.stage(run_id, edge)
                    initial = parent.get("replies")
                    if initial is not None and not isinstance(initial, dict):
                        raise PilotError("답글 확장 응답의 형식이 올바르지 않습니다.")
                    # A missing expansion is not evidence that there are no replies.
                    for replies, reply_after in self.pages(f"{parent['id']}/replies", COMMENT_FIELDS, initial=initial):
                        self.store.save_page(run_id, account_id, media_id, edge, replies, reply_after,
                                             self.owner, parent_id=parent["id"])
                    self.store.stage(run_id, "comments")
            self.store.stage(run_id, "done")
            self.store.finish(run_id, "complete", self.client.request_count)
            return {"run_id": run_id, "status": "complete", "account_id": account_id,
                    "media_id": media_id, "api_requests": self.client.request_count,
                    "stored": self.store.summary(account_id, media_id)}
        except BaseException as error:
            safe = str(error) if isinstance(error, PilotError) else "사용자 중단" if isinstance(error, KeyboardInterrupt) else "내부 처리 실패: 저장된 원본을 유지했습니다."
            self.store.finish(run_id, "partial" if saved else "failed", self.client.request_count, safe)
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise PilotError(f"{safe} 실행 기록: {run_id}") from None
