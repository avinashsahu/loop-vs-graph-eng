import json
import os
import time

CACHE_DIR = os.environ.get("CACHE_DIR", ".cache")


def cached(key: str, ttl_seconds: float, fetch_fn):
    """Return fetch_fn()'s (JSON-serializable) result, memoized to a per-key file under CACHE_DIR.

    Recomputes when the file is missing, unreadable, or older than ttl_seconds.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"{key}.json")

    if os.path.exists(path):
        try:
            with open(path) as f:
                entry = json.load(f)
            if time.time() - entry["fetched_at"] < ttl_seconds:
                return entry["data"]
        except (OSError, ValueError, KeyError):
            pass

    data = fetch_fn()
    with open(path, "w") as f:
        json.dump({"fetched_at": time.time(), "data": data}, f)
    return data
