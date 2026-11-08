"""SQLite storage layer for toolrecall-light.

Persists deduplicated tool results and track seen keys across turns within sessions.
Supports WAL mode, thread-safety, LRU/TTL garbage collection, and hash-sharing.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

CREATE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS results (
    id TEXT PRIMARY KEY,
    hash TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    session_id TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_results_hash ON results(hash);
CREATE INDEX IF NOT EXISTS idx_results_session ON results(session_id);

CREATE TABLE IF NOT EXISTS seen (
    session_id TEXT NOT NULL,
    key TEXT NOT NULL,
    hash TEXT NOT NULL,
    generation INTEGER NOT NULL,
    served_gen INTEGER NOT NULL,
    streak INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, key)
);
"""


def get_default_connection(db_path: Optional[Path | str] = None) -> sqlite3.Connection:
    """Obtain a SQLite connection via Hermes plugin_storage or fallback to local file/memory."""
    if db_path is not None:
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
    else:
        try:
            from plugins.plugin_storage import plugin_db
            conn = plugin_db("toolrecall-light")
        except Exception:
            try:
                from plugins.plugin_storage import plugin_data_dir
                target = plugin_data_dir("toolrecall-light") / "data.db"
                conn = sqlite3.connect(str(target), check_same_thread=False)
            except Exception:
                # Fallback to local default under user home or temp directory
                try:
                    target = Path.home() / ".hermes" / "plugin-data" / "toolrecall-light" / "data.db"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    conn = sqlite3.connect(str(target), check_same_thread=False)
                except OSError:
                    import tempfile
                    target = Path(tempfile.gettempdir()) / "hermes-toolrecall-light" / "data.db"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    conn = sqlite3.connect(str(target), check_same_thread=False)

    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
    except sqlite3.OperationalError:
        pass

    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Ensure database tables and indices are initialized."""
    with conn:
        conn.executescript(CREATE_SCHEMA_SQL)


def put_result(
    conn: sqlite3.Connection,
    result_id: str,
    content_hash: str,
    content: str,
    session_id: str,
    created_at: Optional[float] = None,
) -> str:
    """Store tool result content. Reuses existing ID if content hash already exists.

    Returns the canonical result_id.
    """
    now = time.time() if created_at is None else created_at

    with conn:
        cur = conn.execute("SELECT id FROM results WHERE hash = ? LIMIT 1", (content_hash,))
        row = cur.fetchone()
        if row:
            return row[0]

        conn.execute(
            """
            INSERT INTO results (id, hash, content, created_at, session_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (result_id, content_hash, content, now, session_id),
        )
        return result_id


def get_result(
    conn: sqlite3.Connection,
    result_id: str,
    session_id: Optional[str] = None,
) -> Optional[str]:
    """Retrieve full content by result_id, optionally validating session scoping."""
    if session_id:
        cur = conn.execute(
            "SELECT content FROM results WHERE id = ? AND session_id = ? LIMIT 1",
            (result_id, session_id),
        )
    else:
        cur = conn.execute("SELECT content FROM results WHERE id = ? LIMIT 1", (result_id,))

    row = cur.fetchone()
    return row[0] if row else None


def get_seen(
    conn: sqlite3.Connection,
    session_id: str,
    key: str,
) -> Optional[Tuple[str, int, int, int]]:
    """Retrieve (hash, generation, served_gen, streak) for a session key."""
    cur = conn.execute(
        "SELECT hash, generation, served_gen, streak FROM seen WHERE session_id = ? AND key = ? LIMIT 1",
        (session_id, key),
    )
    row = cur.fetchone()
    if row:
        return (row[0], row[1], row[2], row[3])
    return None


def upsert_seen(
    conn: sqlite3.Connection,
    session_id: str,
    key: str,
    content_hash: str,
    generation: int,
    served_gen: int,
    streak: int,
) -> None:
    """Record or update tracking state for a key within a session."""
    with conn:
        conn.execute(
            """
            INSERT INTO seen (session_id, key, hash, generation, served_gen, streak)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id, key) DO UPDATE SET
                hash = excluded.hash,
                generation = excluded.generation,
                served_gen = excluded.served_gen,
                streak = excluded.streak
            """,
            (session_id, key, content_hash, generation, served_gen, streak),
        )


def gc(conn: sqlite3.Connection, ttl_hours: float = 24.0) -> int:
    """Evict results older than ttl_hours. Returns count of removed items."""
    cutoff = time.time() - (ttl_hours * 3600.0)
    with conn:
        cur = conn.execute("DELETE FROM results WHERE created_at < ?", (cutoff,))
        return cur.rowcount
