"""CLI consent and recovery against signed scope handshakes and the real API."""

import json
import os
import stat

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from test_manage_cli import DeviceClock, MockMerchant, arguments

from extore import manage_client as remote

ORIGIN = "https://merchant.example"
QUEUE = ["queue.view", "queue.process", "queue.retry"]


class ScopeMerchant(MockMerchant):
    def __init__(self):
        super().__init__()
        self.scope_requests = {}
        self.authorizations = {}
        self.bindings = {}
        self.states = ["approved"]
        self.pipeline_products = ["product-a", "product-b"]
        self.effective_permissions = None
        self.lost_response = None
        self.tamper_claim = None

    def __call__(self, request):
        path = request.url.path
        if not path.startswith("/api/cli/scopes/"):
            if self.lost_response == "session" and path == "/api/cli/session":
                self.lost_response = None
                super().__call__(request)
                raise httpx.ReadError("lost scope session response", request=request)
            return super().__call__(request)
        payload = json.loads(request.read())
        self.calls.append(
            (request.method, path, request.headers.get("Authorization"), request.read())
        )
        assert "token" not in payload
        assert (
            request.headers.get("Cookie") is None
            and request.headers.get("Authorization") is None
        )
        public = Ed25519PublicKey.from_public_bytes(
            remote._unb64(payload["public_key"])
        )
        if path.endswith("/request"):
            unsigned = {
                key: value for key, value in payload.items() if key != "signature"
            }
            proof = (
                "extore-cli-scope-request-v1\n"
                + ORIGIN
                + "\n"
                + json.dumps(
                    unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=True
                )
            )
            public.verify(remote._unb64(payload["signature"]), proof.encode())
            key = (payload["public_key"], payload["nonce"])
            if key not in self.scope_requests:
                self.scope_requests[key] = {
                    **payload,
                    "request_id": f"request-{len(self.scope_requests) + 1}",
                    "user_code": f"ABCD-EFGH-{len(self.scope_requests) + 1:04d}",
                    "approval_url": ORIGIN + "/cli/device",
                    "challenge": remote._b64(os.urandom(32)),
                    "fingerprint": remote.hashlib.sha256(
                        remote._unb64(payload["public_key"])
                    ).hexdigest(),
                    "expires": remote.time.time() + 600,
                    "interval": 5,
                }
            code = self.scope_requests[key]
            if self.lost_response == "request":
                self.lost_response = None
                raise httpx.ReadError("lost scope code response", request=request)
            return httpx.Response(200, json=code)
        code = next(
            item
            for item in self.scope_requests.values()
            if item["request_id"] == payload["request_id"]
        )
        assert code["public_key"] == payload["public_key"]
        if path.endswith("/status"):
            proof = "\n".join(
                (
                    "extore-cli-scope-status-v1",
                    ORIGIN,
                    code["request_id"],
                    code["public_key"],
                )
            )
            public.verify(remote._unb64(payload["signature"]), proof.encode())
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            return httpx.Response(
                200,
                json={
                    "status": "claimed" if code.get("claimed") else state,
                    "interval": 5,
                    "expires": code["expires"],
                },
            )
        assert path.endswith("/claim")
        proof = "\n".join(
            (
                "extore-cli-scope-claim-v1",
                ORIGIN,
                code["request_id"],
                code["challenge"],
                code["public_key"],
            )
        )
        public.verify(remote._unb64(payload["signature"]), proof.encode())
        if not code.get("claimed"):
            self._materialize(code)
        result = {
            "authorization": self.authorizations[code["claimed"]],
            "bindings": self.bindings[code["claimed"]],
        }
        if self.lost_response == "claim":
            self.lost_response = None
            raise httpx.ReadError("lost scope binding response", request=request)
        result = json.loads(json.dumps(result))
        if self.tamper_claim:
            self.tamper_claim(result)
        return httpx.Response(200, json=result)

    def _materialize(self, code):
        products = code["product_ids"] or self.pipeline_products[:]
        permissions = self.effective_permissions or code["permissions"]
        if code["authorization_id"]:
            authorization = self.authorizations[code["authorization_id"]]
            assert authorization["revision"] == code["expected_revision"]
            changed = (
                set(authorization["product_ids"]) != set(products)
                or authorization["permissions"] != permissions
                or authorization["client_name"] != code["client_name"]
                or authorization.get("agent_type") != code.get("agent_type")
            )
            if changed:
                authorization["revision"] += 1
            authorization["product_ids"] = products[:]
            authorization["permissions"] = permissions[:]
            authorization["client_name"] = code["client_name"]
            if code.get("agent_type") is not None:
                authorization["agent_type"] = code["agent_type"]
        else:
            authorization = next(
                (
                    item
                    for item in self.authorizations.values()
                    if item["fingerprint"] == code["fingerprint"]
                    and item["kind"] == code["kind"]
                    and item["product_ids"] == products
                    and item["permissions"] == permissions
                ),
                None,
            )
            if authorization is None:
                authorization = {
                    "id": f"authorization-{len(self.authorizations) + 1}",
                    "kind": code["kind"],
                    "shop_id": code["shop_id"] or "shop-a",
                    "product_ids": products[:],
                    "permissions": permissions[:],
                    "client_name": code["client_name"],
                    **(
                        {"agent_type": code["agent_type"]}
                        if code.get("agent_type") is not None
                        else {}
                    ),
                    "fingerprint": code["fingerprint"],
                    "expires": remote.time.time() + 86400,
                    "revision": 1,
                }
                self.authorizations[authorization["id"]] = authorization
                self.bindings[authorization["id"]] = []
        binding_list = self.bindings[authorization["id"]]
        for product in products:
            binding = next(
                (item for item in binding_list if item["product_id"] == product), None
            )
            if binding is None:
                device_id = f"device-{len(self.devices) + 1}"
                self.devices[device_id] = {
                    "device_id": device_id,
                    "product_id": product,
                    "public_key": code["public_key"],
                    "permissions": permissions[:],
                    "client_name": authorization["client_name"],
                    **(
                        {"agent_type": authorization["agent_type"]}
                        if authorization.get("agent_type") is not None
                        else {}
                    ),
                }
                binding = {
                    **self.devices[device_id],
                    "staff_id": "link-" + device_id,
                    "shop_id": authorization["shop_id"],
                    "fingerprint": code["fingerprint"],
                    "authorization_id": authorization["id"],
                    "already_authorized": True,
                }
                binding_list.append(binding)
            binding["permissions"] = permissions[:]
            self.devices[binding["device_id"]]["permissions"] = permissions[:]
            binding["client_name"] = authorization["client_name"]
            self.devices[binding["device_id"]]["client_name"] = authorization[
                "client_name"
            ]
            if authorization.get("agent_type") is not None:
                binding["agent_type"] = authorization["agent_type"]
                self.devices[binding["device_id"]]["agent_type"] = authorization[
                    "agent_type"
                ]
        code["claimed"] = authorization["id"]


