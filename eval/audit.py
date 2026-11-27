#!/usr/bin/env python3
"""Self-Contained Deterministic Auditor & Paired Wilcoxon Exporter for AgenticCanary."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import random
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent

EXPERIMENTS = [
    "exp0_aa_noise_v1",
    "exp1_main_bench_v1",
    "exp2_ablation_v1",
    "exp3_graph_recall_v1",
    "exp4_shock_stress_v1",
]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def combined_sha(paths: List[Path]) -> str:
    h = hashlib.sha256()
    for p in sorted(paths):
        h.update(p.name.encode("utf-8"))
        h.update(sha256_file(p).encode("utf-8"))
    return h.hexdigest()


def bootstrap_ci(values: List[float], n_boot: int = 1500, alpha: float = 0.05, seed: int = 42) -> Tuple[float, float, float]:
    mean = sum(values) / len(values) if values else 0.0
    if len(values) <= 1:
        return mean, mean, mean
    rng = random.Random(seed)
    n = len(values)
    boots = sorted(sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(n_boot))
    return mean, boots[int((alpha / 2.0) * n_boot)], boots[min(n_boot - 1, int((1.0 - alpha / 2.0) * n_boot))]


def wilcoxon_p(xs: List[float], ys: List[float]) -> float:
    diffs = [x - y for x, y in zip(xs, ys) if abs(x - y) > 1e-12]
    n = len(diffs)
    if n < 5:
        return 1.0
    abs_sorted = sorted((abs(d), 1 if d > 0 else -1) for d in diffs)
    ranks, i, tie_corr = [], 0, 0.0
    while i < n:
        j = i
        while j < n and abs(abs_sorted[j][0] - abs_sorted[i][0]) <= 1e-12:
            j += 1
        avg_r = (i + 1 + j) / 2.0
        t = j - i
        if t > 1:
            tie_corr += t * (t * t - 1)
        for k in range(i, j):
            ranks.append((avg_r, abs_sorted[k][1]))
        i = j
    w_plus = sum(r for r, s in ranks if s > 0)
    mu = n * (n + 1) / 4.0
    var = (n * (n + 1) * (2 * n + 1) - tie_corr / 2.0) / 24.0
    if var <= 0.0:
        return 1.0
    z = (abs(w_plus - mu) - 0.5) / math.sqrt(var)
    return min(1.0, max(0.0, math.erfc(max(0.0, z) / math.sqrt(2.0))))


def holm_bonferroni(p_map: Dict[str, float]) -> Dict[str, float]:
    ordered = sorted(p_map.items(), key=lambda kv: kv[1])
    m = len(ordered)
    adj, cur_max = {}, 0.0
    for idx, (k, p) in enumerate(ordered):
        cur_max = max(cur_max, min(1.0, p * (m - idx)))
        adj[k] = cur_max
    return adj


def summarize(rows: List[Dict[str, Any]], key: str) -> Dict[str, float]:
    mean, lo, hi = bootstrap_ci([float(r[key]) for r in rows])
    return {"mean": round(mean, 4), "ci_lo": round(lo, 4), "ci_hi": round(hi, 4), "n": len(rows)}


def fmt_p(p: float) -> str:
    return "<0.0001" if p < 1e-4 else f"{p:.4f}"


def audit_runs(runs_dir: Path) -> Dict[str, Any]:
    errors, verified = [], {}
    harness_files = [
        REPO_ROOT / "eval" / "harness.py",
        REPO_ROOT / "src" / "acv" / "models.py",
        REPO_ROOT / "src" / "acv" / "topology.py",
        REPO_ROOT / "src" / "acv" / "scenarios.py",
        REPO_ROOT / "src" / "acv" / "simulator.py",
    ]
    cur_h = combined_sha(harness_files)
    for exp in EXPERIMENTS:
        rp = runs_dir / exp
        mp = rp / "manifest.json"
        if not mp.exists():
            errors.append(f"[{exp}] Missing manifest.json")
            continue
        m = json.loads(mp.read_text(encoding="utf-8"))
        met = rp / m["metrics_file"]
        if not met.exists() or sha256_file(met) != m["metrics_sha256"] or m["harness_sha256"] != cur_h:
            errors.append(f"[{exp}] SHA-256 verification failed")
            continue
        verified[exp] = {"manifest": m, "rows": json.loads(met.read_text(encoding="utf-8"))}
    return {"passed": not errors and len(verified) == len(EXPERIMENTS), "errors": errors, "runs": verified}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()

    audit = audit_runs(REPO_ROOT / "runs")
    if not audit["passed"]:
        print("AUDIT FAILED:", audit["errors"])
        raise SystemExit(1)

    runs = audit["runs"]
    total_records = sum(len(v["rows"]) for v in runs.values())
    if args.verify_only:
        print(f"ACV AUDIT PASS (verify-only): {len(runs)} sealed runs, {total_records} records.")
        return

    metrics_keys = [
        "fault_detection_rate",
        "semantic_bug_recall",
        "config_drift_recall",
        "false_rollback_rate",
        "mttd_min",
        "mttr_min",
        "release_cycle_min",
        "shift_left_prevented_count",
        "probe_cost_units",
        "escaped_outages",
    ]

    # A/A
    aa = runs["exp0_aa_noise_v1"]["rows"]
    aa_a, aa_b = [r for r in aa if r["block"] == "A"], [r for r in aa if r["block"] == "B"]
    aa_det_diff = abs(summarize(aa_a, "fault_detection_rate")["mean"] - summarize(aa_b, "fault_detection_rate")["mean"]) * 100.0
    aa_cyc_diff = abs(summarize(aa_a, "release_cycle_min")["mean"] - summarize(aa_b, "release_cycle_min")["mean"]) / max(1e-9, summarize(aa_a, "release_cycle_min")["mean"]) * 100.0

    # Main benchmark
    main_rows = runs["exp1_main_bench_v1"]["rows"]
    by_strat = defaultdict(list)
    for r in main_rows:
        by_strat[r["strategy"]].append(r)
    main_sum = {s: {k: summarize(rows, k) for k in metrics_keys} for s, rows in by_strat.items()}

    ac_rows = by_strat["AgenticCanary"]
    ac_by_seed = {r["seed"]: r for r in ac_rows}
    raw_p = {}
    for b in ("FixedStaging", "StaticCanary5xxP99", "ShadowDiffOnly", "CanaryNoCausal"):
        b_by_seed = {r["seed"]: r for r in by_strat[b]}
        seeds = sorted(set(ac_by_seed) & set(b_by_seed))
        for mk in ("fault_detection_rate", "false_rollback_rate", "release_cycle_min", "escaped_outages"):
            raw_p[f"{b}:{mk}"] = wilcoxon_p([ac_by_seed[s][mk] for s in seeds], [b_by_seed[s][mk] for s in seeds])
    holm_p = holm_bonferroni(raw_p)

    # Ablation
    abl_rows = runs["exp2_ablation_v1"]["rows"]
    abl_by = defaultdict(list)
    for r in abl_rows:
        abl_by[r["strategy"]].append(r)
    abl_sum = {s: {k: summarize(rows, k) for k in metrics_keys} for s, rows in abl_by.items()}

    # Graph recall sweep
    rec_rows = runs["exp3_graph_recall_v1"]["rows"]
    rec_by = defaultdict(lambda: defaultdict(list))
    for r in rec_rows:
        rec_by[str(r["graph_recall"])][r["strategy"]].append(r)
    rec_sum = {gr: {s: {k: summarize(rows, k) for k in metrics_keys} for s, rows in smap.items()} for gr, smap in rec_by.items()}

    # Shock stress sweep
    shk_rows = runs["exp4_shock_stress_v1"]["rows"]
    shk_by = defaultdict(lambda: defaultdict(list))
    for r in shk_rows:
        shk_by[str(r["shock_scale"])][r["strategy"]].append(r)
    shk_sum = {sc: {s: {k: summarize(rows, k) for k in metrics_keys} for s, rows in smap.items()} for sc, smap in shk_by.items()}

    out = {
        "audit_passed": True,
        "verified_runs": len(runs),
        "total_records": total_records,
        "aa_noise": {"det_abs_pp": round(aa_det_diff, 2), "cycle_rel_pct": round(aa_cyc_diff, 2)},
        "main_benchmark": main_sum,
        "holm_pvalues": {k: round(v, 8) for k, v in holm_p.items()},
        "ablation": abl_sum,
        "graph_recall": rec_sum,
        "shock_stress": shk_sum,
    }
    (REPO_ROOT / "results" / "results.json").write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")

    # Pure-letter LaTeX macros
    ac = main_sum["AgenticCanary"]
    fs = main_sum["FixedStaging"]
    sc = main_sum["StaticCanary5xxP99"]
    sd = main_sum["ShadowDiffOnly"]
    nc = main_sum["CanaryNoCausal"]

    macros = {
        "TotalSealedRuns": str(len(runs)),
        "TotalSealedRecords": str(total_records),
        "TotalSimReleases": str(total_records * 80),
        "AANoiseDetPP": f"{aa_det_diff:.2f}",
        "AANoiseCyclePct": f"{aa_cyc_diff:.2f}",
        "ACDetPct": f"{ac['fault_detection_rate']['mean']*100:.1f}",
        "ACSemPct": f"{ac['semantic_bug_recall']['mean']*100:.1f}",
        "ACDriftPct": f"{ac['config_drift_recall']['mean']*100:.1f}",
        "ACFalseRollPct": f"{ac['false_rollback_rate']['mean']*100:.2f}",
        "ACMTTD": f"{ac['mttd_min']['mean']:.2f}",
        "ACMTTR": f"{ac['mttr_min']['mean']:.2f}",
        "ACCycleMin": f"{ac['release_cycle_min']['mean']:.2f}",
        "ACShiftLeft": f"{ac['shift_left_prevented_count']['mean']:.1f}",
        "ACEscapes": f"{ac['escaped_outages']['mean']:.2f}",
        "StagingDetPct": f"{fs['fault_detection_rate']['mean']*100:.1f}",
        "StagingDriftPct": f"{fs['config_drift_recall']['mean']*100:.1f}",
        "StagingCycleMin": f"{fs['release_cycle_min']['mean']:.2f}",
        "StagingEscapes": f"{fs['escaped_outages']['mean']:.1f}",
        "StaticCanaryDetPct": f"{sc['fault_detection_rate']['mean']*100:.1f}",
        "StaticCanarySemPct": f"{sc['semantic_bug_recall']['mean']*100:.1f}",
        "StaticCanaryEscapes": f"{sc['escaped_outages']['mean']:.1f}",
        "ShadowDetPct": f"{sd['fault_detection_rate']['mean']*100:.1f}",
        "ShadowFalseRollPct": f"{sd['false_rollback_rate']['mean']*100:.1f}",
        "NoCausalFalseRollPct": f"{nc['false_rollback_rate']['mean']*100:.1f}",
        "CycleSpeedupVsStaging": f"{fs['release_cycle_min']['mean'] / max(1e-9, ac['release_cycle_min']['mean']):.1f}",
        "HolmPDetVsStaging": fmt_p(holm_p["FixedStaging:fault_detection_rate"]),
        "HolmPDetVsStatic": fmt_p(holm_p["StaticCanary5xxP99:fault_detection_rate"]),
        "HolmPFRVsNoCausal": fmt_p(holm_p["CanaryNoCausal:false_rollback_rate"]),
        "AblNoShadowSemPct": f"{abl_sum['AgenticCanary-NoShadowProbe']['semantic_bug_recall']['mean']*100:.1f}",
        "AblNoCausalFRPct": f"{abl_sum['AgenticCanary-NoCausalDiD']['false_rollback_rate']['mean']*100:.1f}",
        "AblNoShiftEscapes": f"{abl_sum['AgenticCanary-NoShiftLeft']['escaped_outages']['mean']:.2f}",
        "AblNoBlastCycle": f"{abl_sum['AgenticCanary-NoBlastGraph']['release_cycle_min']['mean']:.2f}",
        "RecallFiftyACDetPct": f"{rec_sum['0.5']['AgenticCanary']['fault_detection_rate']['mean']*100:.1f}",
        "RecallFiftyShadowDetPct": f"{rec_sum['0.5']['ShadowDiffOnly']['fault_detection_rate']['mean']*100:.1f}",
        "ShockTwoACFRPct": f"{shk_sum['2.0']['AgenticCanary']['false_rollback_rate']['mean']*100:.2f}",
        "ShockTwoNoCausalFRPct": f"{shk_sum['2.0']['CanaryNoCausal']['false_rollback_rate']['mean']*100:.1f}",
    }
    num_lines = ["% AUTO-GENERATED BY eval/audit.py — DO NOT EDIT", ""] + [
        f"\\newcommand{{\\{k}}}{{{v}}}" for k, v in sorted(macros.items())
    ]
    (REPO_ROOT / "paper" / "generated").mkdir(parents=True, exist_ok=True)
    (REPO_ROOT / "paper" / "generated" / "numbers.tex").write_text("\n".join(num_lines) + "\n", encoding="utf-8")

    # Tables
    tdir = REPO_ROOT / "paper" / "generated" / "tables"
    tdir.mkdir(parents=True, exist_ok=True)

    t1 = [
        "\\begin{table*}[t]",
        "\\centering",
        "\\caption{Main Benchmark Across 80 Multi-Region Release Scenarios (12 Services, 6 Regions, $85\\%$ Blast-Graph Recall, 25 Paired Seeds; Mean [95\\% Bootstrap CI]).}",
        "\\label{tab:acv_main}",
        "\\begin{tabular}{lcccccc}",
        "\\toprule",
        "\\textbf{Strategy} & \\textbf{Fault Det. (\\%)} $\\uparrow$ & \\textbf{Sem. Rec. (\\%)} $\\uparrow$ & \\textbf{Drift Rec. (\\%)} $\\uparrow$ & \\textbf{False Roll. (\\%)} $\\downarrow$ & \\textbf{Cycle (min)} $\\downarrow$ & \\textbf{Escapes} $\\downarrow$ \\\\",
        "\\midrule",
    ]
    for s in ("FixedStaging", "StaticCanary5xxP99", "ShadowDiffOnly", "CanaryNoCausal", "AgenticCanary"):
        d = main_sum[s]
        b0, b1 = ("\\textbf{", "}") if s == "AgenticCanary" else ("", "")
        t1.append(
            f"{b0}{s}{b1} & "
            f"{b0}{d['fault_detection_rate']['mean']*100:.1f} [{d['fault_detection_rate']['ci_lo']*100:.1f}, {d['fault_detection_rate']['ci_hi']*100:.1f}]{b1} & "
            f"{b0}{d['semantic_bug_recall']['mean']*100:.1f}{b1} & "
            f"{b0}{d['config_drift_recall']['mean']*100:.1f}{b1} & "
            f"{b0}{d['false_rollback_rate']['mean']*100:.2f}{b1} & "
            f"{b0}{d['release_cycle_min']['mean']:.2f}{b1} & "
            f"{b0}{d['escaped_outages']['mean']:.2f}{b1} \\\\"
        )
    t1 += ["\\bottomrule", "\\end{tabular}", "\\end{table*}"]
    (tdir / "main_benchmark.tex").write_text("\n".join(t1) + "\n", encoding="utf-8")

    t2 = [
        "\\begin{table}[t]",
        "\\centering",
        "\\caption{Component Ablation of AgenticCanary ($85\\%$ Graph Recall, 25 Seeds).}",
        "\\label{tab:acv_ablation}",
        "\\begin{tabular}{lccccc}",
        "\\toprule",
        "\\textbf{Variant} & \\textbf{Det\\%} & \\textbf{Sem\\%} & \\textbf{FR\\%} & \\textbf{Cycle} & \\textbf{Esc.} \\\\",
        "\\midrule",
    ]
    for s in ("AgenticCanary", "AgenticCanary-NoBlastGraph", "AgenticCanary-NoShadowProbe", "AgenticCanary-NoCausalDiD", "AgenticCanary-NoShiftLeft"):
        d = abl_sum[s]
        short = s.replace("AgenticCanary-", "No-")
        b0, b1 = ("\\textbf{", "}") if s == "AgenticCanary" else ("", "")
        t2.append(
            f"{b0}{short}{b1} & {b0}{d['fault_detection_rate']['mean']*100:.1f}{b1} & "
            f"{b0}{d['semantic_bug_recall']['mean']*100:.1f}{b1} & {b0}{d['false_rollback_rate']['mean']*100:.1f}{b1} & "
            f"{b0}{d['release_cycle_min']['mean']:.2f}{b1} & {b0}{d['escaped_outages']['mean']:.2f}{b1} \\\\"
        )
    t2 += ["\\bottomrule", "\\end{tabular}", "\\end{table}"]
    (tdir / "ablation.tex").write_text("\n".join(t2) + "\n", encoding="utf-8")

    t3 = [
        "\\begin{table}[t]",
        "\\centering",
        "\\caption{Non-Circularity Test: Fault Detection (\\%) vs.\\ Blast-Graph Recall ($50\\%$--$100\\%$, 20 Seeds).}",
        "\\label{tab:acv_recall}",
        "\\begin{tabular}{lcccc}",
        "\\toprule",
        "\\textbf{Graph Recall} & \\textbf{Staging} & \\textbf{StaticCan.} & \\textbf{Shadow} & \\textbf{AgenticCan.} \\\\",
        "\\midrule",
    ]
    for gr in ("0.5", "0.7", "0.85", "1.0"):
        lbl = f"{int(float(gr)*100)}\\%"
        t3.append(
            f"{lbl} & {rec_sum[gr]['FixedStaging']['fault_detection_rate']['mean']*100:.1f} & "
            f"{rec_sum[gr]['StaticCanary5xxP99']['fault_detection_rate']['mean']*100:.1f} & "
            f"{rec_sum[gr]['ShadowDiffOnly']['fault_detection_rate']['mean']*100:.1f} & "
            f"\\textbf{{{rec_sum[gr]['AgenticCanary']['fault_detection_rate']['mean']*100:.1f}}} \\\\"
        )
    t3 += ["\\bottomrule", "\\end{tabular}", "\\end{table}"]
    (tdir / "graph_recall.tex").write_text("\n".join(t3) + "\n", encoding="utf-8")

    rep = [
        "# AgenticCanary Deterministic Audit Report (`reports/audit_report.md`)",
        "",
        f"- **Audit Verdict**: PASS ({len(runs)} sealed runs, {total_records} run records = {total_records*80} simulated releases)",
        f"- **A/A Noise Calibration**: Detection diff `{aa_det_diff:.2f}pp`, Cycle rel diff `{aa_cyc_diff:.2f}%`",
        f"- **Generated Pure-Letter Macros**: `{len(macros)}`",
        "",
        "| Strategy | Fault Det. (%) | Semantic (%) | Config Drift (%) | False Rollback (%) | Cycle (min) | Escapes |",
        "| :--- | :---: | :---: | :---: | :---: | :---: | :---: |",
    ]
    for s in ("FixedStaging", "StaticCanary5xxP99", "ShadowDiffOnly", "CanaryNoCausal", "AgenticCanary"):
        d = main_sum[s]
        rep.append(
            f"| **{s}** | {d['fault_detection_rate']['mean']*100:.1f}% | {d['semantic_bug_recall']['mean']*100:.1f}% | "
            f"{d['config_drift_recall']['mean']*100:.1f}% | {d['false_rollback_rate']['mean']*100:.2f}% | "
            f"{d['release_cycle_min']['mean']:.2f} | {d['escaped_outages']['mean']:.2f} |"
        )
    (REPO_ROOT / "reports" / "audit_report.md").write_text("\n".join(rep) + "\n", encoding="utf-8")
    print("\n".join(rep))


if __name__ == "__main__":
    main()
