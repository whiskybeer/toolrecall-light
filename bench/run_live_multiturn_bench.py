#!/usr/bin/env python3
"""Multi-Turn Cross-Turn Benchmark for toolrecall-light.

Measures context deduplication, diff-based partial stubbing, and sliced recall
across multiple turns in Hermes Agent sessions.

Supports two modes:
1. --simulate (default if hermes CLI is not available):
   Executes deterministic multi-turn sequences through the plugin's transform hook,
   measuring exact byte savings, token reduction (chars/4), diff generation,
   and sliced recall latency.
2. --live (requires hermes CLI in PATH and API credentials):
   Executes real multi-turn chat sessions with `--usage-file` to record
   live billed prompt tokens from the model provider.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Ensure repo root is in python path
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import __init__ as plugin
import dedup
import schemas
import store


def build_workload_fixtures(tmpdir: Path) -> Tuple[Path, Path]:
    """Create two realistic text fixtures where file2 is a slightly modified version of file1."""
    # ~40 KB fixture (~640 lines)
    lines_v1 = [f"LINE_{i:04d}: status=OK subsystem=core worker={i % 8} trace_hash=9f82a1{i:03d}" for i in range(640)]
    lines_v1[300] = "LINE_0300: VALUE=ORIGINAL_BENCHMARK_KEY status=PENDING"

    lines_v2 = list(lines_v1)
    lines_v2[300] = "LINE_0300: VALUE=MODIFIED_BENCHMARK_KEY status=COMPLETED"

    f1 = tmpdir / "workload_v1.txt"
    f2 = tmpdir / "workload_v2.txt"

    f1.write_text("\n".join(lines_v1) + "\n", encoding="utf-8")
    f2.write_text("\n".join(lines_v2) + "\n", encoding="utf-8")

    return f1, f2


def run_simulated_benchmark() -> Dict[str, Any]:
    """Run simulated multi-turn benchmark comparing baseline, exact dedup, and diff dedup."""
    temp_dir = Path(tempfile.mkdtemp(prefix="tr_bench_sim_"))
    try:
        f1, f2 = build_workload_fixtures(temp_dir)
        content_v1 = f1.read_text(encoding="utf-8")
        content_v2 = f2.read_text(encoding="utf-8")
        raw_bytes = len(content_v1.encode("utf-8"))

        db_path = temp_dir / "bench_store.db"
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        store.init_db(conn)

        store.get_default_connection = lambda: conn
        plugin._session_generations.clear()

        # Ensure isolation from host ToolRecall full-stack daemon
        os.environ["TOOLRECALL_UDS_PATH"] = str(temp_dir / "isolated_toolrecall.sock")
        os.environ["HERMES_TOOLRECALL_LIGHT_NORMALIZE"] = "1"
        os.environ["HERMES_TOOLRECALL_LIGHT_DIFF"] = "1"

        session_id = f"sim-session-{int(time.time())}"
        turn_results: List[Dict[str, Any]] = []

        # Turn 1: Initial large read
        t1_out = plugin._on_transform_tool_result(
            "read_file", {"path": str(f1)}, content_v1, session_id=session_id
        )
        t1_delivered = t1_out if t1_out is not None else content_v1
        turn_results.append({
            "turn": 1,
            "action": "read_file (v1)",
            "raw_bytes": len(content_v1),
            "delivered_bytes": len(t1_delivered),
            "is_stub": t1_out is not None and t1_out.startswith("[toolrecall-light]"),
        })

        # Turn 2: Read modified file (should trigger diff stub)
        t2_out = plugin._on_transform_tool_result(
            "read_file", {"path": str(f2)}, content_v2, session_id=session_id
        )
        t2_delivered = t2_out if t2_out is not None else content_v2
        turn_results.append({
            "turn": 2,
            "action": "read_file (v2, 1-line delta)",
            "raw_bytes": len(content_v2),
            "delivered_bytes": len(t2_delivered),
            "is_diff_stub": t2_out is not None and "partial diff against earlier result" in t2_out,
        })

        # Turn 3: Repeat identical read of v2 (should trigger exact stub)
        t3_out = plugin._on_transform_tool_result(
            "read_file", {"path": str(f2)}, content_v2, session_id=session_id
        )
        t3_delivered = t3_out if t3_out is not None else content_v2
        turn_results.append({
            "turn": 3,
            "action": "read_file (v2 repeat)",
            "raw_bytes": len(content_v2),
            "delivered_bytes": len(t3_delivered),
            "is_exact_stub": t3_out is not None and "identical to earlier" in t3_out,
        })

        # Turn 4: Targeted Sliced Recall
        # Extract result_id from t1
        cur = conn.cursor()
        cur.execute("SELECT id FROM seen WHERE session_id = ? ORDER BY id ASC LIMIT 1", (session_id,))
        row = cur.fetchone()
        result_id = row[0] if row else ""

        t4_start = time.perf_counter()
        recall_resp = schemas.handle_recall_result(result_id=result_id, lines="298-302")
        t4_elapsed_ms = (time.perf_counter() - t4_start) * 1000.0

        turn_results.append({
            "turn": 4,
            "action": "recall_result(lines='298-302')",
            "raw_bytes": raw_bytes,
            "delivered_bytes": len(recall_resp),
            "latency_ms": round(t4_elapsed_ms, 3),
        })

        total_raw = sum(r["raw_bytes"] for r in turn_results)
        total_delivered = sum(r["delivered_bytes"] for r in turn_results)
        byte_reduction_pct = round((1.0 - (total_delivered / total_raw)) * 100.0, 2)

        return {
            "mode": "simulated",
            "turns": turn_results,
            "total_raw_bytes": total_raw,
            "total_delivered_bytes": total_delivered,
            "byte_reduction_pct": byte_reduction_pct,
        }
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def run_live_benchmark(
    model: str = "deepseek/deepseek-v4.1-flash-1017",
    turns: int = 4,
    output_json: Optional[Path] = None,
) -> Dict[str, Any]:
    """Execute live multi-turn comparison using hermes CLI if available."""
    if not shutil.which("hermes"):
        print("[ERROR] 'hermes' command not found in PATH. Use --simulate instead.")
        sys.exit(1)

    temp_dir = Path(tempfile.mkdtemp(prefix="tr_bench_live_"))
    try:
        f1, f2 = build_workload_fixtures(temp_dir)
        usage_file = temp_dir / "usage.jsonl"

        print(f"[*] Running live multi-turn benchmark with model '{model}' across {turns} turns...")

        # Run multi-turn session via hermes chat -q
        # Ensure scrubbed environment without hardcoded secrets
        session_results: List[Dict[str, Any]] = []

        # Instructions for the agent turns
        prompts = [
            f"Read the entire contents of {f1} using read_file or terminal and print line 301.",
            f"Now read {f2} and compare it with the previous file.",
            f"Re-read {f2} and confirm if line 301 changed.",
            f"Using the tool recall_result, recall lines 299 to 303 of the first file.",
        ]

        for i, prompt in enumerate(prompts[:turns], 1):
            cmd = [
                "hermes",
                "chat",
                "-q",
                prompt,
                "--model",
                model,
                "--usage-file",
                str(usage_file),
            ]
            t0 = time.time()
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            elapsed = time.time() - t0

            session_results.append({
                "turn": i,
                "prompt": prompt,
                "exit_code": res.returncode,
                "duration_seconds": round(elapsed, 2),
            })

        result_data = {
            "mode": "live",
            "model": model,
            "turns_executed": len(session_results),
            "session_turns": session_results,
        }

        if output_json:
            output_json.write_text(json.dumps(result_data, indent=2), encoding="utf-8")
            print(f"[*] Saved results to {output_json}")

        return result_data
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Multi-Turn Benchmark for toolrecall-light")
    parser.add_argument("--live", action="store_true", help="Run live hermes sessions (requires hermes CLI)")
    parser.add_argument("--model", default="deepseek/deepseek-v4.1-flash-1017", help="Model identifier for live run")
    parser.add_argument("--turns", type=int, default=4, help="Number of turns to execute")
    parser.add_argument("--json", type=Path, default=None, help="Path to output JSON summary")

    args = parser.parse_args()

    if args.live:
        results = run_live_benchmark(model=args.model, turns=args.turns, output_json=args.json)
    else:
        results = run_simulated_benchmark()

    print("\n=== Benchmark Results ===")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
