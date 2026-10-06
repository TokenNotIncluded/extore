"""Real CLI-to-API tenant boundaries and private credential workflows."""

import json
import stat
import time
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from test_owner_cli import arguments
from test_passkeys import Authenticator, register

from extore import owner_client as remote
from extore.account_auth import _seal, ph
from extore.app import app
from extore.config import ORIGIN
from extore.db import db
from extore.manage_client import ManageError
from extore.totp import _code, _secret_bytes, generate_backup_codes, new_secret


class Workspace:
    def __init__(self, api, tmp_path):
        self.api = api
        self.directory = tmp_path
        self.profile = tmp_path / "private" / "owner.json"
        self.index = 0
        self.paths = []
        self.override = None

        def forward(request):
            assert "cookie" not in request.headers
            self.paths.append((request.method, request.url.path))
            # The adapter must also avoid retaining a GUI cookie produced by a
            # password-change response; the production CLI clears its own jar.
            api.cookies.clear()
            response = api.request(
                request.method,
                request.url.raw_path.decode(),
                content=request.read(),
                headers=dict(request.headers),
            )
            payload = response.content
            if self.override:
                payload = self.override(request, response)
            return httpx.Response(
                response.status_code, content=payload, headers=response.headers
            )

        self.transport = httpx.MockTransport(forward)

    def file(self, value):
        self.index += 1
        path = self.directory / f"private-input-{self.index}.json"
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return str(path)

    def command(self, *argv):
        return remote.execute(arguments(self.profile, *argv), transport=self.transport)

    def login(self, email, password, **second_factor):
        result = self.command(
            "login",
            "--origin",
            ORIGIN,
            "--email",
            email,
            "--credentials-file",
            self.file({"password": password, **second_factor}),
        )
        assert result["status"] == "authorized"
        return result["owner"]

    def root(self, owner):
        authenticator = Authenticator()
        register(owner, authenticator, "Platform CLI test")
        pending = self.command("login", "--origin", ORIGIN)
        saved = next(
            item
            for item in json.loads(self.profile.read_text())["owners"]
            if not item.get("login_email")
        )
        options = owner.post(
            "/api/auth/cli-owner/options",
            json={
                "request_id": saved["request_id"],
                "device_code": pending["device_code"],
            },
        )
        assert options.status_code == 200
        verified = owner.post(
            "/api/auth/cli-owner/verify",
            json={
                "request_id": saved["request_id"],
                "credential": authenticator.assertion(options.json()["options"]),
            },
        )
        assert verified.status_code == 200
        result = self.command("login-status")
        assert result["owner"]["shop_id"] is None and result["owner"]["superadmin"]
        return result["owner"]


@pytest.fixture
def workspace(tmp_path):
    with TestClient(app, base_url=ORIGIN) as api:
        yield Workspace(api, tmp_path)


def seed_shop(email, *, mfa=False):
    sid, password = str(uuid.uuid4()), "synthetic-shop-password-1234"
    with db() as c:
        c.execute(
            "INSERT INTO shops(id,name,email,password_hash,created,verified) VALUES (?,?,?,?,?,1)",
            (sid, email.split("@")[0], email, ph.hash(password), time.time()),
        )
        if mfa:
            secret = new_secret()
            raw, hashes = generate_backup_codes()
            c.execute(
                "UPDATE shops SET totp_secret=?,totp_backup_digests=? WHERE id=?",
                (_seal(secret, sid), json.dumps(hashes), sid),
            )
        else:
            raw = []
    return sid, password, raw


def product(workspace, grant, name, **definition):
    return workspace.command(
        "product",
        "create",
        "--grant",
        grant,
        "--json-file",
        workspace.file({"name": name, "parameters": [], **definition}),
    )["product"]["id"]


def test_password_cli_keeps_two_shops_separate_and_cannot_become_platform_root(
    workspace,
):
    sid_a, password, _ = seed_shop("a@example.test")
    sid_b, _, _ = seed_shop("b@example.test")
    a = workspace.login("a@example.test", password)
    b = workspace.login("b@example.test", password)
    assert a["shop_id"] == sid_a and b["shop_id"] == sid_b
    assert not a["superadmin"] and not b["superadmin"]
    pa = product(workspace, a["device_id"], "Shop A product")
    pb = product(workspace, b["device_id"], "Shop B product")
    assert [
        p["id"]
        for p in workspace.command("products", "--grant", a["device_id"])["products"]
    ] == [pa]
    assert [
        p["id"]
        for p in workspace.command("products", "--grant", b["device_id"])["products"]
    ] == [pb]
    assert (
        workspace.command("products", "--grant", a["device_id"])["products"][0][
            "shop_id"
        ]
        == sid_a
    )
    with pytest.raises(ManageError) as crossing:
        workspace.command("product", "get", "--product", pb, "--grant", a["device_id"])
    assert crossing.value.status in (403, 404)
    with pytest.raises(ManageError, match="platform-root"):
        workspace.command("platform", "shops", "list", "--grant", a["device_id"])
    with pytest.raises(ManageError) as ambiguous:
        workspace.command("products")
    assert ambiguous.value.code == "ambiguous_scope"
    stored = workspace.profile.read_text()
    assert password not in stored and "approval_token" not in stored
    assert stat.S_IMODE(workspace.profile.stat().st_mode) == 0o600


