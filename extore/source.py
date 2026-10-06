"""Public repository metadata, cached without requiring a GitHub credential."""

import threading
import time

import httpx
from fastapi import APIRouter

router = APIRouter()
REPOSITORY = "https://github.com/TokenNotIncluded/extore"
_lock = threading.Lock()
_stars = None
_refresh_after = 0.0


@router.get("/api/source")
def source_info():
    global _stars, _refresh_after
    with _lock:
        now = time.monotonic()
        if now >= _refresh_after:
            _refresh_after = now + 60
            try:
                response = httpx.get(
                    "https://api.github.com/repos/TokenNotIncluded/extore",
                    headers={
                        "Accept": "application/vnd.github+json",
                        "User-Agent": "Extore",
                    },
                    timeout=3,
                    follow_redirects=False,
                )
                response.raise_for_status()
                count = response.json().get("stargazers_count")
                if type(count) is int and count >= 0:
                    _stars = count
                    _refresh_after = now + 600
            except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                pass
        return {"url": REPOSITORY, "stars": _stars}
