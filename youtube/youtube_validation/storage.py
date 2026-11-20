"""Private, append-only run evidence and idempotent comment storage."""

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile

STATE = Path(__file__).resolve().parents[1] / '.local' / 'youtube-validation'


def private_dir(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('비공개 저장 경로에 심볼릭 링크를 사용할 수 없습니다.')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def write_json(path, value):
    path = Path(path)
    private_dir(path.parent)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.write-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path):
    with Path(path).open(encoding='utf-8') as stream:
        return json.load(stream)


def persist_run(state, summary, comments):
    """Keep observations per run; never remove comments missing from a later run."""
    state = private_dir(state)
    path = state / 'comments.sqlite3'
    if path.is_symlink():
        raise ValueError('DB 경로에 심볼릭 링크를 사용할 수 없습니다.')
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    path.chmod(0o600)
    channel, run_id = summary['channel']['id'], summary['run_id']
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY, summary_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS comments (
                channel_id TEXT NOT NULL, comment_id TEXT NOT NULL,
                first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY(channel_id, comment_id));
            CREATE TABLE IF NOT EXISTS observations (
                run_id TEXT NOT NULL, comment_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY(run_id, comment_id));
        ''')
        new_count = 0
        for row in comments:
            prior = db.execute('SELECT 1 FROM comments WHERE channel_id=? AND comment_id=?',
                               (channel, row['id'])).fetchone()
            new_count += prior is None
            payload = json.dumps(row, ensure_ascii=False)
            db.execute('''INSERT INTO comments VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(channel_id, comment_id) DO UPDATE SET
                last_seen=excluded.last_seen, payload_json=excluded.payload_json''',
                       (channel, row['id'], run_id, run_id, payload))
            db.execute('INSERT INTO observations VALUES (?, ?, ?)',
                       (run_id, row['id'], payload))
        summary['new_comment_ids'] = new_count
        summary['stored_comment_ids'] = db.execute(
            'SELECT COUNT(*) FROM comments WHERE channel_id=?', (channel,)).fetchone()[0]
        db.execute('INSERT INTO runs VALUES (?, ?)',
                   (run_id, json.dumps(summary, ensure_ascii=False)))
    run_dir = private_dir(state / 'runs' / run_id)
    write_json(run_dir / 'summary.json', summary)
    write_json(run_dir / 'comments.json', comments)
    write_json(state / 'latest.json', summary)
    return summary