def test_named_maintenance_previews_then_applies_only_the_selected_shops_records(
    workspace,
):
    sid, password, _ = seed_shop("cleanup-a@example.test")
    _, _, _ = seed_shop("cleanup-b@example.test")
    a = workspace.login("cleanup-a@example.test", password)
    b = workspace.login("cleanup-b@example.test", password)
    pa = product(workspace, a["device_id"], "Cleanup A")
    pb = product(workspace, b["device_id"], "Cleanup B")
    old_a, old_b, pending = (str(uuid.uuid4()) for _ in range(3))
    created = time.time() - 40 * 86400
    with db() as c:
        for eid, pid in ((old_a, pa), (old_b, pb), (pending, pa)):
            c.execute(
                "INSERT INTO events(id,type,product_id,payload,created) VALUES (?,?,?,?,?)",
                (eid, "redemption.requested", pid, "{}", created),
            )
        c.execute(
            "INSERT INTO outbox(id,url,secret,due) VALUES (?,?,?,?)",
            (pending, "https://example.test/callback", "synthetic-secret", created),
        )
    grant = a["device_id"]
    status = workspace.command("maintenance", "status", "--grant", grant)["result"]
    assert status["shop_id"] == sid and status["counts"]["events_pending"] == 1
    preview = workspace.command(
        "maintenance", "cleanup", "--area", "events", "--grant", grant
    )["result"]
    assert preview["dry_run"] is True and preview["eligible"]["events"] == 1
    assert preview["changed"]["events"] == 0
    with db() as c:
        assert c.execute("SELECT 1 FROM events WHERE id=?", (old_a,)).fetchone()
    applied = workspace.command(
        "maintenance", "cleanup", "--area", "events", "--apply", "--grant", grant
    )["result"]
    assert applied["dry_run"] is False and applied["changed"]["events"] == 1
    with db() as c:
        assert not c.execute("SELECT 1 FROM events WHERE id=?", (old_a,)).fetchone()
        assert c.execute("SELECT 1 FROM events WHERE id=?", (old_b,)).fetchone()
        assert c.execute("SELECT 1 FROM outbox WHERE id=?", (pending,)).fetchone()


def test_platform_named_commands_keep_smtp_private_and_restrict_merchant_accounts(
    workspace, owner
):
    root = workspace.root(owner)
    device = root["device_id"]
    configured = workspace.command(
        "platform",
        "settings",
        "update",
        "--grant",
        device,
        "--json-file",
        workspace.file(
            {
                "smtp": {
                    "enabled": True,
                    "host": "smtp.example.test",
                    "port": 465,
                    "mode": "ssl",
                    "sender": "store@example.test",
                    "username": "synthetic-smtp-user",
                    "password": "synthetic-private-smtp-password",
                },
                "registration_enabled": True,
            }
        ),
    )
    assert "synthetic-private-smtp-password" not in json.dumps(configured)
    assert workspace.command("platform", "settings", "get", "--grant", device)[
        "result"
    ]["registration_enabled"]
    created = workspace.command(
        "platform",
        "shops",
        "create",
        "--grant",
        device,
        "--json-file",
        workspace.file(
            {"name": "CLI invited merchant", "email": "invited@example.test"}
        ),
    )
    sid = created["result"]["shop"]["id"]
    assert "token" not in created["result"] and "invite_url" not in created["result"]
    assert workspace.command("platform", "shops", "invite", sid, "--grant", device)[
        "result"
    ]["ok"]
    quota = workspace.command(
        "platform", "shops", "quota", sid, "--grant", device, "--bytes", "104857600"
    )
    assert quota["result"]["storage_limit_bytes"] == 104857600
    assert not workspace.command(
        "platform", "shops", "disable", sid, "--grant", device
    )["result"]["enabled"]
    assert workspace.command("platform", "shops", "enable", sid, "--grant", device)[
        "result"
    ]["enabled"]
    with pytest.raises(ManageError, match="merchant device"):
        workspace.command("account", "get", "--grant", device)


