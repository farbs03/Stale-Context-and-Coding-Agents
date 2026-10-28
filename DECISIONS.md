# Decisions and deviations from the plans

One line per decision, with rationale.

## Phase 1

- **Python 3.11.9 venv (`.venv`)** although 3.14 is the system default: satisfies "3.11+" and avoids wheel gaps for pinned deps (Phase 2 tooling too).
- **`.env` loaded by a 15-line stdlib loader (`phase1.config.load_dotenv`)** instead of adding `python-dotenv`: keeps the pinned dependency list to what the plan specifies; existing env vars take precedence.
- **Code search partitioned by `size:` ranges, split recursively** while a partition's `total_count` exceeds the documented 1,000-result cap (docs checked 2026-10-28: 10 req/min, default branch only, files < 384 KB, `filename:` is the only qualifier allowed without a search term).
