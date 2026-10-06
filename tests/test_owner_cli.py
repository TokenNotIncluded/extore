import argparse
import hashlib
import json
import stat
import time

import httpx
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from extore import owner_client as remote
from extore.manage_client import ManageError, _unb64

ORIGIN = "https://merchant.example"


def arguments(profile, *argv):
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    remote.add_parser(commands)
    return parser.parse_args(["admin", "--profile", str(profile), *argv])


class OwnerMerchant:
    """Protocol peer that verifies actual key signatures and raw action bodies."""

    def __init__(self):
        self.public = None
        self.nonce = None
        self.approved = False
        self.calls = []
        self.actions = {}
        self.action_index = 0
        self.fail_request_response = False
        self.fail_claim_response = False
        self.reject_write = False
        self.product = {
            "id": "product-a",
            "name": "商品",
            "mode": "manual",
            "delivery": "content",
            "webhook_secret": "real-connector-secret",
            "processor_config": {"secret": "real-processor-secret"},
            "parameters": [],
            "outputs": [],
            "variants": [],
        }
        self.jobs = []
        self.updated = None

    def verify(self, data, *proof):
        Ed25519PublicKey.from_public_bytes(_unb64(data["public_key"])).verify(
            _unb64(data["signature"]), "\n".join(proof).encode()
        )

    def __call__(self, request):
        raw = request.read()
        path = request.url.path
        body = (
            json.loads(raw)
            if raw and "application/json" in request.headers.get("content-type", "")
            else {}
        )
        self.calls.append((request.method, path, raw, dict(request.headers)))
        assert "cookie" not in request.headers
        prefix = remote.OWNER_PREFIX
        if path == prefix + "/request":
            assert "authorization" not in request.headers
            self.verify(
                body,
                "extore-cli-owner-request-v1",
                ORIGIN,
                body["public_key"],
                body["client_name"],
                body["nonce"],
            )
            if self.public is not None:
                assert self.public == body["public_key"]
                assert self.nonce == body["nonce"]
            self.public, self.nonce = body["public_key"], body["nonce"]
            if self.fail_request_response:
                self.fail_request_response = False
                raise httpx.ReadError("response lost", request=request)
            return httpx.Response(
                200,
                json={
                    "request_id": "request-1",
                    "device_code": "ABCD EFGH",
                    "approval_url": ORIGIN + "/cli/owner#request-1",
                    "challenge": "pairing-challenge",
                    "expires": time.time() + 600,
                    "fingerprint": "public-fingerprint",
                },
                headers={"Set-Cookie": "extore_session=browser-secret; Path=/"},
            )
        if path == prefix + "/status" and request.method == "POST":
            assert "authorization" not in request.headers
            self.verify(
                body, "extore-cli-owner-status-v1", ORIGIN, "request-1", self.public
            )
            return httpx.Response(
                200,
                json={
                    "status": "approved" if self.approved else "pending",
                    "expires": time.time() + 600,
                    **(
                        {
                            "role": "admin",
                            "scope": "shop.owner",
                            "shop_id": None,
                            "superadmin": True,
                        }
                        if self.approved
                        else {}
                    ),
                },
            )
        if path == prefix + "/claim":
            assert self.approved
            self.verify(
                body,
                "extore-cli-owner-claim-v1",
                ORIGIN,
                "request-1",
                "pairing-challenge",
                self.public,
            )
            if self.fail_claim_response:
                self.fail_claim_response = False
                raise httpx.ReadError("claim response lost", request=request)
            return httpx.Response(
                200,
                json={
                    "device_id": "owner-device",
                    "role": "admin",
                    "scope": "shop.owner",
                    "shop_id": None,
                    "superadmin": True,
                    "client_name": "Owner bot",
                    "fingerprint": "public-fingerprint",
                    "expires": time.time() + 30 * 86400,
                },
            )
        if path == prefix + "/challenge":
            assert "authorization" not in request.headers
            assert body["device_id"] == "owner-device"
            return httpx.Response(
                200,
                json={
                    "challenge_id": "session-challenge",
                    "challenge": "random-session-challenge",
                },
            )
        if path == prefix + "/session" and request.method == "POST":
            assert "authorization" not in request.headers
            self.verify(
                {**body, "public_key": self.public},
                "extore-cli-owner-session-v1",
                ORIGIN,
                "owner-device",
                "session-challenge",
                "random-session-challenge",
            )
            return httpx.Response(
                200,
                json={
                    "device_id": "owner-device",
                    "session_id": "safe-session-id",
                    "access_token": "short-lived-owner-secret",
                    "role": "admin",
                    "scope": "shop.owner",
                    "shop_id": None,
                    "superadmin": True,
                    "expires": time.time() + 28800,
                },
            )
        if path == "/api/upload-limits":
            assert "authorization" not in request.headers
            return httpx.Response(200, json={"max_file_bytes": 20 * 1024 * 1024})
        assert request.headers["authorization"] == "Bearer short-lived-owner-secret"
        if path == prefix + "/action-challenge":
            assert "x-extore-cli-signature" not in request.headers
            self.action_index += 1
            challenge_id = "action-" + str(self.action_index)
            self.actions[challenge_id] = body
            return httpx.Response(
                200,
                json={
                    "challenge_id": challenge_id,
                    "challenge": "one-use-action-challenge",
                },
            )
        if (
            request.method in remote.WRITE_METHODS
            and "multipart/form-data" not in request.headers.get("content-type", "")
        ):
            challenge_id = request.headers.get("X-Extore-CLI-Challenge")
            if not challenge_id or challenge_id not in self.actions:
                return httpx.Response(401)
            action = self.actions.pop(challenge_id)
            body_hash = hashlib.sha256(raw).hexdigest()
            assert action == {
                "method": request.method,
                "path": request.url.raw_path.decode(),
                "body_sha256": body_hash,
            }
            try:
                self.verify(
                    {
                        "public_key": self.public,
                        "signature": request.headers["X-Extore-CLI-Signature"],
                    },
                    "extore-cli-owner-action-v1",
                    ORIGIN,
                    "owner-device",
                    "safe-session-id",
                    challenge_id,
                    "one-use-action-challenge",
                    request.method,
                    request.url.raw_path.decode(),
                    body_hash,
                )
            except InvalidSignature:
                return httpx.Response(401)
            if self.reject_write:
                return httpx.Response(401)
        if path == prefix + "/status":
            return httpx.Response(
                200,
                json={
                    "device_id": "owner-device",
                    "role": "admin",
                    "scope": "shop.owner",
                    "shop_id": None,
                    "superadmin": True,
                },
            )
        if path == "/api/admin/products":
            if request.method == "POST":
                return httpx.Response(200, json={"id": "new-product", **body})
            return httpx.Response(200, json=[self.product])
        if path == "/api/admin/products/product-a":
            self.updated = body
            return httpx.Response(200, json={"id": "product-a", **body})
        if path == "/api/admin/products/quick":
            return httpx.Response(
                200,
                json={
                    "product": self.product,
                    "management_link": {"url": ORIGIN + "/staff#new-secret"},
                },
            )
        if (
            path in ("/api/manage/cards", "/api/admin/cards")
            and request.method == "POST"
        ):
            return httpx.Response(
                200, json={"codes": ["VERY-PRIVATE-CARD-CODE"], "batch_id": "batch-a"}
            )
        if (
            path in ("/api/admin/staff", "/api/manage/links")
            and request.method == "POST"
        ):
            return httpx.Response(
                200,
                json={
                    "id": "link-a",
                    "product_id": "product-a",
                    "url": ORIGIN + "/staff#private-link",
                },
            )
        if path == "/api/manage/jobs":
            return httpx.Response(200, json=self.jobs)
        if path == "/api/manage/products":
            return httpx.Response(200, json=[self.product])
        if path == "/api/manage/product":
            assert request.url.params["product_id"] == "product-a"
            return httpx.Response(200, json=self.product)
        if path == "/api/manage/files":
            return httpx.Response(200, json=[])
        if path == "/api/upload-limits":
            return httpx.Response(200, json={"max_file_bytes": 20 * 1024 * 1024})
        if path == "/api/manage/files/upload":
            assert b"attachment-content" in raw
            assert "x-extore-cli-signature" not in request.headers
            return httpx.Response(
                200, json={"id": "file-id", "job_id": "job-a", "field_key": "document"}
            )
        if path == "/api/admin/storage":
            return httpx.Response(200, json={"stored_bytes": 0, "limit_bytes": 1024})
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"ok": True})