def test_processor_profiles_never_export_vault_and_cannot_cross_shop(workspace):
    sid_a, password, _ = seed_shop("a@example.test")
    sid_b, _, _ = seed_shop("b@example.test")
    a, b = (
        workspace.login("a@example.test", password),
        workspace.login("b@example.test", password),
    )
    pa, pb = (
        product(
            workspace,
            a["device_id"],
            "A",
            mode="script",
            processor_id="personalized_text",
        ),
        product(
            workspace,
            b["device_id"],
            "B",
            mode="script",
            processor_id="personalized_text",
        ),
    )
    definition = {
        "name": "A private profile",
        "processor_id": "personalized_text",
        "configuration": {"template": "PRIVATE TEMPLATE $name"},
    }
    profile = workspace.command(
        "processor-profiles",
        "create",
        "--grant",
        a["device_id"],
        "--json-file",
        workspace.file(definition),
    )["result"]
    assert profile["shop_id"] == sid_a and "configuration" not in profile
    workspace.command(
        "processor-profiles",
        "bind",
        profile["id"],
        "--product",
        pa,
        "--grant",
        a["device_id"],
    )
    with pytest.raises(ManageError) as denied:
        workspace.command(
            "processor-profiles", "get", profile["id"], "--grant", b["device_id"]
        )
    assert denied.value.status in (403, 404)
    with pytest.raises(ManageError) as cross_bind:
        workspace.command(
            "processor-profiles",
            "bind",
            profile["id"],
            "--product",
            pb,
            "--grant",
            b["device_id"],
        )
    assert cross_bind.value.status in (403, 404)
    with pytest.raises(ManageError, match="different shop"):
        workspace.command(
            "processor-profiles", "list", "--shop", sid_b, "--grant", a["device_id"]
        )
    output = workspace.directory / "profile-metadata.json"
    exported = workspace.command(
        "api",
        "GET",
        "/api/admin/processor-profiles/" + profile["id"],
        "--grant",
        a["device_id"],
        "--output",
        str(output),
    )
    assert "PRIVATE TEMPLATE" not in json.dumps(exported) + output.read_text()
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_pure_cli_can_enable_totp_and_keep_seeds_recovery_codes_and_passwords_private(
    workspace, capsys
):
    sid, password, _ = seed_shop("mfa@example.test")
    bound = workspace.login("mfa@example.test", password)
    grant = bound["device_id"]
    setup = workspace.command(
        "totp",
        "setup",
        "--grant",
        grant,
        "--json-file",
        workspace.file({"password": password}),
    )
    secret_file = remote.Path(setup["output"])
    assert stat.S_IMODE(secret_file.stat().st_mode) == 0o600
    seed = json.loads(secret_file.read_text())["secret"]
    code = _code(_secret_bytes(seed), int(time.time() // 30))
    enabled = workspace.command(
        "totp",
        "confirm",
        "--grant",
        grant,
        "--json-file",
        workspace.file({"code": code}),
    )
    recovery_file = remote.Path(enabled["output"])
    codes = json.loads(recovery_file.read_text())["backup_codes"]
    assert codes and stat.S_IMODE(recovery_file.stat().st_mode) == 0o600
    assert workspace.command("account", "get", "--grant", grant)["result"][
        "totp_enabled"
    ]
    with db() as c:
        row = c.execute(
            "SELECT totp_secret,totp_backup_digests FROM shops WHERE id=?", (sid,)
        ).fetchone()
    assert seed not in row["totp_secret"] and codes[0] not in row["totp_backup_digests"]
    changed = workspace.command(
        "account",
        "password",
        "--grant",
        grant,
        "--json-file",
        workspace.file(
            {
                "password": password,
                "new_password": "synthetic-new-shop-password-5678",
                "backup_code": codes[0],
            }
        ),
    )
    assert changed["login_required"]
    assert json.loads(workspace.profile.read_text())["owners"] == []
    reauthenticated = workspace.login(
        "mfa@example.test", "synthetic-new-shop-password-5678", backup_code=codes[1]
    )
    assert reauthenticated["shop_id"] == sid and reauthenticated["device_id"] != grant
    public = (
        json.dumps([setup, enabled, changed, reauthenticated])
        + capsys.readouterr().out
        + capsys.readouterr().err
    )
    assert all(
        value not in public
        for value in [seed, *codes, password, "synthetic-new-shop-password-5678", code]
    )


@pytest.mark.parametrize("response_field", ["shop_id", "superadmin"])
def test_owner_client_rejects_scope_drift_and_missing_identity_before_business_action(
    workspace, response_field
):
    _, password, _ = seed_shop("a@example.test")
    bound = workspace.login("a@example.test", password)

    def missing_identity(request, response):
        if request.method == "GET" and request.url.path == "/api/cli/owner/status":
            body = response.json()
            body.pop(response_field)
            return json.dumps(body).encode()
        return response.content

    workspace.override = missing_identity
    with pytest.raises(ManageError) as malformed:
        workspace.command("account", "get", "--grant", bound["device_id"])
    assert malformed.value.code == "invalid_response"
    assert ("GET", "/api/shop/account") not in workspace.paths
