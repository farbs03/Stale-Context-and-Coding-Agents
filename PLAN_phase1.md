# Phase 1 Plan: Mining Real-World Staleness in Agent Context Files

**Research question (RQ1):** What kinds of staleness occur in real-world agent context
files (CLAUDE.md, AGENTS.md), and how common is each kind?

**Goal for tonight:** Stages 0–5 complete, producing a coding sheet of real staleness
candidates. The researcher codes them by hand (Thursday morning), then Stage 6 produces
report-ready tables.

**Method in one sentence:** Extract concrete references (paths, scripts, tools, code
identifiers) from every historical version of a repository's context file, and flag
references that resolved when the file version was written but stopped resolving before
the file was next edited. This adapts Tan, Wagner & Treude (EMSE 2024), who detected code
element references that survive in documentation after the code was deleted.

```
Stage 1 discover repos → 2 clone + file history → 3 extract references
→ 4 drift detection → 5 sample + coding sheet → [HUMAN CODING] → 6 analysis
```

---

## Stage 0: Setup

- Python venv; `requirements.txt` with pinned versions: `requests`, `pandas`, `tqdm`,
  `pyyaml`, `matplotlib`, `pytest`.
- Env var `GITHUB_TOKEN` (fine-grained personal access token, read-only public access).
- `config.yaml` (Phase 1 section), initial values:
  ```yaml
  phase1:
    context_filenames: [AGENTS.md, CLAUDE.md]
    min_stars: 10
    min_context_file_commits: 3
    min_days_since_file_created: 30
    n_repos: 150            # reduce to 60 if running late
    n_code_sample: 100      # researcher codes the first 50 tonight/Thursday
    max_per_repo_in_sample: 3
    seed: 42
    reference_types: [PATH, SCRIPT, TOOL, IDENT]   # drop IDENT first if short on time
  ```
- `python -m phase1.check_env`: verifies token works, `git --version` ≥ 2.30.

**Acceptance:** check_env passes.

---

## Stage 1: Repository discovery

1. Query GitHub REST code search (`GET /search/code`) for files named `AGENTS.md` and
   `CLAUDE.md` (qualifier `filename:`). Code search requires authentication, has a low
   per-minute rate limit, and caps results per query (historically ~1000). **Check the
   current docs.** To get past the cap, partition queries (e.g., by `size:` ranges).
2. Rate limiting: honor `Retry-After` and `X-RateLimit-Reset`; exponential backoff on
   403/429. Cache every raw response in `data/raw/search/`.
3. Deduplicate repositories. Fetch metadata: stars, fork, archived, default branch,
   created/pushed dates, primary language.
4. Filter: not a fork, not archived, stars ≥ `min_stars`. For each candidate context file,
   count commits touching it via the commits API with a `path` parameter; keep repos with
   ≥ `min_context_file_commits` commits and ≥ `min_days_since_file_created` days of history.
5. Rank by number of context-file commits; keep the top `n_repos`.
6. **Fallback:** if code search is unusable, read repos from `data/raw/seed_repos.txt`
   (one `owner/name` per line) and tell the user.

**Output:** `data/interim/repos.csv` (owner, name, stars, language, default_branch,
context_file_paths, n_context_commits, file_created_at).

**Acceptance:** ≥ 50 repos pass filters; print summary (count, language distribution,
median context-file commits).

---

## Stage 2: Clone and extract context-file history

1. Clone into `data/repos/{owner}__{name}`. Try `git clone --filter=blob:none` on 3 repos
   first; if `git grep`/`git show` on historical commits is too slow with lazy blob
   fetching, switch to full clones. Record the choice in `DECISIONS.md`.
2. Use the default branch with `--first-parent` to keep history linear.
3. For each context file path: `git log --first-parent --follow --format='%H|%at|%s' -- <path>`.
   Save each version's content (`git show <sha>:<path>`).
4. Aliases: if CLAUDE.md is a symlink to AGENTS.md, or only imports it (e.g., contains just
   `@AGENTS.md`), treat it as an alias and analyze the target once. Record aliases.

**Output:** `data/interim/versions.jsonl`: repo, path, version_idx, commit, timestamp,
message, content.

**Acceptance:** versions extracted for ≥ 95% of repos; failures logged with reasons.

---

## Stage 3: Reference extraction

For each version, extract references, each with line number, full line text, and type:

| Type | What to extract | How it resolves at a commit |
|---|---|---|
| PATH | Repo-relative paths in inline code, code fences, or plain text (contain `/` or a known file extension). Strip `./`. Exclude URLs, placeholders (`<...>`, `{...}`, `your-...`), and globs (`*`), but log globs separately. | Path exists in `git ls-tree -r --name-only <commit>` (also accept a directory prefix match). |
| SCRIPT | `npm/pnpm/yarn/bun run <name>`, `make <target>`, `just <recipe>`. Script files like `./scripts/x.sh` count as PATH. | Script key in `package.json` at that commit; target in Makefile/justfile (regex parse). |
| TOOL | Package manager or runner implied by commands (npm/pnpm/yarn/bun; pip/uv/poetry). | Matching lockfile present (package-lock.json, pnpm-lock.yaml, yarn.lock, bun.lock/bun.lockb; uv.lock, poetry.lock). Unresolvable if a conflicting manager's lockfile is present and the stated one's is absent. |
| IDENT | Code identifiers in backticks: CamelCase, snake_case containing `_`, dotted module paths; length ≥ 4; skip a stoplist of common words and CLI flags. | `git grep -w -F <ident> <commit>` excluding `*.md`, docs directories, and the context file itself. |

