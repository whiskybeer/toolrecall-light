import os
import pytest
from dedup import (
    canonical_args,
    clean_volatile_args,
    format_stub,
    make_key,
    result_hash,
    should_dedup,
    DEFAULT_MIN_CHARS,
    DEFAULT_MAX_CHARS,
)


def test_clean_volatile_args():
    args = {
        "command": "pytest",
        "timeout": 30,
        "_meta": {"turn": 1},
        "nested": {"api_request_id": "req-123", "value": 42},
    }
    cleaned = clean_volatile_args(args)
    assert "timeout" not in cleaned
    assert "_meta" not in cleaned
    assert "api_request_id" not in cleaned["nested"]
    assert cleaned["command"] == "pytest"
    assert cleaned["nested"]["value"] == 42


def test_canonical_args_ordering():
    args1 = {"b": 2, "a": 1, "nested": {"y": 20, "x": 10}}
    args2 = {"nested": {"x": 10, "y": 20}, "a": 1, "b": 2}
    assert canonical_args(args1) == canonical_args(args2)


def test_result_hash():
    s = "hello world"
    h = result_hash(s)
    assert len(h) == 64
    assert h == result_hash(s)
    assert h != result_hash("hello world!")


def test_make_key():
    key1 = make_key("terminal", {"command": "ls -l"})
    key2 = make_key("terminal", {"command": "ls -l"})
    key3 = make_key("terminal", {"command": "ls -la"})
    assert key1 == key2
    assert key1 != key3
    assert key1.startswith("terminal\x00")


def test_format_stub():
    stub = format_stub("terminal", "res123", 4500)
    assert "[toolrecall-light]" in stub
    assert "identical to the earlier terminal result res123" in stub
    assert "(4500 bytes)" in stub
    assert 'recall_result(result_id="res123")' in stub


def test_should_dedup_basic():
    long_res = "x" * DEFAULT_MIN_CHARS
    assert should_dedup("terminal", {"command": "ls"}, long_res) is True


def test_should_dedup_length_boundaries():
    short_res = "x" * (DEFAULT_MIN_CHARS - 1)
    assert should_dedup("terminal", {}, short_res) is False

    long_res = "x" * DEFAULT_MIN_CHARS
    assert should_dedup("terminal", {}, long_res) is True

    too_long = "x" * (DEFAULT_MAX_CHARS + 1)
    assert should_dedup("terminal", {}, too_long) is False


def test_should_dedup_status_and_errors():
    payload = "x" * DEFAULT_MIN_CHARS
    assert should_dedup("terminal", {}, payload, status="error") is False
    assert should_dedup("terminal", {}, payload, error_type="RuntimeError") is False
    assert should_dedup("terminal", {}, 12345) is False


def test_should_dedup_ignored_tools():
    payload = "x" * DEFAULT_MIN_CHARS
    assert should_dedup("read_file", {"path": "a.txt"}, payload) is False
    assert should_dedup("skill_view", {"name": "test"}, payload) is False
    assert should_dedup("search_files", {"pattern": "foo"}, payload) is True


def test_should_dedup_kill_switch(monkeypatch):
    payload = "x" * DEFAULT_MIN_CHARS
    assert should_dedup("terminal", {}, payload) is True
    monkeypatch.setenv("HERMES_CONTEXT_DEDUP", "0")
    assert should_dedup("terminal", {}, payload) is False
    monkeypatch.setenv("HERMES_CONTEXT_DEDUP", "off")
    assert should_dedup("terminal", {}, payload) is False


def test_should_dedup_custom_max_chars(monkeypatch):
    payload_3mb = "x" * 3_000_000
    # Default 2MB cap -> False
    assert should_dedup("terminal", {}, payload_3mb) is False

    # Raised cap -> True
    monkeypatch.setenv("HERMES_TOOL_RECALL_MAX_CHARS", "5000000")
    assert should_dedup("terminal", {}, payload_3mb) is True

    # 0 = unlimited -> True
    monkeypatch.setenv("HERMES_TOOL_RECALL_MAX_CHARS", "0")
    assert should_dedup("terminal", {}, payload_3mb) is True


def test_strip_ansi():
    from dedup import strip_ansi

    colored = "\x1b[31mError:\x1b[0m test failed in \x1b[1;32mmain.py\x1b[0m"
    assert strip_ansi(colored) == "Error: test failed in main.py"


def test_strip_volatile_patterns():
    from dedup import strip_volatile_patterns

    sample = (
        "Finished in 0.42s\n"
        "Started at 2026-11-12T08:30:00Z\n"
        "Memory allocated at 0x7ffd9b821a40, pid=48192\n"
        "Task took 15ms"
    )
    scrubbed = strip_volatile_patterns(sample)
    assert "<duration>" in scrubbed
    assert "<timestamp>" in scrubbed
    assert "<addr>" in scrubbed
    assert "pid=<pid>" in scrubbed


def test_normalize_output():
    from dedup import normalize_output

    out1 = "Result: ok   \r\nExecuted in 1.20s \r\n"
    out2 = "Result: ok\nExecuted in 1.45s\n"
    assert normalize_output(out1) == normalize_output(out2)

    # JSON normalization
    json1 = '{"b": 2, "a": 1, "timestamp": "2026-11-12", "nonce": "abc"}'
    json2 = '{"a": 1, "b": 2}'
    assert normalize_output(json1) == normalize_output(json2)


def test_compute_diff_and_format_stub():
    from dedup import compute_diff, format_diff_stub

    prev = "\n".join(f"line {i}" for i in range(200))
    # change just 1 line
    curr = "\n".join(f"line {i}" if i != 50 else "line 50 MODIFIED" for i in range(200))

    diff = compute_diff(prev, curr, max_diff_chars=1000)
    assert diff is not None
    assert "line 50 MODIFIED" in diff

    stub = format_diff_stub("terminal", "prev-1", "curr-2", diff, len(curr.encode("utf-8")))
    assert "[toolrecall-light] modified output for terminal" in stub
    assert "Diff against earlier result prev-1" in stub
    assert 'recall_result(result_id="curr-2")' in stub

    # When outputs are identical -> no diff
    assert compute_diff(prev, prev) is None

    # When diff is too large / not beneficial -> None
    drastic = "completely different content\n" * 50
    assert compute_diff(prev, drastic, max_diff_chars=100) is None