@pytest.fixture
def merchant():
    return OwnerMerchant()


def execute(profile, merchant, *argv):
    return remote.execute(
        arguments(profile, *argv), transport=httpx.MockTransport(merchant)
    )


def authorize(profile, merchant):
    pending = execute(
        profile, merchant, "login", "--origin", ORIGIN, "--client-name", "Owner bot"
    )
    assert pending["status"] == "pending"
    merchant.approved = True
    result = execute(profile, merchant, "login-status")
    assert result["status"] == "authorized"
    return result


def test_initial_request_key_persisted_before_request_and_output_safe(
    tmp_path, merchant
):
    profile = tmp_path / "private" / "owner.json"

    def observe(request):
        assert profile.exists()
        saved = json.loads(profile.read_text())
        assert saved["owners"][0]["private_key"]
        return merchant(request)

    result = remote.execute(
        arguments(profile, "login", "--origin", ORIGIN),
        transport=httpx.MockTransport(observe),
    )
    assert result["status"] == "pending"
    assert result["device_code"] == "ABCD EFGH"
    serialized = json.dumps(result)
    saved = json.loads(profile.read_text())["owners"][0]
    assert saved["private_key"] not in serialized
    assert saved["nonce"] not in serialized
    assert "pairing-challenge" not in serialized
    assert stat.S_IMODE(profile.stat().st_mode) == 0o600
    assert stat.S_IMODE(profile.parent.stat().st_mode) == 0o700
    assert all("authorization" not in call[3] for call in merchant.calls)


