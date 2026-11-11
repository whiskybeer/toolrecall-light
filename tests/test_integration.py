import json
import sqlite3
import pytest
import __init__ as plugin
import store
import schemas


class MockPluginContext:
    def __init__(self):
        self.hooks = {}
        self.tools = {}

    def register_hook(self, hook_name, callback):
        self.hooks[hook_name] = callback

    def register_tool(self, name, toolset, schema, handler, description=None, **kwargs):
        self.tools[name] = {
            "toolset": toolset,
            "schema": schema,
            "handler": handler,
            "description": description,
        }


@pytest.fixture
def isolated_plugin(monkeypatch, tmp_path):
    # Use temporary sqlite file for isolated integration test
    test_db = tmp_path / "test_data.db"
    conn = sqlite3.connect(str(test_db), check_same_thread=False)
    store.init_db(conn)

    monkeypatch.setattr(store, "get_default_connection", lambda: conn)
    plugin._session_generations.clear()
    return conn


def test_register_flow():
    ctx = MockPluginContext()
    plugin.register(ctx)

    assert "transform_tool_result" in ctx.hooks
    assert "post_compaction" in ctx.hooks
    assert "recall_result" in ctx.tools


def test_dedup_and_recall_lifecycle(isolated_plugin):
    session_id = "test-session-1"
    tool_name = "terminal"
    args = {"command": "cargo test --all"}
    output = "running 42 tests\ntest result: ok. 42 passed\n" + ("x" * 2500)

    # 1. First run: full content passes through (returns None)
    res1 = plugin._on_transform_tool_result(
        tool_name=tool_name,
        args=args,
        result=output,
        session_id=session_id,
        status="ok",
    )
    assert res1 is None

    # 2. Second run: duplicate result -> stub returned
    res2 = plugin._on_transform_tool_result(
        tool_name=tool_name,
        args=args,
        result=output,
        session_id=session_id,
        status="ok",
    )
    assert res2 is not None
    assert "[toolrecall-light]" in res2
    assert "identical to the earlier terminal result" in res2

    # Extract result_id from stub
    # stub format: ... Call recall_result(result_id="{id}") ...
    import re
    m = re.search(r'recall_result\(result_id="([^"]+)"\)', res2)
    assert m is not None
    result_id = m.group(1)

    # 3. Recall result using tool handler
    recall_resp = schemas.handle_recall(
        {"result_id": result_id},
        session_id=session_id,
    )
    recalled_data = json.loads(recall_resp)
    assert recalled_data["result_id"] == result_id
    assert recalled_data["content"] == output


def test_streak_guard(isolated_plugin):
    session_id = "test-session-streak"
    tool_name = "terminal"
    args = {"command": "npm test"}
    output = "PASSED\n" + ("y" * 2500)

    # Call 1: pass-through (initial)
    assert plugin._on_transform_tool_result(tool_name, args, output, session_id=session_id) is None

    # Calls 2 through 6: 5 consecutive stubs (streaks 1..5)
    for _ in range(5):
        stub = plugin._on_transform_tool_result(tool_name, args, output, session_id=session_id)
        assert stub is not None

    # Call 7: streak guard tripped (streak >= 5) -> pass-through full content once, reset streak
    pass_through = plugin._on_transform_tool_result(tool_name, args, output, session_id=session_id)
    assert pass_through is None

    # Call 8: stub again (streak=1)
    stub_next = plugin._on_transform_tool_result(tool_name, args, output, session_id=session_id)
    assert stub_next is not None


def test_custom_streak_env(isolated_plugin, monkeypatch):
    monkeypatch.setenv("HERMES_TOOL_RECALL_MAX_STUB_STREAK", "2")
    session_id = "test-session-custom-streak"
    tool_name = "terminal"
    args = {"command": "cargo test"}
    output = "running 10 tests\n" + ("w" * 2500)

    # Call 1: pass-through
    assert plugin._on_transform_tool_result(tool_name, args, output, session_id=session_id) is None
    # Call 2: stub 1
    assert plugin._on_transform_tool_result(tool_name, args, output, session_id=session_id) is not None
    # Call 3: stub 2
    assert plugin._on_transform_tool_result(tool_name, args, output, session_id=session_id) is not None
    # Call 4: streak guard tripped at 2 -> pass-through
    assert plugin._on_transform_tool_result(tool_name, args, output, session_id=session_id) is None


def test_compaction_generation_reset(isolated_plugin):
    session_id = "test-session-compact"
    tool_name = "search_files"
    args = {"pattern": "def get_"}
    output = "match found in line 12\n" + ("z" * 2500)

    # Call 1: pass-through
    assert plugin._on_transform_tool_result(tool_name, args, output, session_id=session_id) is None
    # Call 2: stub
    stub = plugin._on_transform_tool_result(tool_name, args, output, session_id=session_id)
    assert stub is not None

    # Compaction event occurs!
    plugin._on_post_compaction(session_id=session_id)

    # Call 3 (first repeat post-compaction): re-emit full content once
    full_once = plugin._on_transform_tool_result(tool_name, args, output, session_id=session_id)
    assert full_once is None

    # Call 4 (subsequent repeat): stubs again
    stub_again = plugin._on_transform_tool_result(tool_name, args, output, session_id=session_id)
    assert stub_again is not None


def test_fail_open_on_exception(monkeypatch):
    # Force store.get_default_connection to raise an exception
    monkeypatch.setattr(store, "get_default_connection", lambda: (_ for _ in ()).throw(RuntimeError("DB disk failure")))

    payload = "w" * 3000
    res = plugin._on_transform_tool_result(
        tool_name="terminal",
        args={"command": "ls"},
        result=payload,
        session_id="err-session",
    )
    # Must fail open (return None), never raise
    assert res is None


def test_recall_missing_id():
    resp = schemas.handle_recall({"result_id": "nonexistent_id_12345"})
    data = json.loads(resp)
    assert "error" in data
    assert data["error"] == "not found or expired"


def test_status_answers_is_it_firing(isolated_plugin):
    """`status()` distinguishes not-loaded / not-invoked / nothing-qualified.

    Regression: the plugin could be installed and enabled yet never fire, and the
    only way to notice was to open SQLite and count rows by hand.
    """
    empty = plugin.status()
    assert empty["loads"] == 0
    assert empty["hook_calls"] == 0
    assert empty["stubs"] == 0

    ctx = MockPluginContext()
    plugin.register(ctx)
    assert plugin.status()["loads"] == 1

    output = "z" * 2500
    args = {"command": "git status"}
    first = plugin._on_transform_tool_result(
        tool_name="terminal", args=args, result=output, session_id="stats-session"
    )
    assert first is None  # pass-through, content stored
    stub = plugin._on_transform_tool_result(
        tool_name="terminal", args=args, result=output, session_id="stats-session"
    )
    assert stub is not None  # stubbed

    stats = plugin.status()
    assert stats["hook_calls"] == 2
    assert stats["passes"] == 1
    assert stats["stubs"] == 1
    assert stats["last_stub_at"] > 0
    assert stats["results"] >= 1


def test_session_fallback_is_never_the_literal_default():
    """Id-less sessions must not share stub state (cross-session pointer bug)."""
    a = plugin._active_session(None, None)
    b = plugin._active_session(None, None)
    assert a == b  # stable within the process
    assert a != "default"
    assert plugin._active_session("sess-x", None) == "sess-x"
    assert plugin._active_session(None, "task-y") == "task-y"
