#!/usr/bin/env python3
"""Frozen Sealed Experiment Harness for AgenticCanary (Paper #2).

Executes 5 sealed multi-seed experiment suites over the 12-service, 6-region
progressive production verification benchmark (80 releases per seed):
  1. exp0_aa_noise     : Disjoint 20-seed Block A vs B noise calibration
  2. exp1_main_bench   : Main benchmark (5 primary strategies x 25 paired seeds, graph_recall=0.85)
  3. exp2_ablation     : Component ablation (AgenticCanary vs 4 ablations x 25 paired seeds)
  4. exp3_graph_recall : Non-circularity sweep (graph_recall 0.50, 0.70, 0.85, 1.00 x 20 seeds)
  5. exp4_shock_stress : Background traffic-shock intensity sweep (0.5x, 1.0x, 1.5x, 2.0x x 20 seeds)
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from acv import ALL_STRATEGIES, SimConfig, run_strategy

MAIN_STRATEGIES = [
    "FixedStaging",
    "StaticCanary5xxP99",
    "ShadowDiffOnly",
    "CanaryNoCausal",
    "AgenticCanary",
]

ABLATION_STRATEGIES = [
    "AgenticCanary",
    "AgenticCanary-NoBlastGraph",
    "AgenticCanary-NoShadowProbe",
    "AgenticCanary-NoCausalDiD",
    "AgenticCanary-NoShiftLeft",
]

SEEDS_25 = list(range(3001, 3026))
SEEDS_20 = list(range(3001, 3021))
SEEDS_AA_B = list(range(4001, 4021))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _combined_sha(paths: List[Path]) -> str:
    h = hashlib.sha256()
    for p in sorted(paths):
        h.update(p.name.encode("utf-8"))
        h.update(sha256_file(p).encode("utf-8"))
    return h.hexdigest()


def _git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except Exception:
        return "unknown"


def seal_run(run_id: str, experiment: str, seeds: List[int], records: List[Dict[str, Any]], meta: Dict[str, Any]) -> None:
    run_dir = REPO_ROOT / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    mp = run_dir / "metrics.json"
    mp.write_text(json.dumps(records, indent=2), encoding="utf-8")
    m_sha = sha256_file(mp)

    harness_files = [
        REPO_ROOT / "eval" / "harness.py",
        REPO_ROOT / "src" / "acv" / "models.py",
        REPO_ROOT / "src" / "acv" / "topology.py",
        REPO_ROOT / "src" / "acv" / "scenarios.py",
        REPO_ROOT / "src" / "acv" / "simulator.py",
    ]
    manifest = {
        "schema_version": "2.0",
        "run_id": run_id,
        "experiment": experiment,
        "timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "git_commit": _git_commit(),
        "harness_sha256": _combined_sha(harness_files),
        "dataset_sha256": _combined_sha([REPO_ROOT / "src" / "acv" / "scenarios.py", REPO_ROOT / "src" / "acv" / "topology.py"]),
        "command": "python3 eval/harness.py",
        "seeds": seeds,
        "record_count": len(records),
        "metrics_file": "metrics.json",
        "metrics_sha256": m_sha,
        "artifact_sha256": m_sha,
        "meta": meta,
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"SEALED {run_id}: {len(records)} records, commit={manifest['git_commit']}, sha256={m_sha[:12]}")


def main() -> None:
    base_cfg = SimConfig(graph_recall=0.85, shock_scale=1.0)

    # EXP-0: A/A Noise Calibration
    aa_rows: List[Dict[str, Any]] = []
    for block, seeds in (("A", SEEDS_20), ("B", SEEDS_AA_B)):
        for s in seeds:
            m = run_strategy("AgenticCanary", s, base_cfg).to_dict()
            m["block"] = block
            aa_rows.append(m)
    seal_run("exp0_aa_noise_v1", "exp0_aa_noise", SEEDS_20 + SEEDS_AA_B, aa_rows, {"graph_recall": 0.85})

    # EXP-1: Main Benchmark (5 strategies x 25 paired seeds = 125 runs = 10,000 simulated releases)
    main_rows: List[Dict[str, Any]] = []
    for strat in MAIN_STRATEGIES:
        for s in SEEDS_25:
            main_rows.append(run_strategy(strat, s, base_cfg).to_dict())
    seal_run("exp1_main_bench_v1", "exp1_main_bench", SEEDS_25, main_rows, {"strategies": MAIN_STRATEGIES, "graph_recall": 0.85})

    # EXP-2: Component Ablation
    abl_rows: List[Dict[str, Any]] = []
    for strat in ABLATION_STRATEGIES:
        for s in SEEDS_25:
            abl_rows.append(run_strategy(strat, s, base_cfg).to_dict())
    seal_run("exp2_ablation_v1", "exp2_ablation", SEEDS_25, abl_rows, {"strategies": ABLATION_STRATEGIES})

    # EXP-3: Graph Recall Non-Circularity Sweep (50%..100%)
    rec_rows: List[Dict[str, Any]] = []
    for gr in (0.50, 0.70, 0.85, 1.00):
        cfg = SimConfig(graph_recall=gr, shock_scale=1.0)
        for strat in ("FixedStaging", "StaticCanary5xxP99", "ShadowDiffOnly", "AgenticCanary"):
            for s in SEEDS_20:
                m = run_strategy(strat, s, cfg).to_dict()
                m["graph_recall"] = gr
                rec_rows.append(m)
    seal_run("exp3_graph_recall_v1", "exp3_graph_recall", SEEDS_20, rec_rows, {"graph_recalls": [0.50, 0.70, 0.85, 1.00]})

    # EXP-4: Traffic Shock Stress Sweep (0.5x..2.0x)
    shk_rows: List[Dict[str, Any]] = []
    for sc in (0.5, 1.0, 1.5, 2.0):
        cfg = SimConfig(graph_recall=0.85, shock_scale=sc)
        for strat in ("StaticCanary5xxP99", "ShadowDiffOnly", "CanaryNoCausal", "AgenticCanary"):
            for s in SEEDS_20:
                m = run_strategy(strat, s, cfg).to_dict()
                m["shock_scale"] = sc
                shk_rows.append(m)
    seal_run("exp4_shock_stress_v1", "exp4_shock_stress", SEEDS_20, shk_rows, {"shock_scales": [0.5, 1.0, 1.5, 2.0]})


if __name__ == "__main__":
    main()
