"""Pure deduplication logic for toolrecall-light — in-process tool result deduplication.

Zero dependencies, stdlib only. No Hermes imports.
Handles argument canonicalization, hashing, key generation, output normalization,
diff computation, and stub formatting.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
from typing import Any, Container, Optional

DEFAULT_MIN_CHARS: int = int(os.getenv("HERMES_TOOLRECALL_LIGHT_MIN_CHARS", os.getenv("HERMES_TOOL_RECALL_MIN_CHARS", "2000")))
DEFAULT_MAX_CHARS: int = int(os.getenv("HERMES_TOOLRECALL_LIGHT_MAX_CHARS", os.getenv("HERMES_TOOL_RECALL_MAX_CHARS", "2000000")))
DEFAULT_MAX_STUB_STREAK: int = int(os.getenv("HERMES_TOOLRECALL_LIGHT_MAX_STUB_STREAK", os.getenv("HERMES_TOOL_RECALL_MAX_STUB_STREAK", "5")))
DEFAULT_MAX_DIFF_CHARS: int = int(os.getenv("HERMES_TOOLRECALL_LIGHT_MAX_DIFF_CHARS", "4000"))
DEFAULT_IGNORE_TOOLS: frozenset[str] = frozenset({"read_file", "skill_view"})
VOLATILE_ARG_KEYS: frozenset[str] = frozenset({"timeout", "_meta", "api_request_id"})
NON_SEMANTIC_KEYS: frozenset[str] = frozenset({
    "timestamp",
    "request_id",
    "session_id",
    "nonce",
    "trace_id",
    "span_id",
    "correlation_id",
    "_t",
    "_r",
})

# Pre-compiled regexes for output sanitization
_ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_DURATION_RE = re.compile(
    r"(?i)\b(?:finished in|executed in|elapsed:?|took)\s+\d+(?:\.\d+)?\s*(?:m?s|seconds|minutes)\b"
)
_TIMESTAMP_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
)
_HEX_RE = re.compile(r"\b0x[0-9a-fA-F]{6,16}\b")
_PID_RE = re.compile(r"(?i)\bpid[= :]\s*\d+\b")


def get_min_chars() -> int:
    """Return the minimum character threshold for deduplication."""
    try:
        return int(os.getenv("HERMES_TOOLRECALL_LIGHT_MIN_CHARS", os.getenv("HERMES_TOOL_RECALL_MIN_CHARS", str(DEFAULT_MIN_CHARS))))
    except (TypeError, ValueError):
        return DEFAULT_MIN_CHARS


def get_max_chars() -> int:
    """Return the maximum character threshold for deduplication (0 = unlimited)."""
    try:
        return int(os.getenv("HERMES_TOOLRECALL_LIGHT_MAX_CHARS", os.getenv("HERMES_TOOL_RECALL_MAX_CHARS", str(DEFAULT_MAX_CHARS))))
    except (TypeError, ValueError):
        return DEFAULT_MAX_CHARS


def get_max_stub_streak() -> int:
    """Return consecutive stubs before forcing full pass-through once."""
    try:
        return int(os.getenv("HERMES_TOOLRECALL_LIGHT_MAX_STUB_STREAK", os.getenv("HERMES_TOOL_RECALL_MAX_STUB_STREAK", str(DEFAULT_MAX_STUB_STREAK))))
    except (TypeError, ValueError):
        return DEFAULT_MAX_STUB_STREAK


def get_max_diff_chars() -> int:
    """Return max diff characters threshold to consider for diff stubs."""
    try:
        return int(os.getenv("HERMES_TOOLRECALL_LIGHT_MAX_DIFF_CHARS", str(DEFAULT_MAX_DIFF_CHARS)))
    except (TypeError, ValueError):
        return DEFAULT_MAX_DIFF_CHARS


def is_normalization_enabled() -> bool:
    """Return True if output normalization is enabled."""
    val = os.getenv("HERMES_TOOLRECALL_LIGHT_NORMALIZE", "1").strip().lower()
    return val not in {"0", "false", "no", "off"}


def is_diff_enabled() -> bool:
    """Return True if diff-based stubs for modified outputs are enabled."""
    val = os.getenv("HERMES_TOOLRECALL_LIGHT_DIFF", "1").strip().lower()
    return val not in {"0", "false", "no", "off"}


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text."""
    return _ANSI_RE.sub("", text)


def strip_volatile_patterns(text: str) -> str:
    """Normalize volatile runtimes, timestamps, and memory/PID markers."""
    t = _DURATION_RE.sub("<duration>", text)
    t = _TIMESTAMP_RE.sub("<timestamp>", t)
    t = _HEX_RE.sub("<addr>", t)
    t = _PID_RE.sub("pid=<pid>", t)
    return t


def _clean_json_obj(obj: Any) -> Any:
    """Recursively clean non-semantic keys from JSON objects."""
    if isinstance(obj, dict):
        return {
            k: _clean_json_obj(v)
            for k, v in obj.items()
            if k not in NON_SEMANTIC_KEYS
        }
    if isinstance(obj, list):
        return [_clean_json_obj(item) for item in obj]
    return obj


