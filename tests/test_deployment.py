"""Deployment trust boundaries and recovery behavior, without touching a server."""

import io
import json
import runpy
import sqlite3
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLIENT = runpy.run_path(str(ROOT / "scripts/deploy/client.py"))
SERVER = runpy.run_path(str(ROOT / "scripts/deploy/server.py"))


@pytest.mark.parametrize(
    "sha", ["main", "a" * 39, "A" * 40, "a" * 40 + "\n", "../main", "a;id"]
)
def test_only_full_commit_shas_are_accepted(sha):
    for module in [CLIENT, SERVER]:
        with pytest.raises(ValueError):
            module["require_sha"](sha)


def make_backup(tmp_path, *, omit=None, extra=None):
    database = tmp_path / "source.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            "CREATE TABLE cards(id TEXT); INSERT INTO cards VALUES ('retained'); PRAGMA user_version=22;"
        )
    files = {
        "var/lib/extore/extore.sqlite3": database.read_bytes(),
        "var/lib/extore/issuance.key": b"issuance recovery key",
        "var/lib/extore/master-secrets.key": b"master recovery key",
        "etc/extore/extore.env": b"EXTORE_DATA=/var/lib/extore\n",
    }
    if omit:
        files.pop(omit)
    if extra:
        files.update(extra)
    archive_path = tmp_path / "recovery.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        for name, data in files.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
    return archive_path


def test_backup_restores_database_and_requires_both_keys(tmp_path):
    archive = make_backup(tmp_path)
    restored = tmp_path / "restored"
    restored.mkdir()
    result = CLIENT["verify_backup"](archive, restored)
    assert result == {"user_version": 22, "table_count": 1}
    with sqlite3.connect(restored / "extore.sqlite3") as connection:
        assert connection.execute("SELECT id FROM cards").fetchone() == ("retained",)


@pytest.mark.parametrize(
    "missing", ["var/lib/extore/issuance.key", "var/lib/extore/master-secrets.key"]
)
def test_backup_missing_a_key_cannot_authorize_deployment(tmp_path, missing):
    archive = make_backup(tmp_path, omit=missing)
    with pytest.raises(RuntimeError, match="required recovery file"):
        CLIENT["verify_backup"](archive, tmp_path)


def test_backup_rejects_path_traversal(tmp_path):
    archive = make_backup(tmp_path, extra={"../../outside": b"unsafe"})
    with pytest.raises(RuntimeError, match="Unsafe"):
        CLIENT["verify_backup"](archive, tmp_path)


def test_failed_backup_never_activates(tmp_path, monkeypatch):
    namespace = CLIENT["deploy"].__globals__
    calls = []
    value = {"status": "prepared", "sha": "a" * 40}

    def rpc(config, operation, sha):
        calls.append(operation)
        return value

    def remote(*args, **kwargs):
        raise RuntimeError("transfer interrupted")

    monkeypatch.setitem(namespace, "rpc", rpc)
    monkeypatch.setitem(namespace, "remote", remote)

    class BackupPath(type(tmp_path)):
        def is_relative_to(self, *args):
            return True

    monkeypatch.setitem(namespace, "Path", BackupPath)
    with pytest.raises(RuntimeError, match="transfer interrupted"):
        CLIENT["deploy"]("a" * 40, {"backup_root": str(tmp_path)})
    assert calls == ["prepare", "cleanup"]
    assert (
        json.loads(next(tmp_path.glob("*/prepared.json")).read_text())["sha"]
        == "a" * 40
    )


def test_newer_main_cannot_install_an_older_prepared_commit(monkeypatch):
    namespace = SERVER["activate"].__globals__
    monkeypatch.setitem(namespace, "prepared", lambda sha: {"sha": sha})
    monkeypatch.setitem(namespace, "main_sha", lambda: "b" * 40)
    monkeypatch.setitem(
        namespace, "run", lambda *args, **kwargs: pytest.fail("No mutation is allowed")
    )
    assert SERVER["activate"]("a" * 40)["status"] == "superseded"


