"""HTTP plumbing shared by every source: one identifying User-Agent (met.no requires it,
and it is good manners towards public services), retries with backoff, and raw bytes kept
so the payload can be hashed for provenance."""
from __future__ import annotations

import os
import time

import httpx

DEFAULT_UA = os.environ.get("S33W_USER_AGENT", "S33Weather/0.1 (+https://solas33.com; contact hello@solas33.com)")


def make_client(user_agent: str | None = None, timeout: float = 45.0) -> httpx.Client:
    return httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": user_agent or DEFAULT_UA})


def get(client: httpx.Client, url: str, params: dict | None = None, tries: int = 3) -> httpx.Response:
    last: Exception | None = None
    for attempt in range(tries):
        try:
            r = client.get(url, params=params)
            if r.status_code in (429, 502, 503, 504) and attempt < tries - 1:
                time.sleep(2.0 * (attempt + 1))
                continue
            r.raise_for_status()
            return r
        except (httpx.HTTPError, httpx.TransportError) as e:
            last = e
            if attempt < tries - 1:
                time.sleep(1.5 * (attempt + 1))
    if last:
        raise last
    raise RuntimeError(f"giving up on {url}")