@pytest.mark.parametrize("stage", ("request", "claim"))
def test_interrupted_binding_reuses_persisted_key(tmp_path, merchant, stage):
    profile = tmp_path / "private" / "owner.json"
    if stage == "request":
        merchant.fail_request_response = True
        command = ("login", "--origin", ORIGIN)
    else:
        execute(profile, merchant, "login", "--origin", ORIGIN)
        merchant.approved = True
        merchant.fail_claim_response = True
        command = ("login-status",)
    with pytest.raises(ManageError, match="connect"):
        execute(profile, merchant, *command)
    initial_key = json.loads(profile.read_text())["owners"][0]["private_key"]
    recovered = execute(profile, merchant, *command)
    assert json.loads(profile.read_text())["owners"][0]["private_key"] == initial_key
    assert recovered["status"] == ("pending" if stage == "request" else "authorized")


def test_session_and_signed_non_ascii_config_patch_preserve_secrets(tmp_path, merchant):
    profile = tmp_path / "private" / "owner.json"
    authorize(profile, merchant)
    patch = tmp_path / "patch.json"
    patch.write_text(json.dumps({"name": "改进 · 中文商品"}, ensure_ascii=False))
    result = execute(
        profile,
        merchant,
        "product",
        "update",
        "--product",
        "product-a",
        "--json-file",
        str(patch),
    )
    assert merchant.updated["name"] == "改进 · 中文商品"
    assert merchant.updated["webhook_secret"] == "real-connector-secret"
    assert merchant.updated["processor_config"] == {"secret": "real-processor-secret"}
    assert "real-connector-secret" not in json.dumps(result)
    writes = [
        call for call in merchant.calls if call[1] == "/api/admin/products/product-a"
    ]
    assert len(writes) == 1
    assert b"\xe4\xb8\xad\xe6\x96\x87" in writes[0][2]
    assert writes[0][3]["x-extore-cli-signature"]


