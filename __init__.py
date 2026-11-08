"""toolrecall-light — in-process deterministic deduplication and on-demand recall for Hermes Agent.

Replaces byte-identical repeated tool outputs with a compact stub before they enter model
context and registers recall_result to restore the full content on demand.
"""

from __future__ import annotations

import logging
import threading
import uuid
from typing import Any, Optional

try:
    from . import dedup, schemas, store
except (ImportError, ValueError):
    import dedup, schemas, store

logger = logging.getLogger(__name__)

_session_generations: dict[str, int] = {}
_lock = threading.Lock()


def get_session_generation(session_id: str) -> int:
    """Return the current generation counter for a session."""
    with _lock:
        return _session_generations.get(session_id, 0)


def advance_session_generation(session_id: str) -> int:
    """Increment and return the compaction generation for a session."""
    with _lock:
        gen = _session_generations.get(session_id, 0) + 1
        _session_generations[session_id] = gen
        return gen


def _on_transform_tool_result(
    tool_name: str,
    args: Any,
    result: Any,
    task_id: Optional[str] = None,
    session_id: Optional[str] = None,
    status: Optional[str] = None,
    error_type: Optional[str] = None,
    **kwargs: Any,
) -> Optional[str]:
    """Hook invoked by Hermes before a tool result enters model context.

    Fail-open: any exception returns None (preserving original result untouched).
    """
    try:
        if not dedup.should_dedup(tool_name, args, result, status=status, error_type=error_type):
            return None

        active_session = session_id or task_id or "default"
        conn = store.get_default_connection()
        current_gen = get_session_generation(active_session)

        content_hash = dedup.result_hash(result)
        key = dedup.make_key(tool_name, args)
        seen = store.get_seen(conn, active_session, key)

        if seen is None or seen[0] != content_hash:
            # First time seeing this (or output changed) -> store and pass through
            new_id = uuid.uuid4().hex[:12]
            store.put_result(conn, new_id, content_hash, result, active_session)
            store.upsert_seen(
                conn,
                active_session,
                key,
                content_hash,
                generation=current_gen,
                served_gen=current_gen,
                streak=0,
            )
            return None

        # Key already seen with identical content
        _stored_hash, _stored_gen, served_gen, streak = seen

        # Post-compaction generation reset: serve full content once in the new generation
        if served_gen != current_gen:
            store.upsert_seen(
                conn,
                active_session,
                key,
                content_hash,
                generation=current_gen,
                served_gen=current_gen,
                streak=0,
            )
            return None

        # Streak guard: pass through full content every (MAX_STUB_STREAK + 1) calls
        if streak >= dedup.get_max_stub_streak():
            store.upsert_seen(
                conn,
                active_session,
                key,
                content_hash,
                generation=current_gen,
                served_gen=current_gen,
                streak=0,
            )
            return None

        # Qualifies for stubbing: retrieve/persist canonical ID and return compact stub
        new_id = uuid.uuid4().hex[:12]
        res_id = store.put_result(conn, new_id, content_hash, result, active_session)
        store.upsert_seen(
            conn,
            active_session,
            key,
            content_hash,
            generation=current_gen,
            served_gen=served_gen,
            streak=streak + 1,
        )

        nbytes = len(result.encode("utf-8"))
        return dedup.format_stub(tool_name, res_id, nbytes)

    except Exception as err:
        logger.debug("transform_tool_result hook error (failing open): %s", err)
        return None


def _on_post_compaction(session_id: Optional[str] = None, **kwargs: Any) -> None:
    """Observer hook fired after context compaction committed.

    Advances the generation counter so subsequent repeats re-serve full content once.
    """
    try:
        if session_id:
            advance_session_generation(session_id)
    except Exception as err:
        logger.debug("post_compaction hook error: %s", err)


def register(ctx: Any) -> None:
    """Register hooks and tools with the Hermes PluginContext."""
    ctx.register_hook("transform_tool_result", _on_transform_tool_result)
    ctx.register_hook("post_compaction", _on_post_compaction)
    ctx.register_tool(
        name="recall_result",
        toolset="toolrecall_light",
        schema=schemas.RECALL_RESULT,
        handler=schemas.handle_recall,
        description=schemas.RECALL_RESULT["description"],
    )
