# Architecture & Internals — `toolrecall-light`

> In-process context deduplication, unified diff stubbing, and on-demand tool recall for [Hermes Agent](https://github.com/NousResearch/hermes-agent).

---

## 1. Overview & Core Mission

Large language models in autonomous agent loops compound context with every turn. When tools repeatedly emit large payloads—such as recurring test runs, build outputs, or file inspections—context windows inflate rapidly, accelerating compaction cycles and increasing inference costs.

While Hermes Agent includes an intra-turn deduplication guardrail (`IDENTICAL_RESULT_STUB_MIN_CHARS = 512`), it resets its tracking table at every turn boundary (`reset_for_turn()`). A 40 KB test traceback emitted on turn 1 is re-sent in full on turns 2, 3, and 4.

`toolrecall-light` operates as an in-process plugin for Hermes Agent that:
1. **Tracks outputs across turns** within a session using local SQLite storage.
2. **Normalizes volatile output noise** (ANSI colors, execution timings, timestamps, JSON key orders) before hashing.
3. **Serves unified diff stubs** when consecutive runs produce small deltas rather than identical bytes.
4. **Enables precision sliced recall** (`recall_result`), allowing the agent to fetch targeted line ranges or regex matches without dumping entire payloads back into context.

---

## 2. System Architecture & Execution Flow

```text
                     Hermes Tool Execution Result
                                  │
                                  ▼
                Hook: _on_transform_tool_result()
                                  │
                  [Eligible Tool & Result Size?]
               (>= 2,000 chars, <= 2,000,000 chars)
                                  │
                 ├── No ──────────┴────────────────────────┐
                 │                                         │
                 ▼                                         ▼
      Volatile Normalization                     Pass Full Content
   (Strip ANSI, timings, JSON sort)              Through to Context
                 │
                 ▼
          Canonical SHA-256
                 │
                 ▼
       Query Local SQLite Store
                 │
      ┌──────────┴─────────────────────────┐
      ▼                                    ▼
Exact Match Found?                  Hash Mismatch
(Same tool & normalized content)    (Prior result exists for tool?)
      │                                    │
      ▼                                    ▼
Streak >= 5?                       Compute Unified Diff
(Loop Breaker)                    (difflib.unified_diff)
  ├── Yes ──► Pass Full Send               │
  │           (Reset streak)       Diff < 4KB & < 50% size?
  └── No  ──► Emit Exact Stub        ├── Yes ──► Emit Diff Stub
              (Record stub)          └── No  ──► Store & Pass Full
```

---

## 3. Component Pipeline

### A. Interception & Qualification (`__init__.py`, `dedup.py`)
- Registered via the official Hermes v2 plugin interface using the `transform_tool_result` hook.
- `dedup.should_dedup(tool_name, args, result)` acts as the primary gatekeeper:
  - Validates minimum size (`HERMES_TOOLRECALL_LIGHT_MIN_CHARS`, default 2,000 characters).
  - Enforces safety ceiling (`HERMES_TOOLRECALL_LIGHT_MAX_CHARS`, default 2 MB).
  - Skips error states (`status == "error"`) and reserved tools (`recall_result`).

### B. Volatile Output Normalization (`dedup.py`)
Outputs that are semantically identical often differ by ephemeral runtime noise. The normalization pipeline transforms text before calculating hashes:
1. **ANSI Sequence Stripping:** Removes terminal formatting, colors, and cursor controls via regex `\x1b\[[0-9;]*[a-zA-Z]`.
2. **Timing & Timestamp Scrubbing:** Replaces execution durations (e.g. `in 0.42s`, `took 12.3ms`) and standard ISO/RFC timestamps with normalized tokens.
3. **Canonical JSON Serialization:** If content is valid JSON, parses and reformats with `sort_keys=True` and normalized spacing, ignoring non-semantic key ordering.
4. **Whitespace Sanitization:** Normalizes CRLF (`\r\n` to `\n`) and strips trailing line whitespace.

*Note: Normalization is used exclusively for hash matching and diff computation. The raw, byte-for-byte un-normalized payload is always stored in SQLite.*

### C. Dual Deduplication Engines (`dedup.py`, `store.py`)

#### 1. Exact Match Engine & Streak Guard
- Computes SHA-256 digest over normalized content.
- If the digest exists in `seen` for the current session and generation:
  - Increments consecutive stub streak for that key.
  - If streak reaches `MAX_STUB_STREAK` (default 5), passes full content through once to guarantee the LLM cannot be trapped in an unrecoverable reasoning loop.
  - Otherwise, delivers a compact 1-line stub:
    ```text
    [toolrecall-light] identical to earlier terminal result 50f59afa067a (2317 bytes) in this session.
    Call recall_result(result_id="50f59afa067a") if you need the full content.
    ```

#### 2. Unified Diff Engine
- When an execution produces a new hash, `store.get_last_seen_id(session_id, tool_name)` checks if an earlier result was stored for that tool in the same session.
- If a predecessor exists, `dedup.compute_diff()` compares the normalized prior content with the current content.
- If the resulting unified diff is:
  - Less than `MAX_DIFF_CHARS` (default 4,000 characters), and
  - Less than 50% of the raw output length,
- The plugin emits a unified diff stub:
    ```text
    [toolrecall-light] modified output for terminal (178 bytes diff vs 39680 bytes full).
    Diff against earlier result 8a1b2c3d:
    --- previous
    +++ current
    @@ -298,3 +298,3 @@
     LINE_0299: status=OK
    -LINE_0300: VALUE=ORIGINAL_KEY
    +LINE_0300: VALUE=MODIFIED_KEY
     LINE_0301: status=OK
    Call recall_result(result_id="...", lines="...", grep="...") if you need full content.
    ```

### D. Targeted Sliced Recall (`schemas.py`)
When the model invokes `recall_result`:
1. Retrieves the original, un-altered content from SQLite.
2. **JSON Envelope Unwrapping:** If the original tool result was wrapped in a JSON structure (e.g. `{"content": "..."}` or `{"output": "..."}`), extracts the primary text for filtering while preserving context.
3. **Line-Range Slicing (`lines="10-50"`):** Extracts 1-indexed line spans.
4. **Head / Tail Slicing (`head=N`, `tail=N`):** Returns the leading or trailing lines.
5. **Pattern Search (`grep="ERROR:"`):** Performs case-sensitive regex/substring matching, returning matching lines prefixed with original line numbers:
   ```text
   L0048: assert response.status_code == 200
   L0085: [ERROR] Authentication failed for user alice: Signature expired
   ```

### E. Coexistence & Environmental Detection (`compat.py`)
- Automatically probes for the ToolRecall full-stack daemon:
  - Checks active Unix Domain Socket (`/tmp/toolrecall.sock` or `TOOLRECALL_UDS_PATH`).
  - Probes local forward proxy port (`127.0.0.1:8569`).
  - Scans for MCP multiplexer processes.
- Reports coexistence state cleanly via `status()["core_active"]` without crashing or throwing errors.

---

## 4. Storage Architecture (`store.py`)

Persistent state is stored in SQLite under `~/.hermes/plugins/toolrecall-light/store.db` (configurable via `HERMES_HOME`).

### Database Schema

```sql
CREATE TABLE IF NOT EXISTS seen (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    tool_hash TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    content TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    created_at REAL NOT NULL,
    generation INTEGER DEFAULT 0,
    last_id TEXT
);

CREATE TABLE IF NOT EXISTS stats (
    key TEXT PRIMARY KEY,
    value INTEGER DEFAULT 0
);
```

- **Generation Resets:** Hermes compaction events (`post_compaction` hook) advance the session generation counter. The first repeat post-compaction serves full content once in the new generation to ensure the compressed summary retains critical facts.
- **Garbage Collection:** Automatic TTL sweep runs opportunistically every 6 hours (`gc_ttl_hours = 24`), pruning records older than the retention threshold.

---

## 5. Design Principles

1. **Zero Runtime Dependencies:** Built strictly on the Python 3.10+ standard library (`sqlite3`, `difflib`, `hashlib`, `json`, `re`). Requires no third-party wheels or virtual environment bloat.
2. **100% Fail-Open:** Every public hook and entry point is wrapped in defensive exception handlers. Any storage or parsing error returns `None`, guaranteeing that tool results pass through to the model untouched rather than interrupting an agent task.
3. **Deterministic & Auditable:** All hashing is canonical. Cache keys, diffs, and sliced extracts are fully reproducible.
4. **Local-Only & Private:** No network egress, analytics, or external telemetry. All caching occurs on local disk within the user's Hermes directory.
