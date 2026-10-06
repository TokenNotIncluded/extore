import json

import pytest
from fastapi import HTTPException

from extore import text_cards
from extore.db import db
from extore.models import Product
from extore.secret_store import SecretStoreError


@pytest.fixture(autouse=True)
def stock_schema():
    with db() as c:
        text_cards.init_schema(c)


def stock_product(owner, **values):
    response = owner.post(
        "/api/admin/products",
        json={"name": "一张卡一段文本", "mode": "stock", **values},
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_import_normalizes_lines_and_deduplicates_only_current_import():
    items, stats = text_cards.import_lines(
        "\ufeff  first  \r\n\r\nsecond  with  spaces\rfirst\r\n"
    )
    assert items == ["first", "second  with  spaces"]
    assert stats == {"lines": 4, "blank": 1, "duplicates": 1, "created": 2}
    assert text_cards.import_lines(b"\xef\xbb\xbfhello\r\nhello\n")[0] == ["hello"]
    # No Unicode/case/interior-space folding can merge different credentials.
    assert text_cards.import_lines("AbC\nabc\na b\na  b")[0] == [
        "AbC",
        "abc",
        "a b",
        "a  b",
    ]


@pytest.mark.parametrize(
    "value", ["", " \r\n\t", b"\xff", "x\x00y", "x" * 10001, "汉" * 700000]
)
def test_import_rejects_invalid_or_unbounded_input_without_echo(value):
    with pytest.raises(HTTPException) as caught:
        text_cards.import_lines(value)
    assert "x\x00y" not in str(caught.value.detail)


def test_maximum_unique_count_applies_after_dedup():
    assert text_cards.import_lines("same\n" * 2000)[1]["created"] == 1
    with pytest.raises(HTTPException):
        text_cards.import_lines("\n".join(str(index) for index in range(1001)))


@pytest.mark.parametrize(
    "values",
    [
        {"parameters": [{"key": "question", "label": {"en": "Question"}}]},
        {"progress_steps": [{"id": "first", "label": {"en": "First"}}]},
        {"delivery": "service"},
        {"outputs": []},
        {"outputs": [{"key": "content", "label": {"en": "Text"}, "required": False}]},
        {"outputs": [{"key": "file", "label": {"en": "File"}, "type": "file"}]},
    ],
)
def test_stock_product_has_simple_declared_text_contract(values):
    with pytest.raises(ValueError):
        Product(name="Text", mode="stock", **values)
    plain = Product(name="Text", mode="stock", view_policy="once")
    assert plain.parameters == [] and plain.outputs[0].key == "content"


def test_import_assigns_unique_cards_and_freezes_variant_and_view_policy(owner):
    pid = stock_product(
        owner,
        view_policy="once",
        variants=[{"id": "small", "name": "小份", "price": "1.9"}],
    )
    with db() as c:
        result = text_cards.issue_text_cards(
            c, pid, "secret-A\nsecret-A\nsecret-B", variant_id="small", label="原文批次"
        )
        assert len(set(result["codes"])) == 2
        assert result["stats"]["duplicates"] == 1
        rows = c.execute(
            "SELECT * FROM text_card_payloads WHERE product_id=?", (pid,)
        ).fetchall()
        assert len(rows) == 2
        assert all(row["ciphertext"].startswith("v1.") for row in rows)
        assert "secret-" not in json.dumps([dict(row) for row in rows])
        for row in rows:
            assert text_cards.assigned_payload(c, row["card_id"]) in (
                "secret-A",
                "secret-B",
            )
            definition = text_cards.card_definition(c, row["card_id"])
            assert definition["mode"] == "stock" and definition["view_policy"] == "once"
            meta = c.execute(
                "SELECT * FROM card_meta WHERE card_id=?", (row["card_id"],)
            ).fetchone()
            assert meta["variant_id"] == "small"
            assert json.loads(meta["variant_snapshot"])["price"] == "1.9"
        assert result["items"][0]["content"] == "secret-A"
        assert result["batch_id"]
        # Merchant intent can replenish identical stock in another import.
        assert (
            len(
                text_cards.issue_text_cards(c, pid, "secret-A", variant_id="small")[
                    "codes"
                ]
            )
            == 1
        )
        assert not c.execute(
            "SELECT 1 FROM events WHERE payload LIKE '%secret-%'"
        ).fetchone()
        assert (
            "secret-"
            not in c.execute(
                "SELECT config FROM products WHERE id=?", (pid,)
            ).fetchone()[0]
        )


def test_missing_or_cleared_assignments_never_fall_back_to_stock(owner):
    pid = stock_product(owner)
    with db() as c:
        text_cards.issue_text_cards(c, pid, "specific-secret")
        cid = c.execute("SELECT id FROM cards WHERE product_id=?", (pid,)).fetchone()[0]
        assert text_cards.card_definition(c, "legacy-missing") is None
        with pytest.raises(HTTPException) as caught:
            text_cards.assigned_payload(c, "legacy-missing")
        assert caught.value.status_code == 410
        text_cards.clear_assignment(c, cid)
        assert text_cards.card_definition(c, cid)["mode"] == "stock"
        with pytest.raises(HTTPException) as caught:
            text_cards.assigned_payload(c, cid)
        assert caught.value.status_code == 410


def test_payload_ciphertext_is_bound_to_card_and_tenant(owner):
    pid = stock_product(owner)
    with db() as c:
        text_cards.issue_text_cards(c, pid, "tenant-secret\nother-secret")
        rows = c.execute(
            "SELECT * FROM text_card_payloads WHERE product_id=?", (pid,)
        ).fetchall()
        c.execute(
            "UPDATE text_card_payloads SET ciphertext=? WHERE card_id=?",
            (rows[0]["ciphertext"], rows[1]["card_id"]),
        )
        with pytest.raises(SecretStoreError):
            text_cards.assigned_payload(c, rows[1]["card_id"])
        # Changing the current product tenant cannot authorize the old payload.
        c.execute("UPDATE products SET shop_id=NULL WHERE id=?", (pid,))
        with pytest.raises(HTTPException) as caught:
            text_cards.assigned_payload(c, rows[0]["card_id"])
        assert caught.value.status_code == 409


def test_import_rejects_non_stock_and_disabled_variant_before_issuance(owner):
    pid = stock_product(
        owner, variants=[{"id": "default", "name": "默认", "enabled": False}]
    )
    with db() as c:
        with pytest.raises(HTTPException):
            text_cards.issue_text_cards(c, pid, "not-issued")
        assert (
            c.execute(
                "SELECT COUNT(*) FROM cards WHERE product_id=?", (pid,)
            ).fetchone()[0]
            == 0
        )
        config = json.loads(
            c.execute("SELECT config FROM products WHERE id=?", (pid,)).fetchone()[0]
        )
        config["mode"] = "manual"
        c.execute("UPDATE products SET config=? WHERE id=?", (json.dumps(config), pid))
        with pytest.raises(HTTPException):
            text_cards.issue_text_cards(c, pid, "not-issued")


def test_product_with_stock_issued_cards_cannot_change_mode(owner):
    pid = stock_product(owner)
    with db() as c:
        text_cards.issue_text_cards(c, pid, "mode-frozen")
    response = owner.put(
        "/api/admin/products/" + pid, json={"name": "Text", "mode": "manual"}
    )
    assert response.status_code == 409


def test_stock_uses_shared_storage_budget_until_delivery_is_discarded(
    owner, monkeypatch
):
    from extore import storage

    pid = stock_product(owner)
    with db() as c:
        text_cards.issue_text_cards(c, pid, "quota-protected")
        row = c.execute(
            "SELECT * FROM text_card_payloads WHERE product_id=?", (pid,)
        ).fetchone()
        allocation = row["size"]
        assert allocation > 0
        assert storage.storage_usage(c, row["shop_id"])["stored_bytes"] == allocation
        # Completing delivery clears the vault original but the receipt still
        # occupies its reservation until once-reveal or explicit destruction.
        text_cards.clear_assignment(c, row["card_id"])
        assert text_cards.allocated_bytes(c) == allocation
        c.execute(
            "UPDATE shops SET storage_limit_bytes=? WHERE id=?",
            (allocation, row["shop_id"]),
        )
        with pytest.raises(HTTPException) as caught:
            text_cards.issue_text_cards(c, pid, "not-allocated")
        assert caught.value.status_code == 507
        assert (
            c.execute(
                "SELECT COUNT(*) FROM cards WHERE product_id=?", (pid,)
            ).fetchone()[0]
            == 1
        )
        c.execute(
            "UPDATE shops SET storage_limit_bytes=? WHERE id=?",
            (10_000_000, row["shop_id"]),
        )
        monkeypatch.setattr(storage, "UPLOAD_TOTAL_BYTES", allocation)
        with pytest.raises(HTTPException) as caught:
            text_cards.issue_text_cards(c, pid, "global-not-allocated")
        assert caught.value.status_code == 507
        text_cards.discard_assignment(c, row["card_id"])
        assert text_cards.allocated_bytes(c) == 0
        assert text_cards.card_definition(c, row["card_id"])["mode"] == "stock"


def test_stock_checks_disk_headroom_before_creating_any_cards(owner, monkeypatch):
    from extore import storage

    pid = stock_product(owner)
    monkeypatch.setattr(
        storage, "disk_free_bytes", lambda: storage.UPLOAD_DISK_RESERVE_BYTES
    )
    with db() as c:
        with pytest.raises(HTTPException) as caught:
            text_cards.issue_text_cards(c, pid, "must-not-write")
        assert caught.value.status_code == 507
        assert (
            c.execute(
                "SELECT COUNT(*) FROM cards WHERE product_id=?", (pid,)
            ).fetchone()[0]
            == 0
        )


def test_text_allocation_is_scoped_to_the_original_shop(owner):
    from extore import storage

    pid = stock_product(owner)
    with db() as c:
        text_cards.issue_text_cards(c, pid, "shop-a-stock")
        shop_id = c.execute(
            "SELECT shop_id FROM products WHERE id=?", (pid,)
        ).fetchone()[0]
        assert text_cards.allocated_bytes(c, shop_id) > 0
        assert text_cards.allocated_bytes(c, "other-shop") == 0
        assert storage._shop_totals(c, "other-shop") == (0, 0)
