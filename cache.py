import json
import os
import re
import time
from dataclasses import dataclass

CACHE_DIR = os.environ.get("CACHE_DIR", ".cache")

_UNSAFE_KEY_CHARS = re.compile(r"[^A-Za-z0-9_-]")


@dataclass(frozen=True)
class CachedValue:
    data: object
    fetched_at: float
    cache_hit: bool


def _path(key: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    safe_key = _UNSAFE_KEY_CHARS.sub("_", key)
    return os.path.join(CACHE_DIR, f"{safe_key}.json")


def read_entry(key: str, ttl_seconds: float) -> CachedValue | None:
    """Return a fresh cached value with provenance, or None."""
    path = _path(key)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            entry = json.load(f)
        if time.time() - entry["fetched_at"] < ttl_seconds:
            return CachedValue(
                data=entry["data"],
                fetched_at=entry["fetched_at"],
                cache_hit=True,
            )
    except (OSError, ValueError, KeyError):
        pass
    return None


def read(key: str, ttl_seconds: float):
    """Compatibility interface returning only the cached data."""
    entry = read_entry(key, ttl_seconds)
    return entry.data if entry is not None else None


def write(key: str, data) -> float:
    fetched_at = time.time()
    with open(_path(key), "w") as f:
        json.dump({"fetched_at": fetched_at, "data": data}, f)
    return fetched_at


def cached_entry(key: str, ttl_seconds: float, fetch_fn) -> CachedValue:
    """Return memoized data together with fetch time and hit/miss provenance."""
    hit = read_entry(key, ttl_seconds)
    if hit is not None:
        return hit
    data = fetch_fn()
    fetched_at = write(key, data)
    return CachedValue(data=data, fetched_at=fetched_at, cache_hit=False)


def cached(key: str, ttl_seconds: float, fetch_fn):
    """Return fetch_fn()'s (JSON-serializable) result, memoized to a per-key file under CACHE_DIR.

    Recomputes when the file is missing, unreadable, or older than ttl_seconds.
    """
    return cached_entry(key, ttl_seconds, fetch_fn).data
