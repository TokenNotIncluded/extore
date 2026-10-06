"""Bounded, tenant-scoped retention for links and finished records."""

import json
import math
import time
from typing import Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from .db import audit, db, set_setting, setting
from .security import authorize_management, fail, session

router = APIRouter(prefix="/api")
DAY = 86400
AREAS = ("links", "events", "audit")


class RetentionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(default=True, strict=True)
    event_retention_days: int = Field(default=30, strict=True, ge=1, le=3650)
    dead_letter_retention_days: int = Field(default=90, strict=True, ge=1, le=3650)
    audit_retention_days: int = Field(default=180, strict=True, ge=90, le=3650)
    link_retention_days: int = Field(default=90, strict=True, ge=1, le=3650)


class PolicyInput(RetentionPolicy):
    shop_id: str | None = Field(default=None, min_length=1, max_length=100)


class CleanupInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    areas: list[Literal["links", "events", "audit"]] = Field(
        default_factory=lambda: list(AREAS), min_length=1, max_length=3
    )
    dry_run: bool = Field(default=True, strict=True)
    limit: int = Field(default=100, strict=True, ge=1, le=500)
    product_id: str | None = Field(default=None, min_length=1, max_length=100)
    shop_id: str | None = Field(default=None, min_length=1, max_length=100)


class LinkCleanupInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dry_run: bool = Field(default=True, strict=True)
    limit: int = Field(default=100, strict=True, ge=1, le=500)
    product_id: str | None = Field(default=None, min_length=1, max_length=100)


def resolve_audit_shop(c, actor, target):
    """Unknown historical targets remain platform-only; never trust request scope."""
    if not c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='shops'"
    ).fetchone():
        return None
    if isinstance(actor, str) and actor.startswith("shop:"):
        row = c.execute("SELECT id FROM shops WHERE id=?", (actor[5:],)).fetchone()
        if row:
            return row["id"]
    row = c.execute(
        "SELECT products.shop_id FROM staff JOIN products ON products.id=staff.product_id WHERE staff.id=?",
        (actor,),
    ).fetchone()
    if row:
        return row["shop_id"]
    # This table is created after the historical audit migration. Older
    # installations must still resolve their existing records during init.
    if c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='pipeline_authorizations'"
    ).fetchone():
        row = c.execute(
            "SELECT shop_id FROM pipeline_authorizations WHERE id=?", (target,)
        ).fetchone()
        if row:
            return row["shop_id"]
    if c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cli_scope_requests'"
    ).fetchone():
        row = c.execute(
            "SELECT COALESCE(approved_shop_id,shop_id) AS shop_id "
            "FROM cli_scope_requests WHERE id=?",
            (target,),
        ).fetchone()
        if row:
            return row["shop_id"]
    for sql in (
        "SELECT id AS shop_id FROM shops WHERE id=?",
        "SELECT shop_id FROM products WHERE id=?",
        "SELECT products.shop_id FROM jobs JOIN products ON products.id=jobs.product_id WHERE jobs.id=?",
        "SELECT products.shop_id FROM cards JOIN products ON products.id=cards.product_id WHERE cards.id=?",
        "SELECT products.shop_id FROM staff JOIN products ON products.id=staff.product_id WHERE staff.id=?",
        "SELECT products.shop_id FROM events JOIN products ON products.id=events.product_id WHERE events.id=?",
        "SELECT products.shop_id FROM cli_devices JOIN staff ON staff.id=cli_devices.staff_id JOIN products ON products.id=staff.product_id WHERE cli_devices.id=?",
        "SELECT shop_id FROM sessions WHERE id=?",
        "SELECT shop_id FROM owner_cli_devices WHERE id=?",
        "SELECT shop_id FROM owner_cli_requests WHERE id=?",
    ):
        row = c.execute(sql, (target,)).fetchone()
        if row:
            return row["shop_id"]
    if isinstance(target, str) and ":" in target:
        row = c.execute(
            "SELECT shop_id FROM products WHERE id=?", (target.split(":", 1)[0],)
        ).fetchone()
        if row:
            return row["shop_id"]
    return None


