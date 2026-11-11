# ToolRecall Light (`toolrecall-light`)

> **Deterministic context deduplication and on-demand tool recall for [Hermes Agent](https://github.com/NousResearch/hermes-agent).**  
> *Looking for host-wide multi-agent pooling, MCP multiplexing, or forward proxy stream replay? See [ToolRecall Core](https://toolrecall.dev).*

Replaces byte-identical repeated tool outputs (`terminal`, `search_files`, MCP servers, connectors, third-party toolsets) with a compact stub **before they enter model context**, and exposes an explicit `recall_result(result_id)` tool so the model can restore the full original bytes on demand.

[![ToolRecall Light Walkthrough Video](assets/thumbnail.png)](assets/demo.mp4)
> 🎥 **[Watch the Narrated Walkthrough Video (`assets/demo.mp4`, 1080p with English Voiceover)](assets/demo.mp4)** — or run `python3 demo.py` locally in your terminal.

---

## Why ToolRecall Light?

Hermes core already deduplicates file reads and skill views (`tools/file_tools.py` and `tools/skills_tool_dedup.py`). However, core **does not deduplicate any other tool**:
- Repetitive test runs (`terminal` running `pytest`, `cargo test`, `npm test`)
- Repetitive build commands, compiler tracebacks, git status queries
- Repetitive codebase scans (`search_files`)
- MCP servers, web extracts, and connector responses

In real multi-turn agent sessions, these un-deduplicated tools compound on every subsequent turn. A 10 KB duplicate emitted on Turn 4 is re-sent on Turns 5, 6, 7 ... 20, wasting hundreds of thousands of prompt tokens and exhausting context windows.

`toolrecall-light` intercepts these repeats before they enter context, saves them to local SQLite, and serves a 1-line stub. If the model ever needs the exact original lines back, it uses the tool to recall tools: `recall_result`.

---

## Empirical Receipts (from Real `state.db` Data)

We queried 668 real Hermes sessions in `state.db` (131,520 tool results):
* **60.1%** of all tool results $\ge 2,000$ characters are **exact intra-session duplicates**.
* That accounts for **74.5 MB** (~18.6M tokens) of redundant tool outputs re-ingested across turns.
* `terminal` alone generated over **2,067** large duplicate results (>10 MB raw).

In our simulated 20-turn SWE debugging benchmark:
* **-54.04% prompt tokens avoided** (73,460 tokens saved in a single session).
* **0.23 ms** average recall latency.

---

## Model-Facing Stub & Recall

When an identical result is detected, the model context receives a compact stub:

```
[toolrecall-light] identical to earlier terminal result 50f59afa067a (2317 bytes) in this session.
Call recall_result(result_id="50f59afa067a") if you need the full content.
```

If the agent needs the full traceback or compiler lines, it invokes `recall_result`:
```json
{
  "name": "recall_result",
  "arguments": {"result_id": "50f59afa067a"}
}
```
`recall_result` returns the exact byte-for-byte original content in **sub-millisecond time**.

---

## Hardened Safety & Non-Goals

1. **Loop Breaker (Streak Guard):** Forces full content pass-through after 5 consecutive stubs for the same key (configurable via `HERMES_TOOLRECALL_LIGHT_MAX_STUB_STREAK`) to prevent stuck model evaluation loops while preserving 95.4% deduplication efficiency.
2. **Compaction Aware (`post_compaction`):** When history is compacted (`/compress`), the generation advances; the first repeat post-compaction re-serves full content once before re-stubbing.
3. **100% Fail-Open:** Any database error or exception cleanly returns `None` (passes through raw result untouched).
4. **Zero Runtime Dependencies:** Python stdlib only (`sqlite3`, `hashlib`, `json`).
5. **No Core Overrides:** Built on official Hermes v2 hooks (`transform_tool_result`, `post_compaction`) and `ctx.register_tool` (Catalog Rule 9 compliant).
6. **Defense-in-Depth Security:** Parameterized SQL queries, 2 MB payload cap (`MAX_CHARS`), cryptographic SHA-256 verification, and zero network egress. See [`SECURITY.md`](SECURITY.md) for the full threat model and trust boundary documentation.

---

## Installation

### Via Hermes Plugins CLI (Recommended)

```bash
hermes plugins install https://github.com/whiskybeer/toolrecall-light
```

### Via Git Clone

```bash
git clone https://github.com/whiskybeer/toolrecall-light.git ~/.hermes/plugins/toolrecall-light
hermes plugins enable toolrecall-light
```

### Local Development Symlink

```bash
mkdir -p ~/.hermes/plugins
ln -s /path/to/hermes-tool-recall ~/.hermes/plugins/toolrecall-light
hermes plugins enable toolrecall-light
```

---

## Is it firing? (activation is observable)

"Installed" is not "active". A plugin can be on disk, listed in
`plugins.enabled`, and still never run — a mismatched manifest key, a hook the
host does not invoke, or every result under the size floor all look identical
from the outside. This plugin therefore persists its own counters and exposes
`status()`:

| Signal | Meaning |
|---|---|
| `loads == 0` | never registered with the host — check the **manifest key** (`toolrecall-light`), not the directory name, in `plugins.enabled` |
| `hook_calls == 0` | registered, but `transform_tool_result` is never invoked |
| `stubs == 0`, `hook_calls > 0` | invoked; nothing qualified (`min_chars` floor, or every tool on the ignore list) |
| `stubs > 0` | working — `last_stub_at` is the last stub time, `passes` the full sends |

Enable it by the manifest key, never by path:

```bash
hermes plugins enable toolrecall-light     # key from plugin.yaml "name:"
hermes plugins list | grep toolrecall      # must read "enabled"
```

Counters live in the plugin's own SQLite store (`stats` table) and survive log
rotation; `loads`/`first stub` also emit one INFO line each so the host log
answers "did it load?" without a shell.

Store hygiene: results carry the bytes behind every stub, so a sweep runs at
most every 6 h on the stub path (`gc_ttl_hours = 24`), pruning expired results
**and** dead per-session rows.

## Validation & Testing

```bash
# Run test suite (29/29 unit & integration tests)
pytest tests/ -v

# Run official Hermes plugin admission check
hermes plugins validate .
```

---

## Configuration

| Environment Variable | Default | Description |
|---|---|---|
| `HERMES_TOOLRECALL_LIGHT` | `1` | Global kill-switch (`0` or `off` to disable). Also respects legacy `HERMES_TOOL_RECALL`. |
| `HERMES_TOOLRECALL_LIGHT_MIN_CHARS` | `2000` | Minimum character length before deduplication is considered. |
| `HERMES_TOOLRECALL_LIGHT_MAX_CHARS` | `2000000` | Maximum character length (2 MB; `0` = unlimited). |
| `HERMES_TOOLRECALL_LIGHT_MAX_STUB_STREAK` | `5` | Consecutive stubs before forcing full pass-through once. |
| `ttl_hours` | `24` | TTL duration for database garbage collection. |

---

## Benchmark Receipt (20-turn SWE Trajectory)

| Metric | Baseline (Vanilla) | Treatment (With toolrecall-light) | Impact |
|---|---|---|---|
| **Stubs Emitted** | 0 | 11 stubs | - |
| **Deduplicated Bytes** | 0 B | 33,639 B | - |
| **Cumulative Prompt Tokens** | 135,937 tokens | 62,477 tokens | **-73,460 tokens (-54.04%)** |
| **Billed Cost ($3/M tokens)**| $0.4078 | $0.1874 | **$0.2204 saved (54.04%)** |
| **Recall Latency** | N/A | 0.23 ms | Sub-millisecond |

See full report: [`bench/results/BENCHMARK_REPORT.md`](bench/results/BENCHMARK_REPORT.md)

---

## License

[MIT](LICENSE) © 2026 Robin Schultka <robin@toolrecall.dev>  
Project page: [toolrecall.dev](https://toolrecall.dev)
