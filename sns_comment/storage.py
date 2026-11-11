"""Durable raw storage; a page and its checkpoint commit atomically."""

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid

from .api import PilotError


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def timestamp(value):
    if not isinstance(value, str):
        raise PilotError("댓글 작성 시각이 없습니다. 해당 페이지를 완료 처리하지 않습니다.")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(dt.timezone.utc).isoformat()
    except ValueError:
        raise PilotError("시간대가 있는 댓글 작성 시각을 확인해야 합니다.") from None


@contextlib.contextmanager
def run_lock(db_path):
    path = Path(str(db_path) + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PilotError("같은 DB의 수집 작업이 이미 실행 중입니다.") from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.path)
        os.chmod(self.path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
          id TEXT PRIMARY KEY, account_id TEXT, post_url TEXT NOT NULL,
          media_id TEXT, started_at TEXT NOT NULL, ended_at TEXT,
          status TEXT NOT NULL, stage TEXT NOT NULL, error TEXT,
          requests INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS media (
          account_id TEXT NOT NULL, id TEXT NOT NULL, permalink TEXT NOT NULL,
          caption TEXT, written_at TEXT, raw_json TEXT NOT NULL,
          last_completed_at TEXT, PRIMARY KEY(account_id,id)
        );
        CREATE TABLE IF NOT EXISTS comments (
          account_id TEXT NOT NULL, id TEXT NOT NULL, media_id TEXT NOT NULL,
          parent_id TEXT, author_id TEXT, username TEXT, is_owner INTEGER,
          text TEXT, written_at TEXT NOT NULL,
          first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
          payload_hash TEXT NOT NULL, raw_json TEXT NOT NULL,
          PRIMARY KEY(account_id,id),
          FOREIGN KEY(account_id,media_id) REFERENCES media(account_id,id)
        );
        CREATE TABLE IF NOT EXISTS observations (
          account_id TEXT NOT NULL, comment_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
          observed_at TEXT NOT NULL, raw_json TEXT NOT NULL,
          PRIMARY KEY(account_id,comment_id,payload_hash)
        );
        CREATE TABLE IF NOT EXISTS checkpoints (
          account_id TEXT NOT NULL, media_id TEXT NOT NULL, edge TEXT NOT NULL,
          run_id TEXT NOT NULL, next_cursor TEXT, complete INTEGER NOT NULL,
          saved_at TEXT NOT NULL, PRIMARY KEY(account_id,media_id,edge)
        );
        CREATE INDEX IF NOT EXISTS comments_period ON comments(account_id,written_at);
        CREATE TABLE IF NOT EXISTS collection_scopes (
          id TEXT PRIMARY KEY, account_id TEXT NOT NULL, owner TEXT NOT NULL,
          window_start TEXT NOT NULL, window_end TEXT NOT NULL,
          catalog_cursor TEXT, catalog_complete INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT 'pending', started_at TEXT NOT NULL,
          updated_at TEXT NOT NULL, completed_at TEXT, error TEXT,
          requests INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS scopes_window ON collection_scopes(account_id,window_start,window_end,started_at);
        CREATE TABLE IF NOT EXISTS scope_media (
          scope_id TEXT NOT NULL, account_id TEXT NOT NULL, media_id TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending', error TEXT, completed_at TEXT,
          PRIMARY KEY(scope_id,media_id),
          FOREIGN KEY(scope_id) REFERENCES collection_scopes(id),
          FOREIGN KEY(account_id,media_id) REFERENCES media(account_id,id)
        );
        CREATE TABLE IF NOT EXISTS scope_edges (
          scope_id TEXT NOT NULL, media_id TEXT NOT NULL, edge TEXT NOT NULL,
          parent_id TEXT, next_cursor TEXT, complete INTEGER NOT NULL DEFAULT 0,
          saved_at TEXT NOT NULL, PRIMARY KEY(scope_id,media_id,edge),
          FOREIGN KEY(scope_id,media_id) REFERENCES scope_media(scope_id,media_id)
        );
        CREATE INDEX IF NOT EXISTS scope_pending_edges ON scope_edges(scope_id,media_id,complete,edge);
        """)
        self._allow_unavailable_text()
        if "scope_id" not in {r["name"] for r in self.db.execute("PRAGMA table_info(runs)")}:
            self.db.execute("ALTER TABLE runs ADD COLUMN scope_id TEXT")
            self.db.commit()
        scope_columns = {r["name"] for r in self.db.execute("PRAGMA table_info(collection_scopes)")}
        if "media_policy" not in scope_columns:
            self.db.execute("ALTER TABLE collection_scopes ADD COLUMN media_policy TEXT NOT NULL DEFAULT 'all_accessible'")
        if "discovery_exhaustive" not in scope_columns:
            self.db.execute("ALTER TABLE collection_scopes ADD COLUMN discovery_exhaustive INTEGER NOT NULL DEFAULT 0")
        if "selection_reason" not in {r["name"] for r in self.db.execute("PRAGMA table_info(scope_media)")}:
            self.db.execute("ALTER TABLE scope_media ADD COLUMN selection_reason TEXT NOT NULL DEFAULT 'all_accessible'")
        self.db.execute("""CREATE TABLE IF NOT EXISTS tracked_media (
            account_id TEXT NOT NULL,media_id TEXT NOT NULL,last_activity_at TEXT NOT NULL,
            source TEXT NOT NULL,first_detected_at TEXT NOT NULL,last_detected_at TEXT NOT NULL,
            PRIMARY KEY(account_id,media_id))""")
        self.db.commit()
        self.db.execute("CREATE INDEX IF NOT EXISTS comments_first_seen ON comments(account_id,media_id,first_seen_at)")

    def _allow_unavailable_text(self):
        legacy = any(row["name"] == "text" and row["notnull"]
                     for row in self.db.execute("PRAGMA table_info(comments)"))
        if not legacy:
            return
        # SQLite cannot remove NOT NULL in place. Keep the rebuild atomic and
        # preserve every stored field, key, observation and media relationship.
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("""CREATE TABLE comments_optional_text (
              account_id TEXT NOT NULL, id TEXT NOT NULL, media_id TEXT NOT NULL,
              parent_id TEXT, author_id TEXT, username TEXT, is_owner INTEGER,
              text TEXT, written_at TEXT NOT NULL,
              first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
              payload_hash TEXT NOT NULL, raw_json TEXT NOT NULL,
              PRIMARY KEY(account_id,id),
              FOREIGN KEY(account_id,media_id) REFERENCES media(account_id,id)
            )""")
            self.db.execute("INSERT INTO comments_optional_text SELECT * FROM comments")
            self.db.execute("DROP TABLE comments")
            self.db.execute("ALTER TABLE comments_optional_text RENAME TO comments")
            self.db.execute("CREATE INDEX comments_period ON comments(account_id,written_at)")

    def close(self):
        self.db.close()

    def start_run(self, post_url):
        run_id = uuid.uuid4().hex
        with self.db:
            self.db.execute(
                "INSERT INTO runs(id,post_url,started_at,status,stage) VALUES(?,?,?,'running','account')",
                (run_id, post_url, now()),
            )
        return run_id

    def stage(self, run_id, stage, account_id=None, media_id=None):
        with self.db:
            self.db.execute("""UPDATE runs SET stage=?,account_id=COALESCE(?,account_id),
                            media_id=COALESCE(?,media_id) WHERE id=?""",
                            (stage, account_id, media_id, run_id))

    def save_media(self, account_id, media):
        with self.db:
            self.db.execute("""INSERT INTO media(account_id,id,permalink,caption,written_at,raw_json)
                VALUES(?,?,?,?,?,?) ON CONFLICT(account_id,id) DO UPDATE SET
                permalink=excluded.permalink,caption=excluded.caption,
                written_at=excluded.written_at,raw_json=excluded.raw_json""",
                (account_id, media["id"], media["permalink"], media.get("caption"),
                 media.get("timestamp"), json.dumps(media, ensure_ascii=False, sort_keys=True)))

    def save_page(self, run_id, account_id, media_id, edge, items, after, owner, parent_id=None, scope_id=None):
        seen = now()
        with self.db:
            for item in items:
                text = item.get("text")
                if not isinstance(item.get("id"), str) or not item["id"].isdigit() or (text is not None and not isinstance(text, str)):
                    raise PilotError("댓글 ID 또는 원문 형식이 올바르지 않습니다. 해당 페이지는 저장하지 않습니다.")
                raw = {key: value for key, value in item.items() if key != "replies"}
                body = json.dumps(raw, ensure_ascii=False, sort_keys=True)
                digest = hashlib.sha256(body.encode()).hexdigest()
                author = item.get("from") if isinstance(item.get("from"), dict) else {}
                username = item.get("username") or author.get("username")
                if not isinstance(username, str):
                    username = None
                author_id = author.get("id")
                if not isinstance(author_id, str):
                    author_id = None
                is_owner = int(username.casefold() == owner.casefold()) if username else None
                self.db.execute("""INSERT INTO comments VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(account_id,id) DO UPDATE SET parent_id=excluded.parent_id,
                    author_id=excluded.author_id,username=excluded.username,is_owner=excluded.is_owner,
                    text=excluded.text,written_at=excluded.written_at,last_seen_at=excluded.last_seen_at,
                    payload_hash=excluded.payload_hash,raw_json=excluded.raw_json""",
                    (account_id, item["id"], media_id, parent_id, author_id, username, is_owner,
                     text, timestamp(item.get("timestamp")), seen, seen, digest, body))
                self.db.execute("INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?)",
                                (account_id, item["id"], digest, seen, body))
            self.db.execute("""INSERT INTO checkpoints VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(account_id,media_id,edge) DO UPDATE SET run_id=excluded.run_id,
                next_cursor=excluded.next_cursor,complete=excluded.complete,saved_at=excluded.saved_at""",
                (account_id, media_id, edge, run_id, after, int(after is None), seen))
            if scope_id:
                self.db.execute("""INSERT INTO scope_edges VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(scope_id,media_id,edge) DO UPDATE SET next_cursor=excluded.next_cursor,
                    complete=excluded.complete,saved_at=excluded.saved_at""",
                    (scope_id, media_id, edge, parent_id, after, int(after is None), seen))
                if parent_id is None:
                    for item in items:
                        self.db.execute("INSERT OR IGNORE INTO scope_edges VALUES(?,?,?,?,NULL,0,?)",
                                        (scope_id, media_id, "replies:" + item["id"], item["id"], seen))

    def finish(self, run_id, status, request_count, error=None):
        ended = now()
        with self.db:
            self.db.execute("UPDATE runs SET status=?,ended_at=?,requests=?,error=? WHERE id=?",
                            (status, ended, request_count, error, run_id))
            if status == "complete":
                self.db.execute("""UPDATE media SET last_completed_at=? WHERE (account_id,id)=
                    (SELECT account_id,media_id FROM runs WHERE id=? AND scope_id IS NULL)""", (ended, run_id))

    def mark_interrupted(self):
        with self.db:
            self.db.execute("""UPDATE runs SET status='interrupted',ended_at=?,
                error='이전 실행 중단: 저장된 원본을 유지하고 대상 범위를 다시 조회' WHERE status='running'""", (now(),))

    def collection_scope(self, account_id, owner, start, end, restart=False, media_policy="all_accessible"):
        if not restart:
            row = self.db.execute("""SELECT * FROM collection_scopes
                WHERE account_id=? AND owner=? AND window_start=? AND window_end=? AND media_policy=?
                ORDER BY started_at DESC,id DESC LIMIT 1""", (account_id, owner, start, end, media_policy)).fetchone()
            if row:
                return dict(row)
        scope_id, at = uuid.uuid4().hex, now()
        with self.db:
            self.db.execute("""INSERT INTO collection_scopes
                (id,account_id,owner,window_start,window_end,started_at,updated_at,media_policy)
                VALUES(?,?,?,?,?,?,?,?)""", (scope_id, account_id, owner, start, end, at, at, media_policy))
        return self.scope(scope_id)

    def scope(self, scope_id):
        row = self.db.execute("SELECT * FROM collection_scopes WHERE id=?", (scope_id,)).fetchone()
        if row is None:
            raise PilotError("저장된 기간 수집 작업을 찾지 못했습니다.")
        return dict(row)

    def attach_scope(self, run_id, scope_id):
        with self.db:
            self.db.execute("UPDATE runs SET scope_id=? WHERE id=?", (scope_id, run_id))
            self.db.execute("UPDATE collection_scopes SET status='running',updated_at=?,error=NULL WHERE id=? AND status!='complete'",
                            (now(), scope_id))

    def save_catalog_page(self, scope_id, account_id, items, after):
        at = now()
        with self.db:
            for item in items:
                self.db.execute("""INSERT INTO media(account_id,id,permalink,caption,written_at,raw_json)
                    VALUES(?,?,?,?,?,?) ON CONFLICT(account_id,id) DO UPDATE SET
                    permalink=excluded.permalink,caption=excluded.caption,
                    written_at=excluded.written_at,raw_json=excluded.raw_json""",
                    (account_id, item["id"], item["permalink"], item.get("caption"), item.get("timestamp"),
                     json.dumps(item, ensure_ascii=False, sort_keys=True)))
                self.db.execute("INSERT OR IGNORE INTO scope_media(scope_id,account_id,media_id) VALUES(?,?,?)",
                                (scope_id, account_id, item["id"]))
                self.db.execute("INSERT OR IGNORE INTO scope_edges VALUES(?,?,'comments',NULL,NULL,0,?)",
                                (scope_id, item["id"], at))
            self.db.execute("UPDATE collection_scopes SET catalog_cursor=?,catalog_complete=?,updated_at=? WHERE id=?",
                            (after, int(after is None), at, scope_id))

    def pending_scope_media(self, scope_id):
        return [dict(r) for r in self.db.execute("""SELECT s.*,m.permalink FROM scope_media s
            JOIN media m ON m.account_id=s.account_id AND m.id=s.media_id
            WHERE s.scope_id=? AND s.status!='complete' ORDER BY m.written_at DESC,s.media_id""", (scope_id,))]

    def reuse_completed_media(self, scope_id):
        """Reuse evidence for the same fixed comment interval, retaining origin."""
        scope=self.scope(scope_id)
        with self.db:
            for row in self.pending_scope_media(scope_id):
                previous=self.db.execute('''SELECT s.id,sm.completed_at FROM scope_media sm
                    JOIN collection_scopes s ON s.id=sm.scope_id WHERE sm.account_id=? AND sm.media_id=?
                    AND sm.status='complete' AND s.id!=? AND s.window_start<=? AND s.window_end>=?
                    AND s.media_policy!='recent_comments'
                    ORDER BY sm.completed_at DESC LIMIT 1''',
                    (scope['account_id'],row['media_id'],scope_id,scope['window_start'],scope['window_end'])).fetchone()
                if previous:
                    self.db.execute('UPDATE scope_edges SET complete=1 WHERE scope_id=? AND media_id=?',(scope_id,row['media_id']))
                    self.db.execute('''UPDATE scope_media SET status='complete',error=NULL,completed_at=?,selection_reason=?
                        WHERE scope_id=? AND media_id=?''',(previous['completed_at'],'covered_by_scope:'+previous['id'],scope_id,row['media_id']))

    def track_media(self, account_id, media_id, activity_at, source="manual"):
        if source not in {"manual", "observed_comment", "verified_webhook"}:
            raise PilotError("이전 게시물 추적의 발견 출처를 확인해주세요.")
        if not isinstance(media_id, str) or not media_id.isdigit():
            raise PilotError("숫자로 된 게시물 ID가 필요합니다.")
        activity_at = timestamp(activity_at)
        at = now()
        with self.db:
            self.db.execute("""INSERT INTO tracked_media VALUES(?,?,?,?,?,?)
                ON CONFLICT(account_id,media_id) DO UPDATE SET
                last_activity_at=MAX(tracked_media.last_activity_at,excluded.last_activity_at),
                source=CASE WHEN excluded.last_activity_at>=tracked_media.last_activity_at
                    THEN excluded.source ELSE tracked_media.source END,last_detected_at=excluded.last_detected_at""",
                (account_id,media_id,activity_at,source,at,at))

    def tracked_media(self, account_id):
        return [dict(r) for r in self.db.execute("SELECT * FROM tracked_media WHERE account_id=? ORDER BY last_activity_at DESC", (account_id,))]

    def missing_tracked_media(self, account_id, start):
        return [dict(r) for r in self.db.execute("""SELECT t.* FROM tracked_media t LEFT JOIN media m
            ON m.account_id=t.account_id AND m.id=t.media_id
            WHERE t.account_id=? AND t.last_activity_at>=? AND m.id IS NULL""", (account_id,start))]

    def recent_targets(self, account_id, start, end):
        # A local index lookup over cached metadata does not query 1,340 posts.
        start, end = timestamp(start), timestamp(end)
        with self.db:
            signals = self.db.execute("""SELECT media_id,MAX(written_at) activity_at FROM comments
                WHERE account_id=? AND written_at>=? AND written_at<? GROUP BY media_id""", (account_id,start,end)).fetchall()
            for row in signals:
                self.track_media(account_id,row["media_id"],row["activity_at"],"observed_comment")
        tracked = {r["media_id"]:r for r in self.tracked_media(account_id) if r["last_activity_at"]>=start}
        selected = []
        for row in self.db.execute("SELECT * FROM media WHERE account_id=? ORDER BY written_at DESC,id", (account_id,)):
            item = dict(row)
            published = timestamp(item["written_at"])
            if start <= published < end:
                item["selection_reason"] = "recent_post"
            elif published < start and item["id"] in tracked:
                item["selection_reason"] = "tracked_activity"
            else:
                continue
            selected.append(item)
        return selected

    def prepare_recent_scope(self, scope_id, discovery_exhaustive=False):
        scope, at = self.scope(scope_id), now()
        targets = self.recent_targets(scope["account_id"], scope["window_start"], scope["window_end"])
        reason = "recent_post" if scope["media_policy"] == "recent_posts" else "tracked_activity"
        targets = [item for item in targets if item["selection_reason"] == reason]
        with self.db:
            for item in targets:
                self.db.execute("""INSERT OR IGNORE INTO scope_media
                    (scope_id,account_id,media_id,selection_reason) VALUES(?,?,?,?)""",
                    (scope_id,scope["account_id"],item["id"],item["selection_reason"]))
                self.db.execute("INSERT OR IGNORE INTO scope_edges VALUES(?,?,'comments',NULL,NULL,0,?)", (scope_id,item["id"],at))
            self.db.execute("""UPDATE collection_scopes SET catalog_complete=1,catalog_cursor=NULL,
                discovery_exhaustive=?,updated_at=? WHERE id=?""", (int(discovery_exhaustive),at,scope_id))
        return targets

    def scope_media_ids(self, scope_id, reason=None):
        return [r[0] for r in self.db.execute("SELECT media_id FROM scope_media WHERE scope_id=? AND (? IS NULL OR selection_reason=?) ORDER BY media_id", (scope_id,reason,reason))]

    def scope_edge(self, scope_id, media_id, edge):
        row = self.db.execute("SELECT * FROM scope_edges WHERE scope_id=? AND media_id=? AND edge=?",
                              (scope_id, media_id, edge)).fetchone()
        if row is None:
            raise PilotError("기간 수집의 페이지 처리 위치가 없습니다.")
        return dict(row)

    def pending_scope_edges(self, scope_id, media_id):
        return [dict(r) for r in self.db.execute("""SELECT * FROM scope_edges
            WHERE scope_id=? AND media_id=? AND complete=0
            ORDER BY CASE WHEN edge='comments' THEN 1 ELSE 0 END,parent_id""", (scope_id,media_id))]

    def pending_scope_reply(self, scope_id, media_id):
        row = self.db.execute("""SELECT * FROM scope_edges WHERE scope_id=? AND media_id=?
            AND edge!='comments' AND complete=0 ORDER BY parent_id LIMIT 1""", (scope_id, media_id)).fetchone()
        return dict(row) if row else None

    def finish_scope_media(self, scope_id, media_id, status, error=None):
        at = now()
        with self.db:
            if status == "complete" and self.db.execute("SELECT COUNT(*) FROM scope_edges WHERE scope_id=? AND media_id=? AND complete=0",
                (scope_id, media_id)).fetchone()[0]:
                raise PilotError("댓글·답글 중 미완료 페이지가 있어 게시물을 완료 처리할 수 없습니다.")
            self.db.execute("UPDATE scope_media SET status=?,error=?,completed_at=? WHERE scope_id=? AND media_id=?",
                            (status, error, at if status == "complete" else None, scope_id, media_id))
            if status == "complete":
                self.db.execute("""UPDATE media SET last_completed_at=? WHERE (account_id,id)=
                    (SELECT account_id,media_id FROM scope_media WHERE scope_id=? AND media_id=?)""", (at, scope_id, media_id))

    def finish_scope(self, scope_id, requests, error=None):
        progress = self.scope_progress(scope_id)
        complete = progress["catalog_complete"] and progress["media_pending"] == 0 and error is None
        at = now()
        with self.db:
            self.db.execute("""UPDATE collection_scopes SET status=?,updated_at=?,completed_at=?,
                requests=requests+?,error=? WHERE id=?""", ("complete" if complete else "partial", at,
                at if complete else None, requests, error, scope_id))
        return self.scope_progress(scope_id)

    def scope_progress(self, scope_id):
        scope = self.scope(scope_id)
        row = self.db.execute("""SELECT COUNT(*) AS media_discovered,
            COALESCE(SUM(status='complete'),0) AS media_completed,
            COALESCE(SUM(status!='complete'),0) AS media_pending FROM scope_media WHERE scope_id=?""", (scope_id,)).fetchone()
        return {**scope, **dict(row), "catalog_complete": bool(scope["catalog_complete"]),
                "media_total_known": bool(scope["catalog_complete"])}

    def account_scopes(self, account_id=None):
        return [dict(r) for r in self.db.execute("""SELECT * FROM collection_scopes
            WHERE (? IS NULL OR account_id=?) ORDER BY started_at DESC,id DESC LIMIT 20""", (account_id, account_id))]

    def summary(self, account_id=None, media_id=None, start=None, end=None, media_ids=None):
        clauses, params = [], []
        for column, op, value in (("account_id", "=", account_id), ("media_id", "=", media_id),
                                   ("written_at", ">=", start), ("written_at", "<", end)):
            if value is not None:
                clauses.append(f"{column}{op}?")
                params.append(value)
        if media_ids is not None:
            if media_ids:
                clauses.append("media_id IN (" + ",".join("?" for _ in media_ids) + ")")
                params.extend(media_ids)
            else:
                clauses.append("0=1")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        row = self.db.execute("""SELECT COUNT(*) AS stored_comments_and_replies,
            COALESCE(SUM(text IS NOT NULL),0) AS text_available,
            COALESCE(SUM(text IS NULL),0) AS text_unavailable,
            COALESCE(SUM(parent_id IS NULL),0) AS top_level_comments,
            COALESCE(SUM(parent_id IS NOT NULL),0) AS replies,
            COALESCE(SUM(is_owner=1),0) AS official_account,
            COALESCE(SUM(is_owner=0),0) AS known_audience,
            COALESCE(SUM(is_owner IS NULL),0) AS owner_unknown,
            COUNT(DISTINCT CASE WHEN is_owner=0 THEN author_id END) AS known_audience_author_ids
            FROM comments""" + where, params).fetchone()
        return dict(row)

    def runs(self, account_id=None):
        return [dict(row) for row in self.db.execute("SELECT * FROM runs WHERE (? IS NULL OR account_id=?) ORDER BY started_at DESC LIMIT 10", (account_id, account_id))]

    def comments(self, limit=20, account_id=None):
        return [dict(row) for row in self.db.execute("""SELECT id,media_id,parent_id,username,
            text,written_at,first_seen_at,last_seen_at FROM comments
            WHERE (? IS NULL OR account_id=?) ORDER BY written_at DESC LIMIT ?""", (account_id, account_id, limit))]
