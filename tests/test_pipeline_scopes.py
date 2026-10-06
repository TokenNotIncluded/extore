"""Scoped actors survive upgrades without enlarging the signed approval."""

import base64
import hashlib
import json
import sqlite3
import time
import uuid

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException
from starlette.responses import Response

from extore import pipeline_scopes as scopes
from extore.db import db
from extore.link_access import consume_link
from extore.models import LINK_PERMISSIONS
from extore.security import create_session, digest, staff_authorization


@pytest.fixture(autouse=True)
def scope_tables(clean):
    with db() as c:
        scopes.init_schema(c)
    yield
    with db() as c:
        # The shared integration fixture owns the schema migration; keep this
        # focused file runnable before it learns the new dependency order.
        c.execute("DELETE FROM pipeline_bindings")
        c.execute("DELETE FROM pipeline_authorizations")


def identity():
    raw = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )
    return base64.urlsafe_b64encode(raw).decode().rstrip("="), hashlib.sha256(
        raw
    ).hexdigest()


def shop(c):
    shop_id = str(uuid.uuid4())
    c.execute(
        "INSERT INTO shops(id,name,email,created,verified) VALUES (?,?,?,?,1)",
        (shop_id, "Scope test", f"{shop_id}@example.com", time.time()),
    )
    return shop_id


def product(c, shop_id, mode="manual"):
    product_id = str(uuid.uuid4())
    c.execute(
        "INSERT INTO products(id,config,created,shop_id) VALUES (?,?,?,?)",
        (
            product_id,
            json.dumps({"name": product_id, "mode": mode}),
            time.time(),
            shop_id,
        ),
    )
    return product_id


def draft(
    shop_id, product_ids, *, kind="shop.pipeline", permissions=None, expires=None
):
    return {
        "shop_id": shop_id,
        "kind": kind,
        "product_ids": product_ids,
        "permissions": permissions or list(scopes.PIPELINE_PERMISSIONS),
        **({"expires": expires} if expires is not None else {}),
    }


def materialize(c, values, key=None, **kwargs):
    public, fingerprint = key or identity()
    return scopes.materialize(
        c,
        values,
        public,
        "Document Bot",
        fingerprint,
        f"shop:{values['shop_id']}",
        **kwargs,
    )


def binding_rows(c, value):
    binding = value["bindings"][0]
    staff = staff_authorization(c, binding["staff_id"])
    device = c.execute(
        "SELECT * FROM cli_devices WHERE id=?", (binding["device_id"],)
    ).fetchone()
    return staff, device


def job(c, product_id, staff_id):
    card_id, job_id = str(uuid.uuid4()), str(uuid.uuid4())
    c.execute(
        "INSERT INTO cards(id,digest,product_id,created) VALUES (?,?,?,?)",
        (card_id, digest(card_id), product_id, time.time()),
    )
    c.execute(
        "INSERT INTO jobs(id,card_id,product_id,state,params,progress,claimed_by,lease,"
        "created,updated,completed_steps,result_json) VALUES (?,?,?,'processing',?,75,?,?,?,?,?,?)",
        (
            job_id,
            card_id,
            product_id,
            '{"requirements":"keep"}',
            staff_id,
            time.time() + 3600,
            time.time(),
            time.time(),
            '["review"]',
            '{"draft":"keep"}',
        ),
    )
    return job_id


def test_schema_only_adds_tables_without_business_rows_or_old_ddl_changes():
    with db() as c:
        before = {
            row["name"]: row["sql"]
            for row in c.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='table'"
            )
            if row["name"] not in ("pipeline_authorizations", "pipeline_bindings")
        }
        counts = {
            table: c.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("staff", "cli_devices", "sessions", "products")
        }
        scopes.init_schema(c)
        scopes.init_schema(c)
        assert before == {
            row["name"]: row["sql"]
            for row in c.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='table'"
            )
            if row["name"] in before
        }
        assert counts == {
            table: c.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in counts
        }


