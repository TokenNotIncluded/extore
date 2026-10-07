"""Immutable card attributes and bounded, idempotent post-delivery work.

Revisions reuse the original job and monotonically increasing attempt fence.
Successful older deliveries have independent read authority and storage lifetime.
"""

import hashlib
import hmac
import json
import sqlite3
import time

from .card_tracking import card_expired
from .config import DATA
from .security import fail
from .variants import card_variant, validate_attributes

MAX_REVISIONS = 1000


def init_schema(c):
    c.execute(
        "CREATE TABLE IF NOT EXISTS card_entitlements ("
        "card_id TEXT PRIMARY KEY REFERENCES cards(id) ON DELETE CASCADE,"
        "attributes TEXT NOT NULL,policy TEXT,created REAL NOT NULL)"
    )
    c.execute(
        "CREATE TABLE IF NOT EXISTS job_revision_requests ("
        "job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,"
        "request_id TEXT NOT NULL,request_digest TEXT NOT NULL,"
        "revision INTEGER NOT NULL CHECK(revision>0),message TEXT NOT NULL,"
        "created REAL NOT NULL,PRIMARY KEY(job_id,request_id),UNIQUE(job_id,revision))"
    )
    c.execute(
        "CREATE TABLE IF NOT EXISTS job_delivery_versions ("
        "job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,"
        "revision INTEGER NOT NULL CHECK(revision>=0),attempt INTEGER NOT NULL,"
        "content TEXT,result_json TEXT,revealed INTEGER NOT NULL DEFAULT 0,"
        "created REAL NOT NULL,PRIMARY KEY(job_id,revision))"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS delivery_versions_attempt ON job_delivery_versions(job_id,attempt)"
    )
    columns = {row["name"] for row in c.execute("PRAGMA table_info(jobs)")}
    for name, definition in (
        ("revision_round", "INTEGER NOT NULL DEFAULT 0"),
        ("revision_start_attempt", "INTEGER NOT NULL DEFAULT 1"),
        ("revision_message", "TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in columns:
            c.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")


def _value(row, name, default=None):
    return row[name] if name in row.keys() else default


def _missing_schema(error):
    return "no such table" in str(error) or "no such column" in str(error)


def validate_capacity(attributes, policy):
    if not policy:
        return 0
    value = attributes.get(policy["attribute_key"], 0)
    if type(value) is not int or not 0 <= value <= MAX_REVISIONS:
        raise ValueError("修改额度属性必须是 0–1000 的整数，不能使用文本、布尔值或小数")
    return value


def freeze_card(c, card_id, attributes, policy):
    """Every new card records even a null policy; old cards never inherit one."""
    attributes = validate_attributes(attributes)
    validate_capacity(attributes, policy)
    c.execute(
        "INSERT INTO card_entitlements(card_id,attributes,policy,created) VALUES (?,?,?,?)",
        (
            card_id,
            json.dumps(attributes, ensure_ascii=False, allow_nan=False),
            json.dumps(policy, ensure_ascii=False) if policy else None,
            time.time(),
        ),
    )


def _record(c, card_id):
    try:
        return c.execute(
            "SELECT attributes,policy FROM card_entitlements WHERE card_id=?",
            (card_id,),
        ).fetchone()
    except sqlite3.OperationalError as error:
        if not _missing_schema(error):
            raise
        return None


def card_attributes(c, card):
    record = _record(c, card["id"] if "card_id" not in card.keys() else card["card_id"])
    return (
        json.loads(record["attributes"])
        if record
        else card_variant(c, card)["attributes"]
    )


def frozen_policy(c, card_id):
    record = _record(c, card_id)
    return json.loads(record["policy"]) if record and record["policy"] else None


def round_attempt(row):
    return row["attempt"] - _value(row, "revision_start_attempt", 1) + 1


def revision_view(row):
    current = _value(row, "revision_round", 0)
    return {
        "current": current,
        "message": _value(row, "revision_message", ""),
        "is_revision": current > 0,
    }


def _versions(c, row):
    try:
        return [
            dict(item)
            for item in c.execute(
                "SELECT * FROM job_delivery_versions WHERE job_id=? ORDER BY revision",
                (row["id"],),
            )
        ]
    except sqlite3.OperationalError as error:
        if not _missing_schema(error):
            raise
        return []


def _current_delivery(row):
    return {
        "job_id": row["id"],
        "revision": _value(row, "revision_round", 0),
        "attempt": row["attempt"],
        "content": row["content"],
        "result_json": row["result_json"],
        "revealed": row["revealed"],
        "created": row["updated"],
        "current": True,
    }


def delivery_view(c, row):
    from .field_values import ATTACHMENT_TYPES, attachment_ids
    from .service import job_product

    # Polling never materializes historical document bodies in Python memory.
    try:
        result = [
            dict(item)
            for item in c.execute(
                "SELECT v.revision,v.attempt,v.created,v.revealed,EXISTS(SELECT 1 FROM job_files f "
                "WHERE f.job_id=v.job_id AND f.attempt=v.attempt AND f.kind='output' AND f.bound=1 AND f.content IS NOT NULL) AS has_files "
                "FROM job_delivery_versions v WHERE v.job_id=? ORDER BY v.revision",
                (row["id"],),
            )
        ]
    except sqlite3.OperationalError as error:
        if not _missing_schema(error):
            raise
        result = []
    for item in result:
        item["revealed"] = bool(item["revealed"])
        item["has_files"] = bool(item["has_files"])
    if row["state"] == "succeeded":
        current = _current_delivery(row)
        output = json.loads(current["result_json"] or "{}")
        fields = job_product(c, row)["outputs"]
        result.append(
            {
                **{key: current[key] for key in ("revision", "attempt", "created")},
                "revealed": bool(current["revealed"]),
                "has_files": any(
                    attachment_ids(field, output.get(field["key"], ""))
                    for field in fields
                    if field["type"] in ATTACHMENT_TYPES
                ),
            }
        )
    return result


def last_delivery(c, row):
    if row["state"] == "succeeded":
        current = _current_delivery(row)
        return {key: current[key] for key in ("revision", "attempt", "created")}
    try:
        saved = c.execute(
            "SELECT revision,attempt,created FROM job_delivery_versions WHERE job_id=? ORDER BY revision DESC LIMIT 1",
            (row["id"],),
        ).fetchone()
    except sqlite3.OperationalError as error:
        if not _missing_schema(error):
            raise
        saved = None
    return dict(saved) if saved else None


def has_previous_delivery(c, row):
    try:
        return bool(
            c.execute(
                "SELECT 1 FROM job_delivery_versions WHERE job_id=? LIMIT 1",
                (row["id"],),
            ).fetchone()
        )
    except sqlite3.OperationalError as error:
        if not _missing_schema(error):
            raise
        return False


def preserved_output_ids(c, row):
    # bind_outputs only binds selected outputs, and deletes all other drafts.
    # This retention query is not download authority: downloads still check the
    # archived JSON's exact field/file reference independently.
    try:
        return {
            item[0]
            for item in c.execute(
                "SELECT id FROM job_files f WHERE f.job_id=? AND f.kind='output' AND f.bound=1 "
                "AND EXISTS(SELECT 1 FROM job_delivery_versions v WHERE v.job_id=f.job_id AND v.attempt=f.attempt AND v.result_json IS NOT NULL)",
                (row["id"],),
            )
        }
    except sqlite3.OperationalError as error:
        if not _missing_schema(error):
            raise
        return set()


def file_delivery(c, row, item, revision=None):
    """Select an exact completed result that actually references this file."""
    from .field_values import ATTACHMENT_TYPES, attachment_ids
    from .service import job_product

    if row["state"] == "destroyed":
        fail("文件已销毁或无法领取", 410)
    versions = [
        dict(version)
        for version in c.execute(
            "SELECT * FROM job_delivery_versions WHERE job_id=? AND attempt=?",
            (row["id"], item["attempt"]),
        )
    ]
    if row["state"] == "succeeded":
        versions.append(_current_delivery(row))
    fields = job_product(c, row)["outputs"]
    for delivery in versions:
        if revision is not None and delivery["revision"] != revision:
            continue
        output = json.loads(delivery["result_json"] or "{}")
        if delivery["attempt"] != item["attempt"]:
            continue
        for field in fields:
            if (
                field["key"] != item["field_key"]
                or field["type"] not in ATTACHMENT_TYPES
            ):
                continue
            if item["id"] in attachment_ids(field, output.get(field["key"], "")):
                return {**delivery, "current": delivery.get("current", False)}
    return None


def card_view(c, card, row=None):
    policy = frozen_policy(c, card["id"])
    attributes = card_attributes(c, card)
    if not policy:
        return {"card_attributes": attributes, "entitlements": None}
    if row is None:
        row = c.execute("SELECT * FROM jobs WHERE card_id=?", (card["id"],)).fetchone()
    total = validate_capacity(attributes, policy)
    used = (
        c.execute(
            "SELECT COUNT(*) FROM job_revision_requests WHERE job_id=?", (row["id"],)
        ).fetchone()[0]
        if row
        else 0
    )
    reason = None
    if card["state"] == "revoked":
        reason = "revoked"
    elif card["state"] == "rejected" or row and row["state"] == "rejected":
        reason = "rejected"
    elif row and row["state"] == "destroyed":
        reason = "destroyed"
    elif card_expired(c, card["id"]):
        reason = "expired"
    else:
        shop = c.execute(
            "SELECT shops.enabled FROM shops JOIN products ON products.shop_id=shops.id WHERE products.id=?",
            (card["product_id"],),
        ).fetchone()
        if not shop or not shop["enabled"]:
            reason = "shop_disabled"
        elif not row:
            reason = "not_delivered"
        elif row["state"] != "succeeded":
            reason = (
                "in_progress"
                if row["state"] in ("queued", "processing")
                else "not_delivered"
            )
        elif used >= total:
            reason = "exhausted"
    return {
        "card_attributes": attributes,
        "entitlements": {
            "attribute_key": policy["attribute_key"],
            "label": policy["label"],
            "total": total,
            "used": used,
            "remaining": max(0, total - used),
            "can_request": reason is None,
            "reason": reason,
        },
    }


def job_context(c, row, *, include_deliveries=True):
    card = c.execute("SELECT * FROM cards WHERE id=?", (row["card_id"],)).fetchone()
    context = {
        **card_view(c, card, row),
        "revision": revision_view(row),
        "last_delivery": last_delivery(c, row),
    }
    if include_deliveries:
        context["deliveries"] = delivery_view(c, row)
    return context


def select_delivery(c, row, revision=None):
    if row["state"] == "destroyed":
        fail("尚无可领取内容", 409)
    current = _current_delivery(row) if row["state"] == "succeeded" else None
    if current and (revision is None or revision == current["revision"]):
        return current
    try:
        saved = c.execute(
            "SELECT * FROM job_delivery_versions WHERE job_id=? AND (? IS NULL OR revision=?) ORDER BY revision DESC LIMIT 1",
            (row["id"], revision, revision),
        ).fetchone()
    except sqlite3.OperationalError as error:
        if not _missing_schema(error):
            raise
        saved = None
    if not saved:
        fail(
            "尚无可领取内容" if revision is None else "交付版本不存在",
            409 if revision is None else 404,
        )
    return {**dict(saved), "current": False}


def delivery_row(row, delivery):
    """Use the archived attempt for the existing exact file validators."""
    return {**dict(row), **delivery, "id": row["id"], "state": "succeeded"}


def mark_revealed(c, row, delivery):
    if delivery["current"]:
        c.execute("UPDATE jobs SET revealed=1 WHERE id=?", (row["id"],))
    else:
        c.execute(
            "UPDATE job_delivery_versions SET revealed=1 WHERE job_id=? AND revision=?",
            (row["id"], delivery["revision"]),
        )


def allocated_bytes(c, shop_id=None, card_id=None):
    """Historical text and revision suggestions share normal storage quotas."""
    tables = {
        item[0]
        for item in c.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "job_delivery_versions" not in tables:
        return 0
    scope = " AND (? IS NULL OR p.shop_id=?) AND (? IS NULL OR j.card_id=?)"
    values = (shop_id, shop_id, card_id, card_id)
    saved = c.execute(
        "SELECT COALESCE(SUM(COALESCE(length(CAST(v.content AS BLOB)),0)+COALESCE(length(CAST(v.result_json AS BLOB)),0)),0) "
        "FROM job_delivery_versions v JOIN jobs j ON j.id=v.job_id JOIN products p ON p.id=j.product_id "
        "WHERE v.result_json IS NOT NULL" + scope,
        values,
    ).fetchone()[0]
    messages = c.execute(
        "SELECT COALESCE(SUM(length(CAST(r.message AS BLOB))),0) "
        "FROM job_revision_requests r JOIN jobs j ON j.id=r.job_id JOIN products p ON p.id=j.product_id "
        "WHERE 1=1" + scope,
        values,
    ).fetchone()[0]
    current = c.execute(
        "SELECT COALESCE(SUM(COALESCE(length(CAST(j.content AS BLOB)),0)+COALESCE(length(CAST(j.result_json AS BLOB)),0)),0) "
        "FROM jobs j JOIN products p ON p.id=j.product_id WHERE j.revision_round>0 AND j.result_json IS NOT NULL"
        + scope,
        values,
    ).fetchone()[0]
    return saved + messages + current


def _request_digest(row, request):
    raw = json.dumps(
        [row["id"], request.expected_revision, request.message],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(
        (DATA / "issuance.key").read_bytes(), raw, hashlib.sha256
    ).hexdigest()


def request_revision(c, card, request):
    from .db import audit, event
    from .files import MAX_CARD_BYTES, bind_inputs
    from .service import job, job_product
    from .shops import require_enabled_product
    from .storage import check_storage_quota

    require_enabled_product(c, card["product_id"])
    row = c.execute("SELECT * FROM jobs WHERE card_id=?", (card["id"],)).fetchone()
    if not row:
        fail("请先完成首次兑换再申请修改", 409)
    if row["state"] == "destroyed" or card["state"] in ("revoked", "rejected"):
        fail("卡密已撤销、拒绝或交付已销毁，不能申请修改", 410)
    fingerprint = _request_digest(row, request)
    previous = c.execute(
        "SELECT request_digest FROM job_revision_requests WHERE job_id=? AND request_id=?",
        (row["id"], request.request_id),
    ).fetchone()
    if previous:
        if not hmac.compare_digest(previous["request_digest"], fingerprint):
            fail("同一个修改请求编号不能用于不同内容", 409)
        return row
    state = card_view(c, card, row)["entitlements"]
    if state is None:
        fail("此卡密未配置交付后修改权益", 409)
    if request.expected_revision != _value(row, "revision_round", 0):
        fail("交付轮次已改变，请刷新后再提交修改", 409)
    if not state["can_request"]:
        fail("当前不能申请修改：" + state["reason"], 409)
    p = job_product(c, row)
    if (
        p["view_policy"] != "repeat"
        or p["delivery"] != "content"
        or p["mode"] == "stock"
        or p.get("task_flow")
    ):
        fail("此商品的处理方式不支持交付后修改", 409)
    # Current revision bodies are already quota-accounted. Archiving transfers
    # those bytes; only the legacy first delivery and new suggestion add bytes.
    extra = len(request.message.encode("utf-8"))
    if not _value(row, "revision_round", 0):
        extra += sum(
            len((row[key] or "").encode("utf-8")) for key in ("content", "result_json")
        )
    files = c.execute(
        "SELECT COALESCE(SUM(size),0) FROM job_files WHERE card_id=? AND content IS NOT NULL",
        (card["id"],),
    ).fetchone()[0]
    if files + allocated_bytes(c, card_id=card["id"]) + extra > MAX_CARD_BYTES:
        fail("此卡密的交付历史和文件总量已达到容量上限", 413)
    check_storage_quota(c, extra, product_id=card["product_id"])
    now = time.time()
    current = _value(row, "revision_round", 0)
    c.execute(
        "INSERT INTO job_delivery_versions(job_id,revision,attempt,content,result_json,revealed,created) VALUES (?,?,?,?,?,?,?)",
        (
            row["id"],
            current,
            row["attempt"],
            row["content"],
            row["result_json"],
            row["revealed"],
            row["updated"],
        ),
    )
    c.execute(
        "INSERT INTO job_revision_requests(job_id,request_id,request_digest,revision,message,created) VALUES (?,?,?,?,?,?)",
        (row["id"], request.request_id, fingerprint, current + 1, request.message, now),
    )
    c.execute(
        "UPDATE jobs SET revision_round=?,revision_message=?,revision_start_attempt=attempt+1,attempt=attempt+1,"
        "state='queued',content=NULL,result_json=NULL,revealed=0,message='',progress=0,completed_steps='[]',"
        "retryable=0,claimed_by=NULL,lease=NULL,updated=? WHERE id=?",
        (current + 1, request.message, now, row["id"]),
    )
    # Previously fulfilled cards stay used during every revision/retry outcome.
    c.execute("UPDATE cards SET state='used' WHERE id=?", (card["id"],))
    row = job(c, row["id"])
    bind_inputs(c, row, json.loads(row["params"]))
    event(c, "revision.requested", row["product_id"], row)
    audit(c, "customer", "revision.request", row["id"])
    return row


def destroy(c, row):
    c.execute(
        "UPDATE job_delivery_versions SET content=NULL,result_json=NULL WHERE job_id=?",
        (row["id"],),
    )
    c.execute(
        "UPDATE job_revision_requests SET message='',request_digest='' WHERE job_id=?",
        (row["id"],),
    )
    c.execute("UPDATE jobs SET revision_message='' WHERE id=?", (row["id"],))
