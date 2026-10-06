from concurrent.futures import ThreadPoolExecutor

from extore.db import db
from extore.security import card_digest


def redeem(client, code):
    r = client.post("/api/exchange", json={"code": code})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    r = client.post(
        "/api/redeem", json={"token": token, "params": {"email": "user@example.com"}}
    )
    assert r.status_code == 200, r.text
    return token, r.json()


def finish(owner, j, content="secret"):
    r = owner.post(
        "/api/manage/batch",
        json={"product_id": j["product_id"], "ids": [j["id"]], "action": "claim"},
    )
    assert r.status_code == 200, r.text
    r = owner.post(
        "/api/manage/batch",
        json={
            "product_id": j["product_id"],
            "ids": [j["id"]],
            "action": "succeed",
            "content": content,
        },
    )
    assert r.status_code == 200, r.text


def test_private_product_and_hash(owner, setup_product):
    pid, code = setup_product()
    assert owner.get("/api/products").json() == []
    assert owner.get("/api/products/" + pid).status_code == 404
    with db() as c:
        row = c.execute("SELECT * FROM cards").fetchone()
        assert row["digest"] == card_digest(code) and code not in str(dict(row))
    assert owner.post("/api/exchange", json={"code": "bad-code"}).status_code == 404
    assert (
        owner.post(
            "/api/exchange", json={"code": code.lower().replace("-", " ")}
        ).json()["product"]["name"]
        == "测试商品"
    )


def test_duplicate_submission_one_job(owner, setup_product):
    _, code = setup_product()
    t, j = redeem(owner, code)

    def post(_):
        return owner.post(
            "/api/redeem", json={"token": t, "params": {"email": "user@example.com"}}
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(post, range(12)))
    assert all(r.status_code == 200 and r.json()["id"] == j["id"] for r in responses)
    with db() as c:
        assert c.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
        assert (
            c.execute(
                "SELECT count(*) FROM events WHERE type='redemption.requested'"
            ).fetchone()[0]
            == 1
        )


def test_repeat_view_then_destroy(owner, setup_product):
    _, code = setup_product()
    t, j = redeem(owner, code)
    finish(owner, j)
    for _ in range(2):
        assert (
            owner.post("/api/receipt/reveal", json={"token": t}).json()["content"]
            == "secret"
        )
    assert owner.post("/api/exchange", json={"code": code}).status_code == 200
    assert owner.post("/api/receipt/destroy", json={"token": t}).status_code == 200
    assert owner.post("/api/receipt/reveal", json={"token": t}).status_code == 409
    assert owner.post("/api/exchange", json={"code": code}).status_code == 410
    with db() as c:
        r = c.execute("SELECT * FROM jobs").fetchone()
        assert r["content"] is None and r["params"] == "{}"
        assert all(
            "user@example.com" not in r["payload"]
            for r in c.execute("SELECT payload FROM events")
        )


def test_single_view_atomic(owner, setup_product):
    _, code = setup_product(view_policy="once")
    t, j = redeem(owner, code)
    finish(owner, j)
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(
            pool.map(
                lambda _: owner.post("/api/receipt/reveal", json={"token": t}), range(4)
            )
        )
    assert sum(r.status_code == 200 for r in responses) == 1
    assert sum(r.status_code == 410 for r in responses) == 3
    assert owner.post("/api/exchange", json={"code": code}).status_code == 410
    with db() as c:
        assert c.execute("SELECT content FROM jobs").fetchone()[0] is None


def test_retry_attempt_limit(owner, setup_product):
    _, code = setup_product(max_attempts=2)
    t, j = redeem(owner, code)
    for attempt in (1, 2):
        assert j["attempt"] == attempt
        owner.post(
            "/api/manage/batch",
            json={"product_id": j["product_id"], "ids": [j["id"]], "action": "claim"},
        )
        assert (
            owner.post(
                "/api/manage/batch",
                json={
                    "product_id": j["product_id"],
                    "ids": [j["id"]],
                    "action": "fail",
                    "retryable": True,
                },
            ).status_code
            == 200
        )
        result = owner.post(
            "/api/redeem", json={"token": t, "params": {"email": "user@example.com"}}
        )
        if attempt == 1:
            j = result.json()
            assert result.status_code == 200
        else:
            assert result.status_code == 409


def test_failed_uncertain_requires_owner_confirmation(owner, setup_product):
    _, code = setup_product()
    t, j = redeem(owner, code)
    owner.post(
        "/api/manage/batch",
        json={"product_id": j["product_id"], "ids": [j["id"]], "action": "claim"},
    )
    owner.post(
        "/api/manage/batch",
        json={
            "product_id": j["product_id"],
            "ids": [j["id"]],
            "action": "fail",
            "retryable": False,
        },
    )
    assert (
        owner.post(
            "/api/redeem", json={"token": t, "params": {"email": "user@example.com"}}
        ).status_code
        == 409
    )
    assert (
        owner.post(
            "/api/manage/batch",
            json={"product_id": j["product_id"], "ids": [j["id"]], "action": "retry"},
        ).status_code
        == 200
    )
    assert (
        owner.post(
            "/api/redeem", json={"token": t, "params": {"email": "user@example.com"}}
        ).status_code
        == 200
    )


def test_params_validated(owner, setup_product):
    _, code = setup_product()
    t = owner.post("/api/exchange", json={"code": code}).json()["token"]
    for params in (
        {},
        {"email": "bad"},
        {"email": "user@example.com", "extra": "sneak"},
    ):
        assert (
            owner.post("/api/redeem", json={"token": t, "params": params}).status_code
            == 400
        )
    with db() as c:
        assert c.execute("SELECT state FROM cards").fetchone()[0] == "ready"


def test_service_has_no_payload(owner, setup_product):
    _, code = setup_product(delivery="service")
    t, j = redeem(owner, code)
    finish(owner, j)
    assert (
        owner.post("/api/receipt/reveal", json={"token": t}).json()["content"] is None
    )
    with db() as c:
        assert c.execute("SELECT content FROM jobs").fetchone()[0] is None


def test_batch_rollback(owner, setup_product):
    _, code = setup_product()
    _, j = redeem(owner, code)
    assert (
        owner.post(
            "/api/manage/batch",
            json={
                "product_id": j["product_id"],
                "ids": [j["id"], "nonexistent"],
                "action": "claim",
            },
        ).status_code
        == 404
    )
    with db() as c:
        assert c.execute("SELECT state FROM jobs").fetchone()[0] == "queued"
