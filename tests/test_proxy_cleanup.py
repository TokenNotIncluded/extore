"""Synthetic cleanup invariants; never contact a real issuer or shop."""

import pytest

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
