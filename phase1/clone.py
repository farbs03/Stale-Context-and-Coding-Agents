"""Stage 2a: clone repositories (bare, default branch only).

Usage:
  python -m phase1.clone --benchmark   # partial vs full clone timing on 3 repos
  python -m phase1.clone               # clone all repos in repos.csv (mode from config)
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
from tqdm import tqdm

from phase1.config import DATA_INTERIM, DATA_REPOS, phase1_config
from phase1.gitutil import GitError, git, git_text, is_valid_repo, repo_dir


def clone_repo(owner: str, name: str, branch: str, dest: Path, mode: str,
               timeout: float) -> None:
    if is_valid_repo(dest):
        return
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = ["clone", "--bare", "--single-branch", "--no-tags", "--branch", branch]
    if mode == "partial":
        args.append("--filter=blob:none")
    args += [f"https://github.com/{owner}/{name}.git", str(dest)]
    try:
        git(None, *args, timeout=timeout)
    except GitError:
        shutil.rmtree(dest, ignore_errors=True)
        raise


def _timed(fn) -> float:
    t = time.perf_counter()
    fn()
    return time.perf_counter() - t


def benchmark(repos: pd.DataFrame, cfg: dict) -> None:
    """Time historical git operations on partial vs full clones of 3 repos."""
    bench_root = DATA_REPOS / "_benchmark"
    rows = []
    for r in repos.head(3).itertuples():
        path = r.context_file_paths.split(";")[0]
        for mode in ("partial", "full"):
            dest = bench_root / mode / f"{r.owner}__{r.name}"
            t_clone = _timed(lambda: clone_repo(r.owner, r.name, r.default_branch, dest, mode,
                                                cfg["clone"]["timeout_seconds"]))
            log = git_text(dest, "log", "--first-parent", "--format=%H", "HEAD", "--", path)
            oldest = log.split()[-1]
            t_show = _timed(lambda: git(dest, "show", f"{oldest}:{path}"))
            t_tree = _timed(lambda: git(dest, "ls-tree", "-r", "--name-only", oldest))
            t_grep = _timed(lambda: git(dest, "grep", "-w", "-F", "-l", "import", oldest,
                                        "--", ".", ":(exclude)*.md", check=False))
            size_mb = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file()) / 1e6
            rows.append({"repo": f"{r.owner}/{r.name}", "mode": mode, "clone_s": round(t_clone, 1),
                         "show_s": round(t_show, 2), "ls_tree_s": round(t_tree, 2),
                         "grep_s": round(t_grep, 2), "disk_mb": round(size_mb, 1)})
            print(rows[-1], flush=True)
    df = pd.DataFrame(rows)
    df.to_csv(DATA_INTERIM / "clone_benchmark.csv", index=False)
    print(df.to_string(index=False))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--benchmark", action="store_true")
    args = ap.parse_args()
    cfg = phase1_config()
    repos = pd.read_csv(DATA_INTERIM / "repos.csv")
    if args.benchmark:
        benchmark(repos, cfg)
        return 0
    mode = cfg["clone"]["mode"]

    def one(r) -> dict:
        dest = repo_dir(DATA_REPOS, r.owner, r.name)
        try:
            clone_repo(r.owner, r.name, r.default_branch, dest, mode, cfg["clone"]["timeout_seconds"])
            return {"repo": f"{r.owner}/{r.name}", "ok": True, "error": ""}
        except GitError as e:
            print(f"  clone failed: {r.owner}/{r.name}: {e}", flush=True)
            return {"repo": f"{r.owner}/{r.name}", "ok": False, "error": str(e)}

    rows = list(repos.itertuples())
    with ThreadPoolExecutor(max_workers=cfg["clone"]["workers"]) as ex:
        status = list(tqdm(ex.map(one, rows), total=len(rows), desc=f"clone ({mode})"))
    st = pd.DataFrame(status)
    st.to_csv(DATA_INTERIM / "clone_status.csv", index=False)
    print(f"cloned {int(st['ok'].sum())}/{len(st)} repos ({mode})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
