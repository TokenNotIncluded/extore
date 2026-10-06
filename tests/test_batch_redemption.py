import time

from extore.db import db
from extore.security import digest


def issue(owner, product_id, count=1):
    response = owner.post(
        "/api/admin/cards", json={"product_id": product_id, "count": count}
    )
    assert response.status_code == 200, response.text
    return response.json()["codes"]


def exchange(client, text):
    return client.post("/api/exchange", json={"code": text})


def test_single_code_keeps_the_original_receipt(owner, setup_product):
    _, code = setup_product()
    response = exchange(owner, code)
    assert response.status_code == 200, response.text
    body = response.json()
    assert "batch" not in body
    assert body["job"] is None
    token = body["token"]
    redeemed = owner.post(
        "/api/redeem", json={"token": token, "params": {"email": "one@example.com"}}
    )
    assert redeemed.status_code == 200, redeemed.text
    assert redeemed.json()["state"] == "queued"
    refused = owner.post(
        "/api/redeem",
        json={"token": token, "items": [{"card_id": "x", "params": {}}]},
    )
    assert refused.status_code == 400
    assert refused.json()["detail"] == "单张卡密请直接提交参数"
    with db() as c:
        assert c.execute("SELECT count(*) FROM grants").fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM receipt_batches").fetchone()[0] == 0


def test_several_codes_share_one_receipt_link(owner, setup_product):
    pid, first = setup_product()
    second = issue(owner, pid)[0]
    pasted = f"  {first}, {second.lower()}\n{first}  "
    response = exchange(owner, pasted)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["batch"] is True
    assert body["product"]["name"] == "测试商品"
    assert [item["suffix"] for item in body["items"]] == [
        first.replace("-", "")[-6:],
        second.replace("-", "")[-6:],
    ]
    assert first not in response.text and second.upper() not in response.text
    token = body["token"]
    receipt = owner.post("/api/receipt", json={"token": token}).json()
    assert [item["card_id"] for item in receipt["items"]] == [
        item["card_id"] for item in body["items"]
    ]
    assert all(item["job"] is None for item in receipt["items"])
    missing = owner.post("/api/redeem", json={"token": token})
    assert missing.status_code == 400
    assert missing.json()["detail"] == "请为每张待兑换的卡密填写启动参数"
    partial = owner.post(
        "/api/redeem",
        json={
            "token": token,
            "items": [
                {
                    "card_id": body["items"][0]["card_id"],
                    "params": {"email": "first@example.com"},
                }
            ],
        },
    )
    assert partial.status_code == 200, partial.text
    assert partial.json()["items"][0]["job"]["state"] == "queued"
    assert partial.json()["items"][1]["job"] is None
    rolled = owner.post(
        "/api/redeem",
        json={
            "token": token,
            "items": [
                {
                    "card_id": body["items"][1]["card_id"],
                    "params": {"email": "not-an-email"},
                }
            ],
        },
    )
    assert rolled.status_code == 400
    still = owner.post("/api/receipt", json={"token": token}).json()
    assert still["items"][1]["job"] is None
    done = owner.post(
        "/api/redeem",
        json={
            "token": token,
            "items": [
                {
                    "card_id": body["items"][1]["card_id"],
                    "params": {"email": "second@example.com"},
                }
            ],
        },
    )
    assert done.status_code == 200, done.text
    jobs = done.json()["items"]
    assert all(item["job"]["state"] == "queued" for item in jobs)
    owner.post(
        "/api/manage/batch",
        json={
            "product_id": pid,
            "ids": [item["job"]["id"] for item in jobs],
            "action": "claim",
        },
    )
    owner.post(
        "/api/manage/batch",
        json={
            "product_id": pid,
            "ids": [jobs[0]["job"]["id"]],
            "action": "succeed",
            "content": "alpha",
        },
    )
    owner.post(
        "/api/manage/batch",
        json={
            "product_id": pid,
            "ids": [jobs[1]["job"]["id"]],
            "action": "succeed",
            "content": "beta",
        },
    )
    needs_choice = owner.post("/api/receipt/reveal", json={"token": token})
    assert needs_choice.status_code == 400
    assert needs_choice.json()["detail"] == "请选择一张卡密"
    first_card = jobs[0]["card_id"]
    revealed = owner.post(
        "/api/receipt/reveal", json={"token": token, "card_id": first_card}
    )
    assert revealed.status_code == 200, revealed.text
    assert revealed.json()["content"] == "alpha"
    other = owner.post(
        "/api/receipt/reveal",
        json={"token": token, "card_id": jobs[1]["card_id"]},
    )
    assert other.json()["content"] == "beta"
    destroyed = owner.post(
        "/api/receipt/destroy", json={"token": token, "card_id": first_card}
    )
    assert destroyed.status_code == 200, destroyed.text
    after = owner.post("/api/receipt", json={"token": token}).json()
    states = {item["card_id"]: item["job"]["state"] for item in after["items"]}
    assert states[first_card] == "destroyed"
    assert states[jobs[1]["card_id"]] == "succeeded"
    with db() as c:
        assert (
            c.execute(
                "SELECT count(*) FROM grants WHERE digest=?", (digest(token),)
            ).fetchone()[0]
            == 0
        )
        assert (
            c.execute(
                "SELECT count(*) FROM receipt_batches WHERE digest=?",
                (digest(token),),
            ).fetchone()[0]
            == 1
        )