**Tests (required):** `tests/test_extract.py` with ≥ 15 handcrafted snippets covering each
type, including placeholders, URLs, globs, and code fences.

**Output:** `data/interim/references.jsonl`.

**Acceptance:** tests pass; counts per type printed.

> **HUMAN CHECKPOINT 1 (~10 min):** Print 20 randomly sampled extracted references
> (seeded) with their source lines. Stop and wait for the user to confirm extraction
> quality or request fixes.

---

## Stage 4: Drift detection

A version `v_i` is **live** from its commit `c_i` until the next commit `c_{i+1}` that
edits the file (or HEAD, for the latest version).

1. Resolve each reference at the start `S = c_i` and the end `E = parent(c_{i+1})`
   (or HEAD). Cache resolution data per commit.
   - Resolves at S, fails at E → **DRIFT** (became stale while live). These are the candidates.
   - Fails at S → **NEVER_RESOLVED** (wrong from the start or external). Excluded from
     staleness, but count them.
   - Resolves at both → OK.
2. **Cause commit** for each DRIFT, searched within `S..E`:
   - PATH: `git log --first-parent --diff-filter=DR --find-renames --name-status S..E -- <path>`.
     If renamed, **record `renamed_to`**; Phase 2 uses this for historical grounding.
   - IDENT: pickaxe `git log -S<ident> --format=%H S..E` excluding markdown; take the
     commit where occurrences reach zero.
   - SCRIPT: commit in range touching `package.json`/Makefile that removed the key/target.
   - TOOL: commit in range that added or removed the relevant lockfiles.
3. **Fix detection:** the first later version where the stale line is changed or the
   reference disappears. Record `fix_commit`, `fix_message`, `fix_timestamp`, or
   `still_stale_at_head = True`.
4. **Stale duration:** fix (or HEAD) timestamp minus cause timestamp, in days.
   Flag right-censored cases.
5. **Correction-message flag:** fix commit message matches
   `outdated|stale|fix|update|rename|correct|no longer|deprecat|moved|removed` (case-insensitive).

**Output:** `data/processed/candidates.jsonl` with fields: candidate_id, repo, context_path,
version_commit, line_no, line_text, ref_type, ref_value, cause_commit, cause_message,
cause_timestamp, renamed_to, fix_commit, fix_message, still_stale_at_head,
stale_duration_days, correction_message_flag.

**Acceptance:** summary table (DRIFT / NEVER_RESOLVED / OK counts by type). If fewer than
~100 DRIFT candidates, report it and suggest increasing `n_repos`.

---

## Stage 5: Sampling and coding sheet

1. Deduplicate (same repo + same reference + same cause commit = one instance).
2. Draw a stratified random sample of `n_code_sample` (strata = ref_type; at most
   `max_per_repo_in_sample` per repo), then shuffle with the fixed seed.
   **The first 50 rows form the pilot**, so pilot coding is a valid prefix of the full sample.
3. Write `data/processed/coding_sheet.csv`: all candidate fields plus **empty HUMAN
   columns**: `is_stale` (yes/no/unsure), `content_category`, `change_type`,
   `loud_or_silent`, `coder_notes`. Never fill these.
4. Write one readable card per instance, `data/processed/coding_cards/{candidate_id}.md`:
   - GitHub permalink to the context file at the version commit, anchored to the line
   - the stale line with ±3 lines of context
   - cause commit link, message, and a short diff snippet showing the change
   - fix commit link and message, if any
   - resolution evidence (what existed at S, what is missing at E)
5. Write `phase1/CODEBOOK.md`:
   - **Seed content categories** (adapted from Agent READMEs): Build & Run, Testing,
     Project Structure/Architecture, Implementation Details/APIs, Conventions & Style,
     Tooling & Dependencies, Other.
   - **Change types:** rename, move, deletion, tool switch, version/config change, other.
   - **loud_or_silent:** would an agent following the stale fact likely get an immediate
     error (loud) or not (silent)?
   - Instructions: add new codes inductively when needed, with a one-line definition and an
     example, and log them in the codebook.

**Acceptance:** sheet opens in a spreadsheet; 5 random permalinks verified to load.

> **HUMAN CHECKPOINT 2:** Stop. The researcher codes the pilot (first 50 rows).

---

## Stage 6: Analysis (run after human coding)

`python -m phase1.analyze` reads the coded sheet and produces:

- Detector precision: % `is_stale = yes`, overall and per ref_type
- Category frequencies among confirmed stale instances (counts and %)
- Category × change_type cross-table
- Loud vs. silent distribution per category
- Stale duration (median, IQR; % still stale at HEAD); % fixed
- Number of repos contributing

**Outputs:** `outputs/tables/*.tex` (booktabs), `outputs/figures/*.pdf`,
`outputs/phase1_summary.md`.

**Optional, after the midterm:** an LLM second-coder script (OpenAI-compatible endpoint,
disabled by default in config) that applies the codebook blind to human labels, plus
Cohen's kappa against the human codes.

---

## If running late tonight

Cut in this order: (1) drop IDENT references (most expensive and noisiest); (2) lower
`n_repos` to 60; (3) skip correction-message flags. Keep Stages 4–5 intact, since the
coding sheet is tonight's essential output.

## Data ethics

Public repositories only. Store only what the analysis needs. Do not open issues or PRs
on mined repositories.
