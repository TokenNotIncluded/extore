"""Product specifications and immutable, server-owned card snapshots."""

import json
import math
import re
import sqlite3
from copy import deepcopy


def default_variant():
    return {
        "id": "default",
        "name": "默认规格",
        "description": "",
        "price": None,
        "currency": "CNY",
        "attributes": {},
        "enabled": True,
    }


def normalize_price(value):
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) > 100
        or not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,6})?", value)
    ):
        raise ValueError("规格参考价格必须是非负十进制文本，最多六位小数")
    integer, _, fraction = value.partition(".")
    integer = integer.lstrip("0") or "0"
    if len(integer) > 12:
        raise ValueError("规格参考价格最多十二位整数")
    fraction = fraction.rstrip("0")
    return integer + ("." + fraction if fraction else "")


def validate_attributes(value):
    if not isinstance(value, dict) or len(value) > 20:
        raise ValueError("规格属性最多二十项")
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip() or len(key) > 100:
            raise ValueError("规格属性名必须是非空文本，最多一百字符")
        if item is None or isinstance(item, bool):
            continue
        if isinstance(item, str):
            if len(item) <= 1000:
                continue
        elif isinstance(item, (int, float)):
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError("规格属性数字必须有限")
            if abs(item) > 9007199254740991 and (
                isinstance(item, int) or item.is_integer()
            ):
                raise ValueError("规格属性整数超出安全范围，请用文本表示大整数")
            if len(str(item)) <= 1000:
                continue
        raise ValueError("规格属性必须是简单文本、数字、布尔值或空值，最多一千字符")
    return deepcopy(value)


def resolve_product_variants(config):
    """Stored pre-variant products get a default without mutating the database."""
    variants = config.get("variants")
    if variants is None:
        return [default_variant()]
    return deepcopy(variants)


def card_variant(c, card_or_id):
    """Read the issued snapshot; legacy cards never inherit later SKU attributes."""
    if isinstance(card_or_id, str):
        card_id = card_or_id
    else:
        keys = card_or_id.keys()
        card_id = card_or_id["card_id"] if "card_id" in keys else card_or_id["id"]
    try:
        row = c.execute(
            "SELECT variant_snapshot FROM card_meta WHERE card_id=?", (card_id,)
        ).fetchone()
    except sqlite3.OperationalError as exc:
        # Safe for pre-migration databases/tools that contain only legacy cards.
        if "no such column" not in str(exc) and "no such table" not in str(exc):
            raise
        row = None
    if row is None:
        return default_variant()
    snapshot = json.loads(row["variant_snapshot"] or "{}")
    if not snapshot:
        return default_variant()
    # Only the SKU contract can leave the server, even for imported metadata.
    return {
        key: deepcopy(snapshot.get(key, value))
        for key, value in default_variant().items()
    }


def issued_variant_ids(c, product_id):
    """All issuance history locks SKU IDs, including revoked and legacy cards."""
    try:
        rows = c.execute(
            "SELECT DISTINCT COALESCE(NULLIF(m.variant_id,''),'default') AS variant_id "
            "FROM cards c LEFT JOIN card_meta m ON m.card_id=c.id WHERE c.product_id=?",
            (product_id,),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such column" not in str(exc) and "no such table" not in str(exc):
            raise
        return (
            {"default"}
            if c.execute(
                "SELECT 1 FROM cards WHERE product_id=? LIMIT 1", (product_id,)
            ).fetchone()
            else set()
        )
    return {row["variant_id"] for row in rows}
