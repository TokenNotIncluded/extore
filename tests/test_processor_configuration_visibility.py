import asyncio
import json

from fastapi.testclient import TestClient
from test_processor_profiles import binding, create_product, issue, merchant, submit

from extore import processor_profiles, processors
from extore.app import app
from extore.db import db
from extore.service import public_product
from extore.worker import job_once


def test_only_explicit_false_configuration_fields_are_owner_readable(monkeypatch):
    fields = [
        {"key": "template", "secret": False},
        {"key": "password", "secret": True},
        {"key": "missing"},
        {"key": "null", "secret": None},
        {"key": "zero", "secret": 0},
        {"key": "string", "secret": "false"},
        {"key": "duplicate", "secret": False},
        {"key": "duplicate", "secret": False},
        {"key": "object", "secret": False},
    ]
    monkeypatch.setattr(
        processors, "specification", lambda _: {"configuration": fields}
    )
    values = {field["key"]: "stored-value" for field in fields}
    values.update(unknown="unclassified-secret", object={"nested": "secret"})
    assert processors.editable_configuration("future_processor", values) == {
        "template": "stored-value"
    }
    assert (
        processors.public_configuration(
            {"processor_id": "future_processor", "processor_config": values}
        )
        == {}
    )
    assert processors.editable_configuration("future_processor", []) == {}


def test_owner_editor_returns_text_but_keeps_secret_url_encrypted(owner):
    client, sid = merchant()
    secret = "https://example.com/secret-download-721492"
    message = "本店交付说明 721492"
    product = create_product(
        client,
        processor_id="resource_link",
        configuration={"resource_url": secret, "message": message},
    )
    assert product["processor_config"] == {"message": message}
    assert set(product["configured_fields"]) == {"resource_url", "message"}
    profile = binding(client, product["id"])
    responses = [
        client.get("/api/admin/processor-profiles"),
        client.get(f"/api/admin/processor-profiles/{profile['id']}"),
        client.get("/api/admin/products"),
        client.get("/api/manage/product", params={"product_id": product["id"]}),
        owner.get(f"/api/admin/processor-profiles/{profile['id']}"),
        owner.get("/api/admin/processor-profiles", params={"shop_id": sid}),
    ]
    for response in responses:
        assert response.status_code == 200, response.text
        assert message in response.text
        assert secret not in response.text and "ciphertext" not in response.text
    assert profile["configuration"] == {"message": message}
    assert set(profile["configured_fields"]) == {"resource_url", "message"}
    with db() as c:
        raw = c.execute(
            "SELECT config FROM products WHERE id=?", (product["id"],)
        ).fetchone()[0]
        ciphertext = c.execute(
            "SELECT ciphertext FROM processor_profile_revisions WHERE profile_id=?",
            (profile["id"],),
        ).fetchone()[0]
        audit = json.dumps([dict(row) for row in c.execute("SELECT * FROM audit")])
    assert json.loads(raw)["processor_config"] == {}
    assert ciphertext.startswith("v1.")
    assert all(value not in raw + ciphertext + audit for value in (secret, message))


def test_text_readback_is_isolated_from_other_shops_staff_and_public(owner):
    client, _ = merchant()
    outsider, _ = merchant()
    marker = "OWNER-ONLY-TEMPLATE-835213 $name"
    product = create_product(client, marker)
    profile = binding(client, product["id"])
    assert (
        outsider.get(f"/api/admin/processor-profiles/{profile['id']}").status_code
        == 404
    )
    assert (
        outsider.get(
            f"/api/admin/processor-profiles/bindings/{product['id']}"
        ).status_code
        == 403
    )
    assert outsider.get("/api/admin/processor-profiles").json() == []
    anonymous = TestClient(app, base_url="http://localhost:8000")
    assert (
        anonymous.get(f"/api/admin/processor-profiles/{profile['id']}").status_code
        == 401
    )
    response = client.post(
        "/api/admin/staff",
        json={
            "product_id": product["id"],
            "name": "商品管理",
            "permissions": ["product.edit", "fulfillment.configure"],
        },
    )
    assert response.status_code == 200, response.text
    staff = TestClient(
        app,
        base_url="http://localhost:8000",
        headers={"Origin": "http://localhost:8000"},
    )
    assert (
        staff.post(
            "/api/staff/login", json={"token": response.json()["url"].split("#")[1]}
        ).status_code
        == 200
    )
    managed = staff.get("/api/manage/product")
    assert managed.status_code == 200, managed.text
    assert managed.json()["processor_config"] == {}
    assert marker not in managed.text and "configured_fields" not in managed.json()
    assert (
        staff.get(f"/api/admin/processor-profiles/{profile['id']}").status_code == 401
    )
    updated = client.put(
        f"/api/admin/products/{product['id']}", json={**product, "public": True}
    )
    assert updated.status_code == 200, updated.text
    code = issue(client, product["id"])
    exchanged = client.post("/api/exchange", json={"code": code})
    for response in (
        anonymous.get("/api/products"),
        exchanged,
    ):
        assert response.status_code == 200, response.text
        assert marker not in response.text
        assert "processor_config" not in response.text
        assert "configured_fields" not in response.text
    shared = public_product(updated.json())
    assert "processor_config" not in shared and "configured_fields" not in shared
    assert marker not in json.dumps(shared)


