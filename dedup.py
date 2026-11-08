"""Pure deduplication logic for toolrecall-light — in-process tool result deduplication.

Zero dependencies, stdlib only. No Hermes imports.
Handles argument canonicalization, hashing, key generation, and stub formatting.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Container, Optional

DEFAULT_MIN_CHARS: int = int(os.getenv("HERMES_TOOLRECALL_LIGHT_MIN_CHARS", os.getenv("HERMES_TOOL_RECALL_MIN_CHARS", "2000")))
DEFAULT_MAX_CHARS: int = int(os.getenv("HERMES_TOOLRECALL_LIGHT_MAX_CHARS", os.getenv("HERMES_TOOL_RECALL_MAX_CHARS", "2000000")))
DEFAULT_MAX_STUB_STREAK: int = int(os.getenv("HERMES_TOOLRECALL_LIGHT_MAX_STUB_STREAK", os.getenv("HERMES_TOOL_RECALL_MAX_STUB_STREAK", "5")))
DEFAULT_IGNORE_TOOLS: frozenset[str] = frozenset({"read_file", "skill_view"})
VOLATILE_ARG_KEYS: frozenset[str] = frozenset({"timeout", "_meta", "api_request_id"})


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
