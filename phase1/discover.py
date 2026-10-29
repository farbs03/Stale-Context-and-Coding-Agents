"""Stage 1: discover repositories with context files.

Usage:
  python -m phase1.discover                 # code-search sampling (default)
  python -m phase1.discover --seed-file     # fallback: data/raw/seed_repos.txt

Steps: sample size windows -> code search hits -> repo metadata (GraphQL, batched)
-> fork/archived/star filter -> commits-per-context-file (REST commits API with path)
-> filter -> rank -> data/interim/repos.csv
"""

from __future__ import annotations

import argparse
import math
import random
import re
import sys
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

import pandas as pd
from tqdm import tqdm

from phase1.config import DATA_INTERIM, DATA_RAW, phase1_config
from phase1.github_api import GitHubClient


# ------------------------------------------------------------------ sampling
def sample_windows(filename: str, n: int, lo: int, hi: int, rel_width: float,
                   seed: int) -> list[tuple[int, int]]:
    """Seeded, prefix-stable list of (start, end) byte-size windows, log-uniform starts."""
    rng = random.Random(f"{seed}:{filename}")
    windows = []
    for _ in range(n):
        start = int(round(math.exp(rng.uniform(math.log(lo), math.log(hi)))))
        width = max(1, int(start * rel_width))
        windows.append((start, start + width - 1))
    return windows