def test_revoked_owner_session_is_rechecked_before_configuration_decryption(
    owner, monkeypatch
):
    product = create_product(owner, "do not decrypt revoked account $name")
    profile = binding(owner, product["id"])
    original_session = processor_profiles.session
    opened = []
    original_open = processor_profiles._open_revision

    def revoke_after_session(request):
        current = original_session(request)
        with db() as c:
            c.execute(
                "UPDATE sessions SET revoked=1 WHERE digest=?", (current["digest"],)
            )
        return current

    def capture_open(*args, **kwargs):
        opened.append(True)
        return original_open(*args, **kwargs)

    monkeypatch.setattr(processor_profiles, "session", revoke_after_session)
    monkeypatch.setattr(processor_profiles, "_open_revision", capture_open)
    response = owner.get(f"/api/admin/processor-profiles/{profile['id']}")
    assert response.status_code == 401, response.text
    assert opened == []


def test_root_catalog_keeps_disabled_shop_metadata_without_decrypting(
    owner, monkeypatch
):
    client, sid = merchant()
    product = create_product(client, "disabled-shop-template $name")
    with db() as c:
        c.execute("UPDATE shops SET enabled=0 WHERE id=?", (sid,))

    def forbidden(*args, **kwargs):
        raise AssertionError("disabled shop configuration must not be decrypted")

    monkeypatch.setattr(processor_profiles, "_open_revision", forbidden)
    response = owner.get("/api/admin/products")
    assert response.status_code == 200, response.text
    assert response.json()[0]["id"] == product["id"]
    assert response.json()[0]["processor_config"] == {}
    assert "disabled-shop-template" not in response.text
    assert client.get("/api/admin/products").status_code == 401