def init_schema(c):
    from .link_cleanup import init_schema as init_links

    init_links(c)
    columns = {row["name"] for row in c.execute("PRAGMA table_info(outbox)")}
    if "finished_at" not in columns:
        c.execute("ALTER TABLE outbox ADD COLUMN finished_at REAL")
        # Old delivery completion timestamps are unknown. Starting retention
        # now preserves the full configured interval after the upgrade.
        c.execute(
            "UPDATE outbox SET finished_at=? WHERE state IN ('delivered','dead','cancelled')",
            (time.time(),),
        )
    columns = {row["name"] for row in c.execute("PRAGMA table_info(audit)")}
    if "shop_id" not in columns:
        c.execute("ALTER TABLE audit ADD COLUMN shop_id TEXT REFERENCES shops(id)")
        for row in c.execute("SELECT id,actor,target FROM audit"):
            shop_id = resolve_audit_shop(c, row["actor"], row["target"])
            if shop_id is not None:
                c.execute("UPDATE audit SET shop_id=? WHERE id=?", (shop_id, row["id"]))
    c.execute("CREATE INDEX IF NOT EXISTS outbox_finished ON outbox(state,finished_at)")
    c.execute(
        "CREATE INDEX IF NOT EXISTS events_retention ON events(created,product_id)"
    )
    c.execute("CREATE INDEX IF NOT EXISTS audit_retention ON audit(shop_id,created,id)")


def _policy_key(shop_id):
    return "maintenance_policy:" + (shop_id if shop_id is not None else "platform")


def _retain_audit_id_floor(c):
    highest = c.execute("SELECT COALESCE(MAX(id),0) FROM audit").fetchone()[0]
    recorded = int(setting(c, "audit_id_sequence", "0"))
    value = max(highest, recorded)
    if value > recorded:
        set_setting(c, "audit_id_sequence", str(value))
    return value


def next_audit_id(c):
    """Retention must not make an exported audit ID identify a different event."""
    value = _retain_audit_id_floor(c) + 1
    set_setting(c, "audit_id_sequence", str(value))
    return value


def get_policy(c, shop_id=None):
    defaults = RetentionPolicy().model_dump()
    raw = setting(c, _policy_key(None))
    if raw:
        defaults.update(json.loads(raw))
    global_enabled = defaults["enabled"]
    if shop_id is not None:
        raw = setting(c, _policy_key(shop_id))
        if raw:
            defaults.update(json.loads(raw))
    defaults["enabled"] = bool(global_enabled and defaults["enabled"])
    return RetentionPolicy.model_validate(defaults).model_dump()


def _scope(c, s, shop_id=None, product_id=None):
    from .shops import shop_row

    authorize_management(c, s)
    own = s.get("shop_id")
    if own is not None and shop_id not in (None, own):
        fail("不能清理其他店铺的记录", 403)
    scope = own if own is not None else shop_id
    if scope is not None:
        shop_row(c, scope, require_enabled=s.get("shop_id") is not None)
    if product_id:
        product = c.execute(
            "SELECT shop_id FROM products WHERE id=?", (product_id,)
        ).fetchone()
        if product is None or (scope is not None and product["shop_id"] != scope):
            fail("商品不存在或不属于此店铺", 404)
        if s["role"] == "staff" and product_id != s["product_id"]:
            fail("没有此商品的管理权限", 403)
        if scope is None:
            scope = product["shop_id"]
    return scope


def _event_ids(c, shop_id, product_id, policy, now, limit):
    return [
        row["id"]
        for row in c.execute(
            "SELECT events.id FROM events LEFT JOIN products ON products.id=events.product_id "
            "LEFT JOIN outbox ON outbox.id=events.id "
            "WHERE (? IS NULL OR products.shop_id=?) AND (? IS NULL OR events.product_id=?) "
            "AND ((outbox.id IS NULL AND events.created<?) "
            "OR (outbox.state IN ('delivered','cancelled') AND outbox.finished_at<?) "
            "OR (outbox.state='dead' AND outbox.finished_at<?)) "
            "ORDER BY events.created,events.id LIMIT ?",
            (
                shop_id,
                shop_id,
                product_id,
                product_id,
                now - policy["event_retention_days"] * DAY,
                now - policy["event_retention_days"] * DAY,
                now - policy["dead_letter_retention_days"] * DAY,
                limit,
            ),
        )
    ]


def _product_audit_clause(product_id):
    # Exact retained resource IDs are safe. Unknown/free-form targets cannot
    # be claimed by a product filter and remain available to its shop policy.
    return (
        " AND (target=? OR target IN (SELECT id FROM jobs WHERE product_id=?) "
        "OR target IN (SELECT id FROM cards WHERE product_id=?) "
        "OR target IN (SELECT id FROM staff WHERE product_id=?) "
        "OR target IN (SELECT id FROM events WHERE product_id=?) "
        "OR actor IN (SELECT id FROM staff WHERE product_id=?))",
        [product_id] * 6,
    )


