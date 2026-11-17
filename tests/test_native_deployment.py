"""Exercise native payload verification and Python-to-Rust program rollback."""

import hashlib
import io
import json
import os
import runpy
import shutil
import sqlite3
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
SERVER = runpy.run_path(str(REPO / "scripts/deploy/server.py"))
CLIENT = runpy.run_path(str(REPO / "scripts/deploy/client.py"))
SHA = "a" * 40


def native_archive(extra=(), *, link="../lib/extore/bin/extore"):
    stream = io.BytesIO()
    files = {name: (name + "\n").encode() for name in SERVER["NATIVE_REQUIRED"]}
    files.update(
        {
            "usr/lib/extore/extore/static/native-theme.css": b"body { color: blue; }\n",
            "etc/extore/extore.env": b"EXTORE_ORIGIN=https://example.test\n",
            ".PKGINFO": b"pkgname = extore\n",
        }
    )
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, contents in [*files.items(), *extra]:
            item = tarfile.TarInfo(name)
            item.mode = (
                0o755 if name.endswith(("/bin/extore", "/extore-admin")) else 0o644
            )
            item.size = len(contents)
            archive.addfile(item, io.BytesIO(contents))
        alias = tarfile.TarInfo("usr/bin/extore")
        alias.type = tarfile.SYMTYPE
        alias.linkname = link
        archive.addfile(alias)
    stream.seek(0)
    return stream, files


def install_fixture(tmp_path, monkeypatch):
    stream, contents = native_archive()
    value = SERVER["manifest_from_tar"](stream)
    value.update(runtime="rust", version="1.0.0", sha=SHA, package_id="extore 1.0.0-1")
    for name, content in contents.items():
        if name.startswith("."):
            continue
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(value["file_modes"].get(name, 0o640))
    alias = tmp_path / "usr/bin/extore"
    alias.symlink_to("../lib/extore/bin/extore")
    namespace = SERVER["verify_native_payload"].__globals__
    monkeypatch.setitem(namespace, "ROOT", tmp_path)
    config = tmp_path / "etc/extore/extore.env"
    original_stat = Path.stat

    def config_owned_by_root(path, *args, **kwargs):
        result = original_stat(path, *args, **kwargs)
        if path == config:
            fields = list(result)
            fields[4] = 0
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(Path, "stat", config_owned_by_root)
    return value


def test_runtime_version_uses_cargo_and_retains_python_metadata(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.11.3"\n')
    assert SERVER["project_runtime"](tmp_path) == ("python", "0.11.3")
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "extore"\nversion = "1.0.0"\n'
    )
    assert SERVER["project_runtime"](tmp_path) == ("rust", "1.0.0")


def test_package_manifest_covers_binary_sdk_units_and_keeps_live_config(
    tmp_path, monkeypatch
):
    value = install_fixture(tmp_path, monkeypatch)
    assert SERVER["NATIVE_REQUIRED"] <= value["files"].keys()
    assert value["symlinks"] == SERVER["NATIVE_LINKS"]
    config = tmp_path / "etc/extore/extore.env"
    config.write_text("EXTORE_ORIGIN=https://production.example\n")
    assert "etc/extore/extore.env" not in value["files"]
    SERVER["verify_native_payload"](value)
    config.chmod(0o666)
    with pytest.raises(RuntimeError, match="configuration must be root-owned"):
        SERVER["verify_native_payload"](value)


@pytest.mark.parametrize(
    "name",
    [
        "usr/lib/extore/bin/extore",
        "usr/lib/systemd/system/extore-worker.service",
        "usr/lib/extore/extore/sdk/client.py",
        "usr/lib/extore/extore/static/native-theme.css",
    ],
)
def test_changed_installed_payload_fails_verification(tmp_path, monkeypatch, name):
    value = install_fixture(tmp_path, monkeypatch)
    (tmp_path / name).write_bytes(b"different from the prepared package")
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        SERVER["verify_native_payload"](value)


def test_binary_permissions_and_link_target_are_verified(tmp_path, monkeypatch):
    value = install_fixture(tmp_path, monkeypatch)
    binary = tmp_path / "usr/lib/extore/bin/extore"
    binary.chmod(0o644)
    with pytest.raises(RuntimeError, match="permissions mismatch"):
        SERVER["verify_native_payload"](value)
    binary.chmod(0o755)
    alias = tmp_path / "usr/bin/extore"
    alias.unlink()
    alias.symlink_to("/unexpected/binary")
    with pytest.raises(RuntimeError, match="symlink mismatch"):
        SERVER["verify_native_payload"](value)