def test_account_latest_and_product_bound_revision_remain_distinct(owner):
    product = create_product(owner, "OLD $name")
    old_code = issue(owner, product["id"])
    profile = binding(owner, product["id"])
    changed = owner.put(
        f"/api/admin/processor-profiles/{profile['id']}",
        json={"configuration": {"template": "NEW $name"}},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["configuration"] == {"template": "NEW $name"}
    bound = binding(owner, product["id"])
    assert bound["revision"] == 2 and bound["bound_revision"] == 1
    assert bound["configuration"] == {"template": "OLD $name"}
    assert owner.get("/api/admin/products").json()[0]["processor_config"] == {
        "template": "OLD $name"
    }
    rebound = owner.put(
        f"/api/admin/processor-profiles/bindings/{product['id']}",
        json={"profile_id": profile["id"]},
    )
    assert rebound.status_code == 200, rebound.text
    assert rebound.json()["profile"]["configuration"] == {"template": "NEW $name"}
    new_code = issue(owner, product["id"])
    old_receipt, _ = submit(owner, old_code)
    new_receipt, _ = submit(owner, new_code)
    assert asyncio.run(job_once()) and asyncio.run(job_once())
    for token, content in ((old_receipt, "OLD Ada"), (new_receipt, "NEW Ada")):
        revealed = owner.post("/api/receipt/reveal", json={"token": token})
        assert revealed.status_code == 200, revealed.text
        assert revealed.json()["output"]["content"] == content


def test_product_text_patch_preserves_hidden_resource_and_frozen_cards(owner):
    secret = "https://example.com/immutable-secret-285483"
    product = create_product(
        owner,
        processor_id="resource_link",
        configuration={"resource_url": secret, "message": "original instructions"},
    )
    original = binding(owner, product["id"])
    code = issue(owner, product["id"])
    same = owner.put(
        f"/api/admin/products/{product['id']}", json={**product, "name": "改标题"}
    )
    assert same.status_code == 200, same.text
    assert binding(owner, product["id"])["id"] == original["id"]
    changed = owner.put(
        f"/api/admin/products/{product['id']}",
        json={
            **same.json(),
            "processor_config": {"resource_url": "", "message": "updated instructions"},
        },
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["processor_config"] == {"message": "updated instructions"}
    assert secret not in changed.text
    current = binding(owner, product["id"])
    assert current["id"] != original["id"]
    new_code = issue(owner, product["id"])
    old_receipt, _ = submit(owner, code, {})
    new_receipt, _ = submit(owner, new_code, {})
    assert asyncio.run(job_once()) and asyncio.run(job_once())
    for token, message in (
        (old_receipt, "original instructions"),
        (new_receipt, "updated instructions"),
    ):
        response = owner.post("/api/receipt/reveal", json={"token": token})
        assert response.json()["output"] == {"resource_url": secret, "message": message}


def test_optional_text_clears_secret_blank_keeps_and_required_text_rejects(owner):
    product = create_product(
        owner,
        processor_id="resource_link",
        configuration={
            "resource_url": "https://example.com/keep-this-secret",
            "message": "clear these instructions",
        },
    )
    profile = binding(owner, product["id"])
    response = owner.put(
        f"/api/admin/processor-profiles/{profile['id']}",
        json={"configuration": {"resource_url": "", "message": ""}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["configuration"] == {"message": ""}
    assert response.json()["configured_fields"] == ["resource_url"]
    rebound = owner.put(
        f"/api/admin/processor-profiles/bindings/{product['id']}",
        json={"profile_id": profile["id"]},
    )
    assert rebound.status_code == 200, rebound.text
    receipt, _ = submit(owner, issue(owner, product["id"]), {})
    assert asyncio.run(job_once())
    assert owner.post("/api/receipt/reveal", json={"token": receipt}).json()[
        "output"
    ] == {"resource_url": "https://example.com/keep-this-secret", "message": ""}
    template = create_product(owner, "keep existing $name")
    template_profile = binding(owner, template["id"])
    response = owner.put(
        f"/api/admin/processor-profiles/{template_profile['id']}",
        json={"configuration": {"template": ""}},
    )
    assert response.status_code == 422, response.text
    unchanged = owner.get(
        f"/api/admin/processor-profiles/{template_profile['id']}"
    ).json()
    assert unchanged["revision"] == 1
    assert unchanged["configuration"] == {"template": "keep existing $name"}
    response = owner.put(
        f"/api/admin/processor-profiles/{template_profile['id']}",
        json={"configuration": {"unknown": "must not be stored"}},
    )
    assert response.status_code == 422, response.text


def test_product_secret_patch_preserves_omitted_text_but_explicit_blank_clears(owner):
    product = create_product(
        owner,
        processor_id="resource_link",
        configuration={
            "resource_url": "https://example.com/original-resource",
            "message": "preserve omitted instructions",
        },
    )
    response = owner.put(
        f"/api/admin/products/{product['id']}",
        json={
            **product,
            "processor_config": {"resource_url": "https://example.com/new-resource"},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["processor_config"] == {
        "message": "preserve omitted instructions"
    }
    response = owner.put(
        f"/api/admin/products/{product['id']}",
        json={**response.json(), "processor_config": {"message": ""}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["processor_config"] == {"message": ""}
    assert response.json()["configured_fields"] == ["resource_url"]
    receipt, _ = submit(owner, issue(owner, product["id"]), {})
    assert asyncio.run(job_once())
    assert owner.post("/api/receipt/reveal", json={"token": receipt}).json()[
        "output"
    ] == {"resource_url": "https://example.com/new-resource", "message": ""}


def test_compact_catalog_never_decrypts_and_revoked_profile_remains_listable(
    owner, monkeypatch
):
    product = create_product(owner, "owner-only template $name")
    profile = binding(owner, product["id"])
    original = processor_profiles._open_revision

    def forbidden(*args, **kwargs):
        raise AssertionError("compact catalog must not decrypt shop configuration")

    monkeypatch.setattr(processor_profiles, "_open_revision", forbidden)
    compact = owner.get("/api/admin/products", params={"compact": True})
    assert compact.status_code == 200, compact.text
    assert "processor_config" not in compact.text
    assert "configured_fields" not in compact.text
    monkeypatch.setattr(processor_profiles, "_open_revision", original)
    assert (
        owner.delete(f"/api/admin/processor-profiles/{profile['id']}").status_code
        == 200
    )
    for response in (
        owner.get("/api/admin/processor-profiles"),
        owner.get(f"/api/admin/processor-profiles/{profile['id']}"),
    ):
        assert response.status_code == 200, response.text
        row = (
            response.json()[0] if isinstance(response.json(), list) else response.json()
        )
        assert row["disabled"] == 1
        assert row["configuration"] == {} and row["configured_fields"] == []
        assert "owner-only template" not in response.text
