import json

import pytest
from fastapi.testclient import TestClient
from test_processor_profiles import binding, create_product, issue, merchant, submit

from extore import processor_profiles
from extore.app import app
from extore.db import db
from extore.secret_store import open_secret, store_secret
from extore.service import job, public_product

DEFAULT_RUNTIME = {
    "timeout_seconds": 120,
    "memory_mb": 256,
    "cpu_seconds": 120,
    "max_output_bytes": 1000000,
}
MIN_RUNTIME = {
    "timeout_seconds": 10,
    "memory_mb": 64,
    "cpu_seconds": 1,
    "max_output_bytes": 65536,
}
MAX_RUNTIME = {**DEFAULT_RUNTIME, "memory_mb": 512}


def create_profile(client, *, workflow=None, configuration=None):
    payload = {
        "name": "合成流水线配置",
        "processor_id": "personalized_text",
        "configuration": configuration or {"template": "Hello $name"},
    }
    if workflow is not None:
        payload["workflow"] = workflow
    response = client.post("/api/admin/processor-profiles", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def bind_profile(client, product_id, profile_id):
    response = client.put(
        f"/api/admin/processor-profiles/bindings/{product_id}",
        json={"profile_id": profile_id},
    )
    assert response.status_code == 200, response.text
    return response.json()["profile"]


def profile_snapshot(profile_id):
    with db() as c:
        profile = dict(
            c.execute(
                "SELECT * FROM processor_profiles WHERE id=?", (profile_id,)
            ).fetchone()
        )
        revisions = [
            dict(row)
            for row in c.execute(
                "SELECT * FROM processor_profile_revisions WHERE profile_id=? ORDER BY revision",
                (profile_id,),
            )
        ]
        audits = [
            dict(row)
            for row in c.execute(
                "SELECT * FROM audit WHERE target=? ORDER BY id", (profile_id,)
            )
        ]
    return profile, revisions, audits


def decrypted_revision(profile_id, revision=None):
    with db() as c:
        row = c.execute(
            "SELECT profiles.*,stored.schema_version,stored.ciphertext "
            "FROM processor_profiles profiles JOIN processor_profile_revisions stored "
            "ON stored.profile_id=profiles.id AND stored.revision=COALESCE(?,profiles.revision) "
            "WHERE profiles.id=?",
            (revision, profile_id),
        ).fetchone()
    return open_secret(
        row["ciphertext"],
        tenant_id=row["shop_id"],
        resource_type=f"processor-config:{row['processor_id']}:v{row['schema_version']}",
        resource_id=f"{row['id']}:{row['revision'] if revision is None else revision}",
    )


def execution(task_id):
    with db() as c:
        before = (
            dict(job(c, task_id)),
            c.execute("SELECT COUNT(*) FROM outbox").fetchone()[0],
            c.execute("SELECT COUNT(*) FROM audit").fetchone()[0],
        )
        result = processor_profiles.runtime_execution(
            c, job(c, task_id), "personalized_text"
        )
        after = (
            dict(job(c, task_id)),
            c.execute("SELECT COUNT(*) FROM outbox").fetchone()[0],
            c.execute("SELECT COUNT(*) FROM audit").fetchone()[0],
        )
        assert after == before
    return result


def test_workflow_defaults_and_owner_view_never_return_secret_values(owner):
    plain = create_profile(owner)
    assert plain["workflow"] == {
        "variables": {},
        "runtime": DEFAULT_RUNTIME,
        "configured_secret_names": [],
    }
    profile = create_profile(
        owner,
        workflow={
            "variables": {"GREETING": "owner-visible-value"},
            "secrets": {
                "Z_TOKEN": "synthetic-secret-z",
                "A_TOKEN": "synthetic-secret-a",
            },
            "runtime": {"memory_mb": 384},
        },
    )
    expected = {
        "variables": {"GREETING": "owner-visible-value"},
        "runtime": {**DEFAULT_RUNTIME, "memory_mb": 384},
        "configured_secret_names": ["A_TOKEN", "Z_TOKEN"],
    }
    assert profile["workflow"] == expected
    for response in (
        owner.get(f"/api/admin/processor-profiles/{profile['id']}"),
        owner.get("/api/admin/processor-profiles"),
        owner.put(
            f"/api/admin/processor-profiles/{profile['id']}",
            json={"name": "改显示名称"},
        ),
    ):
        assert response.status_code == 200, response.text
        rows = (
            response.json() if isinstance(response.json(), list) else [response.json()]
        )
        assert (
            next(row for row in rows if row["id"] == profile["id"])["workflow"]
            == expected
        )
        assert "synthetic-secret-z" not in response.text
        assert "synthetic-secret-a" not in response.text
        assert "ciphertext" not in response.text


def test_workflow_and_processor_configuration_share_encrypted_revision(owner):
    product = create_product(owner, "CONFIG-MARKER-621074 $name")
    profile = create_profile(
        owner,
        configuration={"template": "CONFIG-MARKER-621074 $name"},
        workflow={
            "variables": {"VISIBLE": "VARIABLE-MARKER-621074"},
            "secrets": {"PAYMENT_TOKEN": "SECRET-MARKER-621074"},
            "runtime": MIN_RUNTIME,
        },
    )
    bind_profile(owner, product["id"], profile["id"])
    envelope = decrypted_revision(profile["id"])
    assert envelope == {
        "format": "extore.processor-profile.v2",
        "configuration": {"template": "CONFIG-MARKER-621074 $name"},
        "workflow": {
            "variables": {"VISIBLE": "VARIABLE-MARKER-621074"},
            "secrets": {"PAYMENT_TOKEN": "SECRET-MARKER-621074"},
            "runtime": MIN_RUNTIME,
        },
    }
    with db() as c:
        raw = json.dumps(
            {
                table: [dict(row) for row in c.execute(f"SELECT * FROM {table}")]
                for table in (
                    "products",
                    "processor_profiles",
                    "processor_profile_revisions",
                    "audit",
                )
            }
        )
    for marker in (
        "CONFIG-MARKER-621074",
        "VARIABLE-MARKER-621074",
        "SECRET-MARKER-621074",
    ):
        assert marker not in raw


def test_workflow_reads_and_writes_are_tenant_scoped_and_not_public_or_staff(owner):
    client, shop_id = merchant()
    outsider, _ = merchant()
    product = create_product(client)
    response = client.put(
        f"/api/admin/products/{product['id']}", json={**product, "public": True}
    )
    assert response.status_code == 200, response.text
    profile = create_profile(
        client,
        workflow={
            "variables": {"PRIVATE_TEMPLATE": "ONLY-SHOP-OWNER-745231"},
            "secrets": {"PAYMENT_TOKEN": "ONLY-RUNTIME-SECRET-745231"},
        },
    )
    bind_profile(client, product["id"], profile["id"])
    path = f"/api/admin/processor-profiles/{profile['id']}"
    assert outsider.get(path).status_code == 404
    assert (
        outsider.put(
            path, json={"workflow": {"variables": {"TAMPER": "wrong shop"}}}
        ).status_code
        == 404
    )
    assert outsider.get("/api/admin/processor-profiles").json() == []
    assert (
        outsider.get(
            "/api/admin/processor-profiles", params={"shop_id": shop_id}
        ).status_code
        == 403
    )
    link = client.post(
        "/api/admin/staff",
        json={
            "product_id": product["id"],
            "name": "配置管理者",
            "permissions": ["product.edit", "fulfillment.configure"],
        },
    )
    assert link.status_code == 200, link.text
    staff = TestClient(
        app,
        base_url="http://localhost:8000",
        headers={"Origin": "http://localhost:8000"},
    )
    assert (
        staff.post(
            "/api/staff/login", json={"token": link.json()["url"].split("#")[1]}
        ).status_code
        == 200
    )
    assert staff.get(path).status_code == 401
    assert (
        staff.put(
            path, json={"workflow": {"secrets": {"PAYMENT_TOKEN": "replacement"}}}
        ).status_code
        == 401
    )
    anonymous = TestClient(app, base_url="http://localhost:8000")
    assert anonymous.get(path).status_code == 401
    exchanged = client.post(
        "/api/exchange", json={"code": issue(client, product["id"])}
    )
    for response in (
        staff.get("/api/manage/product"),
        anonymous.get("/api/products"),
        exchanged,
    ):
        assert response.status_code == 200, response.text
        assert "ONLY-SHOP-OWNER-745231" not in response.text
        assert "ONLY-RUNTIME-SECRET-745231" not in response.text
        assert "configured_secret_names" not in response.text
    exported = public_product(client.get("/api/admin/products").json()[0])
    assert "ONLY-SHOP-OWNER-745231" not in json.dumps(exported)
    assert "ONLY-RUNTIME-SECRET-745231" not in json.dumps(exported)


def test_new_blank_secrets_are_not_stored_and_revoked_profile_does_not_decrypt(
    owner, monkeypatch
):
    profile = create_profile(
        owner,
        workflow={
            "variables": {"EMPTY_VALUE": ""},
            "secrets": {"EMPTY_TOKEN": "", "SPACE_TOKEN": "   "},
        },
    )
    assert profile["workflow"] == {
        "variables": {"EMPTY_VALUE": ""},
        "runtime": DEFAULT_RUNTIME,
        "configured_secret_names": [],
    }
    assert decrypted_revision(profile["id"])["workflow"]["secrets"] == {}
    response = owner.put(
        f"/api/admin/processor-profiles/{profile['id']}",
        json={"workflow": {"secrets": {"TOKEN": "synthetic-revoked-secret"}}},
    )
    assert response.status_code == 200, response.text
    assert (
        owner.delete(f"/api/admin/processor-profiles/{profile['id']}").status_code
        == 200
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("revoked workflow credentials must not be decrypted")

    monkeypatch.setattr(processor_profiles, "_open_revision", forbidden)
    for response in (
        owner.get(f"/api/admin/processor-profiles/{profile['id']}"),
        owner.get("/api/admin/processor-profiles"),
    ):
        assert response.status_code == 200, response.text
        rows = (
            response.json() if isinstance(response.json(), list) else [response.json()]
        )
        view = next(row for row in rows if row["id"] == profile["id"])
        assert view["workflow"] == {
            "variables": {},
            "runtime": DEFAULT_RUNTIME,
            "configured_secret_names": [],
        }
        assert "synthetic-revoked-secret" not in response.text


def test_nested_patch_retains_omitted_values_and_blank_secrets(owner):
    profile = create_profile(
        owner,
        workflow={
            "variables": {"KEEP": "old", "CLEAR": "original"},
            "secrets": {"TOKEN": "keep-this-secret", "OTHER_TOKEN": "also-preserved"},
            "runtime": {"timeout_seconds": 80, "memory_mb": 384},
        },
    )
    response = owner.put(
        f"/api/admin/processor-profiles/{profile['id']}",
        json={
            "workflow": {
                "variables": {"CLEAR": "", "NEW": "new"},
                "secrets": {"TOKEN": "", "EMPTY_NEW": "", "SPACE_NEW": "   "},
                "runtime": {"cpu_seconds": 44},
            }
        },
    )
    assert response.status_code == 200, response.text
    view = response.json()["workflow"]
    assert view["variables"] == {"KEEP": "old", "CLEAR": "", "NEW": "new"}
    assert view["configured_secret_names"] == ["OTHER_TOKEN", "TOKEN"]
    assert view["runtime"] == {
        **DEFAULT_RUNTIME,
        "timeout_seconds": 80,
        "memory_mb": 384,
        "cpu_seconds": 44,
    }
    assert decrypted_revision(profile["id"])["workflow"]["secrets"] == {
        "TOKEN": "keep-this-secret",
        "OTHER_TOKEN": "also-preserved",
    }
    response = owner.put(
        f"/api/admin/processor-profiles/{profile['id']}",
        json={"configuration": {"template": "Changed $name"}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["workflow"] == view


def test_explicit_deletion_and_cross_collection_migration(owner):
    profile = create_profile(
        owner,
        workflow={
            "variables": {"MOVE_PRIVATE": "old public", "REMOVE_VAR": "remove"},
            "secrets": {"MOVE_PUBLIC": "old private", "REMOVE_SECRET": "remove secret"},
        },
    )
    response = owner.put(
        f"/api/admin/processor-profiles/{profile['id']}",
        json={
            "workflow": {
                "delete_variables": ["MOVE_PRIVATE", "REMOVE_VAR"],
                "delete_secrets": ["MOVE_PUBLIC", "REMOVE_SECRET"],
                "variables": {"MOVE_PUBLIC": "explicitly public"},
                "secrets": {"MOVE_PRIVATE": "new private"},
            }
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["workflow"]["variables"] == {
        "MOVE_PUBLIC": "explicitly public"
    }
    assert response.json()["workflow"]["configured_secret_names"] == ["MOVE_PRIVATE"]
    stored = decrypted_revision(profile["id"])["workflow"]
    assert stored["variables"] == {"MOVE_PUBLIC": "explicitly public"}
    assert stored["secrets"] == {"MOVE_PRIVATE": "new private"}


@pytest.mark.parametrize(
    "workflow",
    [
        {"variables": {"A": "set"}, "delete_variables": ["A"]},
        {"secrets": {"A": "set"}, "delete_secrets": ["A"]},
        {"secrets": {"A": ""}, "delete_secrets": ["A"]},
        {"variables": {"TOKEN": "conflict with stored secret"}},
        {"secrets": {"VISIBLE": "conflict with stored variable"}},
        {"variables": {"NEW": "public"}, "secrets": {"NEW": "private"}},
    ],
)
def test_ambiguous_sets_and_deletes_roll_back_the_entire_update(owner, workflow):
    profile = create_profile(
        owner,
        workflow={"variables": {"VISIBLE": "old"}, "secrets": {"TOKEN": "private"}},
    )
    snapshot = profile_snapshot(profile["id"])
    response = owner.put(
        f"/api/admin/processor-profiles/{profile['id']}",
        json={
            "name": "must roll back",
            "configuration": {"template": "Must not store $name"},
            "workflow": workflow,
        },
    )
    assert response.status_code == 422, response.text
    assert profile_snapshot(profile["id"]) == snapshot


@pytest.mark.parametrize("runtime", [MIN_RUNTIME, MAX_RUNTIME])
def test_runtime_boundary_values_are_accepted(owner, runtime):
    profile = create_profile(owner, workflow={"runtime": runtime})
    assert profile["workflow"]["runtime"] == runtime


@pytest.mark.parametrize(
    "workflow",
    [
        {"command": "echo unsafe"},
        {"network": True},
        {"mount": "/"},
        {"image": "untrusted/image"},
        {"runtime": {"command": "echo unsafe"}},
        {"runtime": {"network": True}},
        {"runtime": {"timeout_seconds": 9}},
        {"runtime": {"timeout_seconds": 121}},
        {"runtime": {"timeout_seconds": True}},
        {"runtime": {"timeout_seconds": "120"}},
        {"runtime": {"memory_mb": 63}},
        {"runtime": {"memory_mb": 513}},
        {"runtime": {"memory_mb": 128.0}},
        {"runtime": {"cpu_seconds": 0}},
        {"runtime": {"cpu_seconds": 121}},
        {"runtime": {"cpu_seconds": False}},
        {"runtime": {"max_output_bytes": 65535}},
        {"runtime": {"max_output_bytes": 1000001}},
        {"runtime": {"max_output_bytes": 65536.0}},
        {"variables": {"lowercase": "invalid"}},
        {"variables": {"1BAD": "invalid"}},
        {"variables": {"_BAD": "invalid"}},
        {"variables": {"EXTORE_TOKEN": "reserved"}},
        {"secrets": {"EXTORE_API_KEY": "reserved"}},
        {"variables": {"A" * 65: "too long"}},
        {"variables": {"A\n": "invalid"}},
        {"variables": {"NULL_VALUE": None}},
        {"secrets": {"NUMBER_VALUE": 123}},
        {"variables": {f"V{i}": "x" for i in range(65)}},
        {"secrets": {f"S{i}": "x" for i in range(65)}},
        {"variables": {"TOO_LARGE": "x" * 8193}},
        {"secrets": {"TOO_LARGE": "中" * 2731}},
        {
            "variables": {f"V{i}": "x" * 8192 for i in range(4)},
            "secrets": {f"S{i}": "x" * 8192 for i in range(5)},
        },
        {"variables": {"SAME": "public"}, "secrets": {"SAME": "private"}},
        {"delete_variables": ["EXTORE_RESERVED"]},
        {"delete_secrets": ["invalid-name"]},
    ],
)
def test_invalid_or_unsafe_workflows_cannot_create_a_profile(owner, workflow):
    with db() as c:
        before = c.execute("SELECT COUNT(*) FROM processor_profiles").fetchone()[0]
    response = owner.post(
        "/api/admin/processor-profiles",
        json={
            "name": "invalid synthetic profile",
            "processor_id": "personalized_text",
            "configuration": {"template": "Hello $name"},
            "workflow": workflow,
        },
    )
    assert response.status_code == 422, response.text
    with db() as c:
        assert (
            c.execute("SELECT COUNT(*) FROM processor_profiles").fetchone()[0] == before
        )


def test_utf8_byte_limit_and_per_collection_name_limits_are_independent(owner):
    boundary = "中" * 2730 + "aa"
    assert len(boundary.encode()) == 8192
    profile = create_profile(
        owner,
        workflow={
            "variables": {"A" * 64: boundary, **{f"V{i}": "x" for i in range(63)}},
            "secrets": {f"S{i}": "secret" for i in range(64)},
        },
    )
    assert len(profile["workflow"]["variables"]) == 64
    assert len(profile["workflow"]["configured_secret_names"]) == 64
    snapshot = profile_snapshot(profile["id"])
    response = owner.put(
        f"/api/admin/processor-profiles/{profile['id']}",
        json={"workflow": {"variables": {"NEW_NAME": "overflow"}}},
    )
    assert response.status_code == 422, response.text
    assert profile_snapshot(profile["id"]) == snapshot


def test_aggregate_limit_is_checked_after_merging_with_existing_values(owner):
    profile = create_profile(
        owner, workflow={"variables": {f"V{i}": "x" * 8192 for i in range(4)}}
    )
    snapshot = profile_snapshot(profile["id"])
    response = owner.put(
        f"/api/admin/processor-profiles/{profile['id']}",
        json={"workflow": {"secrets": {f"S{i}": "x" * 8192 for i in range(5)}}},
    )
    assert response.status_code == 422, response.text
    assert profile_snapshot(profile["id"]) == snapshot


def test_legacy_flat_ciphertext_reads_default_workflow_and_updates_to_envelope(owner):
    product = create_product(owner)
    profile = binding(owner, product["id"])
    legacy = {"template": "LEGACY $name"}
    ciphertext = store_secret(
        legacy,
        tenant_id=profile["shop_id"],
        resource_type="processor-config:personalized_text:v1",
        resource_id=f"{profile['id']}:{profile['bound_revision']}",
    )
    with db() as c:
        c.execute(
            "UPDATE processor_profile_revisions SET ciphertext=? WHERE profile_id=? AND revision=?",
            (ciphertext, profile["id"], profile["bound_revision"]),
        )
    response = owner.get(f"/api/admin/processor-profiles/{profile['id']}")
    assert response.status_code == 200, response.text
    assert response.json()["configuration"] == legacy
    assert response.json()["workflow"] == {
        "variables": {},
        "runtime": DEFAULT_RUNTIME,
        "configured_secret_names": [],
    }
    _, task = submit(owner, issue(owner, product["id"]))
    configuration, _, workflow = execution(task["id"])
    assert configuration == legacy
    assert workflow == {"variables": {}, "secrets": {}, "runtime": DEFAULT_RUNTIME}
    response = owner.put(
        f"/api/admin/processor-profiles/{profile['id']}",
        json={"workflow": {"variables": {"GREETING": "migration keeps config"}}},
    )
    assert response.status_code == 200, response.text
    stored = decrypted_revision(profile["id"])
    assert stored["format"] == "extore.processor-profile.v2"
    assert stored["configuration"] == legacy
    assert stored["workflow"]["variables"] == {"GREETING": "migration keeps config"}


def test_workflow_and_configuration_are_frozen_per_card_until_future_rebind(owner):
    product = create_product(owner)
    profile = binding(owner, product["id"])
    path = f"/api/admin/processor-profiles/{profile['id']}"
    first = owner.put(
        path,
        json={
            "configuration": {"template": "OLD $name"},
            "workflow": {
                "variables": {"VERSION": "old"},
                "secrets": {"TOKEN": "old-secret"},
                "runtime": MIN_RUNTIME,
            },
        },
    )
    assert first.status_code == 200, first.text
    old_binding = bind_profile(owner, product["id"], profile["id"])
    old_code = issue(owner, product["id"])
    changed = owner.put(
        path,
        json={
            "configuration": {"template": "NEW $name"},
            "workflow": {
                "variables": {"VERSION": "new"},
                "secrets": {"TOKEN": "new-secret"},
                "runtime": MAX_RUNTIME,
            },
        },
    )
    assert changed.status_code == 200, changed.text
    still_bound = binding(owner, product["id"])
    assert still_bound["bound_revision"] == old_binding["bound_revision"]
    assert still_bound["configuration"] == {"template": "OLD $name"}
    assert still_bound["workflow"]["variables"] == {"VERSION": "old"}
    assert still_bound["workflow"]["runtime"] == MIN_RUNTIME
    before_rebind_code = issue(owner, product["id"])
    new_binding = bind_profile(owner, product["id"], profile["id"])
    assert new_binding["bound_revision"] == changed.json()["revision"]
    assert new_binding["workflow"]["variables"] == {"VERSION": "new"}
    new_code = issue(owner, product["id"])
    for code, version, runtime in (
        (old_code, "old", MIN_RUNTIME),
        (before_rebind_code, "old", MIN_RUNTIME),
        (new_code, "new", MAX_RUNTIME),
    ):
        _, task = submit(owner, code)
        configuration, context, workflow = execution(task["id"])
        assert configuration == {"template": version.upper() + " $name"}
        assert context["revision"] == (
            old_binding["bound_revision"]
            if version == "old"
            else new_binding["bound_revision"]
        )
        assert workflow == {
            "variables": {"VERSION": version},
            "secrets": {"TOKEN": version + "-secret"},
            "runtime": runtime,
        }


def test_inline_configuration_edit_preserves_bound_workflow(owner):
    product = create_product(owner)
    original = binding(owner, product["id"])
    response = owner.put(
        f"/api/admin/processor-profiles/{original['id']}",
        json={
            "workflow": {
                "variables": {"GREETING": "keep variable"},
                "secrets": {"PAYMENT_TOKEN": "keep credentials"},
                "runtime": MIN_RUNTIME,
            }
        },
    )
    assert response.status_code == 200, response.text
    bind_profile(owner, product["id"], original["id"])
    response = owner.put(
        f"/api/admin/products/{product['id']}",
        json={**product, "processor_config": {"template": "Changed $name"}},
    )
    assert response.status_code == 200, response.text
    current = binding(owner, product["id"])
    assert current["id"] != original["id"]
    assert current["workflow"]["variables"] == {"GREETING": "keep variable"}
    assert current["workflow"]["runtime"] == MIN_RUNTIME
    _, task = submit(owner, issue(owner, product["id"]))
    configuration, _, workflow = execution(task["id"])
    assert configuration == {"template": "Changed $name"}
    assert workflow == {
        "variables": {"GREETING": "keep variable"},
        "secrets": {"PAYMENT_TOKEN": "keep credentials"},
        "runtime": MIN_RUNTIME,
    }
