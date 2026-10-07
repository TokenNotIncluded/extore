"""Explicit, signed CLI identity and immutable approval recovery."""

import json

import httpx
import pytest
from test_manage_cli import DeviceClock, DeviceMerchant, MockMerchant, arguments
from test_pipeline_manage_cli import ScopeMerchant

from extore import manage_client as remote

ORIGIN = "https://merchant.example"


@pytest.fixture
def identity_profile(tmp_path, monkeypatch):
    monkeypatch.delenv("EXTORE_AGENT_NAME", raising=False)
    monkeypatch.delenv("EXTORE_AGENT_TYPE", raising=False)
    monkeypatch.setattr(remote, "time", DeviceClock())
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    return directory / "cli.json"


def command(merchant, profile, *argv):
    return remote.execute(
        arguments(*argv, profile=profile), transport=httpx.MockTransport(merchant)
    )


def login_args(*identity, scoped=False):
    target = ["--product", "product-a"] if scoped else ["--existing-link"]
    return ["login", "--device-code", "--origin", ORIGIN, *target, *identity]


IDENTITY = ["--client-name", "文档助手", "--agent-type", "Codex"]


@pytest.mark.parametrize(
    "name,agent_type", [(None, None), ("Bot", None), (None, "Codex")]
)
def test_new_private_binding_also_requires_declared_identity(name, agent_type):
    merchant = MockMerchant()
    data = {"version": 1, "grants": []}
    with remote.ManageClient(data, transport=httpx.MockTransport(merchant)) as client:
        with pytest.raises(remote.ManageError) as error:
            client.login(ORIGIN + "/staff#first", name, agent_type=agent_type)
    assert error.value.code == "invalid_input"
    assert data["grants"] == [] and merchant.calls == []


def test_private_binding_identity_is_signed_and_cannot_be_changed_silently():
    merchant = MockMerchant()
    data = {"version": 1, "grants": []}
    with remote.ManageClient(data, transport=httpx.MockTransport(merchant)) as client:
        first = client.login(ORIGIN + "/staff#first", "Bot", agent_type="Codex")
        assert first["grant"]["agent_type"] == "Codex"
        again = client.login(ORIGIN + "/staff#first", None)
        assert (
            again["already_authorized"] and again["grant"]["id"] == first["grant"]["id"]
        )
        before = json.loads(json.dumps(data))
        with pytest.raises(remote.ManageError) as error:
            client.login(ORIGIN + "/staff#first", "Different", agent_type="Codex")
        assert error.value.code == "identity_mismatch" and data == before


@pytest.mark.parametrize(
    "identity", [[], ["--client-name", "Bot"], ["--agent-type", "Codex"]]
)
def test_new_request_requires_both_identity_fields(identity, identity_profile):
    merchant = DeviceMerchant()
    with pytest.raises(remote.ManageError) as error:
        command(merchant, identity_profile, *login_args(*identity), "--no-wait")
    assert error.value.code == "invalid_input"
    assert merchant.calls == []
    stored = json.loads(identity_profile.read_text())
    assert not stored.get("device_keys") and not stored.get("device_requests")


@pytest.mark.parametrize("field,limit", [("client-name", 100), ("agent-type", 64)])
@pytest.mark.parametrize("bad", ["", "\nBot", "Bot\x7f", "Bot\u202e", "Bot\u200b"])
def test_invalid_identity_is_rejected_before_http(field, limit, bad, identity_profile):
    merchant = DeviceMerchant()
    identity = IDENTITY[:]
    identity[identity.index("--" + field) + 1] = bad
    with pytest.raises(remote.ManageError) as error:
        command(merchant, identity_profile, *login_args(*identity), "--no-wait")
    assert error.value.code == "invalid_input" and not merchant.calls
    with pytest.raises(remote.ManageError):
        remote._identity_value("x" * (limit + 1), label=field, limit=limit)


