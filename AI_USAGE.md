# AI Usage Audit Log

> Single source of truth for IEEE/ACM AI-generated content disclosure.

| Date (UTC) | Provider / Model | Pipeline Role | Purpose | Touched Artifacts |
| :--- | :--- | :--- | :--- | :--- |
| 2026-11-27 01:48 | `antigravity/gemini-4-neon` | Coordinator | Scaffolded topic repository 'agentic-canary-verification' from research-paper-factory | CLAUDE.md, AGENTS.md, pipeline.config.json, state/pipeline_state.json |
| 2026-11-27 01:49 | `antigravity/gemini-4-neon` | Scout | Queried OpenAlex/Crossref APIs and verified 16/20 peer-reviewed citations | literature/refs.bib, literature/evidence.md, reports/citation_report.md |
| 2026-11-27 02:03 | `codex/gpt-6.2-sol` | Architect | Implemented 12-service x 6-region AgenticCanary fault-injection benchmark and 12 unit tests | src/acv/*.py, tests/test_acv.py |
| 2026-11-27 02:03 | `antigravity/gemini-4-neon` | Runner + Writer | Executed 5 sealed multi-seed runs (74,400 simulated releases), paired Wilcoxon audit, and IEEEtran draft | eval/*.py, runs/*, paper/* |
