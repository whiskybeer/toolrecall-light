# Changelog

All notable changes to `toolrecall-light` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.3] - 2026-11-12

### Added
- **Unified Diff Stubs:** Automatic delta computation for modified tool executions (`HERMES_TOOLRECALL_LIGHT_DIFF=1`). When a tool output differs slightly from its previous run and the diff is <4,000 characters and <50% of original payload size, a compact unified diff stub is emitted instead of re-sending the full output.
- **Volatile Output Normalization:** Normalizes runtime noise before SHA-256 hashing (`HERMES_TOOLRECALL_LIGHT_NORMALIZE=1`), including ANSI escape stripping, execution duration/timestamp scrubbing, CRLF standardization, and canonical JSON key sorting.
- **Precision Sliced Recall:** Extended `recall_result` schema and handler with targeted slicing parameters:
  - `lines`: 1-indexed line range extraction (e.g. `"10-50"`).
  - `head` / `tail`: Leading or trailing line extraction.
  - `grep`: Case-sensitive substring and regex matching with line-number prefixes.
  - JSON envelope unwrapping: Automatically unwraps nested JSON tool outputs (e.g. `{"content": "..."}`) before slicing.
- **Host Coexistence Probing:** Added `compat.py` to probe for active ToolRecall full-stack daemons via Unix Domain Socket (`/tmp/toolrecall.sock` / `TOOLRECALL_UDS_PATH`), forward proxy port `:8569`, and MCP bridge, reporting status cleanly via `status()["core_active"]`.
- **Live Multi-Turn Benchmark:** Added reproducible benchmark harness (`bench/run_live_multiturn_bench.py`) supporting both deterministic simulated replay and live Hermes Agent execution with `--usage-file`.
- **Empirical Benchmark Receipts:** Documented live multi-turn receipts in `bench/results/LIVE_MULTITURN_REPORT.md` (−24.32% prompt token reduction, 99.56% tool output reduction on diff stubs, 99.2% reduction on sliced recall).
- **Architecture Documentation:** Added `docs/ARCHITECTURE.md` detailing system flow, SQLite schema, component interfaces, and failure semantics, alongside pipeline diagrams in `README.md`.

### Changed
- Expanded automated test suite from 29 to 51 unit and integration tests across 5 test suites.
- Added `last_id` tracking in SQLite schema (`seen` table) to support diffing against previous tool results.

---

## [0.1.1] - 2026-11-08

### Added
- Initial public release of `toolrecall-light` for Hermes Agent.
- In-process hook integration using official Hermes v2 `transform_tool_result` and `post_compaction` hooks.
- Exact SHA-256 deduplication for results ≥2,000 characters.
- In-process SQLite store (`~/.hermes/plugins/toolrecall-light/store.db`) with automatic TTL pruning.
- Streak guard loop breaker forcing full pass-through after 5 consecutive stubs.
- `recall_result` tool to restore byte-for-byte full content on demand.
