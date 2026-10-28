"""Load project configuration and shared paths."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_RAW = ROOT / "data" / "raw"
DATA_INTERIM = ROOT / "data" / "interim"
DATA_PROCESSED = ROOT / "data" / "processed"
DATA_REPOS = ROOT / "data" / "repos"
OUTPUTS = ROOT / "outputs"


@lru_cache(maxsize=1)
def load_config() -> dict[str, Any]:
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def phase1_config() -> dict[str, Any]:
    return load_config()["phase1"]


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Populate os.environ from KEY=VALUE lines in .env without overriding existing vars."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)
