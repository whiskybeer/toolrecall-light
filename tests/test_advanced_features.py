"""Integration tests for ToolRecall full-stack efficiency features in toolrecall-light:
- Volatile data normalization (duration/timestamps)
- Sliced recall (lines, head, tail, grep)
- Diff presentation for modified tool outputs
- Daemon coexistence state
"""

import json
import sqlite3
import pytest

import __init__ as plugin
import store
import schemas
import dedup
import compat


@pytest.fixture
def isolated_plugin(monkeypatch, tmp_path):
    test_db = tmp_path / "test_advanced.db"
    conn = sqlite3.connect(str(test_db), check_same_thread=False)
    store.init_db(conn)

    monkeypatch.setattr(store, "get_default_connection", lambda: conn)
    plugin._session_generations.clear()
    return conn


def test_volatile_normalization_cross_turn_hit(isolated_plugin):
    """Outputs differing ONLY by execution duration or timestamp should dedup as duplicates."""
    session_id = "sess-norm-1"
    tool_name = "terminal"
    args = {"command": "make test"}

    base_payload = "Ran 120 tests: 120 passed\n" + ("log output\n" * 250)
    out1 = base_payload + "\nFinished in 0.42s\n"
    out2 = base_payload + "\nFinished in 0.49s\n"

    # Turn 1: Initial output passes through
    res1 = plugin._on_transform_tool_result(
        tool_name=tool_name,
        args=args,
        result=out1,
        session_id=session_id,
        status="ok",
    )
    assert res1 is None

    # Turn 2: Output only differs by volatile duration footer -> should be stubbed as duplicate!
    res2 = plugin._on_transform_tool_result(
        tool_name=tool_name,
        args=args,
        result=out2,
        session_id=session_id,
        status="ok",
    )
    assert res2 is not None
    assert "[toolrecall-light]" in res2
    assert "identical to the earlier terminal result" in res2


def test_diff_presentation_for_modified_output(isolated_plugin):
    """Outputs for the same tool that changed slightly should produce a diff stub."""
    session_id = "sess-diff-1"
    tool_name = "terminal"
    args = {"command": "git diff"}

    # 150 lines of output (>2000 chars)
    lines1 = [f"line {i:03d}: status unchanged" for i in range(150)]
    out1 = "\n".join(lines1)

    # Turn 1: Pass through
    res1 = plugin._on_transform_tool_result(
        tool_name=tool_name,
        args=args,
        result=out1,
        session_id=session_id,
        status="ok",
    )
    assert res1 is None

    # Turn 2: 1 line changed out of 150
    lines2 = [f"line {i:03d}: status unchanged" if i != 25 else "line 025: STATUS CHANGED" for i in range(150)]
    out2 = "\n".join(lines2)

    res2 = plugin._on_transform_tool_result(
        tool_name=tool_name,
        args=args,
        result=out2,
        session_id=session_id,
        status="ok",
    )
    assert res2 is not None
    assert "[toolrecall-light] modified output for terminal" in res2
    assert "STATUS CHANGED" in res2
    assert "recall_result" in res2

    # Stats should record both a stub and a diff_stub
    stats = store.get_stats(isolated_plugin)
    assert stats["stubs"] == 1
    assert stats["diff_stubs"] == 1


