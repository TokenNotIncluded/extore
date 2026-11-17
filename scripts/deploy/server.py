"""Root-owned, forced-command deployment endpoint on the production server."""

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import time
import tomllib
from pathlib import Path, PurePosixPath

REPOSITORY = "https://github.com/TokenNotIncluded/extore.git"
STATE = Path("/var/lib/extore-deploy")
WORK = Path("/var/lib/extore-build/work")
DATA = Path("/var/lib/extore")
BUILD_USER = "extore-build"
SERVICES = ["extore-api.service", "extore-worker.service"]
ROOT = Path("/")
LEGACY_RUNTIME_PATHS = [
    "usr/lib/extore",
    "etc/extore",
    "usr/lib/systemd/system/extore-api.service",
    "usr/lib/systemd/system/extore-worker.service",
    "usr/lib/sysusers.d/extore.conf",
    "usr/bin/extore-admin",
    "usr/share/licenses/extore",
]
RUNTIME_PATHS = LEGACY_RUNTIME_PATHS + ["usr/bin/extore"]
NATIVE_LINKS = {"usr/bin/extore": "../lib/extore/bin/extore"}
NATIVE_MODULES = {
    "__init__.py",
    "processor_runner.py",
    "variants.py",
    "task_flow_definition.py",
    "task_flow_schema.py",
}
NATIVE_REQUIRED = {
    "usr/lib/extore/bin/extore",
    "usr/lib/extore/extore/static/index.html",
    "usr/lib/extore/extore/sdk/__init__.py",
    "usr/lib/extore/extore/sdk/client.py",
    "usr/lib/extore/extore/sdk/script.py",
    "usr/lib/extore/extore/sdk/flow.py",
    "usr/lib/extore/extore/sdk/instructions.py",
    "usr/lib/extore/processors/official/extore_processors/__init__.py",
    "usr/lib/systemd/system/extore-api.service",
    "usr/lib/systemd/system/extore-worker.service",
    "usr/lib/sysusers.d/extore.conf",
    "usr/bin/extore-admin",
} | {"usr/lib/extore/extore/" + name for name in NATIVE_MODULES}
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")


def require_sha(value):
    if not SHA_PATTERN.fullmatch(value):
        raise ValueError("Expected a full lowercase Git commit SHA")
    return value


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def capture(*args):
    return run(*args, stdout=subprocess.PIPE, text=True).stdout.strip()


def main_sha():
    return require_sha(
        capture("git", "ls-remote", REPOSITORY, "refs/heads/main").split()[0]
    )


