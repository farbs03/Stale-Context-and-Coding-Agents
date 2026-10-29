"""Thin wrappers around the git CLI (subprocess only, per project conventions)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never",
           "LC_ALL": "C"}


class GitError(RuntimeError):
    pass


def git(repo: Path | None, *args: str, timeout: float | None = 600, check: bool = True,
        input_bytes: bytes | None = None) -> bytes:
    cmd = ["git"] + (["-C", str(repo)] if repo else []) + list(args)
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, env=GIT_ENV,
                              input=input_bytes)
    except subprocess.TimeoutExpired as e:
        raise GitError(f"timeout after {timeout}s: {' '.join(cmd[:6])}") from e
    if check and proc.returncode != 0:
        raise GitError(f"{' '.join(cmd[:8])} -> exit {proc.returncode}: "
                       f"{proc.stderr.decode('utf-8', 'replace').strip()[:400]}")
    return proc.stdout


def git_text(repo: Path | None, *args: str, **kw) -> str:
    return git(repo, *args, **kw).decode("utf-8", "replace")


def repo_dir(root: Path, owner: str, name: str) -> Path:
    return root / f"{owner}__{name}"


def is_valid_repo(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        git(path, "rev-parse", "--verify", "HEAD", timeout=60)
        return True
    except GitError:
        return False
