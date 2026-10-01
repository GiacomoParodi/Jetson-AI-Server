"""Database SQLite: utenti, sessioni e lavori."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from .config import settings

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    task TEXT NOT NULL,
    params TEXT NOT NULL,
    input_name TEXT,
    status TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    result TEXT,
    error TEXT,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS jobs_user ON jobs(user_id, created_at);
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    with _lock:
        conn = _connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def init() -> None:
    settings.ensure_dirs()
    with connection() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)


# ---------------------------------------------------------------- impostazioni

def get_setting(key: str) -> str | None:
    with connection() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key: str, value: str | None) -> None:
    with connection() as conn:
        if value is None:
            conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))
        else:
            conn.execute("INSERT INTO app_settings (key, value) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


# ---------------------------------------------------------------- utenti

def create_user(username: str, password_hash: str, is_admin: bool) -> int:
    with connection() as conn:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, is_admin, created_at) VALUES (?, ?, ?, ?)",
            (username, password_hash, int(is_admin), time.time()),
        )
        return int(cur.lastrowid)


def get_user(user_id: int) -> sqlite3.Row | None:
    with connection() as conn:
        return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_user_by_name(username: str) -> sqlite3.Row | None:
    with connection() as conn:
        return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def list_users() -> list[sqlite3.Row]:
    with connection() as conn:
        return conn.execute("SELECT * FROM users ORDER BY username").fetchall()


def count_users() -> int:
    with connection() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])


def count_admins() -> int:
    with connection() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1").fetchone()[0])


def set_password(user_id: int, password_hash: str) -> None:
    with connection() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def delete_user(user_id: int) -> None:
    with connection() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


# ---------------------------------------------------------------- sessioni

def create_session(token_hash: str, user_id: int, expires_at: float) -> None:
    with connection() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
            (token_hash, user_id, expires_at),
        )


def get_session_user(token_hash: str) -> sqlite3.Row | None:
    with connection() as conn:
        return conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = ? AND s.expires_at > ?",
            (token_hash, time.time()),
        ).fetchone()


def delete_session(token_hash: str) -> None:
    with connection() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))


# ---------------------------------------------------------------- lavori

def _job_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["params"] = json.loads(d["params"])
    d["result"] = json.loads(d["result"]) if d["result"] else None
    return d


def create_job(user_id: int, task: str, params: dict[str, Any], input_name: str | None,
               status: str = "queued") -> str:
    job_id = uuid.uuid4().hex
    with connection() as conn:
        conn.execute(
            "INSERT INTO jobs (id, user_id, task, params, input_name, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (job_id, user_id, task, json.dumps(params), input_name, status, time.time()),
        )
    return job_id


def get_job(job_id: str) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute(
            "SELECT j.*, u.username FROM jobs j JOIN users u ON u.id = j.user_id WHERE j.id = ?",
            (job_id,),
        ).fetchone()
    return _job_dict(row) if row else None


def list_jobs(user_id: int | None, limit: int = 100) -> list[dict[str, Any]]:
    """Lavori di un utente (o di tutti se user_id è None), dal più recente."""
    query = "SELECT j.*, u.username FROM jobs j JOIN users u ON u.id = j.user_id"
    args: list[Any] = []
    if user_id is not None:
        query += " WHERE j.user_id = ?"
        args.append(user_id)
    query += " ORDER BY j.created_at DESC LIMIT ?"
    args.append(limit)
    with connection() as conn:
        return [_job_dict(r) for r in conn.execute(query, args).fetchall()]


def queue_position(job_id: str) -> int:
    """Quanti lavori in coda ci sono prima di questo (0 = è il prossimo)."""
    with connection() as conn:
        row = conn.execute("SELECT created_at FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            return 0
        return int(conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status = 'queued' AND created_at < ?",
            (row["created_at"],),
        ).fetchone()[0])


def count_queued() -> int:
    with connection() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'queued'").fetchone()[0])


def claim_next_job() -> dict[str, Any] | None:
    """Prende il lavoro in coda più vecchio e lo segna come in esecuzione."""
    with connection() as conn:
        row = conn.execute(
            "SELECT id FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1"
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE jobs SET status = 'running', started_at = ?, message = 'Avvio…' WHERE id = ?",
            (time.time(), row["id"]),
        )
    return get_job(row["id"])


def update_job(job_id: str, **fields: Any) -> None:
    if "result" in fields and fields["result"] is not None:
        fields["result"] = json.dumps(fields["result"])
    if "params" in fields:
        fields["params"] = json.dumps(fields["params"])
    cols = ", ".join(f"{k} = ?" for k in fields)
    with connection() as conn:
        conn.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))


def cancel_if_queued(job_id: str) -> bool:
    with connection() as conn:
        cur = conn.execute(
            "UPDATE jobs SET status = 'cancelled', finished_at = ?, message = 'Annullato' "
            "WHERE id = ? AND status = 'queued'",
            (time.time(), job_id),
        )
        return cur.rowcount > 0


def fail_interrupted_jobs() -> None:
    """All'avvio: i lavori rimasti 'running' sono stati interrotti da un riavvio;
    quelli rimasti 'uploading' sono caricamenti mai completati."""
    with connection() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'failed', finished_at = ?, "
            "error = 'Interrotto: il server è stato riavviato durante l''esecuzione' "
            "WHERE status = 'running'",
            (time.time(),),
        )
        conn.execute("DELETE FROM jobs WHERE status = 'uploading'")


def delete_job(job_id: str) -> None:
    with connection() as conn:
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
