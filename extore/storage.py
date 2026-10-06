"""Transactional upload quotas, temporary-file leases, and safe draft maintenance."""

import errno
import fcntl
import os
import re
import shutil
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager

from .config import (
    DATA,
    UPLOAD_CONCURRENCY,
    UPLOAD_DISK_RESERVE_BYTES,
    UPLOAD_DRAFT_TTL_SECONDS,
    UPLOAD_TOTAL_BYTES,
)
from .db import db
from .security import fail

_LOCK_NAME = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\.lock\Z")
# A spool, SQLite pages and WAL may coexist before a checkpoint. Keep headroom
# for another page copy rather than estimating from logical attachment bytes.
_WRITE_COPIES = 4


def init_schema(c):
    c.execute(
        "CREATE TABLE IF NOT EXISTS upload_reservations ("
        "id TEXT PRIMARY KEY, size INTEGER NOT NULL DEFAULT 0 CHECK(size>=0), "
        "created REAL NOT NULL)"
    )


def _directory():
    directory = DATA / "upload-tmp"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    return directory


def disk_free_bytes():
    return shutil.disk_usage(DATA).free


def require_disk_space(additional, *, pending=0):
    if disk_free_bytes() < UPLOAD_DISK_RESERVE_BYTES + _WRITE_COPIES * (
        additional + pending
    ):
        fail("服务器文件空间不足，请联系商家", 507)


def _totals(c):
    stored = c.execute(
        "SELECT COALESCE(SUM(size),0) FROM job_files WHERE content IS NOT NULL"
    ).fetchone()[0]
    pending = c.execute(
        "SELECT COALESCE(SUM(size),0) FROM upload_reservations"
    ).fetchone()[0]
    return stored, pending


def check_storage_quota(c, additional, *, reservation_id=None):
    stored, pending = _totals(c)
    if reservation_id:
        row = c.execute(
            "SELECT size FROM upload_reservations WHERE id=?", (reservation_id,)
        ).fetchone()
        if not row:
            fail("上传已失效，请重新上传", 409)
        pending -= row[0]
    if stored + pending + additional > UPLOAD_TOTAL_BYTES:
        fail("服务器文件存储额度已满，请联系商家", 507)
    require_disk_space(additional, pending=pending)


def storage_usage(c):
    stored, pending = _totals(c)
    return {
        "stored_bytes": stored,
        "uploading_bytes": pending,
        "limit_bytes": UPLOAD_TOTAL_BYTES,
        "disk_free_bytes": disk_free_bytes(),
        "disk_reserve_bytes": UPLOAD_DISK_RESERVE_BYTES,
        "active_uploads": c.execute(
            "SELECT COUNT(*) FROM upload_reservations"
        ).fetchone()[0],
        "upload_concurrency": UPLOAD_CONCURRENCY,
    }


