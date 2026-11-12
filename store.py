"""SQLite storage layer for toolrecall-light.

Persists deduplicated tool results, per-session tracking state, and the counters
that make the plugin's activity observable. Supports WAL mode, thread-safety,
hash-sharing within a session, and TTL garbage collection.

Why the ``stats`` table exists: the plugin can be installed, enabled, loaded —
and still never fire (wrong manifest key, hook not registered, every result
under the size floor). Without a persisted counter the only way to answer "is it
working?" was to open SQLite and count rows by hand, which is how a dark plugin
sat unnoticed. ``get_stats`` returns loads, stubs, last_stub_at, results and
seen rows so a status call can distinguish "not loaded" from "loaded, nothing
qualifying yet" from "stubbing".
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_warned_fallback = False


def _fallback_db_path() -> Path:
    """Last-resort store location when Hermes' storage API is unavailable.

    Honors ``HERMES_HOME`` — the profile-safe root Hermes itself resolves paths
    from. Hardcoding ``~/.hermes`` here would put the store outside a custom
    home or profile tree, where a later read would never find it again.
    """
    home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    return Path(home) / "plugin-data" / "toolrecall-light" / "data.db"


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
    updated_at REAL NOT NULL DEFAULT 0,
    last_id TEXT,
    PRIMARY KEY (session_id, key)
);

CREATE TABLE IF NOT EXISTS stats (
    key TEXT PRIMARY KEY,
    value REAL NOT NULL,
    updated_at REAL NOT NULL
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
                # Fallback to the Hermes home when the sanctioned storage API is
                # unavailable. Warn once: silently diverging to a different home
                # is invisible under a profile or custom HERMES_HOME.
                global _warned_fallback
                if not _warned_fallback:
                    _warned_fallback = True
                    logger.warning(
                        "plugins.plugin_storage unavailable; toolrecall-light is "
                        "storing results outside Hermes' storage API at %s",
                        _fallback_db_path(),
                    )
                try:
                    target = _fallback_db_path()
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
    """Ensure database tables and indices are initialized (migrates old stores)."""
    with conn:
        conn.executescript(CREATE_SCHEMA_SQL)
        # Migration: stores created before updated_at existed. Without it every
        # upsert fails on the missing column, which would turn an upgrade into a
        # silent no-op (the plugin fails open, so nothing would be logged above
        # debug).
        cols = {row[1] for row in conn.execute("PRAGMA table_info(seen)").fetchall()}
        if "updated_at" not in cols:
            conn.execute("ALTER TABLE seen ADD COLUMN updated_at REAL NOT NULL DEFAULT 0")
        if "last_id" not in cols:
            conn.execute("ALTER TABLE seen ADD COLUMN last_id TEXT")
        # Index AFTER the migration: a legacy table has no updated_at yet, and
        # CREATE INDEX inside the schema script would abort the whole init.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_seen_updated ON seen(updated_at)")


def put_result(
    conn: sqlite3.Connection,
    result_id: str,
    content_hash: str,
    content: str,
    session_id: str,
    created_at: Optional[float] = None,
) -> str:
    """Store tool result content. Reuses an existing ID within the SAME session.

    Returns the canonical result_id.

    Scope note: reuse is per session, not global. A stub carries the id of the
    row that holds its bytes, and ``recall_result`` resolves it under the caller's
    session; reusing another session's id made the id point at content the
    caller could only reach through the cross-session fallback path. Identical
    content in two sessions therefore gets one row per session — cheap
    (SQLite stores each row once) and honest.
    """
    now = time.time() if created_at is None else created_at

    with conn:
        cur = conn.execute(
            "SELECT id FROM results WHERE hash = ? AND session_id = ? LIMIT 1",
            (content_hash, session_id),
        )
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


def get_seen_entry(
    conn: sqlite3.Connection,
    session_id: str,
    key: str,
) -> Optional[Tuple[str, int, int, int, Optional[str]]]:
    """Retrieve (hash, generation, served_gen, streak, last_id) for a session key."""
    cur = conn.execute(
        "SELECT hash, generation, served_gen, streak, last_id FROM seen WHERE session_id = ? AND key = ? LIMIT 1",
        (session_id, key),
    )
    row = cur.fetchone()
    if row:
        return (row[0], row[1], row[2], row[3], row[4])
    return None


def upsert_seen(
    conn: sqlite3.Connection,
    session_id: str,
    key: str,
    content_hash: str,
    generation: int,
    served_gen: int,
    streak: int,
    updated_at: Optional[float] = None,
    last_id: Optional[str] = None,
) -> None:
    """Record or update tracking state for a key within a session."""
    now = time.time() if updated_at is None else updated_at
    with conn:
        conn.execute(
            """
            INSERT INTO seen (session_id, key, hash, generation, served_gen, streak, updated_at, last_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id, key) DO UPDATE SET
                hash = excluded.hash,
                generation = excluded.generation,
                served_gen = excluded.served_gen,
                streak = excluded.streak,
                updated_at = excluded.updated_at,
                last_id = COALESCE(excluded.last_id, seen.last_id)
            """,
            (session_id, key, content_hash, generation, served_gen, streak, now, last_id),
        )


def bump_stat(
    conn: sqlite3.Connection,
    key: str,
    amount: float = 1.0,
    absolute: bool = False,
) -> float:
    """Increment (or set, with ``absolute``) a persisted counter. Returns the value.

    Best-effort by contract: callers are on the tool-result path and must never
    fail a turn because a counter write failed.
    """
    now = time.time()
    try:
        with conn:
            if absolute:
                conn.execute(
                    "INSERT INTO stats (key, value, updated_at) VALUES (?, ?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
                    " updated_at = excluded.updated_at",
                    (key, float(amount), now),
                )
                return float(amount)
            conn.execute(
                "INSERT INTO stats (key, value, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = value + excluded.value,"
                " updated_at = excluded.updated_at",
                (key, float(amount), now),
            )
            row = conn.execute("SELECT value FROM stats WHERE key = ?", (key,)).fetchone()
            return float(row[0]) if row else 0.0
    except sqlite3.Error as err:
        logger.debug("stats bump failed (%s): %s", key, err)
        return 0.0


def get_stats(conn: sqlite3.Connection) -> dict:
    """Return the persisted activity counters plus live row counts.

    ``stubs == 0`` with ``loads > 0`` is the diagnostic for "loaded but nothing
    qualified" (all results under the size floor, or every tool on the ignore
    list). ``loads == 0`` means the plugin never registered at all.
    """
    out: dict = {
        "loads": 0.0,
        "hook_calls": 0.0,
        "passes": 0.0,
        "stubs": 0.0,
        "diff_stubs": 0.0,
        "last_stub_at": 0.0,
        "last_load_at": 0.0,
        "gc_runs": 0.0,
        "gc_removed": 0.0,
    }
    try:
        for key, value in conn.execute("SELECT key, value FROM stats"):
            out[key] = value
        out["results"] = conn.execute("SELECT count(*) FROM results").fetchone()[0]
        out["seen"] = conn.execute("SELECT count(*) FROM seen").fetchone()[0]
    except sqlite3.Error as err:
        logger.debug("stats read failed: %s", err)
        out.setdefault("results", 0)
        out.setdefault("seen", 0)
    return out


def gc(conn: sqlite3.Connection, ttl_hours: float = 24.0) -> int:
    """Evict stale results AND stale per-session tracking state.

    Returns the number of rows removed (results + seen).

    Both halves matter: `results` holds the bytes behind every stub (unbounded
    growth if never pruned), while `seen` holds one row per (session, tool+args)
    forever — including sessions that ended days ago. Pruning only `results`
    leaves a permanent index of dead sessions.
    """
    cutoff = time.time() - (ttl_hours * 3600.0)
    removed = 0
    try:
        with conn:
            cur = conn.execute("DELETE FROM results WHERE created_at < ?", (cutoff,))
            removed += max(0, cur.rowcount)
            cur = conn.execute("DELETE FROM seen WHERE updated_at < ?", (cutoff,))
            removed += max(0, cur.rowcount)
        # Counters are written AFTER the delete transaction commits: bump_stat
        # opens its own `with conn` block, and nesting them would commit twice.
        bump_stat(conn, "gc_runs", 1)
        if removed:
            bump_stat(conn, "gc_removed", removed)
    except sqlite3.Error as err:
        logger.debug("gc failed: %s", err)
    return removed