def test_write_401_is_not_automatically_replayed(tmp_path, merchant):
    profile = tmp_path / "private" / "owner.json"
    authorize(profile, merchant)
    merchant.reject_write = True
    with pytest.raises(ManageError) as error:
        execute(
            profile, merchant, "cards", "revoke", "card-a", "--product", "product-a"
        )
    assert error.value.status == 401
    writes = [call for call in merchant.calls if call[1].endswith("card-a/revoke")]
    assert len(writes) == 1


def test_invalid_owner_device_can_request_fresh_passkey_binding(tmp_path, merchant):
    profile = tmp_path / "private" / "owner.json"
    authorize(profile, merchant)
    previous = json.loads(profile.read_text())["owners"][0]["private_key"]
    invalid = True

    def unavailable(request):
        nonlocal invalid
        if invalid and request.url.path in (
            remote.OWNER_PREFIX + "/challenge",
            remote.OWNER_PREFIX + "/status",
        ):
            return httpx.Response(401)
        if request.url.path == remote.OWNER_PREFIX + "/request":
            invalid = False
            merchant.public = merchant.nonce = None
            merchant.approved = False
        return merchant(request)

    result = remote.execute(
        arguments(profile, "login", "--origin", ORIGIN),
        transport=httpx.MockTransport(unavailable),
    )
    assert result["status"] == "pending"
    saved = json.loads(profile.read_text())["owners"][0]
    assert saved["private_key"] != previous
    assert "device_id" not in saved
    assert "access_token" not in saved


def test_owner_network_failure_does_not_discard_authorized_device_key(
    tmp_path, merchant
):
    profile = tmp_path / "private" / "owner.json"
    authorize(profile, merchant)
    previous = json.loads(profile.read_text())["owners"][0]["private_key"]

    def offline(request):
        raise httpx.ConnectError("offline", request=request)

    with pytest.raises(ManageError, match="connect"):
        remote.execute(
            arguments(profile, "login", "--origin", ORIGIN),
            transport=httpx.MockTransport(offline),
        )
    assert json.loads(profile.read_text())["owners"][0]["private_key"] == previous


def test_secret_card_and_management_link_results_go_to_private_files(
    tmp_path, merchant
):
    profile = tmp_path / "private" / "owner.json"
    authorize(profile, merchant)
    cards = execute(
        profile, merchant, "cards", "issue", "--product", "product-a", "--count", "1"
    )
    assert "VERY-PRIVATE-CARD-CODE" not in json.dumps(cards)
    output = remote.Path(cards["output"])
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert json.loads(output.read_text())["codes"] == ["VERY-PRIVATE-CARD-CODE"]
    link_data = tmp_path / "link.json"
    link_data.write_text(json.dumps({"name": "Bot", "permissions": ["queue.view"]}))
    links = execute(
        profile,
        merchant,
        "links",
        "create",
        "--product",
        "product-a",
        "--json-file",
        str(link_data),
    )
    assert "private-link" not in json.dumps(links)
    assert "/staff#private-link" in remote.Path(links["output"]).read_text()


def test_existing_export_path_fails_before_mutation(tmp_path, merchant):
    profile = tmp_path / "private" / "owner.json"
    authorize(profile, merchant)
    output = tmp_path / "existing.json"
    output.write_text("retain")
    before = len(merchant.calls)
    with pytest.raises(ManageError, match="already exists"):
        execute(
            profile,
            merchant,
            "cards",
            "issue",
            "--product",
            "product-a",
            "--output",
            str(output),
        )
    assert output.read_text() == "retain"
    assert not any(call[0] == "POST" for call in merchant.calls[before:])


