"""Synthetic cleanup invariants; never contact a real issuer or shop."""

import pytest
from test_card_batches import change as change_batch
from test_redemption import finish

from extore import proxy_routes as routing
from extore.db import db


@pytest.fixture
def issuer(owner, monkeypatch):
    monkeypatch.setattr(routing, "ORIGIN", "https://issuer.example.com")
    response = owner.post("/api/admin/products", json={"name": "Synthetic cleanup"})
    assert response.status_code == 200
    with db() as c:
        sid = c.execute(
            "SELECT shop_id FROM products WHERE id=?", (response.json()["id"],)
        ).fetchone()[0]
        routing.ensure_shop_issuer(c, sid)
    return owner, sid, response.json()["id"]


def test_mandatory_main_replacement_keeps_old_card_valid(issuer):
    owner, sid, pid = issuer
    first = owner.get("/api/admin/proxy/identities", params={"shop_id": sid}).json()
    assert len(first) == 1 and first[0]["current"]
    code = owner.post("/api/admin/cards", json={"product_id": pid, "count": 1}).json()[
        "codes"
    ][0]
    old_route = routing.parse_routed_code(code)["route_id"]
    response = owner.post(
        "/api/admin/proxy/identities", json={"name": "main", "shop_id": sid}
    )
    assert response.status_code == 200 and response.json()["name"] == "main"
    assert response.json()["id"] != first[0]["id"]
    assert (
        len(owner.get("/api/admin/proxy/identities", params={"shop_id": sid}).json())
        == 1
    )
    assert (
        len(
            owner.get(
                "/api/admin/proxy/identities",
                params={"shop_id": sid, "history": "true"},
            ).json()
        )
        == 2
    )
    with db() as c:
        assert (
            routing.unwrap_local_code(c, code)
            == routing.parse_routed_code(code)["secret"]
        )
    preview = owner.get(
        f"/api/admin/proxy/identities/{first[0]['id']}/cleanup-preview",
        params={"shop_id": sid},
    ).json()
    assert preview["issued_card_count"] == 1 and preview["eligible"] is False
    assert (
        owner.delete(
            f"/api/admin/proxy/identities/{first[0]['id']}", params={"shop_id": sid}
        ).status_code
        == 409
    )
    with db() as c:
        assert (
            c.execute(
                "SELECT enabled FROM proxy_routes WHERE route_id=?", (old_route,)
            ).fetchone()[0]
            == 1
        )


def test_unreferenced_history_is_really_deleted_and_current_cannot_disable(issuer):
    owner, sid, _ = issuer
    current = owner.get("/api/admin/proxy/identities", params={"shop_id": sid}).json()[
        0
    ]
    owner.post("/api/admin/proxy/identities", json={"name": "main", "shop_id": sid})
    cleanup = owner.post("/api/admin/proxy/cleanup", json={"shop_id": sid})
    assert (
        cleanup.status_code == 200
        and current["id"] in cleanup.json()["deleted_identity_ids"]
    )
    routes = owner.get("/api/admin/proxy/routes", params={"shop_id": sid}).json()
    assert len(routes) == 1 and routes[0]["default_issuer"]
    rid = routes[0]["route_id"]
    assert (
        owner.put(
            f"/api/admin/proxy/routes/{rid}",
            params={"shop_id": sid},
            json={"enabled": False},
        ).status_code
        == 409
    )
    assert (
        owner.put(
            f"/api/admin/proxy/routes/{rid}",
            params={"shop_id": sid},
            json={"default_issuer": False},
        ).status_code
        == 409
    )
    with db() as c:
        assert (
            c.execute(
                "SELECT count(*) FROM proxy_identities WHERE id=?", (current["id"],)
            ).fetchone()[0]
            == 0
        )


def test_revoked_issued_code_does_not_keep_old_identity_forever(issuer):
    owner, sid, pid = issuer
    current = owner.get("/api/admin/proxy/identities", params={"shop_id": sid}).json()[
        0
    ]
    code = owner.post("/api/admin/cards", json={"product_id": pid, "count": 1}).json()[
        "codes"
    ][0]
    owner.post("/api/admin/proxy/identities", json={"name": "main", "shop_id": sid})
    with db() as c:
        c.execute("UPDATE cards SET state='revoked' WHERE product_id=?", (pid,))
    preview = owner.get(
        f"/api/admin/proxy/identities/{current['id']}/cleanup-preview",
        params={"shop_id": sid},
    ).json()
    assert preview["eligible"] is True and preview["issued_card_count"] == 0
    response = owner.delete(
        f"/api/admin/proxy/identities/{current['id']}", params={"shop_id": sid}
    )
    assert response.status_code == 200
    with db() as c:
        assert c.execute("SELECT count(*) FROM proxy_issued_cards").fetchone()[0] == 0
        assert (
            c.execute(
                "SELECT count(*) FROM cards WHERE product_id=?", (pid,)
            ).fetchone()[0]
            == 1
        )
    assert code.startswith("EXR1.")


