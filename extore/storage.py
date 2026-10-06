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
    columns = {
        row["name"] for row in c.execute("PRAGMA table_info(upload_reservations)")
    }
    if "shop_id" not in columns:
        c.execute(
            "ALTER TABLE upload_reservations ADD COLUMN shop_id TEXT REFERENCES shops(id)"
        )
    if "product_id" not in columns:
        c.execute(
            "ALTER TABLE upload_reservations ADD COLUMN product_id TEXT REFERENCES products(id)"
        )
    c.execute(
        "CREATE INDEX IF NOT EXISTS upload_reservations_shop ON upload_reservations(shop_id)"
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


def _product_shop(c, product_id):
    row = c.execute(
        "SELECT shops.id,shops.storage_limit_bytes,shops.enabled FROM products "
        "JOIN shops ON shops.id=products.shop_id WHERE products.id=?",
        (product_id,),
    ).fetchone()
    if row is None or not row["enabled"]:
        fail("商品不存在", 404)
    return row


def _shop_totals(c, shop_id):
    stored = c.execute(
        "SELECT COALESCE(SUM(job_files.size),0) FROM job_files "
        "JOIN products ON products.id=job_files.product_id "
        "WHERE products.shop_id=? AND job_files.content IS NOT NULL",
        (shop_id,),
    ).fetchone()[0]
    pending = c.execute(
        "SELECT COALESCE(SUM(size),0) FROM upload_reservations WHERE shop_id=?",
        (shop_id,),
    ).fetchone()[0]
    return stored, pending


def check_storage_quota(c, additional, *, reservation_id=None, product_id=None):
    """Check replacement bytes on commit, or additional bytes while receiving.

    The caller owns a BEGIN IMMEDIATE transaction. A commit substitutes the
    reservation with the new BLOB exactly once; a stream increment includes all
    currently reserved bytes. The product's shop is always derived from SQLite.
    """
    if not isinstance(additional, int) or additional < 0:
        raise ValueError("Additional storage bytes must be a nonnegative integer")
    stored, pending = _totals(c)
    reservation = None
    if reservation_id:
        reservation = c.execute(
            "SELECT size,shop_id,product_id FROM upload_reservations WHERE id=?",
            (reservation_id,),
        ).fetchone()
        if not reservation:
            fail("上传已失效，请重新上传", 409)
        pending -= reservation["size"]
        if reservation["product_id"] is not None:
            if product_id not in (None, reservation["product_id"]):
                fail("上传不能更换所属商品", 409)
            product_id = reservation["product_id"]
    if stored + pending + additional > UPLOAD_TOTAL_BYTES:
        fail("服务器文件存储额度已满，请联系商家", 507)
    require_disk_space(additional, pending=pending)
    if product_id is not None:
        shop = _product_shop(c, product_id)
        if reservation and reservation["shop_id"] not in (None, shop["id"]):
            fail("上传不能更换所属店铺", 409)
        own_stored, own_pending = _shop_totals(c, shop["id"])
        if reservation and reservation["shop_id"] == shop["id"]:
            own_pending -= reservation["size"]
        if own_stored + own_pending + additional > shop["storage_limit_bytes"]:
            fail("此店铺的文件存储额度已满，请联系商家", 507)


def storage_usage(c, shop_id=None):
    """Shop callers receive only their own logical allocation, never disk state."""
    if shop_id is not None:
        shop = c.execute(
            "SELECT storage_limit_bytes FROM shops WHERE id=?", (shop_id,)
        ).fetchone()
        if shop is None:
            fail("店铺不存在", 404)
        stored, pending = _shop_totals(c, shop_id)
        return {
            "stored_bytes": stored,
            "uploading_bytes": pending,
            "limit_bytes": shop["storage_limit_bytes"],
            "active_uploads": c.execute(
                "SELECT COUNT(*) FROM upload_reservations WHERE shop_id=?", (shop_id,)
            ).fetchone()[0],
            "upload_concurrency": UPLOAD_CONCURRENCY,
        }
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


def set_shop_storage_limit(c, shop_id, limit):
    """Caller must authorize a superadmin; allocated bytes cannot be discarded."""
    if type(limit) is not int or not 1 <= limit <= UPLOAD_TOTAL_BYTES:
        fail("店铺文件额度必须为正整数且不超过全站文件额度")
    if not c.execute("SELECT id FROM shops WHERE id=?", (shop_id,)).fetchone():
        fail("店铺不存在", 404)
    stored, pending = _shop_totals(c, shop_id)
    if limit < stored + pending:
        fail("店铺文件额度不能小于已存储和正在上传的文件总量", 409)
    c.execute("UPDATE shops SET storage_limit_bytes=? WHERE id=?", (limit, shop_id))
    return storage_usage(c, shop_id)


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

    def bind_product(self, c, product_id):
        """Bind once to an authenticated product before its payload is spooled."""
        row = c.execute(
            "SELECT size,product_id FROM upload_reservations WHERE id=?", (self.id,)
        ).fetchone()
        if row is None:
            fail("上传已失效，请重新上传", 409)
        if row["product_id"] not in (None, product_id):
            fail("上传不能更换所属商品", 409)
        shop = _product_shop(c, product_id)
        check_storage_quota(
            c, row["size"], reservation_id=self.id, product_id=product_id
        )
        c.execute(
            "UPDATE upload_reservations SET shop_id=?,product_id=? WHERE id=?",
            (shop["id"], product_id, self.id),
        )

    def grow(self, size):
        """Reserve file bytes atomically before the multipart spool writes them."""
        if size == 0:
            return
        with db() as c:
            row = c.execute(
                "SELECT product_id,shop_id FROM upload_reservations WHERE id=?",
                (self.id,),
            ).fetchone()
            if row is None:
                fail("上传已失效，请重新上传", 409)
            if row["product_id"] is not None:
                if _product_shop(c, row["product_id"])["id"] != row["shop_id"]:
                    fail("上传所属店铺已变化，请重新上传", 409)
            check_storage_quota(c, size, product_id=row["product_id"])
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