def test_mixed_products_and_bad_codes_do_not_create_a_link(owner, setup_product):
    pid, code = setup_product()
    _, other = setup_product(name="另一件商品")
    mixed = exchange(owner, f"{code} {other}")
    assert mixed.status_code == 400
    assert mixed.json()["detail"] == "这些卡密不是同一件商品，请分开兑换"
    invalid = exchange(owner, f"{code}\nNOT-A-CODE")
    assert invalid.status_code == 404
    assert invalid.json()["detail"].startswith("第 2 张：")
    too_many = exchange(owner, "\n".join(issue(owner, pid, 31)))
    assert too_many.status_code == 400
    assert too_many.json()["detail"] == "一次最多兑换 30 张卡密"
    with db() as c:
        assert c.execute("SELECT count(*) FROM grants").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM receipt_batches").fetchone()[0] == 0


def test_batch_file_upload_is_scoped_to_one_card(owner):
    product = owner.post(
        "/api/admin/products",
        json={
            "name": "文件处理商品",
            "parameters": [
                {"key": "source", "label": {"zh-CN": "输入文件"}, "type": "file"}
            ],
        },
    ).json()
    codes = issue(owner, product["id"], 2)
    body = exchange(owner, "\n".join(codes)).json()
    token = body["token"]
    bare = owner.post(
        "/api/files/upload",
        data={"token": token, "field_key": "source"},
        files={"file": ("a.txt", b"one", "text/plain")},
    )
    assert bare.status_code == 400
    assert bare.json()["detail"] == "请选择一张卡密"
    uploaded = owner.post(
        "/api/files/upload",
        data={
            "token": token,
            "field_key": "source",
            "card_id": body["items"][0]["card_id"],
        },
        files={"file": ("a.txt", b"one", "text/plain")},
    )
    assert uploaded.status_code == 200, uploaded.text
    redeemed = owner.post(
        "/api/redeem",
        json={
            "token": token,
            "items": [
                {
                    "card_id": item["card_id"],
                    "params": {
                        "source": uploaded.json()["id"]
                        if item["card_id"] == body["items"][0]["card_id"]
                        else ""
                    },
                }
                for item in body["items"]
            ],
        },
    )
    assert redeemed.status_code == 400
    with db() as c:
        assert c.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM events").fetchone()[0] == 0
        assert {row["state"] for row in c.execute("SELECT state FROM cards")} == {
            "ready"
        }
        assert (
            c.execute("SELECT count(*) FROM job_files WHERE bound=1").fetchone()[0] == 0
        )
    second = owner.post(
        "/api/files/upload",
        data={
            "token": token,
            "field_key": "source",
            "card_id": body["items"][1]["card_id"],
        },
        files={"file": ("b.txt", b"two", "text/plain")},
    )
    assert second.status_code == 200, second.text
    redeemed = owner.post(
        "/api/redeem",
        json={
            "token": token,
            "items": [
                {
                    "card_id": body["items"][0]["card_id"],
                    "params": {"source": uploaded.json()["id"]},
                },
                {
                    "card_id": body["items"][1]["card_id"],
                    "params": {"source": second.json()["id"]},
                },
            ],
        },
    )
    assert redeemed.status_code == 200, redeemed.text
    assert all(item["job"] for item in redeemed.json()["items"])