@pytest.fixture
def scope_profile(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    return root / "cli.json"


@pytest.fixture
def scope_clock(monkeypatch):
    clock = DeviceClock()
    monkeypatch.setattr(remote, "time", clock)
    return clock


def command(merchant, profile, *argv):
    return remote.execute(
        arguments(*argv, profile=profile), transport=httpx.MockTransport(merchant)
    )


def product_login(merchant, profile, *, permissions=None, no_wait=False):
    argv = [
        "login",
        "--device-code",
        "--origin",
        ORIGIN,
        "--product",
        "product-a",
        "--client-name",
        "文档 Bot",
        "--agent-type",
        "Codex",
    ]
    if permissions:
        argv += ["--permissions", ",".join(permissions)]
    if no_wait:
        argv += ["--no-wait"]
    return command(merchant, profile, *argv)


def shop_login(merchant, profile, *, no_wait=False):
    argv = [
        "login",
        "--device-code",
        "--origin",
        ORIGIN,
        "--shop",
        "shop-a",
        "--pipelines-all",
        "--client-name",
        "店铺 Bot",
        "--agent-type",
        "Codex",
    ]
    if no_wait:
        argv += ["--no-wait"]
    return command(merchant, profile, *argv)


def test_active_product_scope_needs_no_management_link_and_keeps_effective_subset(
    scope_profile, scope_clock, capsys
):
    merchant = ScopeMerchant()
    merchant.effective_permissions = ["queue.view"]
    result = product_login(merchant, scope_profile)
    stored = json.loads(scope_profile.read_text())
    assert result["authorization"]["permissions"] == ["queue.view"]
    assert result["grant"]["permissions"] == ["queue.view"]
    assert result["grant"]["authorization_id"] == result["authorization"]["id"]
    assert stat.S_IMODE(scope_profile.stat().st_mode) == 0o600
    public_output = json.dumps(result) + capsys.readouterr().err
    for secret in (
        stored["authorizations"][0]["private_key"],
        stored["grants"][0]["access_token"],
        next(iter(merchant.scope_requests.values()))["challenge"],
    ):
        assert secret not in public_output
    assert all(call[1] != "/api/cli/authorize" for call in merchant.calls)


def test_shop_snapshot_grants_aggregate_and_future_product_requires_approval(
    scope_profile, scope_clock
):
    merchant = ScopeMerchant()
    first = shop_login(merchant, scope_profile)
    assert len(first["grants"]) == 2
    merchant.pipeline_products.append("product-c")
    assert len(command(merchant, scope_profile, "queues", "--all")["queues"]) == 2
    original = {
        item["product_id"]: (item["id"], item["link_id"]) for item in first["grants"]
    }
    approved = command(
        merchant,
        scope_profile,
        "authorize",
        "--authorization",
        first["authorization"]["id"],
        "--pipelines-all",
    )
    assert len(approved["grants"]) == 3
    assert all(
        (item["id"], item["link_id"]) == original[item["product_id"]]
        for item in approved["grants"]
        if item["product_id"] in original
    )


def test_upgrade_denial_and_pending_leave_old_grant_operational(
    scope_profile, scope_clock
):
    merchant = ScopeMerchant()
    first = product_login(merchant, scope_profile, permissions=["queue.view"])
    old = json.loads(scope_profile.read_text())["grants"][0]
    pending = command(
        merchant,
        scope_profile,
        "authorize",
        "--grant",
        first["grant"]["id"],
        "--permissions",
        ",".join(QUEUE),
        "--reason",
        "处理队列",
        "--no-wait",
    )
    assert pending["pending"]
    assert json.loads(scope_profile.read_text())["grants"][0] == old
    assert command(merchant, scope_profile, "queues", "--all")["ok"]
    merchant.states = ["denied"]
    with pytest.raises(remote.ManageError) as denied:
        command(
            merchant,
            scope_profile,
            "authorize",
            "--grant",
            first["grant"]["id"],
            "--permissions",
            ",".join(QUEUE),
            "--reason",
            "处理队列",
        )
    assert denied.value.code == "authorization_denied"
    assert json.loads(scope_profile.read_text())["grants"][0]["permissions"] == [
        "queue.view"
    ]


@pytest.mark.parametrize("delay", [0, 601])
def test_upgrade_session_response_loss_resumes_claim_without_another_approval(
    delay, scope_profile, scope_clock
):
    merchant = ScopeMerchant()
    first = product_login(merchant, scope_profile, permissions=["queue.view"])
    argv = [
        "authorize",
        "--authorization",
        first["authorization"]["id"],
        "--permissions",
        ",".join(QUEUE),
        "--reason",
        "制作文档",
    ]
    merchant.lost_response = "session"
    with pytest.raises(remote.ManageError):
        command(merchant, scope_profile, *argv)
    assert len(merchant.scope_requests) == 2
    stored = json.loads(scope_profile.read_text())
    assert (
        stored["authorizations"][0]["revision"] == 2
        and stored["device_requests"][0]["scope_claim"]
    )
    scope_clock.elapsed += delay
    resumed = command(merchant, scope_profile, *argv)
    assert resumed["ok"] and len(merchant.scope_requests) == 2
    assert resumed["grant"]["id"] == first["grant"]["id"]
    assert resumed["grant"]["link_id"] == first["grant"]["link_id"]


@pytest.mark.parametrize("stage", ["request", "claim", "session"])
def test_initial_scope_network_failure_preserves_key_and_actor(
    stage, scope_profile, scope_clock
):
    merchant = ScopeMerchant()
    merchant.lost_response = stage
    with pytest.raises(remote.ManageError):
        shop_login(merchant, scope_profile)
    saved_key = json.loads(scope_profile.read_text())["device_requests"][0][
        "private_key"
    ]
    result = shop_login(merchant, scope_profile)
    assert (
        result["ok"]
        and len(merchant.scope_requests) == 1
        and len(merchant.authorizations) == 1
    )
    assert all(
        item["private_key"] == saved_key
        for item in json.loads(scope_profile.read_text())["grants"]
    )


def test_scope_noop_upgrade_is_valid_and_permission_reduction_is_not(
    scope_profile, scope_clock
):
    merchant = ScopeMerchant()
    first = product_login(merchant, scope_profile)
    same = command(
        merchant,
        scope_profile,
        "authorize",
        "--authorization",
        first["authorization"]["id"],
    )
    assert same["authorization"]["revision"] == first["authorization"]["revision"]
    calls = len(merchant.calls)
    with pytest.raises(remote.ManageError):
        command(
            merchant,
            scope_profile,
            "authorize",
            "--authorization",
            first["authorization"]["id"],
            "--permissions",
            "queue.view",
        )
    assert len(merchant.calls) == calls


def test_legacy_upgrade_creates_separate_scope_and_keeps_original_actor(
    scope_profile, scope_clock, monkeypatch
):
    import io
    import sys

    merchant = ScopeMerchant()
    monkeypatch.setattr(sys, "stdin", io.StringIO(ORIGIN + "/staff#first"))
    legacy = command(merchant, scope_profile, "login", "--link-stdin")
    old = json.loads(scope_profile.read_text())["grants"][0]
    upgraded = command(
        merchant,
        scope_profile,
        "authorize",
        "--grant",
        legacy["grant"]["id"],
        "--permissions",
        ",".join([*QUEUE, "product.edit"]),
    )
    stored = json.loads(scope_profile.read_text())
    assert stored["grants"][0] == old
    assert (
        upgraded["grant"]["id"] != old["id"]
        and upgraded["grant"]["link_id"] != old["link_id"]
    )
    assert len(stored["grants"]) == 2


def test_pipeline_all_rejects_management_permissions_before_network(
    scope_profile, scope_clock
):
    merchant = ScopeMerchant()
    with pytest.raises(remote.ManageError):
        command(
            merchant,
            scope_profile,
            "login",
            "--device-code",
            "--origin",
            ORIGIN,
            "--shop",
            "shop-a",
            "--pipelines-all",
            "--permissions",
            "queue.view,product.edit",
        )
    assert not merchant.calls


def test_scopes_do_not_union_product_permissions(scope_profile, scope_clock):
    merchant = ScopeMerchant()
    product_login(merchant, scope_profile, permissions=["queue.view"])
    product_login(merchant, scope_profile, permissions=["product.edit"])
    stored = json.loads(scope_profile.read_text())
    with remote.ManageClient(stored, transport=httpx.MockTransport(merchant)) as client:
        with pytest.raises(remote.ManageError) as error:
            client.grant(
                product="product-a", permissions=("queue.view", "product.edit")
            )
    assert error.value.code == "no_scope"


def test_scope_logout_all_erases_pending_authorizations_and_recovery_keys(
    scope_profile, scope_clock
):
    merchant = ScopeMerchant()
    shop_login(merchant, scope_profile)
    product_login(merchant, scope_profile, no_wait=True)
    assert command(merchant, scope_profile, "logout", "--all")["ok"]
    stored = json.loads(scope_profile.read_text())
    assert (
        stored["grants"] == []
        and stored["authorizations"] == []
        and stored["device_requests"] == []
    )
    assert stored["scoped_device_keys"] == {}


def test_scope_logout_one_grant_cancels_only_its_upgrade_and_preserves_other_scope(
    scope_profile, scope_clock
):
    merchant = ScopeMerchant()
    single = product_login(merchant, scope_profile, permissions=["queue.view"])
    grouped = shop_login(merchant, scope_profile)
    command(
        merchant,
        scope_profile,
        "authorize",
        "--grant",
        single["grant"]["id"],
        "--permissions",
        ",".join(QUEUE),
        "--no-wait",
    )
    assert command(merchant, scope_profile, "logout", "--grant", single["grant"]["id"])[
        "ok"
    ]
    stored = json.loads(scope_profile.read_text())
    assert stored["device_requests"] == []
    assert {item["id"] for item in stored["authorizations"]} == {
        grouped["authorization"]["id"]
    }
    assert {item["id"] for item in stored["grants"]} == {
        item["id"] for item in grouped["grants"]
    }
    assert {
        item.get("authorization_id") for item in stored["scoped_device_keys"].values()
    } == {grouped["authorization"]["id"]}
    assert command(merchant, scope_profile, "queues", "--all")["ok"]


def test_scope_logout_product_clears_unclaimed_keys_without_references(
    scope_profile, scope_clock
):
    merchant = ScopeMerchant()
    product_login(merchant, scope_profile, no_wait=True)
    command(merchant, scope_profile, "logout", "--product", "product-a")
    stored = json.loads(scope_profile.read_text())
    assert stored["device_requests"] == [] and stored["scoped_device_keys"] == {}


def test_scope_logout_keeps_an_unrelated_expired_claim_recovery_key(
    scope_profile, scope_clock
):
    merchant = ScopeMerchant()
    single = product_login(merchant, scope_profile)
    argv = [
        "login",
        "--device-code",
        "--origin",
        ORIGIN,
        "--product",
        "product-c",
        "--client-name",
        "Recovery Bot",
        "--agent-type",
        "Codex",
    ]
    merchant.lost_response = "claim"
    with pytest.raises(remote.ManageError):
        command(merchant, scope_profile, *argv)
    recovery_key = json.loads(scope_profile.read_text())["device_requests"][0][
        "private_key"
    ]
    scope_clock.elapsed += 601
    shop_login(merchant, scope_profile, no_wait=True)
    command(merchant, scope_profile, "logout", "--grant", single["grant"]["id"])
    stored = json.loads(scope_profile.read_text())
    assert recovery_key in {
        item["private_key"] for item in stored["scoped_device_keys"].values()
    }
    recovered = command(merchant, scope_profile, *argv)
    assert recovered["authorization"]["product_ids"] == ["product-c"]
    assert (
        len(
            [
                item
                for item in merchant.authorizations.values()
                if item["product_ids"] == ["product-c"]
            ]
        )
        == 1
    )


def test_invalid_scope_claim_changes_no_existing_profile_grants(
    scope_profile, scope_clock
):
    merchant = ScopeMerchant()
    first = product_login(merchant, scope_profile, permissions=["queue.view"])
    stored = json.loads(scope_profile.read_text())
    merchant.tamper_claim = lambda result: result["bindings"][0].update(
        device_id="replaced-actor"
    )
    with pytest.raises(remote.ManageError) as error:
        command(
            merchant,
            scope_profile,
            "authorize",
            "--authorization",
            first["authorization"]["id"],
            "--permissions",
            ",".join(QUEUE),
        )
    assert error.value.code == "invalid_response"
    assert json.loads(scope_profile.read_text())["grants"] == stored["grants"]


@pytest.fixture
def real_scope_api(owner, scope_profile, scope_clock, monkeypatch):
    import time

    from fastapi.testclient import TestClient

    from extore import pipeline_scopes, scope_auth
    from extore.app import app
    from extore.db import db

    origin = "http://localhost:8000"
    monkeypatch.setattr(scope_auth, "time", scope_clock)
    monkeypatch.setattr(pipeline_scopes, "time", scope_clock)
    # This isolated fixture owns its test session; no real MFA or credentials
    # are fabricated for a live server.
    with db() as c:
        c.execute("UPDATE sessions SET auth_at=?", (time.time(),))

    class API:
        def __init__(self, api):
            self.api = api
            self.drop_stage = None
            self.calls = []

        def transport(self, request):
            self.calls.append(request.url.path)
            if request.url.path.startswith("/api/cli/scopes/"):
                assert request.headers.get("Cookie") is None
                assert request.headers.get("Authorization") is None
                assert "token" not in json.loads(request.read())
            response = self.api.request(
                request.method,
                str(request.url),
                headers=dict(request.headers),
                content=request.read(),
            )
            assert "set-cookie" not in response.headers
            stage = (
                "claim"
                if request.url.path == "/api/cli/scopes/claim"
                else "session"
                if request.url.path == "/api/cli/session"
                else None
            )
            if stage and self.drop_stage == stage and response.status_code == 200:
                self.drop_stage = None
                raise httpx.ReadError("lost real scope response", request=request)
            return httpx.Response(
                response.status_code,
                headers=dict(response.headers),
                content=response.content,
            )

        def command(self, *argv):
            return remote.execute(
                arguments(*argv, profile=scope_profile),
                transport=httpx.MockTransport(self.transport),
            )

        def create_product(self, name):
            response = owner.post(
                "/api/admin/products",
                json={
                    "name": name,
                    "mode": "manual",
                    "delivery": "service",
                    "parameters": [],
                },
            )
            assert response.status_code == 200, response.text
            return response.json()["id"]

        def login(self, *, product=None, shop=None, permissions=None, no_wait=False):
            argv = [
                "login",
                "--device-code",
                "--origin",
                origin,
                "--client-name",
                "Actual pipeline Bot",
                "--agent-type",
                "Codex",
            ]
            argv += (
                ["--product", product]
                if product
                else ["--shop", shop, "--pipelines-all"]
            )
            if permissions:
                argv += ["--permissions", ",".join(permissions)]
            if no_wait:
                argv += ["--no-wait"]
            return self.command(*argv)

        def approve(self, pending, permissions=None):
            body = {"user_code": pending["authorization"]["user_code"]}
            if permissions is not None:
                body["permissions"] = permissions
            review = owner.post("/api/manage/device/options", json=body)
            assert review.status_code == 200, review.text
            value = review.json()
            assert value["flow"] == "scope"
            selected = value["selected"]
            approved = owner.post(
                "/api/manage/device/approve",
                json={
                    "user_code": body["user_code"],
                    "product_ids": selected["product_ids"],
                    "permissions": selected["permissions"],
                    "expires": selected["expires"],
                    "review_digest": value["review_digest"],
                },
            )
            assert approved.status_code == 200, approved.text
            return value

    with TestClient(app, base_url=origin) as api:
        yield API(api)


def test_real_scope_cli_shop_snapshot_upgrade_and_inflight_identity(
    real_scope_api, owner, scope_profile
):
    from extore.db import db

    api = real_scope_api
    first_product = api.create_product("Current document pipeline")
    second_product = api.create_product("Current presentation pipeline")
    with db() as c:
        shop_id = c.execute(
            "SELECT shop_id FROM products WHERE id=?", (first_product,)
        ).fetchone()[0]
        assert c.execute("SELECT count(*) FROM staff").fetchone()[0] == 0
    pending = api.login(shop=shop_id, no_wait=True)
    api.approve(pending, permissions=["queue.view"])
    first = api.login(shop=shop_id)
    assert first["authorization"]["permissions"] == ["queue.view"]
    assert set(first["authorization"]["product_ids"]) == {first_product, second_product}
    old_ids = {
        item["product_id"]: (item["id"], item["link_id"]) for item in first["grants"]
    }
    added = api.create_product("Future product needs approval")
    assert len(api.command("queues", "--all")["queues"]) == 2
    argv = [
        "authorize",
        "--authorization",
        first["authorization"]["id"],
        "--product",
        added,
        "--permissions",
        ",".join(QUEUE),
        "--reason",
        "接管新队列",
    ]
    pending = api.command(*argv, "--no-wait")
    assert len(json.loads(scope_profile.read_text())["grants"]) == 2
    api.approve(pending)
    upgraded = api.command(*argv)
    assert len(upgraded["grants"]) == 3
    assert all(
        (item["id"], item["link_id"]) == old_ids[item["product_id"]]
        for item in upgraded["grants"]
        if item["product_id"] in old_ids
    )
    assert len(api.command("queues", "--all")["queues"]) == 3

    stock = owner.post(
        "/api/admin/cards", json={"product_id": first_product, "count": 1}
    ).json()["codes"][0]
    receipt = owner.post("/api/exchange", json={"code": stock}).json()["token"]
    job = owner.post("/api/redeem", json={"token": receipt, "params": {}}).json()["id"]
    old_grant = next(
        item for item in upgraded["grants"] if item["product_id"] == first_product
    )
    assert api.command(
        "claim", job, "--product", first_product, "--grant", old_grant["id"]
    )["ok"]
    management_permissions = [*QUEUE, "product.edit"]
    independent_pending = api.login(
        product=first_product, permissions=management_permissions, no_wait=True
    )
    api.approve(independent_pending)
    independent = api.login(product=first_product, permissions=management_permissions)
    assert independent["grant"]["id"] != old_grant["id"]
    with pytest.raises(remote.ManageError):
        api.command(
            "complete",
            job,
            "--product",
            first_product,
            "--grant",
            independent["grant"]["id"],
        )
    assert api.command(
        "complete", job, "--product", first_product, "--grant", old_grant["id"]
    )["ok"]
    assert (
        owner.post("/api/receipt", json={"token": receipt}).json()["job"]["state"]
        == "succeeded"
    )
    with db() as c:
        assert (
            c.execute("SELECT count(*) FROM pipeline_authorizations").fetchone()[0] == 2
        )
        assert c.execute("SELECT count(*) FROM pipeline_bindings").fetchone()[0] == 4


@pytest.mark.parametrize(
    "stage,delay", [("session", 0), ("session", 601), ("claim", 601)]
)
def test_real_scope_upgrade_lost_response_preserves_actor_and_authorization(
    stage, delay, real_scope_api, scope_profile, scope_clock
):
    from extore.db import db

    api = real_scope_api
    product = api.create_product("Interrupted active approval")
    pending = api.login(product=product, permissions=["queue.view"], no_wait=True)
    api.approve(pending)
    first = api.login(product=product, permissions=["queue.view"])
    argv = [
        "authorize",
        "--authorization",
        first["authorization"]["id"],
        "--permissions",
        ",".join(QUEUE),
        "--reason",
        "开始处理",
    ]
    pending = api.command(*argv, "--no-wait")
    api.approve(pending)
    api.drop_stage = stage
    with pytest.raises(remote.ManageError):
        api.command(*argv)
    scope_clock.elapsed += delay
    if stage == "claim":
        recovery = api.command(*argv, "--no-wait")
        assert recovery["pending"]
        api.approve(recovery)
    resumed = api.command(*argv)
    assert resumed["authorization"]["id"] == first["authorization"]["id"]
    assert resumed["grant"]["id"] == first["grant"]["id"]
    assert resumed["grant"]["link_id"] == first["grant"]["link_id"]
    assert resumed["grant"]["permissions"] == QUEUE
    with db() as c:
        assert (
            c.execute("SELECT count(*) FROM pipeline_authorizations").fetchone()[0] == 1
        )
        assert c.execute("SELECT count(*) FROM cli_devices").fetchone()[0] == 1
        assert c.execute("SELECT cli_uses FROM staff").fetchone()[0] == 1