def _audit_ids(c, shop_id, product_id, policy, now, limit, *, platform_only=False):
    sql = "SELECT id FROM audit WHERE created<? AND (? IS NULL OR shop_id=?)"
    args = [now - policy["audit_retention_days"] * DAY, shop_id, shop_id]
    if platform_only:
        sql += " AND shop_id IS NULL"
    if product_id:
        clause, values = _product_audit_clause(product_id)
        sql += clause
        args.extend(values)
    return [
        row["id"]
        for row in c.execute(sql + " ORDER BY created,id LIMIT ?", (*args, limit))
    ]


def cleanup_records(
    c,
    *,
    shop_id=None,
    product_id=None,
    areas=AREAS,
    policy=None,
    dry_run=True,
    limit=100,
    now=None,
    actor="owner",
    staff_id=None,
    automatic=False,
    platform_only=False,
):
    from .link_cleanup import cleanup_links, eligible_links
    from .security import staff_authorization

    if type(limit) is not int or not 1 <= limit <= 500:
        fail("清理批次必须在 1 至 500 条之间", 422)
    if not areas or any(area not in AREAS for area in areas):
        fail("清理类别无效", 422)
    if type(dry_run) is not bool:
        fail("预览标记必须是布尔值", 422)
    if actor not in ("owner", "system"):
        if isinstance(actor, str) and actor.startswith("shop:"):
            from .shops import shop_row

            own = actor.removeprefix("shop:")
            shop_row(c, own)
            if shop_id not in (None, own):
                fail("不能清理其他店铺的记录", 403)
            shop_id = own
        else:
            authority = staff_authorization(c, actor)
            if "links.delegate" not in authority["permissions"] or set(areas) != {
                "links"
            }:
                fail("没有执行此清理操作的权限", 403)
            if (
                staff_id not in (None, actor)
                or shop_id not in (None, authority["shop_id"])
                or product_id not in (None, authority["product_id"])
            ):
                fail("不能清理其他商品的管理链接", 403)
            staff_id, shop_id, product_id = (
                actor,
                authority["shop_id"],
                authority["product_id"],
            )
    now = time.time() if now is None else now
    if (
        isinstance(now, bool)
        or not isinstance(now, (int, float))
        or not math.isfinite(now)
    ):
        fail("清理时间无效", 422)
    policy = (
        RetentionPolicy.model_validate(
            {**RetentionPolicy().model_dump(), **policy}
        ).model_dump()
        if policy is not None
        else get_policy(c, shop_id)
    )
    selected = {area: [] for area in AREAS}
    if "links" in areas:
        selected["links"] = eligible_links(
            c,
            shop_id,
            product_id,
            now - policy["link_retention_days"] * DAY if automatic else None,
            limit,
            now,
            staff_id=staff_id,
        )
    if "events" in areas:
        selected["events"] = _event_ids(c, shop_id, product_id, policy, now, limit + 1)
    if "audit" in areas:
        selected["audit"] = _audit_ids(
            c, shop_id, product_id, policy, now, limit + 1, platform_only=platform_only
        )
    eligible = {area: min(limit, len(ids)) for area, ids in selected.items()}
    has_more = {area: len(ids) > limit for area, ids in selected.items()}
    # Link authorization traverses a bounded candidate batch, so a full batch
    # conservatively asks the caller to run one more pass.
    has_more["links"] = len(selected["links"]) == limit
    changed = dict.fromkeys(AREAS, 0)
    if not dry_run:
        if selected["links"]:
            changed["links"] = cleanup_links(c, selected["links"][:limit], actor)
        for area, table in (("events", "events"), ("audit", "audit")):
            ids = selected[area][:limit]
            if not ids:
                continue
            slots = ",".join("?" for _ in ids)
            if area == "events":
                c.execute(f"DELETE FROM outbox WHERE id IN ({slots})", ids)
            else:
                _retain_audit_id_floor(c)
            changed[area] = c.execute(
                f"DELETE FROM {table} WHERE id IN ({slots})", ids
            ).rowcount
        if any(changed.values()):
            audit(c, actor, "maintenance.cleanup", shop_id or "platform")
    return {
        "ok": True,
        "dry_run": dry_run,
        "shop_id": shop_id,
        "product_id": product_id,
        "eligible": eligible,
        "changed": changed,
        "has_more": has_more,
        "policy": policy,
    }