def normalize_output(content: str) -> str:
    """Normalize tool output to increase cache deduplication hits across turns.

    Normalizes line endings, strips ANSI escape sequences, scrubs volatile
    execution times/timestamps, and formats JSON canonically.
    """
    if not content:
        return content

    # 1. Strip ANSI codes
    s = strip_ansi(content)

    # 2. Normalize CRLF to LF and strip trailing whitespace per line
    lines = [line.rstrip() for line in s.replace("\r\n", "\n").split("\n")]
    s = "\n".join(lines).strip()

    # 3. Canonicalize JSON if structured payload
    if s and (s.startswith("{") and s.endswith("}") or s.startswith("[") and s.endswith("]")):
        try:
            parsed = json.loads(s)
            cleaned = _clean_json_obj(parsed)
            return json.dumps(cleaned, sort_keys=True, separators=(",", ":"), default=str)
        except Exception:
            pass

    # 4. Scrub ephemeral timestamps and duration footers
    s = strip_volatile_patterns(s)
    return s


def compute_diff(
    prev_content: str,
    curr_content: str,
    max_diff_chars: Optional[int] = None,
) -> Optional[str]:
    """Compute unified diff if it is meaningfully smaller than the full content.

    Returns the diff text if diff size is within max_diff_chars and at least
    40% smaller than the full content, otherwise None.
    """
    if prev_content == curr_content:
        return None

    limit = get_max_diff_chars() if max_diff_chars is None else max_diff_chars
    if limit <= 0:
        return None

    prev_lines = prev_content.splitlines(keepends=True)
    curr_lines = curr_content.splitlines(keepends=True)
    diff = list(
        difflib.unified_diff(
            prev_lines,
            curr_lines,
            fromfile="previous",
            tofile="current",
            lineterm="",
        )
    )
    if not diff:
        return None

    diff_text = "\n".join(diff)
    diff_len = len(diff_text)
    curr_len = len(curr_content)

    # Benefit gate: diff must be <= limit and save at least 40% compared to full send
    if diff_len <= limit and diff_len < (curr_len * 0.6):
        return diff_text
    return None


def format_diff_stub(
    tool_name: str,
    prev_id: str,
    curr_id: str,
    diff_text: str,
    nbytes: int,
) -> str:
    """Generate compact diff stub showing the modifications."""
    diff_bytes = len(diff_text.encode("utf-8"))
    return (
        f"[toolrecall-light] modified output for {tool_name} "
        f"({diff_bytes} bytes diff vs {nbytes} bytes full).\n"
        f"Diff against earlier result {prev_id}:\n"
        f"{diff_text}\n"
        f'Call recall_result(result_id="{curr_id}") if you need the full content.'
    )


def clean_volatile_args(args: Any) -> Any:
    """Recursively strip volatile or non-deterministic metadata arguments."""
    if isinstance(args, dict):
        return {
            k: clean_volatile_args(v)
            for k, v in args.items()
            if k not in VOLATILE_ARG_KEYS
        }
    if isinstance(args, list):
        return [clean_volatile_args(item) for item in args]
    return args


def canonical_args(args: Any) -> str:
    """Format tool arguments into a deterministic, compact JSON string."""
    cleaned = clean_volatile_args(args)
    try:
        return json.dumps(cleaned, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:
        return str(cleaned)


def result_hash(content: str) -> str:
    """Return the SHA-256 hex digest of the string content."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def make_key(tool_name: str, args: Any) -> str:
    """Generate a stable session lookup key combining tool name and canonical arguments."""
    return f"{tool_name}\x00{canonical_args(args)}"


def format_stub(tool_name: str, result_id: str, nbytes: int) -> str:
    """Generate the compact model-facing stub with explicit recall instruction."""
    return (
        f"[toolrecall-light] identical to the earlier {tool_name} result {result_id} "
        f"({nbytes} bytes) in this session.\n"
        f'Call recall_result(result_id="{result_id}") if you need the full content.'
    )


def should_dedup(
    tool_name: str,
    args: Any,
    result: Any,
    status: Optional[str] = "ok",
    error_type: Optional[str] = None,
    ignore_tools: Optional[Container[str]] = None,
    min_chars: Optional[int] = None,
    max_chars: Optional[int] = None,
) -> bool:
    """Evaluate whether a tool result qualifies for deduplication evaluation."""
    # Global kill-switch check
    if os.getenv("HERMES_TOOLRECALL_LIGHT", os.getenv("HERMES_TOOL_RECALL", os.getenv("HERMES_CONTEXT_DEDUP", "1"))).strip().lower() in {"0", "false", "no", "off"}:
        return False

    # Only process successful string results
    if status is not None and status != "ok":
        return False
    if error_type:
        return False
    if not isinstance(result, str):
        return False

    # Exclude tools handled natively by Hermes core or user-specified ignore list
    active_ignore = DEFAULT_IGNORE_TOOLS if ignore_tools is None else ignore_tools
    if tool_name in active_ignore:
        return False

    # Length boundaries (skip small payloads or outsized outputs; 0 = unlimited)
    active_min = get_min_chars() if min_chars is None else min_chars
    active_max = get_max_chars() if max_chars is None else max_chars

    length = len(result)
    if length < active_min:
        return False
    if active_max > 0 and length > active_max:
        return False

    return True