def test_environment_identity_and_flag_override(identity_profile, monkeypatch):
    monkeypatch.setenv("EXTORE_AGENT_NAME", "Environment Bot")
    monkeypatch.setenv("EXTORE_AGENT_TYPE", "Environment Type")
    merchant = DeviceMerchant()
    command(merchant, identity_profile, *login_args(*IDENTITY), "--no-wait")
    request = next(iter(merchant.requests.values()))
    assert (request["client_name"], request["agent_type"]) == ("文档助手", "Codex")
    other = identity_profile.parent / "environment.json"
    command(merchant, other, *login_args(), "--no-wait")
    request = list(merchant.requests.values())[-1]
    assert (request["client_name"], request["agent_type"]) == (
        "Environment Bot",
        "Environment Type",
    )


@pytest.mark.parametrize(
    "resume", [[], ["--client-name", "文档助手"], ["--agent-type", "Codex"]]
)
def test_pending_resumes_exact_identity_without_requiring_flags(
    resume, identity_profile
):
    merchant = DeviceMerchant()
    first = command(merchant, identity_profile, *login_args(*IDENTITY), "--no-wait")
    saved = json.loads(identity_profile.read_text())["device_requests"][0]
    result = command(merchant, identity_profile, *login_args(*resume), "--no-wait")
    assert result == first and len(merchant.requests) == 1
    assert json.loads(identity_profile.read_text())["device_requests"][0] == saved


@pytest.mark.parametrize(
    "changed", [["--client-name", "Other Bot"], ["--agent-type", "Other Type"]]
)
def test_changed_pending_identity_is_rejected(changed, identity_profile):
    merchant = DeviceMerchant()
    command(merchant, identity_profile, *login_args(*IDENTITY), "--no-wait")
    before = identity_profile.read_bytes()
    with pytest.raises(remote.ManageError) as error:
        command(merchant, identity_profile, *login_args(*changed), "--no-wait")
    assert error.value.code == "identity_mismatch"
    assert identity_profile.read_bytes() == before and len(merchant.requests) == 1


@pytest.mark.parametrize("scoped", [False, True])
def test_legacy_pending_omits_type_from_signed_request(scoped, identity_profile):
    merchant = ScopeMerchant() if scoped else DeviceMerchant()
    command(
        merchant, identity_profile, *login_args(*IDENTITY, scoped=scoped), "--no-wait"
    )
    stored = json.loads(identity_profile.read_text())
    pending = stored["device_requests"][0]
    pending.pop("agent_type")
    for key in (
        "request_id",
        "user_code",
        "approval_url",
        "challenge",
        "interval",
        "fingerprint",
    ):
        pending.pop(key)
    requests = merchant.scope_requests if scoped else merchant.requests
    requests.clear()
    remote._save_profile(identity_profile, stored)
    command(merchant, identity_profile, *login_args(scoped=scoped), "--no-wait")
    assert "agent_type" not in next(iter(requests.values()))


def test_identity_upgrade_keeps_old_grants_until_claim_and_resumes(identity_profile):
    merchant = ScopeMerchant()
    first = command(merchant, identity_profile, *login_args(*IDENTITY, scoped=True))
    before = json.loads(identity_profile.read_text())
    argv = ["authorize", "--authorization", first["authorization"]["id"]]
    command(
        merchant,
        identity_profile,
        *argv,
        "--client-name",
        "Renamed Bot",
        "--agent-type",
        "Other AI",
        "--no-wait",
    )
    pending = json.loads(identity_profile.read_text())
    assert pending["grants"] == before["grants"]
    assert pending["authorizations"] == before["authorizations"]
    result = command(merchant, identity_profile, *argv)
    assert result["authorization"]["revision"] == first["authorization"]["revision"] + 1
    assert result["authorization"]["client_name"] == "Renamed Bot"
    assert result["authorization"]["agent_type"] == "Other AI"
    assert result["grant"]["id"] == first["grant"]["id"]


@pytest.mark.parametrize(
    "field,value", [("client_name", "Wrong Bot"), ("agent_type", "Wrong AI")]
)
def test_claim_cannot_change_signed_identity(field, value, identity_profile):
    merchant = ScopeMerchant()
    merchant.tamper_claim = lambda result: result["authorization"].update(
        {field: value}
    )
    with pytest.raises(remote.ManageError) as error:
        command(merchant, identity_profile, *login_args(*IDENTITY, scoped=True))
    assert error.value.code == "invalid_response"
    assert json.loads(identity_profile.read_text())["grants"] == []
