"""Stage 2b: extract every version of each context file along first-parent history.

Usage: python -m phase1.history

Writes data/interim/versions.jsonl, data/interim/aliases.csv,
data/interim/history_status.csv (one row per repo, with failure reasons).
"""

from __future__ import annotations

import json
import posixpath
import re
import sys
from pathlib import Path
from typing import Any

import pandas as pd
from tqdm import tqdm

from phase1.config import DATA_INTERIM, DATA_REPOS
from phase1.gitutil import GitError, git, git_text, is_valid_repo, repo_dir

REC, FLD = "\x1e", "\x1f"
IMPORT_LINE = re.compile(r"^@(\S+)$")
HEADING = re.compile(r"^#{1,6}\s")


def parse_log(out: str) -> list[dict[str, Any]]:
    """Parse `git log --format=%x1e%H%x1f%at%x1f%s --name-status` output (newest first)."""
    commits = []
    for rec in out.split(REC):
        rec = rec.strip("\n")
        if not rec:
            continue
        header, _, rest = rec.partition("\n")
        sha, ts, subject = header.split(FLD, 2)
        status, path_at, old_path = None, None, None
        for ln in rest.splitlines():
            if not ln.strip():
                continue
            parts = ln.split("\t")
            status = parts[0]
            if status[0] in "RC" and len(parts) >= 3:
                old_path, path_at = parts[1], parts[2]
            else:
                path_at = parts[-1]
        commits.append({"commit": sha, "timestamp": int(ts), "message": subject,
                        "status": status, "path_at_commit": path_at, "renamed_from": old_path})
    return commits


def alias_target(content: str, is_symlink: bool, file_path: str) -> str | None:
    """If this version only points at another file (symlink or @-import only), return it."""
    base_dir = posixpath.dirname(file_path)
    if is_symlink:
        target = content.strip()
    else:
        text = re.sub(r"<!--.*?-->", "", content, flags=re.S)
        lines = [ln.strip() for ln in text.splitlines()
                 if ln.strip() and not HEADING.match(ln.strip())]
        imports = [IMPORT_LINE.match(ln) for ln in lines]
        if not lines or not all(imports):
            return None
        target = imports[0].group(1)
    target = target.removeprefix("./")
    return posixpath.normpath(posixpath.join(base_dir, target)) if base_dir else \
        posixpath.normpath(target)


def file_versions(repo: Path, path: str) -> list[dict[str, Any]]:
    out = git_text(repo, "log", "--first-parent", "--follow", "--find-renames",
                   f"--format={REC}%H{FLD}%at{FLD}%s", "--name-status", "HEAD", "--", path)
    commits = list(reversed(parse_log(out)))  # oldest first
    versions = []
    for c in commits:
        p = c["path_at_commit"] or path
        v = {**c, "path_at_commit": p, "deleted": c["status"] == "D", "content": None,
             "is_symlink": False}
        if not v["deleted"]:
            mode = git_text(repo, "ls-tree", c["commit"], "--", p).split(" ", 1)[0]
            v["is_symlink"] = mode == "120000"
            v["content"] = git(repo, "show", f"{c['commit']}:{p}").decode("utf-8", "replace")
        versions.append(v)
    return versions


def process_repo(repo_path: Path, full_name: str,
                 paths: list[str]) -> tuple[list[dict], list[dict]]:
    head = git_text(repo_path, "rev-parse", "HEAD").strip()
    head_ts = int(git_text(repo_path, "log", "-1", "--format=%at", "HEAD").strip())
    chains = {p: file_versions(repo_path, p) for p in paths}

    # Alias targets that are not themselves in the path list get analyzed too.
    aliases = []
    for p, vs in list(chains.items()):
        for v in vs:
            if v["content"] is None:
                continue
            t = alias_target(v["content"], v["is_symlink"], v["path_at_commit"])
            v["alias_target"] = t
            if t:
                aliases.append({"repo": full_name, "path": p, "commit": v["commit"],
                                "kind": "symlink" if v["is_symlink"] else "import",
                                "target": t})
        latest = vs[-1] if vs else None
        t = latest.get("alias_target") if latest else None
        if t and t not in chains and not latest["deleted"]:
            try:
                git(repo_path, "cat-file", "-e", f"HEAD:{t}")
                chains[t] = file_versions(repo_path, t)
                for v in chains[t]:
                    v["alias_target"] = alias_target(v["content"], v["is_symlink"],
                                                     v["path_at_commit"]) if v["content"] else None
            except GitError:
                pass

    # A --follow chain can overlap another path's (e.g. CLAUDE.md renamed to AGENTS.md).
    # Analyze each (commit, path_at_commit) once; the longest chain claims it.
    seen: set[tuple[str, str]] = set()
    rows = []
    for p in sorted(chains, key=lambda k: (-len(chains[k]), k)):
        for idx, v in enumerate(chains[p]):
            key = (v["commit"], v["path_at_commit"])
            dup = key in seen
            seen.add(key)
            rows.append({
                "repo": full_name, "path": p, "version_idx": idx,
                "n_versions": len(chains[p]), "commit": v["commit"],
                "timestamp": v["timestamp"], "message": v["message"],
                "path_at_commit": v["path_at_commit"], "renamed_from": v["renamed_from"],
                "deleted": v["deleted"], "is_symlink": v["is_symlink"],
                "is_alias": bool(v.get("alias_target")), "alias_target": v.get("alias_target"),
                "duplicate_of_other_chain": dup,
                "head_commit": head, "head_timestamp": head_ts,
                "content": v["content"],
            })
    return rows, aliases


def main() -> int:
    repos =pd.read_csv(DATA_INTERIM / "repos.csv")
    status, all_aliases = [], []
    n_versions = 0
    with open(DATA_INTERIM / "versions.jsonl", "w", encoding="utf-8") as f:
        for r in tqdm(list(repos.itertuples()), desc="history"):
            full = f"{r.owner}/{r.name}"
            path = repo_dir(DATA_REPOS, r.owner, r.name)
            if not is_valid_repo(path):
                status.append({"repo": full, "ok": False, "n_versions": 0,
                               "error": "clone missing or invalid"})
                continue
            try:
                rows, aliases = process_repo(path, full, r.context_file_paths.split(";"))
            except (GitError, ValueError) as e:
                status.append({"repo": full, "ok": False, "n_versions": 0, "error": str(e)})
                continue
            for row in rows:
                f.write(json.dumps(row) + "\n")
            n_versions += len(rows)
            all_aliases += aliases
            ok = any(row["content"] is not None for row in rows)
            status.append({"repo": full, "ok": ok, "n_versions": len(rows),
                           "error": "" if ok else "no versions found"})
    st = pd.DataFrame(status)
    st.to_csv(DATA_INTERIM / "history_status.csv", index=False)
    pd.DataFrame(all_aliases, columns=["repo", "path", "commit", "kind", "target"]).to_csv(
        DATA_INTERIM / "aliases.csv", index=False)
    rate = st["ok"].mean() if len(st) else 0.0
    print(f"repos with versions: {int(st['ok'].sum())}/{len(st)} ({rate:.1%}); "
          f"versions: {n_versions}; alias versions: {len(all_aliases)}")
    for row in st[~st["ok"]].itertuples():
        print(f"  FAILED {row.repo}: {row.error}")
    print(f"acceptance (>= 95% repos): {'PASS' if rate >= 0.95 else 'FAIL'}")
    return 0 if rate >= 0.95 else 1


if __name__ == "__main__":
    sys.exit(main())
