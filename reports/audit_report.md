# AgenticCanary Deterministic Audit Report (`reports/audit_report.md`)

- **Audit Verdict**: PASS (5 sealed runs, 930 run records = 74400 simulated releases)
- **A/A Noise Calibration**: Detection diff `0.58pp`, Cycle rel diff `1.15%`
- **Generated Pure-Letter Macros**: `36`

| Strategy | Fault Det. (%) | Semantic (%) | Config Drift (%) | False Rollback (%) | Cycle (min) | Escapes |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **FixedStaging** | 18.3% | 73.3% | 0.0% | 0.00% | 47.86 | 49.00 |
| **StaticCanary5xxP99** | 3.4% | 0.0% | 1.2% | 24.00% | 12.61 | 42.76 |
| **ShadowDiffOnly** | 48.3% | 85.1% | 81.2% | 52.20% | 8.97 | 11.68 |
| **CanaryNoCausal** | 72.6% | 86.7% | 86.6% | 60.00% | 7.83 | 1.84 |
| **AgenticCanary** | 92.1% | 93.1% | 93.8% | 9.40% | 8.21 | 3.56 |
