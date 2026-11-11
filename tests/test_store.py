import sqlite3
import time
import pytest
from store import (
    _fallback_db_path,
    bump_stat,
    gc,
    get_result,
    get_seen,
    get_stats,
    init_db,
    put_result,
    upsert_seen,
)


@pytest.fixture
def memory_db():
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    return conn


def test_put_and_get_result(memory_db):
    res_id = put_result(memory_db, "id-1", "hash-abc", "content-123", "sess-1")
    assert res_id == "id-1"

    fetched = get_result(memory_db, "id-1")
    assert fetched == "content-123"

    # Scoped fetch
    assert get_result(memory_db, "id-1", session_id="sess-1") == "content-123"
    assert get_result(memory_db, "id-1", session_id="sess-other") is None


def test_put_result_isolates_sessions(memory_db):
    """Identical content in two sessions gets one row PER session.

    Regression: reuse was global (``WHERE hash = ?``), so session B's stub carried
    an id whose row belonged to session A — recall then only worked through the
    cross-session fallback path. A stub id must resolve inside its own session.
    """
    id1 = put_result(memory_db, "id-1", "hash-common", "shared content", "sess-1")
    id2 = put_result(memory_db, "id-2", "hash-common", "shared content", "sess-2")
    assert id1 == "id-1"
    assert id2 == "id-2"  # NOT the other session's id

    # Both sessions can recall their own copy; neither resolves the other's id.
    assert get_result(memory_db, "id-1", session_id="sess-1") == "shared content"
    assert get_result(memory_db, "id-2", session_id="sess-2") == "shared content"
    assert get_result(memory_db, "id-1", session_id="sess-2") is None

    # Same session + same hash still shares one row (no duplicate growth).
    id3 = put_result(memory_db, "id-3", "hash-common", "shared content", "sess-1")
    assert id3 == "id-1"
    assert memory_db.execute("SELECT count(*) FROM results").fetchone()[0] == 2


def test_upsert_and_get_seen(memory_db):
    assert get_seen(memory_db, "sess-1", "key-a") is None

    upsert_seen(memory_db, "sess-1", "key-a", "hash-1", generation=0, served_gen=0, streak=0)
    seen = get_seen(memory_db, "sess-1", "key-a")
    assert seen == ("hash-1", 0, 0, 0)

    # Update streak
    upsert_seen(memory_db, "sess-1", "key-a", "hash-1", generation=0, served_gen=0, streak=1)
    seen2 = get_seen(memory_db, "sess-1", "key-a")
    assert seen2 == ("hash-1", 0, 0, 1)


def test_gc_prunes_stale_seen_and_counts(tmp_path):
    """GC must sweep dead session state, not only result bytes.

    `seen` holds one row per (session, tool+args) forever; pruning only results
    leaves a permanent index of sessions that ended days ago.
    """
    conn = sqlite3.connect(str(tmp_path / "gc.db"))
    init_db(conn)
    old = time.time() - 100_000
    now = time.time()

    put_result(conn, "old", "h-old", "old", "s-old", created_at=old)
    upsert_seen(conn, "s-old", "k-old", "h-old", 0, 0, 0, updated_at=old)
    put_result(conn, "new", "h-new", "new", "s-new", created_at=now)
    upsert_seen(conn, "s-new", "k-new", "h-new", 0, 0, 0, updated_at=now)

    removed = gc(conn, ttl_hours=24.0)
    assert removed == 2  # one stale result + one stale seen row
    assert get_result(conn, "old") is None
    assert get_result(conn, "new") == "new"
    assert get_seen(conn, "s-old", "k-old") is None
    assert get_seen(conn, "s-new", "k-new") is not None

    stats = get_stats(conn)
    assert stats["gc_runs"] == 1
    assert stats["gc_removed"] == 2


def test_stats_counters_roundtrip(memory_db):
    assert get_stats(memory_db)["stubs"] == 0
    bump_stat(memory_db, "stubs")
    bump_stat(memory_db, "stubs")
    assert bump_stat(memory_db, "stubs") == 3
    bump_stat(memory_db, "last_stub_at", 1234.5, absolute=True)
    stats = get_stats(memory_db)
    assert stats["stubs"] == 3
    assert stats["last_stub_at"] == 1234.5
    assert stats["results"] == 0 and stats["seen"] == 0


def test_migration_adds_updated_at_to_legacy_seen(tmp_path):
    """An old store (no updated_at) must keep working after upgrade."""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE seen (
            session_id TEXT NOT NULL, key TEXT NOT NULL, hash TEXT NOT NULL,
            generation INTEGER NOT NULL, served_gen INTEGER NOT NULL,
            streak INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (session_id, key)
        );
        """
    )
    conn.commit()
    init_db(conn)  # migration
    cols = {row[1] for row in conn.execute("PRAGMA table_info(seen)")}
    assert "updated_at" in cols
    upsert_seen(conn, "s", "k", "h", 0, 0, 0)
    assert get_seen(conn, "s", "k") == ("h", 0, 0, 0)


def test_gc_ttl(memory_db):
    old_time = time.time() - 100_000
    recent_time = time.time()

    put_result(memory_db, "old-id", "hash-old", "old", "s1", created_at=old_time)
    put_result(memory_db, "new-id", "hash-new", "new", "s1", created_at=recent_time)

    deleted = gc(memory_db, ttl_hours=24.0)
    assert deleted == 1

    assert get_result(memory_db, "old-id") is None
    assert get_result(memory_db, "new-id") == "new"


def test_fallback_path_honors_hermes_home(monkeypatch, tmp_path):
    """A profile or custom HERMES_HOME must get its store inside that tree."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert _fallback_db_path() == tmp_path / "plugin-data" / "toolrecall-light" / "data.db"


def test_fallback_path_defaults_to_dot_hermes(monkeypatch, tmp_path):
    """Without HERMES_HOME the documented default is ~/.hermes."""
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert _fallback_db_path() == tmp_path / ".hermes" / "plugin-data" / "toolrecall-light" / "data.db"