def search_window(client: GitHubClient, filename: str, start: int, end: int,
                  per_page: int, cap: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch all hits for filename in size window, halving the window while over the cap."""
    while True:
        q = f"filename:{filename} size:{start}..{end}"
        first = client.search_code(q, 1, per_page)
        if first["status"] != 200:
            raise RuntimeError(f"search failed for {q!r}: HTTP {first['status']}")
        total = first["body"]["total_count"]
        if total <= cap or end == start:
            break
        end = start + (end - start) // 2
    items = list(first["body"]["items"])
    incomplete = bool(first["body"].get("incomplete_results"))
    n_pages = min(math.ceil(min(total, cap) / per_page), cap // per_page)
    for page in range(2, n_pages + 1):
        r = client.search_code(q, page, per_page)
        if r["status"] != 200:
            raise RuntimeError(f"search failed for {q!r} page {page}: HTTP {r['status']}")
        items.extend(r["body"]["items"])
        incomplete |= bool(r["body"].get("incomplete_results"))
    info = {"filename": filename, "query": q, "size_lo": start, "size_hi": end,
            "total_count": total, "n_items": len(items), "truncated": total > cap,
            "incomplete_results": incomplete}
    return items, info


def collect_hits(client: GitHubClient, cfg: dict[str, Any]) -> pd.DataFrame:
    gh = cfg["github"]
    hit_rows, window_rows = [], []
    for filename in cfg["context_filenames"]:
        windows = sample_windows(filename, gh["search_windows_per_filename"],
                                 gh["search_size_lo"], gh["search_size_hi"],
                                 gh["search_window_rel_width"], cfg["seed"])
        for start, end in tqdm(windows, desc=f"search {filename}"):
            items, info = search_window(client, filename, start, end, gh["per_page"],
                                        gh["max_results_per_query"])
            window_rows.append(info)
            for it in items:
                hit_rows.append({
                    "filename": filename,
                    "full_name": it["repository"]["full_name"],
                    "path": it["path"],
                    "basename": it["name"],
                    "repo_fork": it["repository"]["fork"],
                    "window": info["query"],
                })
    windows_df = pd.DataFrame(window_rows)
    windows_df.to_csv(DATA_INTERIM / "search_windows.csv", index=False)
    hits = pd.DataFrame(hit_rows)
    n_raw = len(hits)
    # Keep exact-case basename matches only (code search's filename: is case-insensitive).
    hits = hits[hits["basename"] == hits["filename"]]
    hits = hits.drop_duplicates(subset=["full_name", "path"])
    print(f"windows searched: {len(windows_df)} (truncated: {int(windows_df['truncated'].sum())}, "
          f"incomplete: {int(windows_df['incomplete_results'].sum())}); "
          f"hits: {n_raw} raw, {len(hits)} exact-case unique files, "
          f"{hits['full_name'].nunique()} repos")
    hits.to_csv(DATA_INTERIM / "search_hits.csv", index=False)
    return hits


def hits_from_seed_file(client: GitHubClient, cfg: dict[str, Any]) -> pd.DataFrame:
    """Fallback: list each seed repo's default-branch tree and match context filenames."""
    seed_path = DATA_RAW / "seed_repos.txt"
    names = [ln.strip() for ln in seed_path.read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.startswith("#")]
    rows = []
    for fn in tqdm(names, desc="seed trees"):
        repo = client.get(f"/repos/{fn}", namespace="repos")
        if repo["status"] != 200:
            print(f"  skip {fn}: HTTP {repo['status']}")
            continue
        branch = repo["body"]["default_branch"]
        tree = client.get(f"/repos/{fn}/git/trees/{branch}", {"recursive": "1"},
                          namespace="trees")
        if tree["status"] != 200:
            print(f"  skip {fn}: tree HTTP {tree['status']}")
            continue
        for entry in tree["body"]["tree"]:
            base = PurePosixPath(entry["path"]).name
            if entry["type"] == "blob" and base in cfg["context_filenames"]:
                rows.append({"filename": base, "full_name": fn, "path": entry["path"],
                             "basename": base, "repo_fork": repo["body"]["fork"],
                             "window": "seed_file"})
    hits = pd.DataFrame(rows)
    hits.to_csv(DATA_INTERIM / "search_hits.csv", index=False)
    print(f"seed repos: {len(names)}; context files found: {len(hits)}")
    return hits


# ------------------------------------------------------------------ metadata
REPO_FIELDS = """
  nameWithOwner stargazerCount isFork isArchived isPrivate createdAt pushedAt
  primaryLanguage { name }
  defaultBranchRef { name }
"""


def fetch_metadata(client: GitHubClient, full_names: list[str], batch: int) -> pd.DataFrame:
    rows = []
    full_names = sorted(set(full_names))
    for i in tqdm(range(0, len(full_names), batch), desc="metadata"):
        chunk = full_names[i:i + batch]
        var_defs, parts, variables = [], [], {}
        for j, fn in enumerate(chunk):
            owner, name = fn.split("/", 1)
            var_defs += [f"$o{j}: String!", f"$n{j}: String!"]
            parts.append(f"r{j}: repository(owner: $o{j}, name: $n{j}) {{ {REPO_FIELDS} }}")
            variables[f"o{j}"], variables[f"n{j}"] = owner, name
        query = f"query({', '.join(var_defs)}) {{ {' '.join(parts)} }}"
        body = client.graphql(query, variables, namespace="graphql_repo_meta")["body"]
        errors = [e for e in body.get("errors") or [] if e.get("type") != "NOT_FOUND"]
        if errors:
            raise RuntimeError(f"GraphQL errors: {errors[:3]}")
        data = body.get("data") or {}
        for j, fn in enumerate(chunk):
            r = data.get(f"r{j}")
            if r is None:
                rows.append({"full_name": fn, "found": False})
                continue
            rows.append({
                "full_name": fn,
                "found": True,
                "canonical_name": r["nameWithOwner"],
                "stars": r["stargazerCount"],
                "fork": r["isFork"],
                "archived": r["isArchived"],
                "private": r["isPrivate"],
                "created_at": r["createdAt"],
                "pushed_at": r["pushedAt"],
                "language": (r["primaryLanguage"] or {}).get("name"),
                "default_branch": (r["defaultBranchRef"] or {}).get("name"),
            })
    meta = pd.DataFrame(rows)
    meta.to_csv(DATA_INTERIM / "repo_metadata.csv", index=False)
    return meta


# ------------------------------------------------------------------- commits
_LAST_PAGE = re.compile(r'[?&]page=(\d+)[^>]*>;\s*rel="last"')


def last_page_from_link(link: str | None) -> int | None:
    if not link:
        return None
    m = _LAST_PAGE.search(link)
    return int(m.group(1)) if m else None


def path_commit_stats(client: GitHubClient, full_name: str, branch: str,
                      path: str) -> dict[str, Any]:
    """Number of commits touching path on branch, and the date of the oldest one."""
    base = f"/repos/{full_name}/commits"
    params = {"sha": branch, "path": path, "per_page": 1}
    first = client.get(base, params, namespace="commits")
    if first["status"] != 200:
        return {"n_commits": None, "first_commit_at": None, "error": f"HTTP {first['status']}"}
    last = last_page_from_link(first["headers"].get("Link"))
    n = last if last is not None else len(first["body"])
    if n == 0:
        return {"n_commits": 0, "first_commit_at": None, "error": None}
    oldest = first if last is None else client.get(base, {**params, "page": last},
                                                   namespace="commits")
    if oldest["status"] != 200 or not oldest["body"]:
        return {"n_commits": n, "first_commit_at": None, "error": f"HTTP {oldest['status']}"}
    date = oldest["body"][-1]["commit"]["committer"]["date"]
    return {"n_commits": n, "first_commit_at": date, "error": None}


# ------------------------------------------------------------------- filters
def excluded_path(path: str, segments: list[str]) -> bool:
    excluded = {s.lower() for s in segments}
    return any(seg.lower() in excluded for seg in PurePosixPath(path).parts[:-1])


def filter_and_rank(hits: pd.DataFrame, meta: pd.DataFrame, client: GitHubClient,
                    cfg: dict[str, Any]) -> pd.DataFrame:
    as_of = datetime.fromisoformat(cfg["as_of_date"]).replace(tzinfo=timezone.utc)
    m = meta[meta["found"]].copy()
    n0 = len(m)
    m = m[(~m["fork"].astype(bool)) & (~m["archived"].astype(bool))
          & (~m["private"].astype(bool)) & m["default_branch"].notna()]
    n1 = len(m)
    m = m[m["stars"] >= cfg["min_stars"]]
    n2 = len(m)
    print(f"metadata: {len(meta)} repos, {n0} found, {n1} non-fork/non-archived, "
          f"{n2} with stars >= {cfg['min_stars']}")
    cap = cfg["github"]["max_repos_to_check_commits"]
    if len(m) > cap:
        print(f"capping commit checks at {cap} repos (random, seeded)")
        m = m.sample(n=cap, random_state=cfg["seed"])

    excluded = hits["path"].map(lambda p: excluded_path(p, cfg["exclude_path_segments"]))
    print(f"context files under excluded directories (mirrors/templates/examples/...): "
          f"{int(excluded.sum())} of {len(hits)}")
    hits = hits[~excluded]
    stat_rows = []
    pairs = hits[hits["full_name"].isin(m["full_name"])][["full_name", "path"]]
    branches = dict(zip(m["full_name"], m["default_branch"]))
    for fn, path in tqdm(list(pairs.itertuples(index=False)), desc="commit counts"):
        s = path_commit_stats(client, fn, branches[fn], path)
        stat_rows.append({"full_name": fn, "path": path, **s})
    stats = pd.DataFrame(stat_rows)
    stats.to_csv(DATA_INTERIM / "context_file_commit_stats.csv", index=False)

    stats["first_commit_at"] = pd.to_datetime(stats["first_commit_at"], utc=True)
    stats["age_days"] = (as_of - stats["first_commit_at"]).dt.days
    ok = stats[(stats["n_commits"] >= cfg["min_context_file_commits"])
               & (stats["age_days"] >= cfg["min_days_since_file_created"])]
    print(f"context files checked: {len(stats)} (errors: {int(stats['error'].notna().sum())}); "
          f"passing commit/age filters: {len(ok)}")

    agg = ok.groupby("full_name").agg(
        context_file_paths=("path", lambda p: ";".join(sorted(p))),
        n_context_commits=("n_commits", "sum"),
        file_created_at=("first_commit_at", "min"),
    ).reset_index()
    out = agg.merge(m, on="full_name")
    out[["owner", "name"]] = out["full_name"].str.split("/", n=1, expand=True)
    out = out.sort_values(["n_context_commits", "stars", "full_name"],
                          ascending=[False, False, True])
    print(f"repos passing all filters: {len(out)}; keeping top {cfg['n_repos']}")
    return out.head(cfg["n_repos"])


def summarize(repos: pd.DataFrame) -> None:
    print(f"\n=== Stage 1 summary: {len(repos)} repos ===")
    print(f"median context-file commits: {repos['n_context_commits'].median():.1f} "
          f"(min {repos['n_context_commits'].min()}, max {repos['n_context_commits'].max()})")
    print(f"median stars: {repos['stars'].median():.0f}")
    print("language distribution (top 15):")
    print(repos["language"].fillna("(none)").value_counts().head(15).to_string())
    names = repos["context_file_paths"].str.split(";").explode().map(lambda p: PurePosixPath(p).name)
    print("context files by name:", names.value_counts().to_dict())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seed-file", action="store_true",
                    help="read repos from data/raw/seed_repos.txt instead of code search")
    args = ap.parse_args()
    cfg = phase1_config()
    DATA_INTERIM.mkdir(parents=True, exist_ok=True)
    client = GitHubClient()

    if args.seed_file:
        print("FALLBACK: using data/raw/seed_repos.txt instead of code search")
        hits = hits_from_seed_file(client, cfg)
    else:
        hits = collect_hits(client, cfg)
    meta = fetch_metadata(client, hits["full_name"].unique().tolist(),
                          cfg["github"]["graphql_batch_size"])
    repos = filter_and_rank(hits, meta, client, cfg)
    cols = ["owner", "name", "stars", "language", "default_branch", "context_file_paths",
            "n_context_commits", "file_created_at", "created_at", "pushed_at"]
    repos[cols].to_csv(DATA_INTERIM / "repos.csv", index=False)
    summarize(repos)
    ok = len(repos) >= 50
    print(f"\nacceptance (>= 50 repos): {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
