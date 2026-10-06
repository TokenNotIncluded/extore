import json

from extore.db import db


def _import(owner, *, once=False):
    response = owner.post(
        "/api/admin/products",
        json={"name": "合成文本商品", "mode": "stock", "view_policy": "once" if once else "repeat"},
    )
    assert response.status_code == 200, response.text
    pid = response.json()["id"]
    response = owner.post(
        "/api/admin/cards/import-text",
        json={"product_id": pid, "text": "\ufeff第一串文本\r\n第二串文本\r\n第一串文本\r\n\r\n"},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["stats"] == {"lines": 4, "blank": 1, "duplicates": 1, "created": 2}
    assert len(result["codes"]) == 2
    return pid, result


def test_text_import_fulfills_matching_line_without_queue_or_public_payload(owner):
    pid, issued = _import(owner)
    count_issue = owner.post("/api/admin/cards", json={"product_id": pid})
    assert count_issue.status_code == 409
    for code, expected in zip(issued["codes"], ("第一串文本", "第二串文本"), strict=True):
        checked = owner.post("/api/exchange", json={"code": code})
        assert checked.status_code == 200, checked.text
        token = checked.json()["token"]
        assert expected not in checked.text
        submitted = owner.post("/api/redeem", json={"token": token, "params": {}})
        assert submitted.status_code == 200, submitted.text
        assert submitted.json()["state"] == "succeeded"
        assert expected not in submitted.text
        for _ in range(2):
            delivery = owner.post("/api/receipt/reveal", json={"token": token})
            assert delivery.status_code == 200, delivery.text
            assert delivery.json()["output"] == {"content": expected}
        with db() as c:
            assert c.execute("SELECT ciphertext FROM text_card_payloads WHERE card_id=(SELECT card_id FROM jobs WHERE id=?)", (submitted.json()["id"],)).fetchone()[0] is None
            assert c.execute("SELECT state FROM cards WHERE id=(SELECT card_id FROM jobs WHERE id=?)", (submitted.json()["id"],)).fetchone()[0] == "used"
            events = [json.loads(row[0]) for row in c.execute("SELECT payload FROM events WHERE job_id=?", (submitted.json()["id"],))]
            assert expected not in json.dumps(events, ensure_ascii=False)
            assert not any(e["type"] == "redemption.requested" for e in events)
        destroyed = owner.post("/api/receipt/destroy", json={"token": token})
        assert destroyed.status_code == 200, destroyed.text
        assert owner.post("/api/receipt/reveal", json={"token": token}).status_code == 409


def test_once_text_reveal_discards_assignment_and_storage_allocation(owner):
    _, issued = _import(owner, once=True)
    exchange = owner.post("/api/exchange", json={"code": issued["codes"][0]}).json()
    token = exchange["token"]
    submitted = owner.post("/api/redeem", json={"token": token}).json()
    first = owner.post("/api/receipt/reveal", json={"token": token})
    assert first.status_code == 200, first.text
    assert first.json()["content"] == "第一串文本"
    assert owner.post("/api/receipt/reveal", json={"token": token}).status_code == 410
    with db() as c:
        row = c.execute("SELECT jobs.content,jobs.result_json,text_card_payloads.ciphertext,text_card_payloads.size FROM jobs JOIN text_card_payloads ON jobs.card_id=text_card_payloads.card_id WHERE jobs.id=?", (submitted["id"],)).fetchone()
        assert tuple(row) == (None, None, None, 0)
