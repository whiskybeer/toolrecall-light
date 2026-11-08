# Security Architecture — `toolrecall-light`

`toolrecall-light` is an **in-process deterministic tool result deduplication and recall plugin** for [Hermes Agent](https://github.com/NousResearch/hermes-agent).

Because autonomous coding agents routinely ingest untrusted data (repository files, compiler output, test tracebacks, and third-party MCP tool responses), `toolrecall-light` assumes that tool arguments and tool outputs may contain adversarial or malformed payloads, including **Prompt Injection** strings, SQL metacharacters, and multi-megabyte output bombs.

> **Looking for host-wide security gates, default-deny path allowlists, MCP keyword access control, or out-of-process daemon isolation?**  
> See [ToolRecall Core (`toolrecall`)](https://github.com/whiskybeer/toolrecall/blob/main/SECURITY.md).

---

## 1. Threat Model & Trust Boundaries

`toolrecall-light` executes inside the Hermes Agent Python process via the official Hermes v2 plugin hooks (`transform_tool_result`, `post_compaction`) and tool registration (`ctx.register_tool("recall_result")`).

| Layer | Trust Status | Security Posture |
|---|---|---|
| **Hermes Agent Process** | Trusted host runtime | Runs under the local OS user; invokes plugin hooks synchronously. |
| **Tool Outputs & Arguments** | **Untrusted** | May contain prompt-injection text, arbitrary binary/UTF-8 sequences, SQL metacharacters, or multi-megabyte output bombs. |
| **LLM Reasoning Context** | **Assumed Compromised** | A prompt-injected model may call `recall_result` with malformed arguments, SQL injection strings, or arbitrary IDs. |
| **Local SQLite Database** | Local user-scoped file | Stored at `~/.hermes/plugin-data/toolrecall-light/data.db` with WAL journaling and parameterized SQL queries. |
| **External Network** | **Zero Egress** | `toolrecall-light` makes zero outbound network connections, collects zero telemetry, and imports zero third-party packages. |

---

## 2. Input Sanitation & Exploit Mitigations

### A. SQL Injection Prevention (`store.py`)
All tool keys, SHA-256 hashes, session identifiers, and raw tool outputs are persisted in a local SQLite database.
- **Mitigation:** Every query in `store.py` uses strict parameterized `?` placeholders via Python's standard `sqlite3` module (e.g., `SELECT id FROM results WHERE hash = ? LIMIT 1`, `INSERT INTO results (id, hash, content, created_at, session_id) VALUES (?, ?, ?, ?, ?)`).
- **Result:** Neither tool arguments, tool outputs, nor `recall_result(result_id=...)` calls from a prompt-injected model can inject SQL syntax, alter schema state, or bypass query predicates.

### B. Out-of-Memory (OOM) & Output Bomb Protection (`dedup.py`)
A runaway command or adversarial tool might emit tens or hundreds of megabytes of stdout, attempting to exhaust memory or bloat the SQLite database during hashing and storage.
- **Mitigation:** `dedup.should_dedup` enforces both a lower bound (`HERMES_TOOLRECALL_LIGHT_MIN_CHARS`, default `2,000` chars) and a strict upper bound (`HERMES_TOOLRECALL_LIGHT_MAX_CHARS`, default `2,000,000` chars / ~2 MB) **before** computing SHA-256 digests or writing to SQLite.
- **Result:** Payloads larger than `2 MB` bypass the deduplication store immediately without allocating secondary copies in SQLite.

### C. Cryptographic Content Verification (`SHA-256`)
Caching or stubbing solely by `(tool_name, args)` is unsafe for stateful tools like `terminal` or `search_files`, where running the same command after an edit produces different output.
- **Mitigation:** `toolrecall-light` never stubs a tool call based on its arguments alone. On every invocation, `_on_transform_tool_result` computes the full `hashlib.sha256(result.encode("utf-8")).hexdigest()` of the freshly produced output and compares it against the last seen hash for `(session_id, key)`:
  - If `seen[0] != content_hash` (even a 1-byte change in output), the full new output is passed through untouched to the model and the stored hash is updated.
  - A compact stub is emitted **only** when both the canonical key `(tool_name, canonical_args)` and the full `SHA-256` output digest are byte-for-byte identical.

### D. Canonical Key Separation & Metadata Stripping
- **Null-Byte Delimiter:** `dedup.make_key(tool_name, args)` joins `tool_name` and canonical JSON arguments using a `\x00` separator (`f"{tool_name}\x00{canonical_args(args)}"`), preventing key-collision ambiguity between tool names and argument payloads.
- **Volatile Metadata Stripping:** `dedup.clean_volatile_args` strips only non-semantic transport metadata (`timeout`, `_meta`, `api_request_id`) while sorting all remaining JSON keys deterministically (`sort_keys=True`).

### E. 100% Fail-Open Execution Guarantee (`__init__.py`, `schemas.py`)
A context optimization plugin must never become a single point of failure for agent execution.
- **Hook Fail-Open:** Both `_on_transform_tool_result` and `_on_post_compaction` wrap their entire execution path in `try ... except Exception` blocks that log at `DEBUG` level and return `None`. In Hermes v2, returning `None` from `transform_tool_result` passes the original raw tool output through untouched.
- **Tool Handler Fail-Safe:** `schemas.handle_recall` catches all exceptions and malformed input types (raw strings, invalid JSON, missing keys, non-dict payloads) and returns a structured JSON error payload rather than raising an unhandled exception into the agent loop.

### F. Loop-Breaker & Compaction Blindness Safeguards
- **Streak Guard (`HERMES_TOOLRECALL_LIGHT_MAX_STUB_STREAK = 5`):** If an autonomous agent repeatedly invokes the exact same command and receives 5 consecutive stubs, the 6th invocation forces a full raw output pass-through and resets `streak = 0`. This breaks infinite evaluation loops if a model ignores stub instructions.
- **Compaction Generation Reset (`post_compaction`):** When Hermes compacts conversation history (`/compress`), the earlier turn containing the full tool output may be summarized away. The `post_compaction` hook increments the per-session generation counter (`_session_generations[session_id]`), forcing the first repeat in the new generation to re-serve the full output once before resuming stubbing.

---

## 3. Data Storage, Privacy & Local Disk Security

### Zero External Dependencies & Zero Telemetry
- **Runtime Dependencies:** None. `toolrecall-light` uses exclusively Python 3.10+ standard library modules (`hashlib`, `json`, `logging`, `os`, `pathlib`, `sqlite3`, `tempfile`, `threading`, `time`, `uuid`).
- **Supply-Chain Surface:** There are no third-party PyPI packages, compiled C extensions, or post-install scripts.
- **Network Activity:** Zero. All hashing, deduplication, and recall operations occur strictly in local memory and local SQLite storage.

### Local SQLite Storage (`~/.hermes/plugin-data/toolrecall-light/data.db`)
- **Plaintext-at-Rest Notice:** To support exact byte-for-byte restoration via `recall_result(result_id)`, `toolrecall-light` stores raw tool outputs in the `results` table of `data.db`. If a tool command prints sensitive values (e.g., environment variables or tokens) to stdout and the output exceeds `MIN_CHARS` (`2,000` characters), that output will reside in `data.db` until evicted.
- **TTL Garbage Collection:** `store.gc(conn, ttl_hours=24.0)` evicts cached results older than 24 hours (configurable via `ttl_hours` in `plugin.yaml`).
- **Filesystem Permissions:** Ensure `~/.hermes/plugin-data/` is owned by your local OS user with restricted permissions (`chmod 700 ~/.hermes/plugin-data`). Do not share `data.db` across untrusted OS users on a multi-tenant host.

---

## 4. Known Limitations & Non-Goals

| Area | Honest Assessment |
|---|---|
| **Not an OS Sandbox or Firewall** | `toolrecall-light` observes and transforms tool results *after* a tool has executed. It does not block dangerous shell commands, restrict file paths, or sandbox subprocesses. Pair Hermes with OS-level containment (Docker, Bubblewrap, or [ToolRecall Core](https://github.com/whiskybeer/toolrecall)) when running untrusted workloads. |
| **Not a Prompt-Injection Filter** | When a tool output is seen for the first time (or recalled via `recall_result`), its exact bytes are delivered to the model. `toolrecall-light` does not scan tool outputs for prompt-injection instructions. |
| **Single-User Local Store** | `store.put_result` deduplicates identical content hashes across the local SQLite database, and `schemas.handle_recall` falls back to global `result_id` lookup (`uuid.uuid4().hex[:12]`, 48 bits of entropy) if a session ID is omitted or mismatched. This design is intended for a single local user's Hermes sessions, not multi-tenant isolation. |
| **No Hook Order Priority Override** | Hermes v2 invokes `transform_tool_result` hooks in plugin load order. If another active plugin mutates tool outputs before `toolrecall-light`, `toolrecall-light` hashes and stores the mutated string. |

---

## 5. Reporting Security Issues

This project is maintained by **Robin Schultka** ([`@whiskybeer`](https://github.com/whiskybeer)).

- **Email:** [robin@toolrecall.dev](mailto:robin@toolrecall.dev)
- **GitHub Issues:** Open an issue with the `security` label at [https://github.com/whiskybeer/toolrecall-light/issues](https://github.com/whiskybeer/toolrecall-light/issues)
