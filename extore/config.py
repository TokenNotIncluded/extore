import os
from pathlib import Path
from urllib.parse import urlsplit

DATA = Path(os.environ.get("EXTORE_DATA", "data")).resolve()
ORIGIN = os.environ.get("EXTORE_ORIGIN", "http://localhost:8000").rstrip("/")
RP_ID = urlsplit(ORIGIN).hostname
COOKIE_SECURE = urlsplit(ORIGIN).scheme == "https"


def _positive_int(name, default, *, maximum=None):
    value = os.environ.get(name, str(default))
    try:
        result = int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer") from exc
    if result < 1 or (maximum is not None and result > maximum):
        limit = f" between 1 and {maximum}" if maximum is not None else " positive"
        raise RuntimeError(f"{name} must be{limit}")
    return result


# Attachment storage remains bounded even when requests omit Content-Length.
UPLOAD_FILE_BYTES = _positive_int(
    "EXTORE_UPLOAD_FILE_BYTES", 20 * 1024 * 1024, maximum=20 * 1024 * 1024
)
UPLOAD_JOB_BYTES = _positive_int("EXTORE_UPLOAD_JOB_BYTES", 100 * 1024 * 1024)
UPLOAD_JOB_FILES = _positive_int("EXTORE_UPLOAD_JOB_FILES", 100)
UPLOAD_TOTAL_BYTES = _positive_int("EXTORE_UPLOAD_TOTAL_BYTES", 5 * 1024 * 1024 * 1024)
UPLOAD_DISK_RESERVE_BYTES = _positive_int(
    "EXTORE_UPLOAD_DISK_RESERVE_BYTES", 512 * 1024 * 1024
)
UPLOAD_CONCURRENCY = _positive_int("EXTORE_UPLOAD_CONCURRENCY", 4, maximum=64)
UPLOAD_TIMEOUT_SECONDS = _positive_int("EXTORE_UPLOAD_TIMEOUT_SECONDS", 300)
UPLOAD_DRAFT_TTL_SECONDS = _positive_int("EXTORE_UPLOAD_DRAFT_TTL_SECONDS", 86400)


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

    if UPLOAD_FILE_BYTES > UPLOAD_JOB_BYTES or UPLOAD_JOB_BYTES > UPLOAD_TOTAL_BYTES:
        raise RuntimeError("Attachment limits must satisfy file <= job <= total bytes")