def _dead_lock(path):
    """Lock an abandoned lease; an active uploader keeps its OS lock until close."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EISDIR, errno.EACCES):
            return False
        raise
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return False
    except BaseException:
        os.close(fd)
        raise
    return fd


def prune_uploads(c, *, now=None):
    """Drop abandoned reservations and drafts, never active or bound attachments.

    Must run inside the caller's BEGIN IMMEDIATE transaction. Expiry alone never
    cancels an upload: a live request owns a file lock, even during a slow read.
    Output drafts on the current processing attempt remain available for delivery.
    """
    now = time.time() if now is None else now
    directory = _directory()
    abandoned = 0
    for row in c.execute("SELECT id FROM upload_reservations").fetchall():
        name = row["id"] + ".lock"
        if not _LOCK_NAME.fullmatch(name):
            continue
        path = directory / name
        lease = _dead_lock(path)
        if lease is False:
            continue
        try:
            c.execute("DELETE FROM upload_reservations WHERE id=?", (row["id"],))
            abandoned += 1
            if lease is not None:
                path.unlink(missing_ok=True)
        finally:
            if lease is not None:
                os.close(lease)
    # A crash between creating the lock and registering its row leaves no quota
    # row. Remove only old, generated lease names after obtaining the same lock.
    known = {
        row[0] + ".lock" for row in c.execute("SELECT id FROM upload_reservations")
    }
    for path in directory.iterdir():
        if not _LOCK_NAME.fullmatch(path.name) or path.name in known:
            continue
        try:
            if path.lstat().st_mtime >= now - 3600:
                continue
        except FileNotFoundError:
            # A request may finish and unlink its lease during this scan.
            continue
        lease = _dead_lock(path)
        if lease is False:
            continue
        try:
            if lease is not None:
                path.unlink(missing_ok=True)
        finally:
            if lease is not None:
                os.close(lease)
    # secure_delete writes erased BLOB pages to WAL. Bound each maintenance
    # transaction so reclaiming a large backlog cannot itself fill the disk.
    expired = c.execute(
        "SELECT id,size,content IS NOT NULL AS available FROM job_files "
        "WHERE bound=0 AND created<? AND "
        "(kind='input' OR NOT EXISTS (SELECT 1 FROM jobs WHERE jobs.id=job_files.job_id "
        "AND jobs.state='processing' AND jobs.attempt=job_files.attempt)) "
        "ORDER BY created,id LIMIT 100",
        (now - UPLOAD_DRAFT_TTL_SECONDS,),
    ).fetchall()
    removed = 0
    erased = 0
    for row in expired:
        size = row["size"] if row["available"] else 0
        if erased + size > 20 * 1024 * 1024:
            break
        if size and disk_free_bytes() < UPLOAD_DISK_RESERVE_BYTES + _WRITE_COPIES * (
            erased + size
        ):
            break
        c.execute("DELETE FROM job_files WHERE id=?", (row["id"],))
        erased += size
        removed += 1
    return {"abandoned_uploads": abandoned, "expired_drafts": removed}


def maintenance_once():
    with db() as c:
        result = prune_uploads(c)
    # A live reader may pin WAL pages. Try briefly, then leave the checkpoint
    # to the next pass rather than interrupting readers or holding a write lock.
    result["wal_checkpoint_busy"] = True
    try:
        with closing(
            sqlite3.connect(DATA / "extore.sqlite3", timeout=0.1, isolation_level=None)
        ) as c:
            c.execute("PRAGMA busy_timeout=100")
            result["wal_checkpoint_busy"] = bool(
                c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
            )
    except sqlite3.Error:
        # Capacity maintenance must not fail completed work because an unrelated
        # writer or reader is busy; its normal SQLite recovery remains intact.
        pass
    return result


class UploadReservation:
    def __init__(self, identifier):
        self.id = identifier

    def grow(self, size):
        """Reserve file bytes atomically before the multipart spool writes them."""
        if size == 0:
            return
        with db() as c:
            check_storage_quota(c, size)
            changed = c.execute(
                "UPDATE upload_reservations SET size=size+? WHERE id=?", (size, self.id)
            ).rowcount
            if not changed:
                fail("上传已失效，请重新上传", 409)

    def finish(self, c):
        c.execute("DELETE FROM upload_reservations WHERE id=?", (self.id,))


@contextmanager
def receiving_upload():
    directory = _directory()
    identifier = str(uuid.uuid4())
    path = directory / (identifier + ".lock")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with db() as c:
            prune_uploads(c)
            require_disk_space(0)
            if (
                c.execute("SELECT COUNT(*) FROM upload_reservations").fetchone()[0]
                >= UPLOAD_CONCURRENCY
            ):
                fail("同时上传的文件太多，请稍后重试", 429)
            c.execute(
                "INSERT INTO upload_reservations(id,created) VALUES (?,?)",
                (identifier, time.time()),
            )
        yield UploadReservation(identifier)
    finally:
        try:
            with db() as c:
                c.execute("DELETE FROM upload_reservations WHERE id=?", (identifier,))
        except sqlite3.Error:
            # A genuinely full disk can prevent a cleanup transaction. Closing
            # the lease still lets the next maintenance pass reclaim this row.
            pass
        os.close(fd)
        path.unlink(missing_ok=True)


def translate_storage_error(exc):
    if (
        isinstance(exc, OSError)
        and exc.errno in (errno.ENOSPC, errno.EDQUOT)
        or isinstance(exc, sqlite3.Error)
        and getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL
    ):
        fail("服务器文件空间不足，请联系商家", 507)
    raise exc
