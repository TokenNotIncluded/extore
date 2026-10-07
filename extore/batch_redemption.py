"""Partial customer redemption, without changing the legacy all-or-nothing API."""

import re
import time
from contextlib import contextmanager

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from . import service, shops
from .card_tracking import ensure_card_usable, record_verified
from .db import db
from .security import batch_cards, card_digest, digest, fail, rate_limit, token
from .variants import card_variant

router = APIRouter(prefix="/api/batch", tags=["Batch redemption"])
MAX_CARDS = 30


async def _body(request, allowed):
    # Framework validation errors echo their input. Parse this secret-bearing
    # request explicitly so neither codes nor receipt tokens leave on failure.
    try:
        body = await request.json()
    except (ValueError, UnicodeError):
        fail("请求必须是 JSON 对象")
    if not isinstance(body, dict) or set(body) - allowed:
        fail("请求字段无效")
    return body


def _receipt_token(body):
    value = body.get("token")
    if not isinstance(value, str) or not 1 <= len(value) <= 100:
        fail("请提供有效的领取凭证")
    try:
        value.encode("utf-8")
    except UnicodeError:
        fail("领取凭证格式无效")
    return value


def _suffix(code):
    # Unknown input is still untrusted text; expose at most six ASCII symbols.
    return re.sub(r"[^A-Z0-9]", "", code.upper())[-6:]


@contextmanager
def _isolated(c, index):
    name = f"batch_card_{index}"
    c.execute(f"SAVEPOINT {name}")
    try:
        yield
    except Exception:
        c.execute(f"ROLLBACK TO SAVEPOINT {name}")
        c.execute(f"RELEASE SAVEPOINT {name}")
        raise
    else:
        c.execute(f"RELEASE SAVEPOINT {name}")


def _error(exc):
    if isinstance(exc, HTTPException):
        detail = exc.detail
        return (
            detail[:500] if isinstance(detail, str) else "这张卡密暂时无法处理",
            exc.status_code,
        )
    # Do not echo or log exception text: processor errors may contain customer
    # input, plaintext card codes, credentials, or private delivery content.
    return "这张卡密暂时无法处理，请稍后重试", 500


def _base(index, suffix, status="invalid", *, error=None, http_status=None):
    return {
        "index": index,
        "suffix": _suffix(suffix) if isinstance(suffix, str) else "",
        "status": status,
        "accepted": False,
        "error": error,
        "http_status": http_status,
    }


def _view(c, card, index, suffix="", *, screening=False):
    from .card_entitlements import card_view

    if screening:
        # Local import avoids importing app while its router list is constructed.
        from .app import _screen_exchange_card

        row, p = _screen_exchange_card(c, card)
    else:
        # Existing receipts must retain progress, rejection reasons, and first
        # reveal of a one-time delivery. New exchange rules cannot be applied
        # to a paid order's existing read authority.
        if card is None or card["state"] == "revoked":
            fail("卡密已撤销", 410)
        row = c.execute("SELECT * FROM jobs WHERE card_id=?", (card["id"],)).fetchone()
        if row:
            p = service.job_product(c, row)
        else:
            ensure_card_usable(c, card)
            shops.require_enabled_product(c, card["product_id"])
            p = service.card_product(c, card)
    meta = c.execute(
        "SELECT code_suffix FROM card_meta WHERE card_id=?", (card["id"],)
    ).fetchone()
    rendered = service.job_view(c, row) if row else None
    status = "valid"
    if row:
        status = "needs_retry" if rendered["can_retry"] else "used"
    return {
        **_base(index, meta["code_suffix"] or suffix if meta else suffix, status),
        "accepted": True,
        "card_id": card["id"],
        "product": service.public_product(p),
        "variant": card_variant(c, card),
        "job": rendered,
        **card_view(c, card, row),
    }


def _rejected_view(card, index, suffix, exc):
    message, status = _error(exc)
    # A consumed one-time delivery is recognizable as used, but must not grant
    # another receipt or expose its product, task, parameters or content.
    used = False
    if card is not None and card["state"] == "used":
        used = status == 410 and message == "卡密已使用，无法再次领取"
    return _base(
        index,
        suffix,
        "used" if used else "invalid",
        error=message,
        http_status=status,
    )


def _summary(items):
    return {
        "total": len(items),
        "accepted": sum(item["accepted"] for item in items),
        **{
            state: sum(item["status"] == state for item in items)
            for state in ("valid", "used", "needs_retry", "invalid", "duplicate")
        },
    }


def _load_batch(c, value):
    cards = batch_cards(c, value)
    if not cards:
        # A single legacy grant intentionally stays on /api/receipt.
        fail("批量领取凭证无效或已过期", 404)
    positions = {
        row["card_id"]: row["position"]
        for row in c.execute(
            "SELECT card_id,position FROM receipt_batch_cards WHERE digest=?",
            (digest(value),),
        )
    }
    return cards, positions


def _snapshot(c, cards, positions):
    items = []
    for card in cards:
        index = positions[card["id"]]
        meta = c.execute(
            "SELECT code_suffix FROM card_meta WHERE card_id=?", (card["id"],)
        ).fetchone()
        suffix = meta["code_suffix"] or "" if meta else ""
        try:
            with _isolated(c, index):
                item = _view(c, card, index, suffix)
        except Exception as exc:
            item = _rejected_view(card, index, suffix, exc)
        items.append(item)
    return {
        "batch": True,
        "partial": True,
        "items": items,
        "summary": _summary(items),
    }


def _cleanup_receipts(c, now):
    c.execute("DELETE FROM grants WHERE expires<?", (now,))
    c.execute(
        "DELETE FROM receipt_batch_cards WHERE digest IN "
        "(SELECT digest FROM receipt_batches WHERE expires<?)",
        (now,),
    )
    c.execute("DELETE FROM receipt_batches WHERE expires<?", (now,))


