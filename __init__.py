"""toolrecall-light — in-process deterministic deduplication and on-demand recall for Hermes Agent.

Cross-Turn Ground:
Hermes core already deduplicates consecutive tool results within a single turn (via
agent/tool_guardrails.py), but calls reset_for_turn() at every turn boundary.
Large tool outputs (e.g. 40 KB test tracebacks, compiler outputs, build logs, file scans)
repeated on later turns are re-sent in full.

toolrecall-light bridges that turn boundary:
- Maintains session-scoped deduplication across turns.
- Replaces byte-identical repeated tool outputs with a 1-line stub (saving -23% to -30%
  prompt tokens and -23% to -36% cost across turns).
- Registers recall_result(result_id) so the model can restore full byte-exact content on demand.
- Protected by a 5-streak loop breaker and post_compaction generation advancement.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from typing import Any, Optional

try:
    from . import compat, dedup, schemas, store
except (ImportError, ValueError):
    import compat, dedup, schemas, store

logger = logging.getLogger(__name__)

_session_generations: dict[str, int] = {}
_lock = threading.Lock()

# Session identity when the host supplies neither session_id nor task_id. The old
# literal "default" made every id-less session share one `seen` row, so session B
# could receive a stub for content only session A had read — a stub pointing at
# bytes the agent never saw. Process-scoped identity keeps id-less calls isolated
# and stays stable for the life of the process.
_PROCESS_SESSION_ID = f"process-{os.getpid()}-{uuid.uuid4().hex[:8]}"

_first_stub_logged = False

# Opportunistic store hygiene. `results` holds the bytes behind every stub, so
# without a sweep the DB grows for the life of the install; at most one sweep per
# interval, on the stub path only.
_last_gc: float = 0.0
_GC_INTERVAL_S = 6 * 3600.0
_GC_TTL_HOURS = 24.0


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


def _active_session(session_id: Optional[str], task_id: Optional[str]) -> str:
    """Resolve the session key used for stub scoping (never the literal "default")."""
    return session_id or task_id or _PROCESS_SESSION_ID


def _maybe_gc(conn: Any) -> None:
    """Run the store sweep at most once per interval. Never raises."""
    global _last_gc
    now = time.time()
    if now - _last_gc < _GC_INTERVAL_S:
        return
    _last_gc = now
    try:
        removed = store.gc(conn, ttl_hours=_GC_TTL_HOURS)
        if removed:
            logger.info("toolrecall-light: store sweep removed %d stale row(s)", removed)
    except Exception as err:  # noqa: BLE001 — hygiene must never break a turn
        logger.debug("store sweep failed: %s", err)


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

        active_session = _active_session(session_id, task_id)
        conn = store.get_default_connection()
        store.bump_stat(conn, "hook_calls")
        current_gen = get_session_generation(active_session)

        raw_hash = dedup.result_hash(result)
        if dedup.is_normalization_enabled():
            normalized = dedup.normalize_output(result)
            content_hash = dedup.result_hash(normalized)
        else:
            content_hash = raw_hash

        key = dedup.make_key(tool_name, args)
        seen_entry = store.get_seen_entry(conn, active_session, key)

        if seen_entry is None:
            # First time seeing this -> store raw bytes and pass through
            new_id = uuid.uuid4().hex[:12]
            store.put_result(conn, new_id, raw_hash, result, active_session)
            store.upsert_seen(
                conn,
                active_session,
                key,
                content_hash,
                generation=current_gen,
                served_gen=current_gen,
                streak=0,
                last_id=new_id,
            )
            store.bump_stat(conn, "passes")
            return None

        stored_hash, stored_gen, served_gen, streak, last_id = seen_entry

        # Post-compaction generation reset: serve full content once in the new generation
        if served_gen != current_gen:
            new_id = uuid.uuid4().hex[:12]
            store.put_result(conn, new_id, raw_hash, result, active_session)
            store.upsert_seen(
                conn,
                active_session,
                key,
                content_hash,
                generation=current_gen,
                served_gen=current_gen,
                streak=0,
                last_id=new_id,
            )
            store.bump_stat(conn, "passes")
            return None

        # Streak guard: pass through full content every (MAX_STUB_STREAK + 1) calls
        if streak >= dedup.get_max_stub_streak():
            new_id = uuid.uuid4().hex[:12]
            store.put_result(conn, new_id, raw_hash, result, active_session)
            store.upsert_seen(
                conn,
                active_session,
                key,
                content_hash,
                generation=current_gen,
                served_gen=current_gen,
                streak=0,
                last_id=new_id,
            )
            store.bump_stat(conn, "passes")
            return None

        # Check if content matches (identical or normalized match)
        if stored_hash == content_hash:
            # Qualifies for identical stub: retrieve/persist canonical ID and return compact stub
            new_id = uuid.uuid4().hex[:12]
            res_id = store.put_result(conn, new_id, raw_hash, result, active_session)
            store.upsert_seen(
                conn,
                active_session,
                key,
                content_hash,
                generation=current_gen,
                served_gen=served_gen,
                streak=streak + 1,
                last_id=res_id,
            )

            nbytes = len(result.encode("utf-8"))
            store.bump_stat(conn, "stubs")
            store.bump_stat(conn, "last_stub_at", time.time(), absolute=True)
            global _first_stub_logged
            if not _first_stub_logged:
                _first_stub_logged = True
                logger.info(
                    "toolrecall-light: first stub (%s, %d bytes) — dedup is active",
                    tool_name,
                    nbytes,
                )
            _maybe_gc(conn)
            return dedup.format_stub(tool_name, res_id, nbytes)

        # Content changed for the same key: check if diff presentation is beneficial
        if dedup.is_diff_enabled() and last_id:
            prev_content = store.get_result(conn, last_id, session_id=active_session)
            if prev_content is not None:
                diff_text = dedup.compute_diff(
                    prev_content,
                    result,
                    max_diff_chars=dedup.get_max_diff_chars(),
                )
                if diff_text:
                    new_id = uuid.uuid4().hex[:12]
                    curr_id = store.put_result(conn, new_id, raw_hash, result, active_session)
                    store.upsert_seen(
                        conn,
                        active_session,
                        key,
                        content_hash,
                        generation=current_gen,
                        served_gen=served_gen,
                        streak=streak + 1,
                        last_id=curr_id,
                    )
                    nbytes = len(result.encode("utf-8"))
                    store.bump_stat(conn, "stubs")
                    store.bump_stat(conn, "diff_stubs")
                    store.bump_stat(conn, "last_stub_at", time.time(), absolute=True)
                    _maybe_gc(conn)
                    return dedup.format_diff_stub(tool_name, last_id, curr_id, diff_text, nbytes)

        # Content changed and diff not beneficial or not enabled: pass through full content
        new_id = uuid.uuid4().hex[:12]
        store.put_result(conn, new_id, raw_hash, result, active_session)
        store.upsert_seen(
            conn,
            active_session,
            key,
            content_hash,
            generation=current_gen,
            served_gen=current_gen,
            streak=0,
            last_id=new_id,
        )
        store.bump_stat(conn, "passes")
        return None

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


def status() -> dict:
    """Return this plugin's activity counters — the answer to "is it firing?".

    Never raises: an ``{"error": ...}`` payload is itself the diagnosis (loaded
    but cannot persist). Interpretation:
      loads == 0        -> never registered with the host
      hook_calls == 0   -> registered, but the host never invoked the hook
      stubs == 0        -> invoked, nothing qualified (size floor / ignore list)
    """
    try:
        conn = store.get_default_connection()
        stats = store.get_stats(conn)
    except Exception as err:  # noqa: BLE001 — a status call must not raise
        return {"error": str(err)}
    # Co-installation is a status fact, not an error: answer "is core also here?"
    # in the same call that answers "is this plugin firing?".
    stats["core_conflict"] = compat.detect()
    stats["coexistence"] = compat.get_coexistence_state()
    return stats


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
    # One INFO line makes "did it load?" answerable from the host log; the
    # persisted counter makes it survive log rotation.
    try:
        conn = store.get_default_connection()
        store.bump_stat(conn, "loads")
        store.bump_stat(conn, "last_load_at", time.time(), absolute=True)
        logger.info(
            "toolrecall-light loaded (min_chars=%d, ignore=%s, max_stub_streak=%d)",
            dedup.get_min_chars(),
            ",".join(sorted(dedup.DEFAULT_IGNORE_TOOLS)),
            dedup.get_max_stub_streak(),
        )
    except Exception as err:  # noqa: BLE001 — registration must not fail on stats
        logger.warning("toolrecall-light loaded, but activity counters unavailable: %s", err)
    # Co-install check: one actionable line instead of a silent double-dedup.
    # Read-only and fail-open — it never blocks loading.
    try:
        compat.warn_if_core_present()
    except Exception as err:  # noqa: BLE001 — a warning must never break loading
        logger.debug("core-conflict check skipped: %s", err)
