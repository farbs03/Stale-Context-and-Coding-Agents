"""Stage 0: verify the GitHub token works and git is recent enough.

Usage: python -m phase1.check_env
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

import requests

from phase1.config import load_dotenv, phase1_config


def parse_git_version(output: str) -> tuple[int, ...]:
    m = re.search(r"git version (\d+)\.(\d+)(?:\.(\d+))?", output)
    if not m:
        raise ValueError(f"cannot parse git version from {output!r}")
    return tuple(int(x) for x in m.groups() if x is not None)


def check_git(min_version: str) -> bool:
    out = subprocess.run(["git", "--version"], capture_output=True, text=True, check=True).stdout
    have = parse_git_version(out)
    need = tuple(int(x) for x in min_version.split("."))
    ok = have[: len(need)] >= need
    print(f"[{'OK' if ok else 'FAIL'}] git {'.'.join(map(str, have))} (need >= {min_version})")
    return ok


def check_token(env_var: str, api_base: str) -> bool:
    token = os.environ.get(env_var)
    if not token:
        print(f"[FAIL] environment variable {env_var} is not set")
        return False
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    r = requests.get(f"{api_base}/rate_limit", headers=headers, timeout=30)
    if r.status_code != 200:
        print(f"[FAIL] GET /rate_limit returned HTTP {r.status_code}")
        return False
    resources = r.json()["resources"]
    core, search, code = resources["core"], resources["search"], resources.get("code_search", {})
    print(f"[OK] {env_var} authenticates; core limit {core['limit']} "
          f"(remaining {core['remaining']}), search {search['limit']}/min, "
          f"code_search {code.get('limit', 'n/a')}/min")
    # Code search requires authentication; a tiny query confirms access.
    r = requests.get(f"{api_base}/search/code", headers=headers,
                     params={"q": "filename:AGENTS.md", "per_page": 1}, timeout=30)
    if r.status_code != 200:
        print(f"[FAIL] code search returned HTTP {r.status_code}: {r.text[:200]}")
        return False
    print(f"[OK] code search works (filename:AGENTS.md total_count={r.json()['total_count']})")
    return core["limit"] >= 5000


def main() -> int:
    load_dotenv()
    cfg = phase1_config()
    ok = check_git(cfg["env"]["min_git_version"])
    ok = check_token(cfg["env"]["token_env_var"], cfg["github"]["api_base"]) and ok
    print("check_env:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