def test_generic_api_has_exact_query_and_body_signature(tmp_path, merchant):
    profile = tmp_path / "private" / "owner.json"
    authorize(profile, merchant)
    body = tmp_path / "action.json"
    body.write_text(
        json.dumps(
            {
                "product_id": "product-a",
                "ids": ["job-a"],
                "action": "progress",
                "message": "中文",
            },
            ensure_ascii=False,
        )
    )
    result = execute(
        profile,
        merchant,
        "api",
        "POST",
        "/api/manage/batch",
        "--product",
        "product-a",
        "--query",
        "extra=with space",
        "--json-file",
        str(body),
    )
    assert result["ok"]
    action = next(call for call in merchant.calls if call[1] == "/api/manage/batch")
    assert action[3]["x-extore-cli-signature"]


@pytest.mark.parametrize(
    "path",
    (
        "https://evil.example/api/admin/products",
        "//evil.example/api/admin/products",
        "/api/admin/products?x=1",
        "/api/admin/../products",
        "/api/auth/password",
        "/api/admin/products%2Fa",
    ),
)
def test_generic_api_rejects_foreign_or_unrecognized_paths(tmp_path, merchant, path):
    profile = tmp_path / "private" / "owner.json"
    authorize(profile, merchant)
    before = len(merchant.calls)
    with pytest.raises(ManageError, match="relative owner API"):
        execute(profile, merchant, "api", "POST", path, "--product", "product-a")
    assert len(merchant.calls) == before


def test_owner_upload_is_draft_only_does_not_complete_task(tmp_path, merchant):
    profile = tmp_path / "private" / "owner.json"
    authorize(profile, merchant)
    merchant.jobs = [
        {
            "id": "job-a",
            "product_id": "product-a",
            "state": "processing",
            "progress": 75,
        }
    ]
    attachment = tmp_path / "document.txt"
    attachment.write_text("attachment-content")
    result = execute(
        profile,
        merchant,
        "upload",
        "job-a",
        "--product",
        "product-a",
        "--field",
        "document",
        "--file",
        str(attachment),
    )
    assert result["file"]["id"] == "file-id"
    assert not any(call[1] == "/api/manage/batch" for call in merchant.calls)
    assert merchant.jobs[0]["progress"] == 75


def test_owner_profile_rejects_symlinks_and_public_permissions(tmp_path, merchant):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    symlink = tmp_path / "linked"
    symlink.symlink_to(private, target_is_directory=True)
    with pytest.raises(ManageError, match="symlinks"):
        execute(symlink / "owner.json", merchant, "login", "--origin", ORIGIN)
    profile = private / "owner.json"
    profile.write_text('{"version":1,"grants":[],"owners":[]}')
    profile.chmod(0o644)
    with pytest.raises(ManageError, match="mode 600"):
        execute(profile, merchant, "login", "--origin", ORIGIN)
    assert not merchant.calls


def test_owner_read_outputs_redact_and_explicit_private_export_preserves_config(
    tmp_path, merchant
):
    profile = tmp_path / "private" / "owner.json"
    authorize(profile, merchant)
    result = execute(
        profile, merchant, "product", "get", "--product", "product-a", "--detail"
    )
    assert result["product"]["webhook_secret"] == "[redacted]"
    assert "real-processor-secret" not in json.dumps(result)
    output = tmp_path / "private-product.json"
    result = execute(
        profile,
        merchant,
        "product",
        "get",
        "--product",
        "product-a",
        "--output",
        str(output),
    )
    assert "real-connector-secret" not in json.dumps(result)
    assert json.loads(output.read_text())["webhook_secret"] == "real-connector-secret"


