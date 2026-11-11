"""Test isolation: no test may touch the live plugin store.

The plugin's storage layer defaults to Hermes' plugin-data directory, so any test
that calls the hook (or `status()`) without an explicit connection reads and
migrates the PRODUCTION store. That is how a live data.db was created empty-but-
migrated during a test run — the exact class of accident ToolRecall's own rules
forbid (pin the output destination to a temp path).

This autouse fixture makes every test hermetic by default: `store.
get_default_connection` returns a per-test temporary database. Tests that need a
specific connection still patch it themselves (see `isolated_plugin`).
"""

from __future__ import annotations

import sqlite3

import pytest

import store


@pytest.fixture(autouse=True)
def _no_live_plugin_store(monkeypatch, tmp_path):
    """Point the default connection at a temp DB for every test."""
    db = tmp_path / "default-store.db"
    conn = sqlite3.connect(str(db), check_same_thread=False)
    store.init_db(conn)
    monkeypatch.setattr(store, "get_default_connection", lambda db_path=None: conn)
    try:
        yield conn
    finally:
        conn.close()