def test_schema_change_blocks_automatic_program_rollback(monkeypatch):
    namespace = SERVER["rollback"].__globals__
    calls = []
    monkeypatch.setitem(
        namespace, "prepared", lambda sha: {"old_database": {"schema_sha256": "old"}}
    )
    monkeypatch.setitem(namespace, "database_summary", lambda: {"schema_sha256": "new"})
    monkeypatch.setitem(namespace, "run", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(RuntimeError, match="Schema changed"):
        SERVER["rollback"]("a" * 40)
    assert calls == [("systemctl", "stop", *SERVER["SERVICES"])]


def test_snapshot_failure_always_restarts_original_services(monkeypatch, tmp_path):
    namespace = SERVER["snapshot"].__globals__
    calls = []
    monkeypatch.setitem(namespace, "STATE", tmp_path)
    monkeypatch.setitem(namespace, "prepared", lambda sha: {"old_pacman_entry": "old"})
    monkeypatch.setitem(namespace, "database_summary", lambda: {})
    monkeypatch.setitem(namespace, "save", lambda *args: None)
    monkeypatch.setitem(namespace, "load", lambda *args: {"old_pacman_entry": "old"})

    def run(*args, **kwargs):
        calls.append(args)
        if args[0] == "tar":
            raise RuntimeError("transfer interrupted")

    monkeypatch.setitem(namespace, "run", run)
    with pytest.raises(RuntimeError, match="transfer interrupted"):
        SERVER["snapshot"]("a" * 40)
    assert calls[-1] == ("systemctl", "start", *SERVER["SERVICES"])


def test_snapshot_restarts_services_before_network_transfer(monkeypatch, tmp_path):
    namespace = SERVER["snapshot"].__globals__
    calls = []
    monkeypatch.setitem(namespace, "STATE", tmp_path)
    monkeypatch.setitem(namespace, "prepared", lambda sha: {"old_pacman_entry": "old"})
    monkeypatch.setitem(namespace, "database_summary", lambda: {})
    output = io.BytesIO()
    monkeypatch.setitem(
        namespace,
        "sys",
        SimpleNamespace(stderr=io.StringIO(), stdout=SimpleNamespace(buffer=output)),
    )

    def run(*args, **kwargs):
        calls.append(args)
        if args[0] == "tar":
            (tmp_path / "recovery.partial").write_bytes(
                b"a consistent recovery archive"
            )

    def transfer(source, destination):
        assert calls[-1] == ("systemctl", "start", *SERVER["SERVICES"])
        destination.write(source.read())

    monkeypatch.setitem(namespace, "run", run)
    monkeypatch.setattr(namespace["shutil"], "copyfileobj", transfer)
    SERVER["snapshot"]("a" * 40)
    assert output.getvalue() == b"a consistent recovery archive"
    assert (
        json.loads((tmp_path / "snapshot.json").read_text())["local_verified"] is False
    )


def test_install_requires_local_backup_acknowledgement(monkeypatch):
    namespace = SERVER["activate"].__globals__
    monkeypatch.setitem(namespace, "prepared", lambda sha: {"sha": sha})
    monkeypatch.setitem(namespace, "main_sha", lambda: "a" * 40)
    monkeypatch.setitem(
        namespace, "snapshot_info", lambda sha: {"local_verified": False}
    )
    monkeypatch.setitem(
        namespace,
        "run",
        lambda *args, **kwargs: pytest.fail("No installation is allowed"),
    )
    with pytest.raises(RuntimeError, match="not been verified locally"):
        SERVER["activate"]("a" * 40)


def test_cleanup_preserves_unverified_server_archive(monkeypatch, tmp_path):
    namespace = SERVER["cleanup"].__globals__
    monkeypatch.setitem(namespace, "STATE", tmp_path)
    work = tmp_path / "build"
    work.mkdir()
    monkeypatch.setitem(namespace, "WORK", work)
    (tmp_path / "recovery.tar.gz").write_bytes(b"only recovery copy")
    (tmp_path / "snapshot.json").write_text(
        json.dumps({"sha": "a" * 40, "local_verified": False})
    )
    assert SERVER["cleanup"]("a" * 40)["status"] == "pending_backup"
    assert work.is_dir()
    assert (tmp_path / "recovery.tar.gz").read_bytes() == b"only recovery copy"


def test_wrong_backup_destination_is_rejected_before_remote_work(monkeypatch):
    namespace = CLIENT["deploy"].__globals__
    monkeypatch.setitem(
        namespace, "rpc", lambda *args: pytest.fail("No remote work is allowed")
    )
    with pytest.raises(RuntimeError, match="Backups must stay"):
        CLIENT["deploy"](
            "a" * 40, {"backup_root": "/home/lightjunction/Backups/../../tmp"}
        )


def test_new_deployment_drains_a_verified_leftover_before_building(
    monkeypatch, tmp_path
):
    namespace = SERVER["prepare"].__globals__
    monkeypatch.setitem(namespace, "STATE", tmp_path)
    monkeypatch.setitem(namespace, "main_sha", lambda: "b" * 40)
    (tmp_path / "recovery.tar.gz").write_bytes(
        b"a verified snapshot left after interruption"
    )
    (tmp_path / "prepared.json").write_text(json.dumps({"sha": "a" * 40}))
    (tmp_path / "snapshot.json").write_text(
        json.dumps({"sha": "a" * 40, "local_verified": True})
    )
    result = SERVER["prepare"]("b" * 40)
    assert result == {"status": "pending_backup", "sha": "a" * 40}