def test_reject_redacted_configuration_patch(tmp_path, merchant):
    profile = tmp_path / "private" / "owner.json"
    authorize(profile, merchant)
    patch = tmp_path / "patch.json"
    patch.write_text('{"webhook_secret":"[redacted]"}')
    with pytest.raises(ManageError, match="private file"):
        execute(
            profile,
            merchant,
            "product",
            "update",
            "--product",
            "product-a",
            "--json-file",
            str(patch),
        )
    assert merchant.updated is None


def test_whole_shop_queues_remain_product_scoped_and_compact(tmp_path, merchant):
    profile = tmp_path / "private" / "owner.json"
    authorize(profile, merchant)
    merchant.jobs = [
        {
            "id": "job-a",
            "product_id": "product-a",
            "state": "queued",
            "params": {"private": "large-customer-text"},
            "progress": 0,
        }
    ]
    result = execute(profile, merchant, "queues")
    assert result["queues"][0]["product_id"] == "product-a"
    assert "large-customer-text" not in json.dumps(result)
    assert result["view"] == "active"


def test_business_parser_covers_merchant_lifecycle_and_real_passkey_operations(
    tmp_path,
):
    profile = tmp_path / "owner.json"
    cases = (
        ("product", "list"),
        ("product", "create", "--json-file", "p.json"),
        ("product", "quick", "--json-file", "p.json"),
        ("product", "templates"),
        ("product", "schema", "--product", "p"),
        ("cards", "inventory", "--product", "p"),
        ("cards", "stats", "--product", "p"),
        ("cards", "history", "card", "--product", "p"),
        ("sessions", "list", "--product", "p"),
        ("devices", "revoke", "device", "--product", "p"),
        ("events", "retry", "event", "--product", "p"),
        ("audit", "--product", "p"),
        ("processors", "--product", "p"),
        ("owner-devices", "list"),
        ("passkeys", "list"),
        ("passkeys", "remove", "credential"),
        ("passkeys", "register-options"),
        ("passkeys", "register-verify", "--json-file", "actual-response.json"),
        ("storage",),
        (
            "request-changes",
            "job",
            "--product",
            "p",
            "--reason",
            "requirements incomplete",
        ),
        ("reject", "job", "--product", "p", "--reason", "outside capability"),
    )
    for case in cases:
        assert arguments(profile, *case).command == "admin"