def _counts(c, shop_id):
    from .link_cleanup import link_state

    counts = dict.fromkeys(("links_active", "links_history", "links_archived"), 0)
    for row in c.execute(
        "SELECT staff.* FROM staff JOIN products ON products.id=staff.product_id "
        "WHERE (? IS NULL OR products.shop_id=?)",
        (shop_id, shop_id),
    ):
        counts["links_" + link_state(c, row)] += 1
    row = c.execute(
        "SELECT count(*) AS total,SUM(outbox.state='pending') AS pending "
        "FROM events LEFT JOIN products ON products.id=events.product_id "
        "LEFT JOIN outbox ON outbox.id=events.id WHERE (? IS NULL OR products.shop_id=?)",
        (shop_id, shop_id),
    ).fetchone()
    counts.update(events_total=row["total"], events_pending=row["pending"] or 0)
    counts["audit_total"] = c.execute(
        "SELECT count(*) FROM audit WHERE (? IS NULL OR shop_id=?)", (shop_id, shop_id)
    ).fetchone()[0]
    return counts


@router.get("/admin/maintenance")
def maintenance_status(
    request: Request, shop_id: str | None = Query(default=None, max_length=100)
):
    s = session(request)
    with db() as c:
        scope = _scope(c, s, shop_id)
        return {
            "ok": True,
            "shop_id": scope,
            "policy": get_policy(c, scope),
            "counts": _counts(c, scope),
        }


@router.put("/admin/maintenance/policy")
def update_policy(body: PolicyInput, request: Request):
    from .account_auth import require_recent
    from .link_access import session_actor

    s = session(request)
    require_recent(s)
    with db() as c:
        scope = _scope(c, s, body.shop_id)
        set_setting(
            c, _policy_key(scope), json.dumps(body.model_dump(exclude={"shop_id"}))
        )
        audit(c, session_actor(s), "maintenance.policy", scope or "platform")
        return {"ok": True, "shop_id": scope, "policy": get_policy(c, scope)}


@router.post("/admin/maintenance/cleanup")
def clean_records(body: CleanupInput, request: Request):
    from .account_auth import require_recent
    from .link_access import session_actor

    s = session(request)
    if not body.dry_run:
        require_recent(s)
    with db() as c:
        scope = _scope(c, s, body.shop_id, body.product_id)
        return cleanup_records(
            c,
            shop_id=scope,
            product_id=body.product_id,
            areas=body.areas,
            dry_run=body.dry_run,
            limit=body.limit,
            actor=session_actor(s),
        )


@router.post("/manage/links/cleanup")
def clean_delegated_links(body: LinkCleanupInput, request: Request):
    from .link_access import session_actor

    s = session(request, ("admin", "staff"))
    with db() as c:
        authorize_management(c, s, "links.delegate")
        product_id = body.product_id or s.get("product_id")
        scope = _scope(c, s, product_id=product_id)
        if s["role"] == "admin" and not body.dry_run:
            from .account_auth import require_recent

            require_recent(s)
        return cleanup_records(
            c,
            shop_id=scope,
            product_id=product_id,
            areas=("links",),
            dry_run=body.dry_run,
            limit=body.limit,
            actor=session_actor(s),
            staff_id=s.get("staff_id") if s["role"] == "staff" else None,
        )


def maintenance_once():
    """One worker tick: at most twenty shops, one hundred rows per category."""
    totals = dict.fromkeys(AREAS, 0)
    with db() as c:
        cursor = setting(c, "maintenance_cursor", "")
        shops = c.execute(
            "SELECT id FROM shops WHERE id>? ORDER BY id LIMIT 21", (cursor,)
        ).fetchall()
        for row in shops[:20]:
            policy = get_policy(c, row["id"])
            if not policy["enabled"]:
                continue
            result = cleanup_records(
                c,
                shop_id=row["id"],
                policy=policy,
                dry_run=False,
                actor="system",
                automatic=True,
            )
            for area, count in result["changed"].items():
                totals[area] += count
        set_setting(c, "maintenance_cursor", shops[19]["id"] if len(shops) > 20 else "")
        policy = get_policy(c)
        if policy["enabled"]:
            result = cleanup_records(
                c,
                areas=("audit",),
                policy=policy,
                dry_run=False,
                actor="system",
                platform_only=True,
            )
            totals["audit"] += result["changed"]["audit"]
    return totals
