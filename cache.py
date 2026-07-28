import json
import os
import re
import time

CACHE_DIR = os.environ.get("CACHE_DIR", ".cache")

_UNSAFE_KEY_CHARS = re.compile(r"[^A-Za-z0-9_-]")


def _path(key: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    safe_key = _UNSAFE_KEY_CHARS.sub("_", key)
    return os.path.join(CACHE_DIR, f"{safe_key}.json")


def read(key: str, ttl_seconds: float):
    """Return the cached value for key if present and fresh, else None."""
    path = _path(key)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            entry = json.load(f)
        if time.time() - entry["fetched_at"] < ttl_seconds:
            return entry["data"]
    except (OSError, ValueError, KeyError):
        pass
    return None


def write(key: str, data) -> None:
    with open(_path(key), "w") as f:
        json.dump({"fetched_at": time.time(), "data": data}, f)


def cached(key: str, ttl_seconds: float, fetch_fn):
    """Return fetch_fn()'s (JSON-serializable) result, memoized to a per-key file under CACHE_DIR.

    Recomputes when the file is missing, unreadable, or older than ttl_seconds.
    """
    hit = read(key, ttl_seconds)
    if hit is not None:
        return hit
    data = fetch_fn()
    write(key, data)
    return data