@pytest.mark.parametrize(
    "name",
    [
        "usr/lib/extore/.venv/bin/python",
        "usr/lib/extore/extore/app.py",
        "usr/bin/other-command",
        "../../outside",
        "usr/lib/extore/bin/extore",
    ],
)
def test_manifest_rejects_old_api_unexpected_paths_and_duplicates(name):
    stream, _ = native_archive([(name, b"unexpected")])
    with pytest.raises(RuntimeError):
        SERVER["manifest_from_tar"](stream)


def test_manifest_rejects_unexpected_binary_link():
    stream, _ = native_archive(link="/tmp/extore")
    with pytest.raises(RuntimeError, match="Unexpected native package payload"):
        SERVER["manifest_from_tar"](stream)


@pytest.mark.parametrize("runtime", ["python", "rust"])
def test_public_check_verifies_runtime_version_and_all_static_hashes(
    monkeypatch, runtime
):
    prefix = (
        "extore/static/" if runtime == "python" else "usr/lib/extore/extore/static/"
    )
    content = {
        "/static/native-theme.css": b"body{}",
        "/AGENTS.md": b"public instructions",
    }
    value = {
        "sha": SHA,
        "version": "1.0.0",
        "runtime": runtime,
        "files": {
            prefix + "native-theme.css": hashlib.sha256(
                content["/static/native-theme.css"]
            ).hexdigest(),
            prefix + "AGENTS.md": hashlib.sha256(content["/AGENTS.md"]).hexdigest(),
            "usr/lib/extore/bin/extore": "unused for the public static check",
        },
    }
    calls = []

    def fetch(args):
        path = args[-1].removeprefix("https://example.test").split("?")[0]
        calls.append(path)
        if path == "/health":
            health = {"status": "ok"}
            if runtime == "rust":
                health.update(runtime="rust", version="1.0.0")
            return json.dumps(health).encode()
        if path == "/openapi.json":
            return b'{"info":{"version":"1.0.0"}}'
        return content[path]

    namespace = CLIENT["public_check"].__globals__
    monkeypatch.setattr(namespace["subprocess"], "check_output", fetch)
    CLIENT["public_check"]({"origin": "https://example.test"}, value)
    assert set(content) <= set(calls)
    assert ("/openapi.json" in calls) == (runtime == "python")
    content["/static/native-theme.css"] = b"stale cache"
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        CLIENT["public_check"]({"origin": "https://example.test"}, value)


@pytest.mark.parametrize(
    "health",
    [
        {"status": "ok"},
        {"status": "ok", "runtime": "python", "version": "1.0.0"},
        {"status": "ok", "runtime": "rust", "version": "0.11.3"},
    ],
)
def test_native_public_health_cannot_pass_with_old_runtime(monkeypatch, health):
    namespace = CLIENT["public_check"].__globals__
    monkeypatch.setattr(
        namespace["subprocess"], "check_output", lambda args: json.dumps(health)
    )
    with pytest.raises(RuntimeError, match="native health/version"):
        CLIENT["public_check"](
            {"origin": "https://example.test"},
            {"runtime": "rust", "version": "1.0.0", "files": {}},
        )


def test_native_init_occurs_after_install_before_either_service_starts(monkeypatch):
    namespace = SERVER["activate"].__globals__
    calls = []
    monkeypatch.setitem(
        namespace,
        "prepared",
        lambda sha: {
            "sha": sha,
            "runtime": "rust",
            "package": "extore-1.0.0-1-x86_64.pkg.tar.zst",
        },
    )
    monkeypatch.setitem(namespace, "main_sha", lambda: SHA)
    monkeypatch.setitem(
        namespace, "snapshot_info", lambda sha: {"offhost_verified": True}
    )
    monkeypatch.setitem(namespace, "verify", lambda sha: {"status": "verified"})
    monkeypatch.setitem(namespace, "run", lambda *args, **kwargs: calls.append(args))
    assert SERVER["activate"](SHA)["status"] == "verified"
    install = next(i for i, args in enumerate(calls) if args[0] == "pacman")
    init = calls.index(
        ("runuser", "-u", "extore", "--", "/usr/bin/extore-admin", "init")
    )
    start = next(
        i for i, args in enumerate(calls) if args[:2] == ("systemctl", "start")
    )
    assert install < init < start
    assert all(args[:2] == ("systemctl", "stop") for args in calls[:install])


