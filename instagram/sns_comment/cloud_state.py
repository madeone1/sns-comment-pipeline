"""Cloud Storage state and one lock file per writer.

state/lease.json is only the daily collection and recover lock. It covers
runs/*, state/comments.sqlite3, state/reports.json, pipeline/*, the connection
registry, and daily-merge/*. The moderation job does not take it.

state/moderation-lease.json covers moderation/decisions/*, moderation/repeat-index/*,
moderation/pending-hides/*, moderation/author-posts/*, moderation/author-cap/*,
moderation/past-sweep/*, moderation/rulebook-cache.json, moderation/daily/*,
and moderation/sheet-pending.json. The morning job takes this lock only while it
classifies, so it cannot write those objects at the same time as the 10-minute job.
The on-demand past-comment sweep takes the same lock and releases it before the
next half-hour moderation slot.
Every write also sends the GCS generation it read.

state/notion-moderation-lease.json covers moderation/notion-decision-database.json
and the daily comment toggle. state/notion-summary-lease.json stops two summary
runs. state/notion-oauth-lease.json covers only the Notion refresh-token write.

state/sheet-hide-lease.json and state/sheet-review-lease.json cover one rulebook
tab each. Numbering and the insert happen inside that lock, then the lock is released.
"""

import contextlib
import datetime as dt
import json
import re
from pathlib import Path
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .api import NoRedirect, PilotError, https_context


DAILY_LEASE = "state/lease.json"
MODERATION_LEASE = "state/moderation-lease.json"
NOTION_MODERATION_LEASE = "state/notion-moderation-lease.json"
NOTION_SUMMARY_LEASE = "state/notion-summary-lease.json"
NOTION_OAUTH_LEASE = "state/notion-oauth-lease.json"
SHEET_HIDE_LEASE = "state/sheet-hide-lease.json"
SHEET_REVIEW_LEASE = "state/sheet-review-lease.json"


class CloudError(PilotError):
    def __init__(self, label, status=0):
        self.status = status
        super().__init__(f"{label} (HTTP {status})")


class GoogleTransport:
    def __init__(self, opener=None):
        self.opener = opener or urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=https_context()))
        self._token, self._expires = None, 0

    def token(self):
        if self._expires < time.time() + 60:
            req = urllib.request.Request("http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
                                         headers={"Metadata-Flavor": "Google"})
            try:
                with self.opener.open(req, timeout=10) as response:
                    data = json.load(response)
                self._token, self._expires = data["access_token"], time.time() + data["expires_in"]
            except (OSError, ValueError, KeyError):
                raise CloudError("Cloud Run 서비스 계정 인증 실패") from None
        return self._token

    def request(self, method, url, data=None, content_type="application/json"):
        host = urllib.parse.urlsplit(url).hostname or ""
        if not url.startswith("https://") or not (host.endswith(".googleapis.com") or host == "storage.googleapis.com"):
            raise CloudError("허용되지 않은 클라우드 API")
        req = urllib.request.Request(url, data=data,
            headers={"Authorization": "Bearer " + self.token(), "Content-Type": content_type}, method=method)
        try:
            with self.opener.open(req, timeout=120) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            raise CloudError("클라우드 API 요청 실패", error.code) from None
        except OSError:
            raise CloudError("클라우드 통신 실패") from None

    def json(self, method, url, value=None):
        raw = self.request(method, url, json.dumps(value, ensure_ascii=False).encode() if value is not None else None)
        return json.loads(raw) if raw else {}