def test_expired_batch_cleanup_keeps_foreign_keys_valid(owner, setup_product):
    pid, first = setup_product()
    second = issue(owner, pid)[0]
    body = exchange(owner, first + "\n" + second).json()
    key = digest(body["token"])
    with db() as c:
        assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        c.execute(
            "UPDATE receipt_batches SET expires=? WHERE digest=?",
            (time.time() - 1, key),
        )
    response = exchange(owner, first)
    assert response.status_code == 200, response.text
    with db() as c:
        assert (
            c.execute("SELECT 1 FROM receipt_batches WHERE digest=?", (key,)).fetchone()
            is None
        )
        assert (
            c.execute(
                "SELECT 1 FROM receipt_batch_cards WHERE digest=?", (key,)
            ).fetchone()
            is None
        )
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []


def test_batch_retains_each_cards_schema_after_product_edits(owner, setup_product):
    pid, first = setup_product()
    second = issue(owner, pid)[0]
    body = exchange(owner, first + "\n" + second).json()
    old, fresh = body["items"]
    response = owner.post(
        "/api/redeem",
        json={
            "token": body["token"],
            "items": [
                {"card_id": old["card_id"], "params": {"email": "old@example.test"}}
            ],
        },
    )
    assert response.status_code == 200, response.text
    config = next(
        row for row in owner.get("/api/admin/products").json() if row["id"] == pid
    )
    config["parameters"] = [
        {"key": "phone", "label": {"zh-CN": "电话"}, "type": "text"}
    ]
    changed = owner.put("/api/admin/products/" + pid, json=config)
    assert changed.status_code == 200, changed.text
    receipt = owner.post("/api/receipt", json={"token": body["token"]}).json()
    assert receipt["product"]["parameters"][0]["key"] == "phone"
    assert receipt["items"][0]["product"]["parameters"][0]["key"] == "email"
    assert receipt["items"][1]["product"]["parameters"][0]["key"] == "phone"
    response = owner.post(
        "/api/redeem",
        json={
            "token": body["token"],
            "items": [{"card_id": fresh["card_id"], "params": {"phone": "123456"}}],
        },
    )
    assert response.status_code == 200, response.text
    assert all(item["job"] for item in response.json()["items"])


def test_revoking_one_card_removes_only_its_batch_authority(owner, setup_product):
    pid, first = setup_product()
    second = issue(owner, pid)[0]
    body = exchange(owner, first + "\n" + second).json()
    removed, remaining = body["items"]
    response = owner.post("/api/admin/cards/" + removed["card_id"] + "/revoke", json={})
    assert response.status_code == 200, response.text
    receipt = owner.post("/api/receipt", json={"token": body["token"]}).json()
    assert [item["card_id"] for item in receipt["items"]] == [remaining["card_id"]]
    response = owner.post(
        "/api/redeem",
        json={
            "token": body["token"],
            "items": [
                {
                    "card_id": removed["card_id"],
                    "params": {"email": "removed@example.test"},
                }
            ],
        },
    )
    assert response.status_code == 404
    with db() as c:
        assert c.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    response = owner.post(
        "/api/redeem",
        json={
            "token": body["token"],
            "items": [
                {
                    "card_id": remaining["card_id"],
                    "params": {"email": "remaining@example.test"},
                }
            ],
        },
    )
    assert response.status_code == 200, response.text


def test_thirty_cards_can_each_upload_their_input_file(owner):
    config = owner.post(
        "/api/admin/products",
        json={
            "name": "批量文件输入",
            "parameters": [
                {"key": "source", "label": {"zh-CN": "文件"}, "type": "file"}
            ],
        },
    ).json()
    body = exchange(owner, "\n".join(issue(owner, config["id"], 30))).json()
    for item in body["items"]:
        response = owner.post(
            "/api/files/upload",
            data={
                "token": body["token"],
                "card_id": item["card_id"],
                "field_key": "source",
            },
            files={"file": ("input.txt", b"small input", "text/plain")},
        )
        assert response.status_code == 200, response.text