def test_sliced_recall_lines_and_grep(isolated_plugin):
    """recall_result with lines, head, tail, or grep should return only matching slice."""
    session_id = "sess-slice-1"
    lines = [
        "line 1: initialization",
        "line 2: connection established",
        "line 3: ERROR: database connection timed out",
        "line 4: retry attempt 1",
        "line 5: ERROR: retry failed",
        "line 6: fallback to local cache",
        "line 7: success",
    ] + [f"padding line {i}" for i in range(8, 120)]
    content = "\n".join(lines)

    result_id = "res-slice-test"
    content_hash = dedup.result_hash(content)
    store.put_result(isolated_plugin, result_id, content_hash, content, session_id)

    # 1. Full recall (no slicing params)
    resp_full = json.loads(schemas.handle_recall({"result_id": result_id}, conn=isolated_plugin, session_id=session_id))
    assert resp_full["content"] == content
    assert "sliced" not in resp_full

    # 2. Line range slicing ("1-3")
    resp_lines = json.loads(schemas.handle_recall(
        {"result_id": result_id, "lines": "1-3"},
        conn=isolated_plugin,
        session_id=session_id,
    ))
    assert resp_lines["sliced"] is True
    assert resp_lines["returned_lines"] == 3
    assert resp_lines["content"] == "line 1: initialization\nline 2: connection established\nline 3: ERROR: database connection timed out"

    # 3. Head slicing (head=2)
    resp_head = json.loads(schemas.handle_recall(
        {"result_id": result_id, "head": 2},
        conn=isolated_plugin,
        session_id=session_id,
    ))
    assert resp_head["sliced"] is True
    assert resp_head["returned_lines"] == 2
    assert resp_head["content"] == "line 1: initialization\nline 2: connection established"

    # 4. Tail slicing (tail=2)
    resp_tail = json.loads(schemas.handle_recall(
        {"result_id": result_id, "tail": 2},
        conn=isolated_plugin,
        session_id=session_id,
    ))
    assert resp_tail["sliced"] is True
    assert resp_tail["returned_lines"] == 2
    assert "padding line 119" in resp_tail["content"]

    # 5. Grep filtering
    resp_grep = json.loads(schemas.handle_recall(
        {"result_id": result_id, "grep": "ERROR:"},
        conn=isolated_plugin,
        session_id=session_id,
    ))
    assert resp_grep["sliced"] is True
    assert resp_grep["returned_lines"] == 2
    assert "L3: line 3: ERROR: database connection timed out" in resp_grep["content"]
    assert "L5: line 5: ERROR: retry failed" in resp_grep["content"]


def test_slice_edge_cases(isolated_plugin):
    """Test slice_content edge cases: invalid regex, out-of-bounds, single line."""
    session_id = "sess-slice-edge"
    content = "alpha\nbeta\ngamma\ndelta\nepsilon"
    result_id = "res-slice-edge"
    store.put_result(isolated_plugin, result_id, dedup.result_hash(content), content, session_id)

    # 1. Invalid regex should fall back safely to substring match
    resp_bad_regex = json.loads(schemas.handle_recall(
        {"result_id": result_id, "grep": "([invalid+"},
        conn=isolated_plugin,
        session_id=session_id,
    ))
    assert resp_bad_regex["sliced"] is True
    assert resp_bad_regex["returned_lines"] == 0

    # 2. Valid regex fallback substring match
    content2 = "alpha\nbeta ([invalid+ test\ngamma"
    store.put_result(isolated_plugin, "res-2", dedup.result_hash(content2), content2, session_id)
    resp_sub = json.loads(schemas.handle_recall(
        {"result_id": "res-2", "grep": "([invalid+"},
        conn=isolated_plugin,
        session_id=session_id,
    ))
    assert resp_sub["sliced"] is True
    assert "L2: beta ([invalid+ test" in resp_sub["content"]

    # 3. Line range out of bounds
    resp_oob = json.loads(schemas.handle_recall(
        {"result_id": result_id, "lines": "100-200"},
        conn=isolated_plugin,
        session_id=session_id,
    ))
    assert resp_oob["sliced"] is True
    assert resp_oob["returned_lines"] == 0
    assert resp_oob["content"] == ""

    # 4. Single line selection ("3")
    resp_single = json.loads(schemas.handle_recall(
        {"result_id": result_id, "lines": "3"},
        conn=isolated_plugin,
        session_id=session_id,
    ))
    assert resp_single["sliced"] is True
    assert resp_single["content"] == "gamma"