def test_snapshot_omits_new_alias_when_backing_up_legacy_install(tmp_path, monkeypatch):
    namespace = SERVER["snapshot"].__globals__
    state = tmp_path / "state"
    state.mkdir()
    root = tmp_path / "root"
    (root / "usr/lib/extore/.venv").mkdir(parents=True)
    (root / "usr/bin").mkdir(parents=True)
    (root / "usr/bin/extore-admin").write_text("old wrapper")
    calls = []
    monkeypatch.setitem(namespace, "STATE", state)
    monkeypatch.setitem(namespace, "ROOT", root)
    monkeypatch.setitem(
        namespace,
        "prepared",
        lambda sha: {"old_pacman_entry": "var/lib/pacman/local/extore-0.11.3-4"},
    )
    monkeypatch.setitem(namespace, "database_summary", lambda: {})
    monkeypatch.setitem(
        namespace,
        "sys",
        SimpleNamespace(
            stderr=io.StringIO(),
            stdout=SimpleNamespace(buffer=io.BytesIO()),
        ),
    )

    def run(*args, **kwargs):
        calls.append(args)
        if args[0] == "tar":
            (state / "recovery.partial").write_bytes(b"archive fixture")

    monkeypatch.setitem(namespace, "run", run)
    SERVER["snapshot"](SHA)
    value = json.loads((state / "prepared.json").read_text())
    assert value["old_runtime_paths"] == ["usr/lib/extore", "usr/bin/extore-admin"]
    archive_call = next(args for args in calls if args[0] == "tar")
    assert "usr/bin/extore" not in archive_call
    assert "var/lib/extore" in archive_call


@pytest.mark.parametrize("old_native", [False, True])
def test_rollback_restores_only_programs_and_keeps_new_business_data(
    tmp_path, monkeypatch, old_native
):
    namespace = SERVER["rollback"].__globals__
    root = tmp_path / "root"
    state = root / "var/lib/extore-deploy"
    state.mkdir(parents=True)
    old_entry = "var/lib/pacman/local/extore-0.11.3-4"
    old_paths = SERVER["LEGACY_RUNTIME_PATHS"] + (
        ["usr/bin/extore"] if old_native else []
    )
    old_files = {
        "usr/lib/extore/bin/extore"
        if old_native
        else "usr/lib/extore/.venv/lib/python3/site-packages/extore/app.py": b"old program",
        "etc/extore/extore.env": b"old configuration",
        "usr/lib/systemd/system/extore-api.service": b"old API unit",
        "usr/lib/systemd/system/extore-worker.service": b"old worker unit",
        "usr/lib/sysusers.d/extore.conf": b"old user",
        "usr/bin/extore-admin": b"old wrapper",
        "usr/share/licenses/extore/LICENSE": b"old license",
        old_entry + "/desc": b"old pacman record",
        "var/lib/extore/extore.sqlite3": b"old business data",
        "var/lib/extore-deploy/current.json": b'{"runtime":"python","sha":"old"}',
    }
    for name, contents in old_files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
    alias = root / "usr/bin/extore"
    if old_native:
        alias.symlink_to("../lib/extore/bin/extore")
    archive = tmp_path / "recovery.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for name in old_paths + [
            old_entry,
            "var/lib/extore",
            "var/lib/extore-deploy/current.json",
        ]:
            tar.add(root / name, arcname=name, recursive=True)
    shutil.rmtree(root / "usr/lib/extore")
    binary = root / "usr/lib/extore/bin/extore"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"new broken program")
    if not old_native:
        alias.symlink_to("../lib/extore/bin/extore")
    (root / "var/lib/extore/extore.sqlite3").write_bytes(b"new business transaction")
    (state / "current.json").write_text('{"runtime":"rust","sha":"new"}')
    new_entry = root / "var/lib/pacman/local/extore-1.0.0-1"
    new_entry.mkdir()
    monkeypatch.setitem(namespace, "ROOT", root)
    monkeypatch.setitem(namespace, "STATE", state)
    monkeypatch.setitem(
        namespace,
        "prepared",
        lambda sha: {
            "old_runtime_paths": old_paths,
            "old_database": {"schema_sha256": "same schema"},
            "old_pacman_entry": old_entry,
            "old_current_exists": True,
        },
    )
    monkeypatch.setitem(
        namespace, "database_summary", lambda: {"schema_sha256": "same schema"}
    )
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        if args[0] == "tar":
            subprocess.run(args, check=True, **kwargs)

    monkeypatch.setitem(namespace, "run", run)
    with archive.open("rb") as source:
        monkeypatch.setitem(
            namespace,
            "sys",
            SimpleNamespace(
                stdin=SimpleNamespace(buffer=source),
                stderr=subprocess.DEVNULL,
            ),
        )
        SERVER["rollback"](SHA)
    assert (
        root / "var/lib/extore/extore.sqlite3"
    ).read_bytes() == b"new business transaction"
    assert alias.is_symlink() == old_native
    assert not new_entry.exists()
    assert (root / old_entry / "desc").read_bytes() == b"old pacman record"
    assert json.loads((state / "current.json").read_text())["sha"] == "old"
    assert (root / "etc/extore/extore.env").read_bytes() == b"old configuration"
    old_program = root / next(iter(old_files))
    assert old_program.read_bytes() == b"old program"
    assert "var/lib/extore" not in next(args for args in calls if args[0] == "tar")


