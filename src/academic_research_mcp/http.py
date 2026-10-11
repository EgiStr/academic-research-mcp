"""Shared HTTP layer for every source.

One place for the timeout, the User-Agent, the polite rate limiter, and the caching,
so individual sources stay short and every outbound request behaves the same way.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

import httpx

from . import config

VERSION = "0.1.0"


def mailto() -> str:
    """Contact address for the polite pools.

    Resolved on every call rather than cached at import: the value may come from a config
    file that is written after this module is imported, and a module-level constant would
    freeze the pre-config answer.
    """
    return config.get("MAILTO")


# Sources are public and keyless, so courtesy is mandatory rather than optional:
# we serialise requests per host and keep a minimum gap between them.
_CACHE_MAX = 256


def default_timeout() -> float:
    return config.get_float("TIMEOUT", 25.0)


def min_interval() -> float:
    return config.get_float("MIN_INTERVAL", 0.34)


def cache_ttl() -> float:
    return config.get_float("CACHE_TTL", 300.0)


# The client and the throttle lock belong to one event loop. An asyncio.Lock or an
# httpx.AsyncClient bound to a loop that has since closed raises "Event loop is closed" —
# which happens whenever a host (or a test runner) uses more than one loop in a process.
# Keying both by the running loop keeps each loop self-contained.
_loops: dict[Any, tuple[httpx.AsyncClient, asyncio.Lock]] = {}
_last_call: dict[str, float] = {}
_cache: dict[str, tuple[float, Any]] = {}


def user_agent() -> str:
    contact = f"; mailto:{mailto()}" if mailto() else ""
    return f"academic-research-mcp/{VERSION} (+https://github.com/EgiStr/academic-research-mcp{contact})"


def _slot() -> tuple[httpx.AsyncClient, asyncio.Lock]:
    """Return the client/lock pair for the running event loop, creating it on first use."""
    loop = asyncio.get_running_loop()
    slot = _loops.get(loop)
    if slot is None:
        client = httpx.AsyncClient(
            timeout=default_timeout(),
            follow_redirects=True,
            headers={"User-Agent": user_agent(), "Accept": "application/json"},
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
        )
        slot = (client, asyncio.Lock())
        _loops[loop] = slot
    return slot


async def aclose() -> None:
    """Release every open connection pool (called on server shutdown)."""
    clients = [client for client, _ in _loops.values()]
    _loops.clear()
    for client in clients:
        await client.aclose()


async def aclose_current_loop() -> None:
    """Drop only the current loop's client. Useful when a loop ends but the process lives."""
    slot = _loops.pop(asyncio.get_running_loop(), None)
    if slot is not None:
        await slot[0].aclose()


def _cache_key(url: str, params: dict[str, Any] | None) -> str:
    raw = json.dumps([url, sorted((params or {}).items())], default=str, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def _cache_get(key: str) -> Any | None:
    hit = _cache.get(key)
    if hit is None:
        return None
    stored_at, value = hit
    if time.monotonic() - stored_at > cache_ttl():
        _cache.pop(key, None)
        return None
    return value


def _cache_put(key: str, value: Any) -> None:
    if len(_cache) >= _CACHE_MAX:
        # Cheap eviction: drop the oldest quarter rather than tracking recency.
        for old in sorted(_cache, key=lambda k: _cache[k][0])[: _CACHE_MAX // 4]:
            _cache.pop(old, None)
    _cache[key] = (time.monotonic(), value)


async def _throttle(host: str) -> None:
    """Space out requests to one host, per event loop."""
    _, lock = _slot()
    async with lock:
        wait = min_interval() - (time.monotonic() - _last_call.get(host, 0.0))
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call[host] = time.monotonic()


class SourceError(RuntimeError):
    """A source failed. Carries a message meant to be read by the calling model."""


async def request_json(
    url: str,
    params: dict[str, Any] | None = None,
    *,
    headers: dict[str, str] | None = None,
    source: str = "source",
) -> Any:
    """GET a JSON document, with caching and per-host throttling.

    Raises SourceError with an actionable message instead of leaking httpx internals.
    """
    key = _cache_key(url, params)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    client, _ = _slot()
    host = httpx.URL(url).host or "unknown"
    await _throttle(host)

    try:
        response = await client.get(url, params=params, headers=headers)
    except httpx.TimeoutException as exc:
        raise SourceError(f"{source}: timed out after {default_timeout():.0f}s ({url})") from exc
    except httpx.HTTPError as exc:
        raise SourceError(f"{source}: network error ({exc.__class__.__name__})") from exc

    if response.status_code == 429:
        raise SourceError(f"{source}: rate limited (HTTP 429). Wait a moment and retry, or lower the number of parallel searches.")
    if response.status_code == 404:
        raise SourceError(f"{source}: not found (HTTP 404).")
    if response.status_code >= 400:
        detail = response.text[:200].replace("\n", " ")
        raise SourceError(f"{source}: HTTP {response.status_code} — {detail}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise SourceError(f"{source}: response was not JSON (content-type {response.headers.get('content-type', '?')})") from exc

    _cache_put(key, payload)
    return payload


async def request_text(
    url: str,
    params: dict[str, Any] | None = None,
    *,
    headers: dict[str, str] | None = None,
    source: str = "source",
) -> str:
    """GET a non-JSON document (arXiv Atom feed, article XML)."""
    key = _cache_key(url, params)
    cached = _cache_get(key)
    if cached is not None:
        return cached  # type: ignore[return-value]

    client, _ = _slot()
    host = httpx.URL(url).host or "unknown"
    await _throttle(host)

    try:
        response = await client.get(url, params=params, headers={"Accept": "*/*", **(headers or {})})
    except httpx.TimeoutException as exc:
        raise SourceError(f"{source}: timed out after {default_timeout():.0f}s ({url})") from exc
    except httpx.HTTPError as exc:
        raise SourceError(f"{source}: network error ({exc.__class__.__name__})") from exc

    if response.status_code >= 400:
        raise SourceError(f"{source}: HTTP {response.status_code}")
    text = response.text
    _cache_put(key, text)
    return text
