"""Tool schemas and handlers for toolrecall-light."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

try:
    from . import store
except (ImportError, ValueError):
    import store

logger = logging.getLogger(__name__)

RECALL_RESULT = {
    "name": "recall_result",
    "description": (
        "Return the content of a tool result that toolrecall-light replaced with a compact stub. "
        "Pass the result_id copied directly from the [toolrecall-light] stub. "
        "Optionally specify lines, head, tail, or grep to retrieve only a relevant slice and avoid context bloat."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "result_id": {
                "type": "string",
                "description": "Result ID copied from a [toolrecall-light] stub message.",
            },
            "lines": {
                "type": "string",
                "description": "Line range to return, e.g. '1-50', '20:', ':30', or '42'. 1-indexed.",
            },
            "head": {
                "type": "integer",
                "description": "Number of lines from the start to return.",
            },
            "tail": {
                "type": "integer",
                "description": "Number of lines from the end to return.",
            },
            "grep": {
                "type": "string",
                "description": "Regex or substring filter. Only lines matching this pattern (with line numbers) are returned.",
            },
        },
        "required": ["result_id"],
    },
}


def slice_content(
    content: str,
    lines: Optional[str] = None,
    head: Optional[int] = None,
    tail: Optional[int] = None,
    grep: Optional[str] = None,
) -> tuple[str, bool, dict[str, Any]]:
    """Slice or filter content based on line ranges, head/tail limits, or regex grep."""
    all_lines = content.splitlines()
    total_lines = len(all_lines)
    sliced = False

    selected: list[tuple[int, str]] = list(enumerate(all_lines, 1))

    # 1. Apply grep if specified
    if grep:
        sliced = True
        try:
            pattern = re.compile(grep)
            selected = [(i, l) for i, l in selected if pattern.search(l)]
        except re.error:
            # Fallback to plain substring search if regex syntax is invalid
            selected = [(i, l) for i, l in selected if grep in l]

    # 2. Apply lines range if specified (e.g. "1-50", "20:", ":30", "42")
    if lines and isinstance(lines, str):
        sliced = True
        lines_str = lines.strip()
        start_idx = 1
        end_idx = total_lines
        if "-" in lines_str or ":" in lines_str:
            sep = "-" if "-" in lines_str else ":"
            parts = lines_str.split(sep, 1)
            if parts[0].strip():
                try:
                    start_idx = max(1, int(parts[0].strip()))
                except ValueError:
                    pass
            if parts[1].strip():
                try:
                    end_idx = int(parts[1].strip())
                except ValueError:
                    pass
            selected = [item for item in selected if start_idx <= item[0] <= end_idx]
        else:
            try:
                target_line = int(lines_str)
                selected = [item for item in selected if item[0] == target_line]
            except ValueError:
                pass

    # 3. Apply head if specified
    if head is not None and isinstance(head, int) and head > 0:
        sliced = True
        selected = selected[:head]

    # 4. Apply tail if specified
    if tail is not None and isinstance(tail, int) and tail > 0:
        sliced = True
        selected = selected[-tail:]

    if not sliced:
        return content, False, {"total_lines": total_lines, "returned_lines": total_lines}

    # Format sliced output with line numbers if grep was used, or plain lines
    if grep:
        result_text = "\n".join(f"L{i}: {line}" for i, line in selected)
    else:
        result_text = "\n".join(line for _, line in selected)

    return result_text, True, {
        "total_lines": total_lines,
        "returned_lines": len(selected),
    }


def handle_recall(
    args: Any,
    conn: Optional[Any] = None,
    session_id: Optional[str] = None,
    **kwargs: Any,
) -> str:
    """Execute recall_result tool to fetch original bytes for a stubbed result.

    Supports optional lines, head, tail, or grep filters to avoid context bloat.
    Never raises; returns JSON-encoded result payload or descriptive error.
    """
    try:
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {"result_id": args.strip()}

        if not isinstance(args, dict):
            return json.dumps({"error": "invalid arguments, expected dict or json"})

        result_id = args.get("result_id")
        if not result_id or not isinstance(result_id, str):
            return json.dumps({"error": "missing or invalid result_id argument"})

        result_id = result_id.strip()
        active_conn = conn or store.get_default_connection()

        # Scoped retrieval: check with session_id first; if none or cross-session allowed, check globally
        content = store.get_result(active_conn, result_id, session_id=session_id)
        if content is None and session_id is not None:
            content = store.get_result(active_conn, result_id)

        if content is None:
            return json.dumps({
                "error": "not found or expired",
                "result_id": result_id,
            })

        lines = args.get("lines")
        head = args.get("head")
        tail = args.get("tail")
        grep = args.get("grep")

        sliced_text, was_sliced, meta = slice_content(
            content,
            lines=lines,
            head=head,
            tail=tail,
            grep=grep,
        )

        resp: dict[str, Any] = {
            "result_id": result_id,
            "content": sliced_text,
        }
        if was_sliced:
            resp["sliced"] = True
            resp["total_lines"] = meta["total_lines"]
            resp["returned_lines"] = meta["returned_lines"]

        return json.dumps(resp)
    except Exception as err:
        logger.debug("recall_result execution error: %s", err)
        return json.dumps({"error": f"failed to recall result: {err}"})
