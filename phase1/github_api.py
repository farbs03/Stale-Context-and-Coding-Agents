"""Cached, rate-limit-aware GitHub API client (REST + GraphQL).

Every response body is cached as gzipped JSON under data/raw/<namespace>/, keyed by
a hash of the request. Cached files are written once and never modified.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import requests

from phase1.config import DATA_RAW, load_dotenv, phase1_config


class GitHubError(RuntimeError):
    pass


class GitHubClient:
    def __init__(self) -> None:
        load_dotenv()
        cfg = phase1_config()
        gh = cfg["github"]
        token = os.environ.get(cfg["env"]["token_env_var"])
        if not token:
            raise GitHubError(f"{cfg['env']['token_env_var']} is not set")
        self.api_base: str = gh["api_base"]
        self.max_retries: int = gh["max_retries"]
        self.backoff_base: float = gh["backoff_base_seconds"]
        self.timeout: float = gh["request_timeout_seconds"]
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        # Minimum spacing between code-search calls (10 requests/minute).
        self._min_interval = {"search_code": 6.5}
        self._last_call: dict[str, float] = {}

    # ---------------------------------------------------------------- caching
    @staticmethod
    def _cache_path(namespace: str, key: dict[str, Any]) -> Path:
        digest = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:24]
        return DATA_RAW / namespace / f"{digest}.json.gz"

    @staticmethod
    def _read_cache(path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def _write_cache(path: Path, record: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(record, f)
        tmp.replace(path)

    # ------------------------------------------------------------ throttling
    def _throttle(self, bucket: str | None) -> None:
        if bucket is None or bucket not in self._min_interval:
            return
        wait = self._last_call.get(bucket, 0) + self._min_interval[bucket] - time.time()
        if wait > 0:
            time.sleep(wait)
        self._last_call[bucket] = time.time()

    def _sleep_for_rate_limit(self, resp: requests.Response, attempt: int) -> None:
        retry_after = resp.headers.get("Retry-After")
        remaining = resp.headers.get("X-RateLimit-Remaining")
        reset = resp.headers.get("X-RateLimit-Reset")
        if retry_after is not None:
            delay = float(retry_after) + 1
        elif remaining == "0" and reset is not None:
            delay = max(0.0, float(reset) - time.time()) + 2
        else:
            delay = self.backoff_base * (2 ** attempt)
        print(f"  rate limited (HTTP {resp.status_code}); sleeping {delay:.0f}s", flush=True)
        time.sleep(delay)

    # --------------------------------------------------------------- requests
    def _request(self, method: str, url: str, *, params: dict[str, Any] | None = None,
                 json_body: dict[str, Any] | None = None, bucket: str | None = None
                 ) -> requests.Response:
        for attempt in range(self.max_retries):
            self._throttle(bucket)
            try:
                resp = self.session.request(method, url, params=params, json=json_body,
                                            timeout=self.timeout)
            except requests.RequestException as e:
                delay = self.backoff_base * (2 ** attempt)
                print(f"  network error {type(e).__name__}; retrying in {delay:.0f}s", flush=True)
                time.sleep(delay)
                continue
            if resp.status_code in (403, 429) and (
                "rate limit" in resp.text.lower() or "Retry-After" in resp.headers
                or resp.headers.get("X-RateLimit-Remaining") == "0"
            ):
                self._sleep_for_rate_limit(resp, attempt)
                continue
            if resp.status_code in (502, 503, 504):
                time.sleep(self.backoff_base * (2 ** attempt))
                continue
            return resp
        raise GitHubError(f"giving up after {self.max_retries} attempts: {method} {url}")

    def get(self, path: str, params: dict[str, Any] | None = None, *, namespace: str,
            bucket: str | None = None) -> dict[str, Any]:
        """GET a REST endpoint; returns the cached record {request, status, headers, body}."""
        key = {"method": "GET", "path": path, "params": params or {}}
        cache = self._cache_path(namespace, key)
        cached = self._read_cache(cache)
        if cached is not None:
            return cached
        resp = self._request("GET", f"{self.api_base}{path}", params=params, bucket=bucket)
        keep = ("Link", "X-RateLimit-Resource")
        record = {
            "request": key,
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "status": resp.status_code,
            "headers": {h: resp.headers[h] for h in keep if h in resp.headers},
            "body": resp.json() if resp.content else None,
        }
        # Cache successes and definitive "not found / gone / conflict (empty repo)" answers.
        if resp.status_code in (200, 404, 409, 410, 451):
            self._write_cache(cache, record)
        elif resp.status_code >= 400:
            raise GitHubError(f"GET {path} -> HTTP {resp.status_code}: {resp.text[:300]}")
        return record

    def graphql(self, query: str, variables: dict[str, Any] | None = None, *,
                namespace: str = "graphql") -> dict[str, Any]:
        key = {"query": query, "variables": variables or {}}
        cache = self._cache_path(namespace, key)
        cached = self._read_cache(cache)
        if cached is not None:
            return cached
        resp = self._request("POST", f"{self.api_base}/graphql",
                             json_body={"query": query, "variables": variables or {}})
        if resp.status_code != 200:
            raise GitHubError(f"GraphQL HTTP {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        record = {"request": key,
                  "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "status": 200, "body": body}
        # Only cache responses whose errors (if any) are per-node NOT_FOUND.
        errors = body.get("errors") or []
        if all(e.get("type") == "NOT_FOUND" for e in errors):
            self._write_cache(cache, record)
        return record

    def search_code(self, q: str, page: int, per_page: int) -> dict[str, Any]:
        return self.get("/search/code", {"q": q, "page": page, "per_page": per_page},
                        namespace="search", bucket="search_code")
