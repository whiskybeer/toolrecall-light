"""Tool schemas and handlers for toolrecall-light."""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

try:
    from . import store
except (ImportError, ValueError):
    import store

logger = logging.getLogger(__name__)

RECALL_RESULT = {
    "name": "recall_result",
    "description": (
        "Return the full content of a tool result that toolrecall-light replaced with a compact stub. "
        "Pass the result_id copied directly from the [toolrecall-light] stub."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "result_id": {
                "type": "string",
                "description": "Result ID copied from a [toolrecall-light] stub message.",
            }
        },
        "required": ["result_id"],
    },
}


def handle_recall(
    args: Any,
    conn: Optional[Any] = None,
    session_id: Optional[str] = None,
    **kwargs: Any,
) -> str:
    """Execute recall_result tool to fetch original bytes for a stubbed result.

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

        return json.dumps({
            "result_id": result_id,
            "content": content,
        })
    except Exception as err:
        logger.debug("recall_result execution error: %s", err)
        return json.dumps({"error": f"failed to recall result: {err}"})
