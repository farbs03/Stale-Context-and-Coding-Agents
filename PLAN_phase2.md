# Phase 2 Plan: Controlled Stale-Context Experiment

**Research questions:**
- **RQ2:** How much does a stale context file degrade resolve rate, token cost, time, and steps,
  compared with no file and with an accurate file?
- **RQ3:** Which kinds of staleness (Phase 1 taxonomy categories) hurt most?
- **RQ4:** Do agents follow, verify, recover from, or flag stale instructions?

**Goal for Thursday (midterm):** a working end-to-end pipeline plus a **smoke test**
(3 tasks × {none, accurate, 1 stale variant} × 1 run). Full runs and analysis happen after
the midterm. Priority if time runs short: A → B → C → E (with one hand-made stale
variant) → D → F.

---

## Stage A: Environment and model backend

1. Install `mini-swe-agent` and the `swebench` evaluation harness; pin versions; record
   `pip show` output in run metadata.
2. **Containers:** the SWE-bench evaluation harness needs Docker on x86_64. Run evaluation
   on the personal machine. mini-swe-agent also supports Singularity/Apptainer, so agent
   runs *may* work on the cluster; verify in its docs. SWE-bench images are large: check
   free disk space first and start image pulls early.
3. **Model backend** (OpenAI-compatible, configured in `config.yaml`; keys from env vars):
   - Smoke test: Meta Muse API (OpenAI-compatible) or a small local model.
   - Main runs (later): a vLLM server on the A100 with an open-weight coding model chosen
     by the researcher; record the exact model ID and revision.
   - Check the mini-swe-agent docs for how to point it at a custom endpoint/model name.
4. Fixed generation settings in config: nonzero temperature (model's recommended default),
   step limit, per-run token cap. Identical across conditions.

**Acceptance:** one SWE-bench Verified instance runs end-to-end with no context file:
trajectory saved, patch produced, harness reports resolved/unresolved.

---

## Stage B: Task selection

1. Load SWE-bench Verified from Hugging Face (`princeton-nlp/SWE-bench_Verified`).
2. **Group tasks by (repo, version)** using the dataset's `version` field. One accurate
   context file per group must be valid at every task's `base_commit` in the group. This
   handles the problem of tasks sitting at different historical commits.
3. Start with `django/django` (largest instance count). Pick one (repo, version) group for
   the smoke test: 3 tasks. Full study later: ~40 tasks across 2–3 repos.
4. Use a difficulty annotation for stratification if the dataset provides one.

**Output:** `phase2/data/tasks.csv` (instance_id, repo, version, base_commit, group_id,
files touched by gold patch, files touched by test_patch).

---

## Stage C: Accurate context files

1. For each group, draft an accurate context file (~40–80 lines) modeled on typical real
   context files from the Phase 1 corpus (sections and length): Setup, Running Tests,
   Project Structure, Key Internal APIs, Conventions.
2. Alongside it, write `facts.yaml`: one entry per factual claim with `fact_id`, `type`
   (PATH / COMMAND / IDENT / TOOL / CONVENTION), `value`, `line_no`, and a machine check.
3. **Validator:** every fact must resolve at every task's base_commit in the group. Remove
   or fix failing facts.
4. **No leakage (automated check):** the file must not mention the issue, contain lines
   overlapping the gold patch's added lines, or name tests from `FAIL_TO_PASS`.

> **HUMAN CHECKPOINT 3 (~10 min per file):** Stop. The researcher reads each accurate
> file and confirms it is correct, realistic, and leak-free.

---

## Stage D: Stale variants (injection engine)

Inputs: accurate file + facts.yaml, the repository's git history, the Phase 1 taxonomy
(categories and change-type frequencies), and each task's gold/test patch.

1. **Task relevance:** a fact is *task-relevant* if it references files or modules touched
   by the gold patch or test_patch, or is the test command. It is *task-irrelevant* if it
   references modules untouched by either and is not a test command.
2. **One fact per variant:** pick one fact of the target category and relevance; replace
   only its value. Enforce with a diff check.
3. **Stale value, in order of preference:**
   - **Grounding level 1 (historical):** a value that was true earlier in *this repository's*
     history, before base_commit: a path's previous name/location
     (`git log --follow --name-status --diff-filter=R`), a previous test command (from
     historical tox.ini, setup.cfg, CI configs, test runner location), or an identifier's
     previous name (pickaxe). The file then reads as if written before that change.
   - **Grounding level 2 (pattern-derived):** if no historical change exists, apply the
     Phase 1 change pattern most common for that category (e.g., directory move,
     tool switch).
4. **Verify** the stale value does *not* resolve at base_commit.
5. Record grounding level and provenance commit for every variant. The report will state
   the share of level-1 variants.

**Conditions per task:** `none`, `accurate`, `stale_<category>_relevant` for the top 3
Phase 1 categories, and `stale_irrelevant`. Until Phase 1 coding finishes, use placeholder
categories PATH, COMMAND, IDENT.

**Output:** `phase2/data/variants/{instance_id}/{condition}.md` and
`phase2/data/variants/manifest.jsonl` (instance_id, condition, fact_id, category,
relevance, accurate_value, stale_value, grounding_level, provenance_commit).

---

## Stage E: Runner

1. **Context delivery:** inject the file content through mini-swe-agent's prompt template,
   in one fixed wrapper (e.g., "This repository contains an AGENTS.md file with project
   instructions:" followed by the contents). `none` omits the block. Do **not** write the
   file into the repository, so it cannot appear in the patch. Log this in `DECISIONS.md`
   as a threat to validity: real tools also let agents read the file from disk.
2. **Run matrix:** tasks × conditions × seeds (3 for full runs). Resumable: skip completed
   runs. Save trajectory, prediction, and metadata per run (model ID, temperature, versions,
   wall-clock time, input/output tokens, steps, errors).
3. Evaluate predictions with the SWE-bench harness; join `resolved` into results.

**Smoke test:** 3 tasks × {none, accurate, one stale variant} × 1 run.

**Output:** `phase2/results/results.csv` (one row per run).

**Acceptance:** all smoke-test rows present with `resolved`, tokens, time, and steps filled.

---

## Stage F: Behavioral detectors (heuristic until validated)

Computed per run from the trajectory plus the manifest:

| Column | Heuristic |
|---|---|
| `followed_stale` | an agent action contains `stale_value` (exact string match) |
| `verified_first` | before first using `stale_value`, the agent ran an inspection command (ls/find/cat/grep/git log) targeting the relevant file, directory, or config |
| `stale_error` | an observation after a stale action contains error markers (No such file, command not found, ModuleNotFoundError, etc.) |
| `recovered` | a later action uses `accurate_value` after a stale error; also `recovery_steps` and `recovery_tokens` |
| `flagged` | final messages mention the discrepancy (keywords such as outdated, stale, AGENTS.md, instructions, does not exist) |

Unit tests on synthetic mini-trajectories are required. A `phase2.validate_detectors`
script samples 30 trajectories into review cards for later human labeling (post-midterm).

---

## Stage G: Analysis (post-midterm)

Task-level paired analysis: per-task resolve proportions; Wilcoxon signed-rank tests on
paired differences; bootstrap CIs resampling tasks; Holm correction within comparison
families. Primary outcomes: resolve rate and `followed_stale`. LaTeX tables to
`outputs/tables/`.

---

## Thursday time budget

| Stage | Estimate |
|---|---|
| A (start image pulls first) | 1.5–2 h |
| B | 30 min |
| C (+ human review) | 1 h |
| E smoke test | 1 h |
| D | 1–1.5 h (can follow the smoke test) |
| F | 1 h (may slip past the midterm) |
