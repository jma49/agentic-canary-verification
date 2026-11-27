# Research Plan (`plan.md`) — Agentic Canaries

- **Topic**: Staging Is Dead, Long Live Agentic Canaries: Change-Aware Synthetic Probing and Causal Auto-Rollback for Safe Testing in Production
- **Target Venues**: FSE / ICSE / IEEE ISSRE / USENIX ATC (IEEEtran two-column format)
- **Compliance**: 100% open-source simulation & synthetic multi-region microservice benchmark, personal GitHub (`jma49`), zero proprietary employer data.

## 1. One-Sentence Contribution
We propose **AgenticCanary**, a change-aware progressive production verification architecture combining (1) pre-deploy blast-radius and multi-region config/flag contract extraction, (2) targeted synthetic probing & shadow semantic diffing, (3) high-cardinality Difference-in-Differences (DiD) causal auto-rollback, and (4) shift-right-to-shift-left rule mining—outperforming fixed staging suites and static HTTP-5xx/P99 canary monitors in fault detection, MTTD/MTTR, false-rollback rate, and release velocity.

## 2. Research Questions
- **RQ1 (Fault Detection & Speed)**: How does `AgenticCanary` compare against `FixedStaging`, `StaticCanary5xxP99`, `ShadowDiffOnly`, and `CanaryNoCausal` across 80 multi-region production release scenarios (code semantic bugs, regional config drift, tail latency regressions, dependency brownouts, and noisy benign traffic spikes)?
- **RQ2 (Component Ablations)**: What is the marginal contribution of blast-radius targeting, shadow semantic probing, DiD causal attribution, and shift-left rule memory?
- **RQ3 (Non-Circularity & Imperfect Blast Graphs)**: How well does `AgenticCanary` perform as blast-radius graph recall varies from 50% to 100% and under intense background regional traffic shocks?
- **RQ4 (Statistical Rigor)**: Are all improvements statistically significant across 20 paired seeds with Holm-Bonferroni-adjusted Wilcoxon signed-rank tests and low A/A noise floor?