def save(name, value):
    STATE.mkdir(mode=0o700, exist_ok=True)
    temporary = STATE / (name + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n")
    temporary.chmod(0o600)
    temporary.replace(STATE / name)


def load(name):
    return json.loads((STATE / name).read_text())


def digest(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def project_runtime(checkout):
    cargo = checkout / "Cargo.toml"
    if cargo.is_file():
        project = tomllib.loads(cargo.read_text())["package"]
        if project["name"] != "extore" or not isinstance(project["version"], str):
            raise RuntimeError("Unexpected native package metadata")
        return "rust", project["version"]
    project = tomllib.loads((checkout / "pyproject.toml").read_text())
    return "python", project["project"]["version"]


def payload_name(name):
    parts = PurePosixPath(name).parts
    if name.startswith("/") or ".." in parts:
        raise RuntimeError("Unsafe package path")
    return str(PurePosixPath(name))


def native_file(name):
    return name in NATIVE_REQUIRED or name.startswith(
        (
            "usr/lib/extore/extore/static/",
            "usr/lib/extore/extore/sdk/",
            "usr/lib/extore/processors/official/extore_processors/",
            "usr/share/licenses/extore/",
        )
    )


def manifest_from_tar(stream):
    """Hash the built payload, not the checkout or a pre-existing installation."""
    files, links, modes, seen = {}, {}, {}, set()
    with tarfile.open(fileobj=stream, mode="r|") as archive:
        for member in archive:
            name = payload_name(member.name)
            if name in seen:
                raise RuntimeError("Duplicate package path")
            seen.add(name)
            if member.isdir():
                continue
            if name in {".PKGINFO", ".BUILDINFO", ".MTREE"} and member.isfile():
                continue
            if name == "etc/extore/extore.env" and member.isfile():
                # pacman's backup= preserves the administrator's live configuration.
                continue
            if member.issym() and NATIVE_LINKS.get(name) == member.linkname:
                links[name] = member.linkname
                continue
            if not member.isfile() or not native_file(name):
                raise RuntimeError(f"Unexpected native package payload: {name}")
            if ".venv" in name.split("/") or "__pycache__" in name.split("/"):
                raise RuntimeError(f"Unexpected Python runtime payload: {name}")
            with archive.extractfile(member) as source:
                files[name] = hashlib.file_digest(source, "sha256").hexdigest()
            modes[name] = stat.S_IMODE(member.mode)
    if not NATIVE_REQUIRED <= files.keys() or links != NATIVE_LINKS:
        raise RuntimeError("Native package is missing required runtime files")
    if "etc/extore/extore.env" not in seen:
        raise RuntimeError("Native package is missing its configuration template")
    return {"files": files, "symlinks": links, "file_modes": modes}


def package_manifest(package):
    with subprocess.Popen(
        ["zstd", "-dc", "--", str(package)], stdout=subprocess.PIPE, stderr=sys.stderr
    ) as process:
        try:
            result = manifest_from_tar(process.stdout)
            while process.stdout.read(1024 * 1024):
                pass
        except BaseException:
            process.kill()
            raise
        if process.wait() != 0:
            raise RuntimeError("Could not read the built package")
    return result


def verify_native_payload(value):
    if (
        not NATIVE_REQUIRED <= value["files"].keys()
        or value.get("symlinks") != NATIVE_LINKS
    ):
        raise RuntimeError("Native runtime manifest is incomplete")
    for name, expected in value["files"].items():
        if payload_name(name) != name or not native_file(name):
            raise RuntimeError("Invalid native runtime manifest path")
        path = ROOT / name
        if path.is_symlink() or not path.is_file() or digest(path) != expected:
            raise RuntimeError(f"Installed runtime checksum mismatch: {name}")
        if stat.S_IMODE(path.stat().st_mode) != value["file_modes"][name]:
            raise RuntimeError(f"Installed runtime permissions mismatch: {name}")
    for name, target in NATIVE_LINKS.items():
        path = ROOT / name
        if not path.is_symlink() or os.readlink(path) != target:
            raise RuntimeError(f"Installed runtime symlink mismatch: {name}")
    config = ROOT / "etc/extore/extore.env"
    if config.is_symlink() or not config.is_file():
        raise RuntimeError("Installed configuration is missing")
    info = config.stat()
    if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
        raise RuntimeError(
            "Installed configuration must be root-owned and not writable by others"
        )


def database_summary(path=DATA / "extore.sqlite3"):
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise RuntimeError("SQLite integrity check failed")
        schema = connection.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL "
            "ORDER BY type,name"
        ).fetchall()
        tables = {row[1] for row in schema if row[0] == "table"}
        counts = {
            name: connection.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
            for name in ["shops", "products", "cards", "jobs", "credentials"]
            if name in tables
        }
        return {
            "user_version": connection.execute("PRAGMA user_version").fetchone()[0],
            "schema_sha256": hashlib.sha256(json.dumps(schema).encode()).hexdigest(),
            "rows": counts,
        }


def build(*args, cwd=None):
    run(
        "runuser",
        "-u",
        BUILD_USER,
        "--",
        *args,
        cwd=cwd,
        stdout=sys.stderr,
        env={
            "HOME": "/var/lib/extore-build",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "UV_CACHE_DIR": "/var/lib/extore-build/cache",
            "CARGO_HOME": "/var/lib/extore-build/cache/cargo",
        },
    )


def prepared(sha):
    value = load("prepared.json")
    if value["sha"] != sha or not WORK.is_dir():
        raise RuntimeError("No matching prepared deployment")
    package = WORK / value["package"]
    if package.parent != WORK or digest(package) != value["package_sha256"]:
        raise RuntimeError("Prepared package checksum mismatch")
    return value


def pending(sha):
    if (STATE / "recovery.tar.gz").exists():
        return {"status": "pending_backup", **load("prepared.json")}
    return {"status": "clear", "sha": sha}


def prepare(sha):
    if sha != main_sha():
        return {"status": "superseded", "sha": sha}
    if (STATE / "recovery.tar.gz").exists():
        return {"status": "pending_backup", **load("prepared.json")}
    if (STATE / "current.json").exists():
        current = load("current.json")
        if current["sha"] == sha:
            verify(sha, current)
            return {"status": "current", **current}
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(mode=0o700)
    shutil.chown(WORK, BUILD_USER, BUILD_USER)
    checkout = WORK / "repository"
    build(
        "git",
        "clone",
        "--depth=1",
        "--branch",
        "main",
        "--recurse-submodules",
        REPOSITORY,
        str(checkout),
    )
    if (
        capture(
            "runuser",
            "-u",
            BUILD_USER,
            "--",
            "git",
            "-C",
            str(checkout),
            "rev-parse",
            "HEAD",
        )
        != sha
    ):
        return {"status": "superseded", "sha": sha}
    runtime, version = project_runtime(checkout)
    shutil.copy2(checkout / "deploy/arch/PKGBUILD", WORK / "PKGBUILD")
    shutil.chown(WORK / "PKGBUILD", BUILD_USER, BUILD_USER)
    build(
        "python",
        str(checkout / "scripts/source_archive.py"),
        str(WORK / "extore-source.tar.gz"),
        "--revision",
        sha,
    )
    archive_sha256 = digest(WORK / "extore-source.tar.gz")
    package_build = WORK / "PKGBUILD"
    package_build.write_text(
        package_build.read_text().replace(
            "sha256sums=('SKIP')", f"sha256sums=('{archive_sha256}')"
        )
    )
    build("makepkg", "--noconfirm", "--cleanbuild", "--clean", cwd=WORK)
    packages = list(WORK.glob("extore-*.pkg.tar.zst"))
    if len(packages) != 1:
        raise RuntimeError("Expected exactly one built Extore package")
    if runtime == "rust":
        manifest = package_manifest(packages[0])
    else:
        files = {}
        for directory in [
            checkout / "extore",
            checkout / "processors/official/extore_processors",
        ]:
            for path in sorted(directory.rglob("*")):
                if path.is_file() and "__pycache__" not in path.parts:
                    prefix = (
                        "extore" if directory.name == "extore" else "extore_processors"
                    )
                    files[prefix + "/" + str(path.relative_to(directory))] = digest(
                        path
                    )
        manifest = {"files": files}
    value = {
        "sha": sha,
        "version": version,
        "runtime": runtime,
        "package": packages[0].name,
        "package_sha256": digest(packages[0]),
        "package_id": capture("pacman", "-Qp", str(packages[0])),
        "source_sha256": archive_sha256,
        **manifest,
        "old_package": capture("pacman", "-Q", "extore"),
        "old_database": database_summary(),
    }
    value["old_pacman_entry"] = (
        "var/lib/pacman/local/extore-" + value["old_package"].split()[1]
    )
    if not (Path("/") / value["old_pacman_entry"]).is_dir():
        raise RuntimeError("Previous pacman package metadata is missing")
    save("prepared.json", value)
    return {"status": "prepared", **value}


def snapshot(sha):
    value = prepared(sha)
    archive = STATE / "recovery.tar.gz"
    if not archive.exists():
        temporary = STATE / "recovery.partial"
        try:
            run("systemctl", "stop", "extore-worker.service", stdout=sys.stderr)
            run("systemctl", "stop", "extore-api.service", stdout=sys.stderr)
            summary = database_summary()
            value["old_runtime_paths"] = [
                name for name in RUNTIME_PATHS if os.path.lexists(ROOT / name)
            ]
            paths = value["old_runtime_paths"] + [
                "var/lib/extore",
                value["old_pacman_entry"],
            ]
            value["old_current_exists"] = (STATE / "current.json").exists()
            if (STATE / "current.json").exists():
                paths.append("var/lib/extore-deploy/current.json")
            value["old_database"] = summary
            save("prepared.json", value)
            run(
                "tar",
                "--acls",
                "--xattrs",
                "--numeric-owner",
                "-czf",
                str(temporary),
                "-C",
                str(ROOT),
                *paths,
                stdout=sys.stderr,
            )
            temporary.chmod(0o600)
            temporary.replace(archive)
            save(
                "snapshot.json",
                {
                    "sha": sha,
                    "database": summary,
                    "archive_sha256": digest(archive),
                    "offhost_verified": False,
                    "old_runtime_paths": value["old_runtime_paths"],
                },
            )
            value["old_database"] = summary
            save("prepared.json", value)
        finally:
            run("systemctl", "start", *SERVICES, stdout=sys.stderr)
    info = snapshot_info(sha)
    if digest(archive) != info["archive_sha256"]:
        raise RuntimeError("Recovery archive checksum mismatch")
    # Network transfer happens only after the previous services are running again.
    with archive.open("rb") as source:
        shutil.copyfileobj(source, sys.stdout.buffer)


def snapshot_info(sha):
    value = load("snapshot.json")
    if value["sha"] != sha:
        raise RuntimeError("No matching recovery snapshot")
    return value


def acknowledge(sha):
    value = snapshot_info(sha)
    value["offhost_verified"] = True
    save("snapshot.json", value)
    return {"status": "backup_verified", "sha": sha}


def verify(sha, value=None):
    value = value or prepared(sha)
    installed_package = capture("pacman", "-Q", "extore")
    if value.get("package_id") and installed_package != value["package_id"]:
        raise RuntimeError("Installed native package version mismatch")
    runtime = value.get("runtime", "python")
    if runtime == "rust":
        verify_native_payload(value)
    elif runtime == "python":
        sites = list((ROOT / "usr/lib/extore/.venv/lib").glob("python*/site-packages"))
        if len(sites) != 1:
            raise RuntimeError("Expected exactly one installed Python site directory")
        for name, expected in value["files"].items():
            if digest(sites[0] / name) != expected:
                raise RuntimeError(f"Installed source checksum mismatch: {name}")
    else:
        raise RuntimeError("Unknown runtime in deployment metadata")
    for service in SERVICES:
        run("systemctl", "is-active", "--quiet", service)
        if capture("systemctl", "show", service, "-p", "NRestarts", "--value") != "0":
            raise RuntimeError(f"Service restarted unexpectedly: {service}")
    for attempt in range(30):
        try:
            health = json.loads(
                capture(
                    "curl", "-fsS", "--max-time", "3", "http://127.0.0.1:8095/health"
                )
            )
            if runtime == "rust":
                healthy = (
                    health.get("status") == "ok"
                    and health.get("runtime") == "rust"
                    and health.get("version") == value["version"]
                )
            else:
                api = json.loads(
                    capture(
                        "curl",
                        "-fsS",
                        "--max-time",
                        "3",
                        "http://127.0.0.1:8095/openapi.json",
                    )
                )
                healthy = (
                    health == {"status": "ok"}
                    and api["info"]["version"] == value["version"]
                )
            if healthy:
                break
        except (subprocess.CalledProcessError, ValueError, KeyError):
            pass
        time.sleep(1)
    else:
        raise RuntimeError("Local health/version check failed")
    return {
        "status": "verified",
        "sha": sha,
        "version": value["version"],
        "runtime": runtime,
        "package": installed_package,
        "database": database_summary(),
    }


def activate(sha):
    value = prepared(sha)
    if sha != main_sha():
        return {"status": "superseded", "sha": sha}
    if not snapshot_info(sha).get("offhost_verified"):
        raise RuntimeError("The recovery snapshot has not been uploaded and verified")
    run("systemctl", "stop", "extore-worker.service", stdout=sys.stderr)
    run("systemctl", "stop", "extore-api.service", stdout=sys.stderr)
    run("pacman", "-U", "--noconfirm", str(WORK / value["package"]), stdout=sys.stderr)
    if value.get("runtime") == "rust":
        run(
            "runuser",
            "-u",
            "extore",
            "--",
            "/usr/bin/extore-admin",
            "init",
            stdout=sys.stderr,
        )
    run("systemctl", "daemon-reload", stdout=sys.stderr)
    run("systemctl", "reset-failed", *SERVICES, stdout=sys.stderr)
    run("systemctl", "start", *SERVICES, stdout=sys.stderr)
    return verify(sha)


def confirm(sha):
    result = verify(sha)
    value = prepared(sha)
    value["confirmed_at"] = int(time.time())
    save("current.json", value)
    return result


def cleanup(sha):
    if (STATE / "prepared.json").exists() and load("prepared.json")["sha"] != sha:
        raise RuntimeError("Cleanup would affect another deployment")
    if (STATE / "recovery.tar.gz").exists() and not snapshot_info(sha).get(
        "offhost_verified"
    ):
        return {"status": "pending_backup", "sha": sha}
    for name in ["recovery.tar.gz", "recovery.partial"]:
        (STATE / name).unlink(missing_ok=True)
    if WORK.exists():
        shutil.rmtree(WORK)
    cache = Path("/var/lib/extore-build/cache")
    if cache.exists():
        shutil.rmtree(cache)
    for name in ["prepared.json", "snapshot.json"]:
        (STATE / name).unlink(missing_ok=True)
    return {"status": "cleaned", "sha": sha}


def rollback(sha):
    value = prepared(sha)
    run("systemctl", "stop", *SERVICES, stdout=sys.stderr)
    if database_summary()["schema_sha256"] != value["old_database"]["schema_sha256"]:
        raise RuntimeError(
            "Schema changed; preserve data and recover manually from the encrypted artifact"
        )
    # Restore program/config files only. Never overwrite new business transactions.
    paths = value.get("old_runtime_paths", LEGACY_RUNTIME_PATHS)
    if not isinstance(paths, list) or any(name not in RUNTIME_PATHS for name in paths):
        raise RuntimeError("Invalid previous runtime paths")
    for name in RUNTIME_PATHS:
        path = ROOT / name
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
    for entry in (ROOT / "var/lib/pacman/local").iterdir():
        if re.fullmatch(r"extore-[0-9][A-Za-z0-9.+_:~-]+", entry.name):
            shutil.rmtree(entry)
    run(
        "tar",
        "--acls",
        "--xattrs",
        "--numeric-owner",
        "-xzf",
        "-",
        "-C",
        str(ROOT),
        *paths,
        value["old_pacman_entry"],
        *(
            ["var/lib/extore-deploy/current.json"]
            if value.get("old_current_exists")
            else []
        ),
        stdin=sys.stdin.buffer,
        stdout=sys.stderr,
    )
    if value.get("old_current_exists") is False:
        (STATE / "current.json").unlink(missing_ok=True)
    run("systemctl", "daemon-reload", stdout=sys.stderr)
    run("systemctl", "reset-failed", *SERVICES, stdout=sys.stderr)
    run("systemctl", "start", *SERVICES, stdout=sys.stderr)
    return {"status": "rolled_back", "sha": sha}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command")
    args = parser.parse_args()
    parts = args.command.split()
    if len(parts) != 2:
        raise ValueError("Expected an operation and commit SHA")
    operation, sha = parts
    require_sha(sha)
    if os.geteuid() != 0:
        raise RuntimeError("The deployment endpoint must run as root")
    operations = {
        "pending": pending,
        "prepare": prepare,
        "snapshot": snapshot,
        "snapshot_info": snapshot_info,
        "acknowledge": acknowledge,
        "activate": activate,
        "verify": verify,
        "confirm": confirm,
        "cleanup": cleanup,
        "rollback": rollback,
    }
    if operation not in operations:
        raise ValueError("Unsupported deployment operation")
    os.umask(0o077)
    result = operations[operation](sha)
    if result is not None:
        print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