def schema22_fixture(tmp_path):
    database = tmp_path / "extore.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            "CREATE TABLE shops(id TEXT PRIMARY KEY);"
            "CREATE TABLE cards(id TEXT PRIMARY KEY);"
            "INSERT INTO shops VALUES ('retained-shop');"
            "INSERT INTO cards VALUES ('old-card');"
            "PRAGMA user_version=22;"
        )
    return database, SERVER["database_summary"](database)


def add_schema23_bindings(database):
    with sqlite3.connect(database) as connection:
        for kind, sql in SERVER["SCHEMA_23_ADDITIONS"].values():
            connection.execute(sql)
        connection.execute("PRAGMA user_version=23")


def test_rollback_accepts_only_verified_schema23_additions_without_reverting_new_rows(
    tmp_path, monkeypatch
):
    database, previous = schema22_fixture(tmp_path)
    add_schema23_bindings(database)
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO cards VALUES ('new-paid-card')")
        connection.execute(
            "INSERT INTO commerce_bindings VALUES ('binding','retained-shop','key-digest','retained binding',0,1,0)"
        )
    namespace = SERVER["rollback_schema_compatible"].__globals__
    monkeypatch.setitem(namespace, "DATA", tmp_path)
    assert SERVER["rollback_schema_compatible"](
        previous, SERVER["database_summary"](database)
    )
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 23
        assert connection.execute("SELECT id FROM cards ORDER BY id").fetchall() == [
            ("new-paid-card",),
            ("old-card",),
        ]
        assert connection.execute("SELECT id FROM commerce_bindings").fetchall() == [
            ("binding",)
        ]


@pytest.mark.parametrize(
    "change",
    [
        "ALTER TABLE cards ADD COLUMN unreviewed TEXT",
        "CREATE TABLE unreviewed(id TEXT)",
        "DROP TABLE cards",
        "ALTER TABLE commerce_bindings ADD COLUMN unreviewed TEXT",
        "DROP INDEX commerce_bindings_shop",
        "DROP INDEX commerce_bindings_shop; CREATE INDEX commerce_bindings_shop ON commerce_bindings(label)",
        "PRAGMA user_version=24",
    ],
)
def test_rollback_still_rejects_every_unreviewed_schema_change(
    tmp_path, monkeypatch, change
):
    database, previous = schema22_fixture(tmp_path)
    add_schema23_bindings(database)
    with sqlite3.connect(database) as connection:
        connection.executescript(change)
    namespace = SERVER["rollback_schema_compatible"].__globals__
    monkeypatch.setitem(namespace, "DATA", tmp_path)
    assert not SERVER["rollback_schema_compatible"](
        previous, SERVER["database_summary"](database)
    )


def test_schema23_exception_cannot_be_used_for_other_previous_versions(
    tmp_path, monkeypatch
):
    database, previous = schema22_fixture(tmp_path)
    add_schema23_bindings(database)
    namespace = SERVER["rollback_schema_compatible"].__globals__
    monkeypatch.setitem(namespace, "DATA", tmp_path)
    for version in [0, 21, 23, 24]:
        assert not SERVER["rollback_schema_compatible"](
            {**previous, "user_version": version}, SERVER["database_summary"](database)
        )


def test_unchanged_runtime_schema_remains_rollback_compatible(tmp_path):
    database, previous = schema22_fixture(tmp_path)
    assert SERVER["rollback_schema_compatible"](
        previous, SERVER["database_summary"](database)
    )
    add_schema23_bindings(database)
    current = SERVER["database_summary"](database)
    assert SERVER["rollback_schema_compatible"](current, current)
