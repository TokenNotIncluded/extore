"""Import one text item per card, without putting unsold content in public data.

The assigning transaction stores an authenticated, tenant/card scoped ciphertext.
The normal receipt owns the delivered copy after fulfillment. Clear this vault
copy in the same transaction as successful delivery so reveal-once and destroy
cannot be bypassed by reading the original assignment again.
"""

import json
import time
import uuid

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from .card_tracking import record_issue
from .db import audit, db
from .models import IssueCards
from .secret_store import open_secret, store_secret
from .security import card_digest, fail, new_card, session

router = APIRouter()
MAX_IMPORT_BYTES = 2 * 1024 * 1024
MAX_ITEMS = 1000
MAX_LINE_CHARS = 10000
DEFINITION_FIELDS = (
    "mode",
    "delivery",
    "view_policy",
    "allow_retry",
    "max_attempts",
    "parameters",
    "outputs",
    "progress_steps",
)


class TextCardsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: str = Field(min_length=1, max_length=100)
    variant_id: str = Field(default="default", pattern=r"^[a-z0-9][a-z0-9_-]{0,39}$")
    text: str = Field(max_length=MAX_IMPORT_BYTES)
    label: str = Field(default="", max_length=100)
    expires: float | None = Field(default=None, allow_inf_nan=False)


def init_schema(c):
    c.execute(
        "CREATE TABLE IF NOT EXISTS text_card_payloads ("
        "card_id TEXT PRIMARY KEY REFERENCES cards(id) ON DELETE CASCADE,"
        "product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,"
        "shop_id TEXT NOT NULL REFERENCES shops(id),"
        "ciphertext TEXT,definition TEXT NOT NULL,created REAL NOT NULL)"
    )


def import_lines(value):
    """Normalize line endings/outer whitespace, preserve meaningful interiors.

    Blank lines do not create cards. Deduplication is deliberately limited to
    this import: a merchant can intentionally replenish identical text later.
    """
    if isinstance(value, bytes):
        if len(value) > MAX_IMPORT_BYTES:
            fail("导入文件最多 2 MiB")
        try:
            value = value.decode("utf-8-sig", errors="strict")
        except UnicodeDecodeError:
            fail("请导入 UTF-8 编码的文本文件")
    if not isinstance(value, str):
        fail("请输入文本或导入 UTF-8 文件")
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError:
        fail("文本包含无效字符")
    if size > MAX_IMPORT_BYTES:
        fail("导入内容最多 2 MiB")
    normalized = value.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    # A terminal newline terminates its preceding item; it is not an extra
    # blank record. Deliberate blank lines before it are still counted.
    if normalized.endswith("\n"):
        lines.pop()
    unique, seen = [], set()
    blank = duplicates = 0
    for raw in lines:
        line = raw.strip()
        if not line:
            blank += 1
            continue
        if len(line) > MAX_LINE_CHARS:
            fail("每行文本最多 10,000 字符")
        if any(ord(char) < 32 and char != "\t" for char in line):
            fail("文本包含不支持的控制字符")
        if line in seen:
            duplicates += 1
            continue
        seen.add(line)
        unique.append(line)
        if len(unique) > MAX_ITEMS:
            fail("每次最多创建 1,000 张卡密，请分批导入")
    if not unique:
        fail("至少填写一行非空文本")
    return unique, {
        "lines": len(lines),
        "blank": blank,
        "duplicates": duplicates,
        "created": len(unique),
    }