def test_diff_fallback_to_full_pass_when_diff_is_too_large(isolated_plugin):
    """When a modification is so large that a diff saves nothing, pass full content."""
    session_id = "sess-diff-fallback"
    tool_name = "terminal"
    args = {"command": "cargo build"}

    out1 = "Compiling pkg v1.0.0\n" + ("build line output\n" * 150)
    out2 = "Entirely completely different failure cascade\n" + ("panic error dump\n" * 150)

    # Call 1: pass through
    assert plugin._on_transform_tool_result(tool_name, args, out1, session_id=session_id) is None

    # Call 2: completely changed output -> diff is not beneficial -> full pass through
    res2 = plugin._on_transform_tool_result(tool_name, args, out2, session_id=session_id)
    assert res2 is None  # Pass through!

    # Call 3: identical to Call 2 -> should now stub!
    res3 = plugin._on_transform_tool_result(tool_name, args, out2, session_id=session_id)
    assert res3 is not None
    assert "[toolrecall-light]" in res3
    assert "identical to the earlier" in res3


def test_streak_guard_with_diff_stubs(isolated_plugin, monkeypatch):
    """Streak guard forces a full send after MAX_STREAK consecutive suppressions."""
    monkeypatch.setenv("HERMES_TOOLRECALL_LIGHT_MAX_STUB_STREAK", "2")
    session_id = "sess-diff-streak"
    tool_name = "terminal"
    args = {"command": "pytest"}

    # 150 lines of ~30 chars each = ~4500 chars (> DEFAULT_MIN_CHARS 2000)
    lines = [f"test line {i:03d}: status ok and execution normal" for i in range(150)]
    out1 = "\n".join(lines)
    # small mod
    lines[10] = "test line 010: STATUS FAILED WITH ASSERTION ERROR"
    out2 = "\n".join(lines)

    # Turn 1: initial pass-through
    assert plugin._on_transform_tool_result(tool_name, args, out1, session_id=session_id) is None

    # Turn 2: diff stub (streak=1)
    res2 = plugin._on_transform_tool_result(tool_name, args, out2, session_id=session_id)
    assert res2 is not None
    assert "modified output" in res2

    # Turn 3: identical repeat of out2 (streak=2)
    res3 = plugin._on_transform_tool_result(tool_name, args, out2, session_id=session_id)
    assert res3 is not None
    assert "identical to the earlier" in res3

    # Turn 4: streak limit reached (streak=2 >= MAX_STREAK=2) -> must pass through full content!
    res4 = plugin._on_transform_tool_result(tool_name, args, out2, session_id=session_id)
    assert res4 is None  # Pass through!


def test_compaction_reset_with_diff_stubs(isolated_plugin):
    """Compaction reset forces full re-send before resuming diff/stubs in new generation."""
    session_id = "sess-compaction-diff"
    tool_name = "terminal"
    args = {"command": "make"}

    # 150 lines of ~30 chars each = ~4500 chars (> DEFAULT_MIN_CHARS 2000)
    lines = [f"make step {i:03d}: compile unit and verify target" for i in range(150)]
    out1 = "\n".join(lines)
    lines[5] = "make step 005: COMPILE ERROR IN SYMBOL RESOLUTION"
    out2 = "\n".join(lines)

    # Turn 1: pass through
    assert plugin._on_transform_tool_result(tool_name, args, out1, session_id=session_id) is None

    # Turn 2: diff stub
    res2 = plugin._on_transform_tool_result(tool_name, args, out2, session_id=session_id)
    assert res2 is not None
    assert "modified output" in res2

    # Compaction occurs!
    plugin._on_post_compaction(session_id=session_id)

    # Turn 3: generation changed -> must re-anchor with full content once!
    res3 = plugin._on_transform_tool_result(tool_name, args, out2, session_id=session_id)
    assert res3 is None  # Full send anchor!

    # Turn 4: now in the new generation -> stubbing resumes
    res4 = plugin._on_transform_tool_result(tool_name, args, out2, session_id=session_id)
    assert res4 is not None
    assert "identical" in res4


def test_coexistence_state_reporting(isolated_plugin):
    """status() should report coexistence info."""
    st = plugin.status()
    assert "coexistence" in st
    assert "coexistence_mode" in st["coexistence"]
    assert "daemon_online" in st["coexistence"]
