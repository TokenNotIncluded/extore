from fastapi.testclient import TestClient

from extore.app import app


def test_compact_catalog_avoids_tutorial_payloads_and_retains_product_scope(owner):
    created = owner.post(
        "/api/admin/products",
        json={
            "name": "Compact catalogue",
            "parameters": [
                {
                    "key": "request",
                    "label": {"en": "Request"},
                    "description": {"en": "A detailed customer tutorial. " * 300},
                }
            ],
        },
    )
    assert created.status_code == 200
    pid = created.json()["id"]
    other = owner.post("/api/admin/products", json={"name": "Other product"})
    assert other.status_code == 200
    full = owner.get("/api/admin/products")
    compact = owner.get("/api/admin/products", params={"compact": True})
    assert compact.status_code == full.status_code == 200
    summary = next(p for p in compact.json() if p["id"] == pid)
    assert summary["parameters_count"] == 1
    assert "parameters" not in summary and "description" not in summary
    assert len(compact.content) < len(full.content) // 10

    link = owner.post(
        "/api/admin/staff", json={"product_id": pid, "name": "Scoped CLI agent"}
    ).json()
    with TestClient(
        app,
        base_url="http://localhost:8000",
        headers={"Origin": "http://localhost:8000"},
    ) as staff:
        assert (
            staff.post(
                "/api/staff/login", json={"token": link["url"].split("#")[1]}
            ).status_code
            == 200
        )
        catalogue = staff.get("/api/manage/products", params={"compact": True})
        assert catalogue.status_code == 200
        assert [p["id"] for p in catalogue.json()] == [pid]
        assert catalogue.json()[0] == {
            key: value for key, value in summary.items() if key != "shop_id"
        }
        detailed = staff.get("/api/manage/products").json()
        assert detailed[0]["parameters"][0]["description"]["en"].startswith(
            "A detailed customer tutorial."
        )
