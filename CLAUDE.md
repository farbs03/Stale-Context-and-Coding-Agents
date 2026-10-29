# Stale Context Files & Coding Agents — Course Research Project

Empirical study of how outdated repository context files (CLAUDE.md / AGENTS.md)
affect AI coding agents. Two phases:

- **Phase 1 (RQ1):** Mine real GitHub repositories to find and categorize real-world
  staleness in context files. Produces the staleness taxonomy. Plan: `PLAN_phase1.md`.
- **Phase 2 (RQ2–RQ4):** Inject historically grounded stale facts into otherwise accurate
  context files and run a coding agent (mini-swe-agent) on SWE-bench Verified tasks.
  Plan: `PLAN_phase2.md`.

## Repository layout

```
CLAUDE.md, PLAN_phase1.md, PLAN_phase2.md
config.yaml              # all thresholds, sizes, seeds, model settings
requirements.txt         # pinned versions
phase1/                  # mining pipeline (python -m phase1.<stage>)
phase2/                  # experiment pipeline (python -m phase2.<stage>)
tests/                   # pytest; required for parsers and detectors
data/raw/                # immutable inputs (API responses, downloads)
data/interim/            # derived, reproducible from scripts
data/processed/          # final datasets, coding sheets
data/repos/              # git clones (gitignored)
outputs/tables/, outputs/figures/   # report-ready artifacts (LaTeX tables, PDFs)
DECISIONS.md             # deviations from the plans, one line each
```

## Phase 1 commands

Use the venv: `.venv/Scripts/python` (Python 3.11). `GITHUB_TOKEN` is read from the
environment or `.env`.

```
python -m phase1.check_env      # Stage 0: token + git version
python -m phase1.discover       # Stage 1: code-search sample -> data/interim/repos.csv
python -m phase1.clone [--benchmark]  # Stage 2a: bare full clones -> data/repos/
python -m phase1.history        # Stage 2b: context-file versions -> data/interim/versions.jsonl
python -m pytest -q tests       # run before committing
```

API responses are cached gzipped under `data/raw/<namespace>/` (`phase1/github_api.py`);
`data/raw/commits/` is gitignored because it holds commit-author emails.

## How to work

- Implement the plans **stage by stage**. After each stage: run its acceptance checks,
  print a short summary of results, and make a git commit.
- **Stop at every `HUMAN CHECKPOINT`** in the plans and wait for the user. Do not proceed
  past one on your own.
- When unsure how an external API or tool behaves (GitHub API, git, mini-swe-agent,
  SWE-bench harness, vLLM), check its documentation or `--help` instead of assuming.
  Note any deviation from the plan in `DECISIONS.md` with a one-line rationale.

## Non-negotiable rules

1. **Never fabricate or simulate data, results, or API responses.** If a step fails,
   report the failure. Never silently substitute synthetic or placeholder data.
2. **Never fill human-coding columns** (`is_stale`, `content_category`, `change_type`,
   `loud_or_silent`, `coder_notes`, or any column marked HUMAN). The researcher codes these.
3. **Reproducibility:** all parameters live in `config.yaml`; fixed random seeds; pinned
   dependency versions; record tool and model versions in run metadata. `data/raw/` is
   never modified after download.
4. **Secrets:** read tokens and API keys from environment variables only. Never print or
   commit them. `.env` is gitignored.
5. **Rate limits and cost:** cache every API response; back off on rate-limit errors.
   No paid model API calls unless `config.yaml` explicitly enables them.
6. **Heuristics are labeled as heuristics.** Any detector or classifier output that a
   human has not validated is named and documented as heuristic.

## Code conventions

- Python 3.11+, type hints, small single-purpose modules, CLI entry points.
- Use `subprocess` calls to the `git` CLI (not a git library) for history operations.
- Large artifacts (clones, Docker images, model weights) are gitignored.

## Keep this file accurate

This project studies stale context files. If the layout, commands, or conventions change,
update this file in the same commit.
