import json
import os
import sqlite3

import pytest

from extore import secret_store


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setattr(secret_store, "DATA", tmp_path)
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    secret_store.init_schema(c)
    yield c, tmp_path
    c.close()


def context(**values):
    return {
        "tenant_id": "shop-a",
        "resource_type": "payment-account",
        "resource_id": "account-a",
        **values,
    }


def test_ciphertext_is_authenticated_tenant_and_resource_scoped(vault):
    data = {"api_key": "SECRET-CREDENTIAL", "account": "merchant-private"}
    sealed = secret_store.store_secret(data, **context())
    assert all(value not in sealed for value in data.values())
    assert secret_store.open_secret(sealed, **context()) == data
    for change in (
        {"tenant_id": "shop-b"},
        {"tenant_id": None},
        {"resource_type": "smtp"},
        {"resource_id": "account-b"},
    ):
        with pytest.raises(
            secret_store.SecretStoreError, match="authentication failed"
        ):
            secret_store.open_secret(sealed, **context(**change))


def test_nonces_are_random_and_missing_key_never_recreated(vault):
    c, path = vault
    a = secret_store.store_secret("private", **context())
    b = secret_store.store_secret("private", **context())
    assert a != b
    (path / secret_store.KEY_FILE).unlink()
    with pytest.raises(secret_store.SecretStoreError):
        secret_store.init_schema(c)
    with pytest.raises(secret_store.SecretStoreError):
        secret_store.store_secret("private", **context())
    assert not (path / secret_store.KEY_FILE).exists()


def test_changed_key_or_insecure_permissions_fail_closed(vault):
    c, path = vault
    key = path / secret_store.KEY_FILE
    assert key.stat().st_mode & 0o777 == 0o600
    key.chmod(0o644)
    with pytest.raises(secret_store.SecretStoreError, match="permissions"):
        secret_store.init_schema(c)
    key.chmod(0o600)
    key.write_bytes(os.urandom(32))
    with pytest.raises(secret_store.SecretStoreError, match="does not match"):
        secret_store.init_schema(c)


def test_malformed_ciphertext_and_invalid_values_never_echo_values(vault):
    for malformed in ("PRIVATE", "v1.!PRIVATE", "v1.A", "v2.PRIVATE", None):
        with pytest.raises(secret_store.SecretStoreError) as caught:
            secret_store.open_secret(malformed, **context())
        assert "PRIVATE" not in str(caught.value)
    for value in (
        [],
        {"invalid": float("nan")},
        {"nested": object()},
        "x" * secret_store.MAX_SECRET_BYTES,
    ):
        with pytest.raises(secret_store.SecretStoreError):
            secret_store.store_secret(value, **context())
    with pytest.raises(secret_store.SecretStoreError):
        secret_store.store_secret({}, **context(resource_id=""))


def test_keyless_existing_ciphertexts_without_marker_do_not_rotate(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(secret_store, "DATA", tmp_path)
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE processor_profile_revisions(ciphertext TEXT)")
    c.execute("INSERT INTO processor_profile_revisions VALUES ('v1.EXISTING')")
    with pytest.raises(secret_store.SecretStoreError, match="original encryption key"):
        secret_store.init_schema(c)
    assert not (tmp_path / secret_store.KEY_FILE).exists()
    c.close()


def test_ciphertext_tamper_and_wrong_version_are_rejected(vault):
    sealed = secret_store.store_secret({"value": "private"}, **context())
    changed = sealed[:-2] + ("AA" if sealed[-2:] != "AA" else "BB")
    with pytest.raises(secret_store.SecretStoreError):
        secret_store.open_secret(changed, **context())
    assert "private" not in json.dumps({"ciphertext": sealed})