class CloudState:
    def __init__(self, bucket, transport=None, prefix=""):
        self.bucket = bucket
        self.transport = transport or GoogleTransport()
        if prefix and not re.fullmatch(r"accounts/(?:[a-z0-9_][a-z0-9_.]{0,29}|ig_[0-9]{5,30})", prefix):
            raise CloudError("계정별 저장 경로 형식을 확인해주세요")
        self.prefix = prefix + "/" if prefix else ""

    def url(self, name, **query):
        return f"https://storage.googleapis.com/storage/v1/b/{self.bucket}/o/{urllib.parse.quote(self.prefix + name, safe='')}" + ("?" + urllib.parse.urlencode(query) if query else "")

    def get(self, name):
        try:
            meta = self.transport.json("GET", self.url(name))
            raw = self.transport.request("GET", self.url(name, alt="media", generation=meta["generation"]))
            return raw, int(meta["generation"])
        except CloudError as error:
            if error.status == 404:
                return None, 0
            raise

    def put(self, name, raw, generation=None):
        query = {"uploadType": "media", "name": self.prefix + name}
        if generation is not None:
            query["ifGenerationMatch"] = generation
        url = f"https://storage.googleapis.com/upload/storage/v1/b/{self.bucket}/o?" + urllib.parse.urlencode(query)
        result = self.transport.request("POST", url, raw, "application/octet-stream")
        return int(json.loads(result)["generation"])

    def json_get(self, name, default=None):
        raw, generation = self.get(name)
        return (json.loads(raw) if raw is not None else default), generation

    def json_put(self, name, value, generation=None):
        return self.put(name, json.dumps(value, ensure_ascii=False).encode(), generation)

    def list_objects(self, prefix, limit=100):
        """One bounded queue snapshot; never traverse the raw-data bucket."""
        items, token = self.list_page(prefix, limit, None)
        return items, bool(token)

    def list_page(self, prefix, limit=50, page_token=None, start_offset=None):
        """One page of names. A start offset is the last name already handled, exclusive."""
        query = {'prefix': self.prefix + prefix, 'maxResults': max(1, int(limit)),
                 'fields': 'items(name,generation),nextPageToken'}
        if page_token:
            query['pageToken'] = page_token
        if start_offset:
            query['startOffset'] = self.prefix + start_offset + "\n"
        value = self.transport.json('GET', 'https://storage.googleapis.com/storage/v1/b/' + self.bucket + '/o?'
                                    + urllib.parse.urlencode(query))
        items = [{'name': row['name'][len(self.prefix):], 'generation': int(row['generation'])}
                 for row in value.get('items', [])]
        token = value.get('nextPageToken')
        return items, token if isinstance(token, str) and token else None

    def list_all(self, prefix, page_size=200):
        items, token = [], None
        while True:
            query = {'prefix': self.prefix + prefix, 'maxResults': page_size,
                     'fields': 'items(name,generation),nextPageToken'}
            if token:
                query['pageToken'] = token
            value = self.transport.json('GET', 'https://storage.googleapis.com/storage/v1/b/' + self.bucket + '/o?'
                                        + urllib.parse.urlencode(query))
            for row in value.get('items', []):
                items.append({'name': row['name'][len(self.prefix):], 'generation': int(row['generation'])})
            token = value.get('nextPageToken')
            if not token:
                return items

    def delete(self, name, generation):
        self.transport.request('DELETE', self.url(name, ifGenerationMatch=generation))

    @contextlib.contextmanager
    def lease(self, seconds=3900, name="state/lease.json"):
        if not re.fullmatch(r"state/[a-z0-9-]{1,40}\.json", name):
            raise CloudError("잠금 파일 이름을 확인해주세요")
        owner = uuid.uuid4().hex
        current, generation = self.json_get(name, {})
        if current.get("expires_at", 0) > time.time():
            raise CloudError("다른 클라우드 실행이 진행 중입니다")
        try:
            generation = self.json_put(name, {"owner": owner, "expires_at": time.time() + seconds}, generation)
        except CloudError as error:
            if error.status == 412:
                raise CloudError("다른 실행이 먼저 잠금을 획득했습니다") from None
            raise
        try:
            yield
        finally:
            try:
                self.transport.request("DELETE", self.url(name, ifGenerationMatch=generation))
            except CloudError:
                # Never delete a newer lease; expiry permits later recovery.
                pass

    def restore_database(self, path):
        raw, generation = self.get("state/comments.sqlite3")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if raw is not None:
            path.write_bytes(raw)
        return generation

    def save_database(self, store, generation):
        target = store.path.with_suffix(".snapshot.sqlite3")
        with sqlite3.connect(target) as connection:
            store.db.backup(connection)
        try:
            return self.put("state/comments.sqlite3", target.read_bytes(), generation)
        finally:
            target.unlink(missing_ok=True)


def _lock_busy(error):
    text = str(error)
    return "진행 중" in text or "잠금" in text


@contextlib.contextmanager
def lease_retry(state, seconds=3900, name=DAILY_LEASE, attempts=4, wait_seconds=5, sleeper=time.sleep):
    """Take a named lock. A busy lock is retried; an error inside the lock is not."""
    last = None
    for attempt in range(max(1, int(attempts))):
        manager = state.lease(seconds, name)
        try:
            manager.__enter__()
        except CloudError as error:
            if not _lock_busy(error) or attempt + 1 >= attempts:
                raise
            last = error
            sleeper(wait_seconds)
            continue
        try:
            yield
        finally:
            manager.__exit__(*sys.exc_info())
        return
    raise last