@router.post("/exchange")
async def exchange(request: Request):
    await run_in_threadpool(rate_limit, request, "exchange", 20, 60)
    body = await _body(request, {"code"})
    raw = body.get("code")
    if not isinstance(raw, str) or not 1 <= len(raw) <= 8000:
        fail("请输入卡密，一次最多 8000 字符")
    return await run_in_threadpool(_exchange, raw)


def _exchange(raw):
    with db() as c:
        # Retain the legacy acceptance of spaces within one valid card code.
        try:
            whole_digest = card_digest(raw)
        except UnicodeError:
            whole_digest = None
        single = (
            whole_digest
            and c.execute(
                "SELECT 1 FROM cards WHERE digest=?", (whole_digest,)
            ).fetchone()
        )
        codes = (
            [raw]
            if single
            else [part for part in re.split(r"[\s,，;；]+", raw.strip()) if part]
        )
        if not codes or len(codes) > MAX_CARDS:
            fail("请输入卡密，一次最多兑换 30 张")
        seen = {}
        items = []
        accepted = []
        for index, code in enumerate(codes):
            suffix = _suffix(code)
            try:
                from .proxy_routes import unwrap_local_code

                key = card_digest(unwrap_local_code(c, code))
            except (UnicodeError, HTTPException):
                items.append(
                    _base(index, suffix, error="卡密格式无效", http_status=400)
                )
                continue
            if key in seen:
                items.append(
                    {
                        **_base(index, suffix, "duplicate", error="卡密重复输入"),
                        "duplicate_of": seen[key],
                    }
                )
                continue
            seen[key] = index
            card = c.execute("SELECT * FROM cards WHERE digest=?", (key,)).fetchone()
            try:
                with _isolated(c, index):
                    item = _view(c, card, index, suffix, screening=True)
                    record_verified(c, card["id"])
            except Exception as exc:
                item = _rejected_view(card, index, suffix, exc)
            else:
                accepted.append((card["id"], index))
            items.append(item)
        value = None
        if accepted:
            value = token()
            now = time.time()
            _cleanup_receipts(c, now)
            key = digest(value)
            c.execute(
                "INSERT INTO receipt_batches(digest,expires,created) VALUES (?,?,?)",
                (key, now + 30 * 86400, now),
            )
            c.executemany(
                "INSERT INTO receipt_batch_cards(digest,card_id,position) VALUES (?,?,?)",
                [(key, card_id, position) for card_id, position in accepted],
            )
        return {
            "batch": True,
            "partial": True,
            "token": value,
            "items": items,
            "summary": _summary(items),
        }


@router.post("/receipt")
async def receipt(request: Request):
    body = await _body(request, {"token"})
    value = _receipt_token(body)
    return await run_in_threadpool(_receipt, value)


def _receipt(value):
    with db() as c:
        return _snapshot(c, *_load_batch(c, value))


@router.post("/redeem")
async def redeem(request: Request):
    await run_in_threadpool(rate_limit, request, "redeem", 20, 60)
    body = await _body(request, {"token", "items"})
    value = _receipt_token(body)
    supplied = body.get("items")
    if not isinstance(supplied, list) or not 1 <= len(supplied) <= MAX_CARDS:
        fail("请逐卡提交参数，一次最多提交 30 张")
    return await run_in_threadpool(_redeem, value, supplied)


def _redeem(value, supplied):
    with db() as c:
        cards, positions = _load_batch(c, value)
        by_id = {card["id"]: card for card in cards}
        results = []
        seen = set()
        for index, item in enumerate(supplied):
            result = {"index": index, "status": "error", "error": None}
            try:
                with _isolated(c, index):
                    if not isinstance(item, dict) or set(item) != {"card_id", "params"}:
                        fail("每张卡密必须单独提供 card_id 和 params")
                    card_id = item["card_id"]
                    if not isinstance(card_id, str) or not 1 <= len(card_id) <= 80:
                        fail("卡密选择无效")
                    card = by_id.get(card_id)
                    if card is None:
                        fail("这张卡密不在此领取链接中", 404)
                    result["card_id"] = card_id
                    if card_id in seen:
                        result["status"] = "duplicate"
                        fail("卡密重复提交")
                    seen.add(card_id)
                    params = item["params"]
                    if (
                        not isinstance(params, dict)
                        or len(params) > 100
                        or any(
                            not isinstance(key, str)
                            or not 1 <= len(key) <= 100
                            or not isinstance(value, str)
                            or len(value) > 10000
                            for key, value in params.items()
                        )
                    ):
                        fail("每张卡密的 params 必须是独立的文本参数对象")
                    _view(c, card, positions[card_id], screening=True)
                    previous = c.execute(
                        "SELECT state FROM jobs WHERE card_id=?", (card_id,)
                    ).fetchone()
                    row = service.submit(c, card, params)
                    result["job"] = service.job_view(c, row)
                    result["status"] = (
                        "unchanged"
                        if previous
                        and previous["state"] not in ("failed", "needs_input")
                        else "submitted"
                    )
            except Exception as exc:
                result["error"], result["http_status"] = _error(exc)
                result.pop("job", None)
            else:
                result["http_status"] = 200
            results.append(result)
        response = _snapshot(c, cards, positions)
        response["results"] = results
        response["submission_summary"] = {
            "total": len(results),
            "succeeded": sum(
                item["status"] in ("submitted", "unchanged") for item in results
            ),
            "failed": sum(item["status"] in ("error", "duplicate") for item in results),
        }
        return response