def test_actual_owner_client_pairing_signed_business_and_draft_files(tmp_path, owner):
    """Real API middleware, WebAuthn verifier, signatures, storage and SQLite."""
    from fastapi.testclient import TestClient
    from test_passkeys import Authenticator, register

    from extore.app import app
    from extore.config import ORIGIN as actual_origin
    from extore.db import db

    authenticator = Authenticator()
    register(owner, authenticator, "Actual owner CLI approval test")
    profile = tmp_path / "private" / "owner.json"
    observed = []
    with TestClient(app, base_url=actual_origin) as api:

        def forward(request):
            raw = request.read()
            assert "cookie" not in request.headers
            observed.append((request.method, request.url.path))
            response = api.request(
                request.method,
                request.url.raw_path.decode(),
                content=raw,
                headers=dict(request.headers),
            )
            return httpx.Response(
                response.status_code, content=response.content, headers=response.headers
            )

        transport = httpx.MockTransport(forward)

        def command(*argv):
            return remote.execute(arguments(profile, *argv), transport=transport)

        pending = command(
            "login", "--origin", actual_origin, "--client-name", "Actual owner CLI"
        )
        assert pending["status"] == "pending"
        saved = json.loads(profile.read_text())["owners"][0]
        options = owner.post(
            "/api/auth/cli-owner/options",
            json={
                "request_id": saved["request_id"],
                "device_code": pending["device_code"],
            },
        )
        assert options.status_code == 200, options.text
        approved = owner.post(
            "/api/auth/cli-owner/verify",
            json={
                "request_id": saved["request_id"],
                "credential": authenticator.assertion(options.json()["options"]),
            },
        )
        assert approved.status_code == 200, approved.text
        assert command("login-status")["status"] == "authorized"
        owner_device_id = json.loads(profile.read_text())["owners"][0]["device_id"]

        def json_file(name, value):
            path = tmp_path / name
            path.write_text(json.dumps(value, ensure_ascii=False))
            return str(path)

        definition = {
            "name": "CLI 文件交付集成验证",
            "parameters": [
                {"key": "request", "label": {"zh-CN": "要求"}, "type": "textarea"}
            ],
            "outputs": [
                {"key": key, "label": {"zh-CN": key}, "type": "file"}
                for key in ("first", "second")
            ],
            "webhook_secret": "private-owner-integration-connector-secret",
        }
        first = command(
            "product",
            "create",
            "--json-file",
            json_file("first-product.json", definition),
        )["product"]["id"]
        second = command(
            "product",
            "create",
            "--json-file",
            json_file(
                "second-product.json",
                {
                    "name": "第二条服务流水线",
                    "delivery": "service",
                    "parameters": [],
                    "outputs": [],
                },
            ),
        )["product"]["id"]
        codes = []
        for product_id in (first, second):
            result = command("cards", "issue", "--product", product_id)
            exported = remote.Path(result["output"])
            assert stat.S_IMODE(exported.stat().st_mode) == 0o600
            code = json.loads(exported.read_text())["codes"][0]
            assert code not in json.dumps(result)
            codes.append(code)
        with TestClient(
            app, base_url=actual_origin, headers={"Origin": actual_origin}
        ) as customer:
            tasks = []
            for index, code in enumerate(codes):
                response = customer.post("/api/exchange", json={"code": code})
                assert response.status_code == 200, response.text
                response = customer.post(
                    "/api/redeem",
                    json={
                        "token": response.json()["token"],
                        "params": {"request": "请准备两份附件"} if index == 0 else {},
                    },
                )
                assert response.status_code == 200, response.text
                tasks.append(response.json()["id"])
        queues = command("queues")
        assert {queue["product_id"] for queue in queues["queues"]} == {first, second}
        assert [len(queue["jobs"]) for queue in queues["queues"]] == [1, 1]
        assert "请准备两份附件" not in json.dumps(queues)
        command("claim", tasks[0], "--product", first)
        command(
            "progress",
            tasks[0],
            "--product",
            first,
            "--progress",
            "75",
            "--message",
            "附件等待确认",
        )
        for field in ("first", "second"):
            source = tmp_path / (field + ".txt")
            source.write_text("Synthetic delivery attachment · " + field)
            file = command(
                "upload",
                tasks[0],
                "--product",
                first,
                "--field",
                field,
                "--file",
                str(source),
            )["file"]
            assert file["field_key"] == field
        job = command("job", tasks[0], "--product", first)["job"]
        assert job["state"] == "processing" and job["progress"] == 75
        assert len(job["files"]) == 2
        assert not job.get("content")
        assert ("POST", "/api/manage/files/upload") in observed
        exported_product = tmp_path / "private-full-product.json"
        result = command(
            "product", "get", "--product", first, "--output", str(exported_product)
        )
        assert "private-owner-integration-connector-secret" not in json.dumps(result)
        assert (
            json.loads(exported_product.read_text())["webhook_secret"]
            == "private-owner-integration-connector-secret"
        )
        assert stat.S_IMODE(exported_product.stat().st_mode) == 0o600
        assert len(command("passkeys", "list")["passkeys"]) == 1
        command("logout", "--origin", actual_origin)
        assert json.loads(profile.read_text())["owners"] == []
        with db() as c:
            assert (
                c.execute(
                    "SELECT revoked FROM owner_cli_devices WHERE id=?",
                    (owner_device_id,),
                ).fetchone()["revoked"]
                == 1
            )
            assert (
                c.execute(
                    "SELECT count(*) FROM sessions WHERE owner_device_id=? AND revoked=0",
                    (owner_device_id,),
                ).fetchone()[0]
                == 0
            )
