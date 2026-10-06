import os
from pathlib import Path
from urllib.parse import urlsplit

DATA = Path(os.environ.get("EXTORE_DATA", "data")).resolve()
ORIGIN = os.environ.get("EXTORE_ORIGIN", "http://localhost:8000").rstrip("/")
RP_ID = urlsplit(ORIGIN).hostname
SCRIPT_DIR = Path(os.environ.get("EXTORE_SCRIPTS", "scripts")).resolve()
COOKIE_SECURE = urlsplit(ORIGIN).scheme == "https"


def check_config():
    u = urlsplit(ORIGIN)
    if (
        u.scheme not in ("http", "https")
        or not u.hostname
        or u.path
        or u.query
        or u.fragment
        or u.username
    ):
        raise RuntimeError("EXTORE_ORIGIN must be a bare origin")
    if u.scheme != "https" and u.hostname not in ("localhost", "127.0.0.1"):
        raise RuntimeError("Production requires HTTPS")
