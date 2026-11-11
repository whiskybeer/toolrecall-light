import sqlite3
import time
import pytest
from store import (
    _fallback_db_path,
    gc,
    get_result,
    get_seen,
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


def test_put_result_shares_identical_hash(memory_db):
    id1 = put_result(memory_db, "id-1", "hash-common", "shared content", "sess-1")
    id2 = put_result(memory_db, "id-2", "hash-common", "shared content", "sess-2")
    assert id1 == "id-1"
    assert id2 == "id-1"  # reused existing id

    # Ensure only 1 row exists in results
    cur = memory_db.execute("SELECT count(*) FROM results")
    assert cur.fetchone()[0] == 1


def test_upsert_and_get_seen(memory_db):
    assert get_seen(memory_db, "sess-1", "key-a") is None

    upsert_seen(memory_db, "sess-1", "key-a", "hash-1", generation=0, served_gen=0, streak=0)
    seen = get_seen(memory_db, "sess-1", "key-a")
    assert seen == ("hash-1", 0, 0, 0)

    # Update streak
    upsert_seen(memory_db, "sess-1", "key-a", "hash-1", generation=0, served_gen=0, streak=1)
    seen2 = get_seen(memory_db, "sess-1", "key-a")
    assert seen2 == ("hash-1", 0, 0, 1)


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
