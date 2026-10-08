import sqlite3

import pytest

from extore import secret_store


def test_issuance_recovery_ciphertext_cannot_bootstrap_a_new_lost_master_key(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(secret_store, "DATA", tmp_path)
    with sqlite3.connect(":memory:") as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE commerce_issuances (response_ciphertext TEXT)")
        connection.execute(
            "INSERT INTO commerce_issuances VALUES (?)", ("v1.synthetic-ciphertext",)
        )
        with pytest.raises(
            secret_store.SecretStoreError,
            match="Existing credentials require the original encryption key",
        ):
            secret_store.init_schema(connection)
        assert not (tmp_path / secret_store.KEY_FILE).exists()
        assert (
            connection.execute("SELECT COUNT(*) FROM secret_store_state").fetchone()[0]
            == 0
        )