def issue_text_cards(c, pid, text, *, variant_id="default", label="", expires=None):
    """Return current issuance only; no endpoint reads older assigned texts."""
    from .service import product
    from .shops import require_enabled_product

    source = require_enabled_product(c, pid)
    p = product(c, pid)
    if p["mode"] != "stock":
        fail("请先将商品处理方式设为一卡一文本", 409)
    if p.get("task_flow"):
        fail("一卡一文本商品不能同时配置任务编排", 409)
    variant = next((v for v in p["variants"] if v["id"] == variant_id), None)
    if variant is None:
        fail("商品规格不存在")
    if not variant["enabled"]:
        fail("商品规格已停用，不能发行新卡密", 409)
    lines, stats = import_lines(text)
    # Validate labels/expiry before writing any card, including helper callers
    # that do not use the HTTP request model.
    IssueCards(
        product_id=pid,
        count=len(lines),
        variant_id=variant_id,
        label=label,
        expires=expires,
    )
    if expires is not None and expires <= time.time():
        fail("卡密到期时间必须是未来时间")
    frozen = json.dumps({key: p[key] for key in DEFINITION_FIELDS}, ensure_ascii=False)
    items = []
    for line in lines:
        cid = str(uuid.uuid4())
        code = new_card()
        ciphertext = store_secret(
            line,
            tenant_id=source["shop_id"],
            resource_type="text-card",
            resource_id=cid,
        )
        now = time.time()
        c.execute(
            "INSERT INTO cards(id,digest,product_id,created) VALUES (?,?,?,?)",
            (cid, card_digest(code), pid, now),
        )
        c.execute(
            "INSERT INTO text_card_payloads VALUES (?,?,?,?,?,?)",
            (cid, pid, source["shop_id"], ciphertext, frozen, now),
        )
        items.append({"code": code, "content": line})
    codes = [item["code"] for item in items]
    record_issue(
        c,
        pid,
        codes,
        label=label,
        expires=expires,
        variant_id=variant_id,
        variant_snapshot=variant,
    )
    batch = c.execute(
        "SELECT batch_id FROM card_meta WHERE card_id=(SELECT id FROM cards WHERE digest=?)",
        (card_digest(codes[0]),),
    ).fetchone()
    return {
        "codes": codes,
        "items": items,
        "batch_id": batch["batch_id"],
        "stats": stats,
    }


def _assignment(c, card_id):
    row = c.execute(
        "SELECT payload.*,products.shop_id AS current_shop,cards.product_id AS current_product "
        "FROM text_card_payloads payload JOIN cards ON cards.id=payload.card_id "
        "JOIN products ON products.id=cards.product_id WHERE payload.card_id=?",
        (card_id,),
    ).fetchone()
    if row and (
        row["shop_id"] != row["current_shop"]
        or row["product_id"] != row["current_product"]
    ):
        fail("卡密的交付范围已失效", 409)
    return row


def card_definition(c, card_id):
    """Safe fulfillment snapshot. Missing row means legacy, never implicit stock."""
    row = _assignment(c, card_id)
    return json.loads(row["definition"]) if row is not None else None


def assigned_payload(c, card_id):
    """Internal fulfillment only; HTTP callers must first authorize the card."""
    row = _assignment(c, card_id)
    if row is None or row["ciphertext"] is None:
        fail("此卡密没有可交付文本", 410)
    value = open_secret(
        row["ciphertext"],
        tenant_id=row["shop_id"],
        resource_type="text-card",
        resource_id=card_id,
    )
    if not isinstance(value, str) or not value.strip():
        fail("此卡密的交付内容不可用", 409)
    return value


def clear_assignment(c, card_id):
    """Keep the non-secret snapshot for receipts; permanently clear vault copy."""
    c.execute(
        "UPDATE text_card_payloads SET ciphertext=NULL WHERE card_id=?", (card_id,)
    )


@router.post("/api/admin/cards/import-text")
@router.post("/api/manage/cards/import-text")
def import_text_cards(body: TextCardsInput, request: Request):
    from .app import management_actor, management_scope

    s = session(request, ("admin", "staff"))
    with db() as c:
        pid = management_scope(c, s, body.product_id, "cards.manage")
        result = issue_text_cards(
            c,
            pid,
            body.text,
            variant_id=body.variant_id,
            label=body.label,
            expires=body.expires,
        )
        audit(
            c, management_actor(s), "cards.import_text", f"{pid}:{len(result['codes'])}"
        )
        return result