def test_removed_proxy_page_redirects_without_query_code(client):
    response = client.get("/proxy?code=synthetic-never-forward", follow_redirects=False)
    assert response.status_code == 308
    assert response.headers["location"] == "/"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_recoverable_batch_keeps_old_signing_key_until_permanent_purge(issuer):
    owner, sid, pid = issuer
    original = owner.get("/api/admin/proxy/identities", params={"shop_id": sid}).json()[
        0
    ]
    issued = owner.post("/api/admin/cards", json={"product_id": pid, "count": 1}).json()
    bid, code = issued["batch_id"], issued["codes"][0]
    replacement = owner.post(
        "/api/admin/proxy/identities", json={"name": "main", "shop_id": sid}
    )
    assert replacement.status_code == 200

    def batch_action(action):
        plan = owner.post(
            f"/api/admin/card-batches/{bid}/{action}-preview",
            params={"product_id": pid},
        )
        assert plan.status_code == 200, plan.text
        return owner.request(
            "DELETE" if action == "delete" else "POST",
            f"/api/admin/card-batches/{bid}" + ("" if action == "delete" else "/purge"),
            params={"product_id": pid},
            json={"confirmed": True, "revision": plan.json()["revision"]},
        )

    assert batch_action("delete").status_code == 200
    preview = owner.get(
        f"/api/admin/proxy/identities/{original['id']}/cleanup-preview",
        params={"shop_id": sid},
    ).json()
    assert preview["issued_card_count"] == 1 and preview["eligible"] is False
    assert (
        owner.delete(
            f"/api/admin/proxy/identities/{original['id']}", params={"shop_id": sid}
        ).status_code
        == 409
    )
    restored = owner.post(
        f"/api/admin/card-batches/{bid}/restore", params={"product_id": pid}
    )
    assert restored.status_code == 200
    assert owner.post("/api/exchange", json={"code": code}).status_code == 200
    assert batch_action("delete").status_code == 200
    assert batch_action("purge").status_code == 200
    preview = owner.get(
        f"/api/admin/proxy/identities/{original['id']}/cleanup-preview",
        params={"shop_id": sid},
    ).json()
    assert preview["issued_card_count"] == 0 and preview["eligible"] is True
    assert (
        owner.delete(
            f"/api/admin/proxy/identities/{original['id']}", params={"shop_id": sid}
        ).status_code
        == 200
    )


def test_purged_completed_batch_keeps_repeat_delivery_key_until_destroyed(issuer):
    owner, sid, pid = issuer
    original = owner.get("/api/admin/proxy/identities", params={"shop_id": sid}).json()[
        0
    ]
    issued = owner.post("/api/admin/cards", json={"product_id": pid, "count": 1}).json()
    bid, code = issued["batch_id"], issued["codes"][0]
    exchange = owner.post("/api/exchange", json={"code": code})
    assert exchange.status_code == 200
    receipt = exchange.json()["token"]
    redemption = owner.post("/api/redeem", json={"token": receipt, "params": {}})
    assert redemption.status_code == 200, redemption.text
    finish(owner, redemption.json(), "DELIVERY MUST SURVIVE STOCK CLEANUP")
    assert (
        owner.post(
            "/api/admin/proxy/identities", json={"name": "main", "shop_id": sid}
        ).status_code
        == 200
    )
    assert change_batch(owner, pid, bid).status_code == 200
    assert change_batch(owner, pid, bid, "purge").status_code == 200
    preview_url = f"/api/admin/proxy/identities/{original['id']}/cleanup-preview"
    cleanup_url = f"/api/admin/proxy/identities/{original['id']}"
    preview = owner.get(preview_url, params={"shop_id": sid}).json()
    assert preview["issued_card_count"] == 1 and preview["eligible"] is False
    assert owner.delete(cleanup_url, params={"shop_id": sid}).status_code == 409
    assert owner.post("/api/exchange", json={"code": code}).status_code == 200
    revealed = owner.post("/api/receipt/reveal", json={"token": receipt})
    assert revealed.status_code == 200
    assert revealed.json()["content"] == "DELIVERY MUST SURVIVE STOCK CLEANUP"
    assert (
        owner.post("/api/receipt/destroy", json={"token": receipt}).status_code == 200
    )
    preview = owner.get(preview_url, params={"shop_id": sid}).json()
    assert preview["issued_card_count"] == 0 and preview["eligible"] is True
    assert owner.delete(cleanup_url, params={"shop_id": sid}).status_code == 200