def test_snapshot_creates_private_single_cli_actors_and_does_not_add_future_skus():
    with db() as c:
        shop_id = shop(c)
        pids = [product(c, shop_id), product(c, shop_id)]
        value = materialize(c, draft(shop_id, pids))
        scope = value["authorization"]
        assert scope["product_ids"] == sorted(pids)
        assert scope["permissions"] == list(scopes.PIPELINE_PERMISSIONS)
        assert scope["revision"] == 1
        assert 6.99 * 86400 < scope["expires"] - time.time() <= 7 * 86400
        assert not {"digest", "token", "url", "public_key"} & set(scope)
        future = product(c, shop_id)
        assert future not in scopes.authorization(c, scope["id"])["product_ids"]
        for binding in value["bindings"]:
            assert binding["authorization_id"] == scope["id"]
            assert binding["authorization_revision"] == 1
            assert not binding["already_authorized"]
            staff = staff_authorization(c, binding["staff_id"])
            assert (
                staff["max_uses"],
                staff["uses"],
                staff["max_cli_uses"],
                staff["cli_uses"],
            ) == (0, 0, 1, 1)
            with pytest.raises(HTTPException):
                consume_link(c, staff, channel="browser")
            with pytest.raises(HTTPException):
                consume_link(c, staff, channel="cli")


def test_product_scope_can_explicitly_approve_all_product_permissions():
    with db() as c:
        shop_id = shop(c)
        pid = product(c, shop_id, mode="script")
        value = materialize(
            c, draft(shop_id, [pid], kind="product", permissions=list(LINK_PERMISSIONS))
        )
        assert value["authorization"]["permissions"] == list(LINK_PERMISSIONS)
        staff, device = binding_rows(c, value)
        assert (
            scopes.check_device_scope(c, device, staff)["id"]
            == value["authorization"]["id"]
        )


@pytest.mark.parametrize(
    "permissions",
    [
        ["product.edit"],
        ["links.delegate"],
        ["queue.process"],
        ["queue.view", "queue.view"],
    ],
)
def test_pipeline_ceiling_and_permission_dependencies(permissions):
    with pytest.raises(HTTPException), db() as c:
        shop_id = shop(c)
        materialize(c, draft(shop_id, [product(c, shop_id)], permissions=permissions))


