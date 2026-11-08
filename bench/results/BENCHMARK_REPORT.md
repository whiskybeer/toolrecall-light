# Benchmark Report — toolrecall-light

> **The tool to recall tools.**  
> Deterministic context deduplication and on-demand tool recall for [Hermes Agent](https://github.com/NousResearch/hermes-agent).

## 1. Executive Summary

- **Simulated Scenario:** 20-turn SWE debugging session (pytest, file search, git status, compaction, recall).
- **Total Turns Executed:** 18
- **Stubs Emitted:** 11
- **Bytes Deduplicated:** 35,786 bytes
- **Net Prompt Tokens Avoided:** **73,348 tokens**
- **Compounded Context Inflation Reduction:** **53.96%**
- **Baseline Billed Cost (at $3/M prompt tokens):** $0.4078
- **Treatment Billed Cost (at $3/M prompt tokens):** $0.1878
- **Per-Session Dollar Savings:** **$0.2200** (53.96% reduction)
- **Average On-Demand Recall Latency:** **0.25 ms** (sub-millisecond restoration)

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
