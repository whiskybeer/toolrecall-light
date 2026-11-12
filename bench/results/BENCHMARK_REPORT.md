# Benchmark Report — toolrecall-light

> **The tool to recall tools.**  
> Deterministic context deduplication and on-demand tool recall for [Hermes Agent](https://github.com/NousResearch/hermes-agent).

## 0. Provenance — read this before quoting a number

| Section | Kind | Reproducible | What it measures |
|---|---|---|---|
| 1. Executive summary + §3 per-turn table | **Modelled** | `python3 bench/run_benchmark.py` | A hand-written 20-turn transcript; tokens = `chars / 4`; cost = $3.00/M assumption. **Not a session.** |
| 2. Macro receipts (`state.db`) | **Measured** (snapshot) | SQL over `~/.hermes/state.db` | Duplication in real transcripts. Re-computed 2026-11: 659 sessions, 28,866 candidates ≥2 KB, 16,859 duplicate rows, 74.6 MB. |
| 4. Streak guard / capture efficiency | **Derived** | replay of the duplicate corpus | Share of duplicates still caught at `streak = 5` — not a cost reduction. |
| 5. Recall latency | **Microbenchmark** | `run_benchmark.py` | In-process SQLite round-trip (~0.28 ms). |
| 6. Cross-turn exact dedup | **Measured** (live session) | `hermes chat` with `--usage-file` | Suppression of exact 40 KB results across turn boundaries (−24.6% to −29.6% prompt tokens). |
| 7. Cross-turn diff stubs & sliced recall | **Measured** (live session) | `bench/run_live_multiturn_bench.py` | Suppression of modified 40 KB results via 178-byte diff stubs (−24.3% prompt tokens; 99.2% sliced recall reduction). |

The modelled percentage holds **only** in its own regime (small fixed context, duplicates dominate
it). A measured live session with a ~12 k-token system prompt and 6 × 7 KB duplicates showed ~0.5 %
difference, inside noise — see the repo README, "Evidence, by provenance".

## 1. Executive Summary — modelled simulation

- **Simulated Scenario:** 20-turn SWE debugging session (pytest, file search, git status, compaction, recall).
- **Total Turns Executed:** 18
- **Stubs Emitted:** 11
- **Bytes Deduplicated:** 35,786 bytes
- **Net Prompt Tokens Avoided:** **73,348 tokens**
- **Compounded Context Inflation Reduction:** **53.96%**
- **Baseline Billed Cost (at $3/M prompt tokens):** $0.4078
- **Treatment Billed Cost (at $3/M prompt tokens):** $0.1878
- **Per-Session Dollar Savings:** **$0.2200** (53.96% reduction)
- **Average On-Demand Recall Latency:** **0.28 ms** (sub-millisecond restoration)

## 2. Macro Empirical Receipts (Real Historical Hermes Sessions)

Empirical query of `668` real agent sessions in Hermes `state.db` (131,520 total tool results):

- **Tool Results $\ge 2,000$ Chars:** 28,003
- **Exact Intra-Session Duplicates:** **16,844 (60.15%)**
- **Total Redundant Tool Output:** **71.09 MB** (74,542,086 bytes)
- **Estimated Redundant Prompt Tokens:** **~18,635,521 tokens**

### Duplication Breakdown by Tool Category

| Tool Name | Duplicate Count | Redundant Volume |
|---|---|---|
| `terminal` | 2,067 | 10.15 MB |
| `read_file` | 1,182 | 7.49 MB |
| `session_search` | 136 | 2.63 MB |
| `skill_view` | 178 | 2.42 MB |
| `patch` | 333 | 1.08 MB |
| `browser_exec` | 174 | 0.96 MB |
| `search_files` | 233 | 0.93 MB |
| `web_search` | 102 | 0.43 MB |
| `execute_code` | 58 | 0.41 MB |
| `skills_list` | 30 | 0.30 MB |
| `web_extract` | 22 | 0.13 MB |
| `tool_describe` | 16 | 0.07 MB |
| `tool_search` | 10 | 0.02 MB |

## 3. Micro A/B Simulation: Compounded Context Growth

In multi-turn agent sessions, prompt context compounds on every turn. A duplicated test run or search result emitted early continues to inflate every subsequent call.

| Turn | Baseline Tokens | Treatment Tokens | Tokens Avoided | Reduction |
|---|---|---|---|---|
| 01 | 889 | 889 | 0 | 0.0% |
| 02 | 1,968 | 1,968 | 0 | 0.0% |
| 03 | 2,494 | 2,494 | 0 | 0.0% |
| 04 | 3,383 | 2,538 | 845 | 25.0% |
| 05 | 4,463 | 2,584 | 1,879 | 42.1% |
| 06 | 4,476 | 2,597 | 1,879 | 42.0% |
| 07 | 5,002 | 2,641 | 2,361 | 47.2% |
| 08 | 5,891 | 2,685 | 3,206 | 54.4% |
| 09 | 6,780 | 2,730 | 4,050 | 59.7% |
| 10 | 7,670 | 2,774 | 4,896 | 63.8% |
| 11 | 8,559 | 3,663 | 4,896 | 57.2% |
| 12 | 9,448 | 3,707 | 5,741 | 60.8% |
| 13 | 10,527 | 4,787 | 5,740 | 54.5% |
| 14 | 11,607 | 4,832 | 6,775 | 58.4% |
| 15 | 12,133 | 5,358 | 6,775 | 55.8% |
| 16 | 12,659 | 5,402 | 7,257 | 57.3% |
| 17 | 13,548 | 5,446 | 8,102 | 59.8% |
| 18 | 14,437 | 5,491 | 8,946 | 62.0% |

## 4. Safety & Liveness Guarantees Tested

1. **Loop Breaker (Streak Guard):** Forces full content pass-through after 5 consecutive identical stubs (configurable via `HERMES_TOOL_RECALL_MAX_STUB_STREAK`), preventing agent reasoning loops while preserving 95.4% deduplication efficiency.
2. **Compaction Awareness (`post_compaction`):** Turn 11 triggered history compaction; Turn 12 served full content once in the new generation before resume-stubbing.
3. **On-Demand Precision Recall:** Turn 14 invoked `recall_result(result_id=...)`, restoring 100% byte-for-byte fidelity in **sub-millisecond time**.

## 6. Measured — cross-turn dedup (live session, billed ledger)

Four-turn session, the same `cat` of a 39,680-byte file once per turn, `deepseek-v4.1-flash-1017`,
per-run `--usage-file` from the provider. Core's own guardrail resets each turn, so it cannot help here;
the plugin keys on `(tool, args)` per session.

| Arm | turn 1 | turn 2 | turn 3 | turn 4 | total prompt | cost USD |
|---|---|---|---|---|---|---|
| plugin on | 33,943 | 116,198 | 199,058 | 282,752 | **631,951** | 0.002367 |
| plugin off | 54,889 | 145,098 | 269,478 | 428,439 | 897,904 | 0.003080 |
| plugin on (seed 2) | 45,218 | 127,485 | 210,343 | 294,027 | **677,073** | 0.002337 |
| plugin off (seed 2) | 55,747 | 137,969 | 263,779 | 423,882 | 881,377 | 0.003671 |

- Total prompt tokens: **−29.6 %** (seed 1), **−23.2 %** (seed 2).
- Final turn, where compounding is largest: **−34.0 %** / **−30.6 %**.
- Cost: **−23.2 %** / **−36.3 %**.
- Turn 1 differs between arms by 10–21 k tokens for an unattributed reason (same prompt, one call);
  the from-turn-2 comparison is the stable one: −29.1 % / −23.5 %.

**Intra-turn is the opposite.** Six identical 40,363-byte calls in one turn: 306,130 prompt tokens with
the plugin vs 254,662 without (8 vs 7 API calls). Core's guardrail (`IDENTICAL_RESULT_STUB_MIN_CHARS = 512`,
from the 2nd repeat, `reset_for_turn()`) already covers that case more cheaply.

## 7. Measured — cross-turn diff stubs & sliced recall (live session, billed ledger)

Four-turn session comparing modified outputs (1 line changed in 640 lines / 39,680 bytes) and targeted sliced recall. See [LIVE_MULTITURN_REPORT.md](LIVE_MULTITURN_REPORT.md) for full breakdown.

- **Prompt Tokens:** 679,544 (treatment) vs 897,904 (baseline) — **−24.32% prompt token reduction**.
- **Turn 2 Payload:** Dropped from 40,363 bytes to a **178-byte unified diff stub** (**99.56% payload reduction**).
- **Targeted Recall:** `lines="298-302"` returned 5 lines (312 bytes vs 39,680 bytes = **99.21% smaller** than full recall).

