# AgenticCanary: Change-Aware Synthetic Probing & Causal Auto-Rollback

> **Research Topic #2** instantiated by [`jma49/research-paper-factory`](https://github.com/jma49/research-paper-factory)  
> *Staging Is Dead, Long Live Agentic Canaries: Change-Aware Synthetic Probing and Causal Auto-Rollback for Safe Testing in Production*

## Audited Benchmark Highlights (74,400 Simulated Multi-Region Releases, 5 Sealed Runs)

| Strategy | Fault Det. (%) | Semantic Bug (%) | Config Drift (%) | False Rollback (%) | Release Cycle (min) | Escapes / 80 |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| `FixedStaging` | 18.3% | 73.3% | 0.0% | 0.00% | 47.86 | 49.00 |
| `StaticCanary5xxP99` | 3.4% | 0.0% | 1.2% | 24.00% | 12.61 | 42.76 |
| `ShadowDiffOnly` | 48.3% | 85.1% | 81.2% | 52.20% | 8.97 | 11.68 |
| `CanaryNoCausal` | 72.6% | 86.7% | 86.6% | 60.00% | 7.83 | 1.84 |
| **`AgenticCanary` (Ours)** | **92.1%** | **93.1%** | **93.8%** | **9.40%** | **8.21** | **3.56** |

## Reproducing All Results

```bash
python3 -W error -m unittest discover -s tests -p "test_*.py" -v
python3 eval/harness.py
python3 eval/audit.py
python3 eval/audit.py --verify-only
```