def test_upgrade_preserves_actor_device_claim_and_expiry_and_only_admits_new_product_once():
    key = identity()
    with db() as c:
        shop_id = shop(c)
        first, second = product(c, shop_id), product(c, shop_id)
        initial = materialize(
            c, draft(shop_id, [first], permissions=["queue.view"]), key
        )
        old = initial["bindings"][0]
        job_id = job(c, first, old["staff_id"])
        upgraded = materialize(
            c,
            draft(shop_id, [first, second]),
            key,
            existing_authorization_id=initial["authorization"]["id"],
            expected_revision=1,
        )
        retained = next(b for b in upgraded["bindings"] if b["product_id"] == first)
        added = next(b for b in upgraded["bindings"] if b["product_id"] == second)
        assert (retained["staff_id"], retained["device_id"]) == (
            old["staff_id"],
            old["device_id"],
        )
        assert retained["already_authorized"] and not added["already_authorized"]
        assert retained["cli_uses"] == added["cli_uses"] == 1
        assert upgraded["authorization"]["revision"] == 2
        assert (
            upgraded["authorization"]["expires"] == initial["authorization"]["expires"]
        )
        assert tuple(
            c.execute(
                "SELECT claimed_by,progress,state FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
        ) == (old["staff_id"], 75, "processing")


def test_only_reviewed_explicit_recovery_preserves_expiry_revision_and_actors():
    key = identity()
    with db() as c:
        shop_id = shop(c)
        pid = product(c, shop_id)
        value = materialize(c, draft(shop_id, [pid]), key)
        explicit = materialize(
            c,
            draft(shop_id, [pid]),
            key,
            existing_authorization_id=value["authorization"]["id"],
            expected_revision=1,
        )
        with pytest.raises(HTTPException) as conflict:
            materialize(c, draft(shop_id, [pid], expires=time.time() + 20 * 86400), key)
        assert conflict.value.status_code == 409
        assert explicit["authorization"] == value["authorization"]
        assert explicit["bindings"][0]["device_id"] == value["bindings"][0]["device_id"]
        assert c.execute("SELECT count(*) FROM staff").fetchone()[0] == 1
        assert (
            c.execute("SELECT count(*) FROM pipeline_authorizations").fetchone()[0] == 1
        )


def test_intervening_longer_scope_never_replaces_a_fresh_seven_day_approval():
    key = identity()
    with db() as c:
        shop_id = shop(c)
        pid = product(c, shop_id)
        value = materialize(
            c, draft(shop_id, [pid], expires=time.time() + 89 * 86400), key
        )
        existing = scopes.find_recovery(
            c,
            shop_id,
            "shop.pipeline",
            key[0],
            [pid],
            list(scopes.PIPELINE_PERMISSIONS),
        )
        assert existing["expires"] == value["authorization"]["expires"]
        with pytest.raises(HTTPException) as conflict:
            materialize(c, draft(shop_id, [pid], expires=time.time() + 7 * 86400), key)
        assert conflict.value.status_code == 409
        assert (
            c.execute("SELECT count(*) FROM pipeline_authorizations").fetchone()[0] == 1
        )


@pytest.mark.parametrize(
    "change",
    [
        "revision",
        "key",
        "expiry",
        "shorter_expiry",
        "kind",
        "product_swap",
        "permissions",
        "shop",
    ],
)
def test_upgrade_rejects_stale_or_replaced_approved_scope_without_mutation(change):
    key = identity()
    with db() as c:
        shop_id = shop(c)
        first, second = product(c, shop_id), product(c, shop_id)
        value = materialize(c, draft(shop_id, [first]), key)
        aid = value["authorization"]["id"]
        updated = draft(shop_id, [first])
        revision = 1
        if change == "revision":
            revision = 0
        elif change == "key":
            key = identity()
        elif change == "expiry":
            updated["expires"] = value["authorization"]["expires"] + 1
        elif change == "shorter_expiry":
            updated["expires"] = value["authorization"]["expires"] - 1
        elif change == "kind":
            updated["kind"] = "product"
        elif change == "product_swap":
            updated["product_ids"] = [second]
        elif change == "permissions":
            updated["permissions"] = ["queue.view"]
        elif change == "shop":
            updated["shop_id"] = shop(c)
        with pytest.raises(HTTPException):
            materialize(
                c,
                updated,
                key,
                existing_authorization_id=aid,
                expected_revision=revision,
            )
        assert scopes.authorization(c, aid) == value["authorization"]
        assert c.execute("SELECT count(*) FROM staff").fetchone()[0] == 1


@pytest.mark.parametrize(
    "condition",
    [
        "device_revoked",
        "staff_revoked",
        "scope_revoked",
        "scope_expired",
        "shop_disabled",
    ],
)
def test_upgrade_cannot_resurrect_revoked_expired_or_disabled_authority(condition):
    key = identity()
    with db() as c:
        shop_id = shop(c)
        pid = product(c, shop_id)
        value = materialize(c, draft(shop_id, [pid], permissions=["queue.view"]), key)
        binding, aid = value["bindings"][0], value["authorization"]["id"]
        if condition == "device_revoked":
            c.execute(
                "UPDATE cli_devices SET revoked=1 WHERE id=?", (binding["device_id"],)
            )
        elif condition == "staff_revoked":
            c.execute("UPDATE staff SET revoked=1 WHERE id=?", (binding["staff_id"],))
        elif condition == "scope_revoked":
            c.execute("UPDATE pipeline_authorizations SET revoked=1 WHERE id=?", (aid,))
        elif condition == "scope_expired":
            c.execute(
                "UPDATE pipeline_authorizations SET expires=? WHERE id=?",
                (time.time() - 1, aid),
            )
        elif condition == "shop_disabled":
            c.execute("UPDATE shops SET enabled=0 WHERE id=?", (shop_id,))
        with pytest.raises(HTTPException):
            materialize(
                c,
                draft(shop_id, [pid]),
                key,
                existing_authorization_id=aid,
                expected_revision=1,
            )
        assert (
            c.execute(
                "SELECT revision FROM pipeline_authorizations WHERE id=?", (aid,)
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize(
    "corruption",
    ["move", "mode", "permissions", "expiry", "device_key", "device_revoked", "parent"],
)
def test_current_ancestor_validation_rejects_mutated_scope_and_revoked_root_device(
    corruption,
):
    with db() as c:
        shop_id = shop(c)
        pid = product(c, shop_id)
        value = materialize(c, draft(shop_id, [pid]))
        binding = value["bindings"][0]
        if corruption == "move":
            c.execute("UPDATE products SET shop_id=? WHERE id=?", (shop(c), pid))
        elif corruption == "mode":
            c.execute(
                "UPDATE products SET config=? WHERE id=?", ('{"mode":"script"}', pid)
            )
        elif corruption == "permissions":
            c.execute(
                "UPDATE staff SET permissions=? WHERE id=?",
                ('["queue.view","product.edit"]', binding["staff_id"]),
            )
        elif corruption == "expiry":
            c.execute(
                "UPDATE staff SET expires=expires+1 WHERE id=?", (binding["staff_id"],)
            )
        elif corruption == "device_key":
            c.execute(
                "UPDATE cli_devices SET public_key=? WHERE id=?",
                (identity()[0], binding["device_id"]),
            )
        elif corruption == "parent":
            c.execute(
                "UPDATE staff SET parent_id=? WHERE id=?",
                (binding["staff_id"], binding["staff_id"]),
            )
        elif corruption == "device_revoked":
            c.execute(
                "UPDATE cli_devices SET revoked=1 WHERE id=?", (binding["device_id"],)
            )
        raw = c.execute(
            "SELECT * FROM staff WHERE id=?", (binding["staff_id"],)
        ).fetchone()
        with pytest.raises(HTTPException):
            scopes.validate_staff_scope(c, raw)


def test_revoke_releases_root_and_delegated_leases_and_sessions_but_keeps_progress_and_other_scope():
    with db() as c:
        shop_id = shop(c)
        pid = product(c, shop_id)
        value = materialize(
            c, draft(shop_id, [pid], kind="product", permissions=list(LINK_PERMISSIONS))
        )
        root = value["bindings"][0]["staff_id"]
        child = str(uuid.uuid4())
        c.execute(
            "INSERT INTO staff(id,digest,product_id,name,expires,permissions,parent_id,created) VALUES (?,?,?,?,?,?,?,?)",
            (
                child,
                digest(child),
                pid,
                "Delegated",
                value["authorization"]["expires"],
                '["queue.view","queue.process"]',
                root,
                time.time(),
            ),
        )
        create_session(c, Response(), "staff", child)
        root_job, child_job = job(c, pid, root), job(c, pid, child)
        unrelated = materialize(c, draft(shop_id, [product(c, shop_id)]))
        untouched = unrelated["bindings"][0]["staff_id"]
        result = scopes.revoke_authorization(
            c, value["authorization"]["id"], actor=f"shop:{shop_id}"
        )
        assert result == {"authorizations": 1, "bindings": 1, "jobs": 2}
        for jid in (root_job, child_job):
            row = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
            assert (row["state"], row["claimed_by"], row["lease"]) == (
                "queued",
                None,
                None,
            )
            assert (
                row["progress"],
                row["params"],
                row["completed_steps"],
                row["result_json"],
            ) == (75, '{"requirements":"keep"}', '["review"]', '{"draft":"keep"}')
        assert (
            c.execute(
                "SELECT revoked FROM sessions WHERE staff_id=?", (child,)
            ).fetchone()[0]
            == 1
        )
        assert (
            c.execute("SELECT revoked FROM staff WHERE id=?", (untouched,)).fetchone()[
                0
            ]
            == 0
        )
        assert (
            scopes.revoke_authorization(c, value["authorization"]["id"])[
                "authorizations"
            ]
            == 0
        )


def test_root_reset_issuer_filter_and_shop_reset_boundaries():
    with db() as c:
        first, second = shop(c), shop(c)
        owner_scope = materialize(c, draft(first, [product(c, first)]))
        root_scope = materialize(
            c, draft(first, [product(c, first)]), issuer_role="root"
        )
        other_scope = materialize(c, draft(second, [product(c, second)]))
        assert (
            scopes.revoke_authorizations(c, actor="owner", issuer_role="root")[
                "authorizations"
            ]
            == 1
        )
        assert (
            scopes.authorization_raw(c, root_scope["authorization"]["id"])["revoked"]
            == 1
        )
        assert scopes.authorization(c, owner_scope["authorization"]["id"])
        assert (
            scopes.revoke_authorizations(c, first, actor=f"shop:{first}")[
                "authorizations"
            ]
            == 1
        )
        assert scopes.authorization(c, other_scope["authorization"]["id"])


@pytest.mark.parametrize("previous,next_issuer", [("root", "shop"), ("shop", "root")])
def test_full_reapproval_transfers_issuer_and_bumps_revision_without_replacing_actors(
    previous, next_issuer
):
    key = identity()
    with db() as c:
        shop_id = shop(c)
        pid = product(c, shop_id)
        initial = materialize(c, draft(shop_id, [pid]), key, issuer_role=previous)
        old_binding = initial["bindings"][0]
        jid = job(c, pid, old_binding["staff_id"])
        changed = materialize(
            c,
            draft(shop_id, [pid]),
            key,
            existing_authorization_id=initial["authorization"]["id"],
            expected_revision=1,
            issuer_role=next_issuer,
        )
        assert changed["authorization"]["issuer_role"] == next_issuer
        assert changed["authorization"]["issuer_shop_id"] == (
            shop_id if next_issuer == "shop" else None
        )
        assert changed["authorization"]["revision"] == 2
        assert (
            changed["authorization"]["expires"] == initial["authorization"]["expires"]
        )
        assert changed["bindings"][0]["staff_id"] == old_binding["staff_id"]
        assert changed["bindings"][0]["device_id"] == old_binding["device_id"]
        assert (
            c.execute("SELECT claimed_by FROM jobs WHERE id=?", (jid,)).fetchone()[0]
            == old_binding["staff_id"]
        )
        assert (
            c.execute(
                "SELECT target FROM audit WHERE action=?",
                (f"cli.scope.issuer.{previous}_to_{next_issuer}",),
            ).fetchone()[0]
            == initial["authorization"]["id"]
        )
        reset = scopes.revoke_authorizations(c, actor="owner", issuer_role="root")
        assert reset["authorizations"] == (1 if next_issuer == "root" else 0)


def test_failed_claim_transaction_does_not_transfer_issuer_or_change_revision():
    key = identity()
    with db() as c:
        shop_id = shop(c)
        pid = product(c, shop_id)
        initial = materialize(c, draft(shop_id, [pid]), key, issuer_role="root")
    with pytest.raises(RuntimeError), db() as c:
        materialize(
            c,
            draft(shop_id, [pid]),
            key,
            existing_authorization_id=initial["authorization"]["id"],
            expected_revision=1,
            issuer_role="shop",
        )
        raise RuntimeError("abort signed claim before commit")
    with db() as c:
        assert (
            scopes.authorization(c, initial["authorization"]["id"])["issuer_role"]
            == "root"
        )
        assert scopes.authorization(c, initial["authorization"]["id"])["revision"] == 1
        assert (
            c.execute(
                "SELECT count(*) FROM audit WHERE action='cli.scope.issuer.root_to_shop'"
            ).fetchone()[0]
            == 0
        )


def test_total_cap_includes_tombstones_while_exact_recovery_can_still_succeed(
    monkeypatch,
):
    monkeypatch.setattr(scopes, "MAX_KEY_TOTAL_AUTHORIZATIONS", 1)
    key = identity()
    with db() as c:
        shop_id = shop(c)
        pid = product(c, shop_id)
        value = materialize(c, draft(shop_id, [pid]), key)
        assert (
            materialize(
                c,
                draft(shop_id, [pid]),
                key,
                existing_authorization_id=value["authorization"]["id"],
                expected_revision=1,
            )["authorization"]["id"]
            == value["authorization"]["id"]
        )
        scopes.revoke_authorization(c, value["authorization"]["id"])
        with pytest.raises(HTTPException) as limit:
            materialize(c, draft(shop_id, [product(c, shop_id)]), key)
        assert limit.value.status_code == 409
        assert "历史已达到保留上限" in limit.value.detail
        assert "撤销" not in limit.value.detail


def test_revocation_frees_active_capacity_but_does_not_remove_history(monkeypatch):
    monkeypatch.setattr(scopes, "MAX_KEY_AUTHORIZATIONS", 1)
    key = identity()
    with db() as c:
        shop_id = shop(c)
        first, second = product(c, shop_id), product(c, shop_id)
        old = materialize(c, draft(shop_id, [first]), key)
        with pytest.raises(HTTPException) as limit:
            materialize(c, draft(shop_id, [second]), key)
        assert limit.value.status_code == 409
        assert "有效流水线授权过多" in limit.value.detail
        assert "撤销" in limit.value.detail
        scopes.revoke_authorization(c, old["authorization"]["id"])
        replacement = materialize(c, draft(shop_id, [second]), key)
        assert replacement["authorization"]["id"] != old["authorization"]["id"]
        assert (
            c.execute("SELECT count(*) FROM pipeline_authorizations").fetchone()[0] == 2
        )


def test_claim_requires_transaction_and_rejects_invalid_key_fingerprint():
    with sqlite3.connect(":memory:", isolation_level=None) as c:
        with pytest.raises(RuntimeError):
            scopes.materialize(c, {}, "", "", "", "owner")
    with db() as c:
        shop_id = shop(c)
        pid = product(c, shop_id)
        with pytest.raises(HTTPException):
            materialize(c, draft(shop_id, [pid]), (identity()[0], "f" * 64))
