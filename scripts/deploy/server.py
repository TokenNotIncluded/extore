"""Root-owned, forced-command deployment endpoint on the production server."""

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import tomllib
from pathlib import Path

REPOSITORY = "https://github.com/TokenNotIncluded/extore.git"
STATE = Path("/var/lib/extore-deploy")
WORK = Path("/var/lib/extore-build/work")
DATA = Path("/var/lib/extore")
BUILD_USER = "extore-build"
SERVICES = ["extore-api.service", "extore-worker.service"]
RUNTIME_PATHS = [
    "usr/lib/extore",
    "etc/extore",
    "usr/lib/systemd/system/extore-api.service",
    "usr/lib/systemd/system/extore-worker.service",
    "usr/lib/sysusers.d/extore.conf",
    "usr/bin/extore-admin",
    "usr/share/licenses/extore",
]
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


def prepare(sha):
    if sha != main_sha():
        return {"status": "superseded", "sha": sha}
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
    project = tomllib.loads((checkout / "pyproject.toml").read_text())
    version = project["project"]["version"]
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
    files = {}
    for directory in [
        checkout / "extore",
        checkout / "processors/official/extore_processors",
    ]:
        for path in sorted(directory.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                prefix = "extore" if directory.name == "extore" else "extore_processors"
                files[prefix + "/" + str(path.relative_to(directory))] = digest(path)
    value = {
        "sha": sha,
        "version": version,
        "package": packages[0].name,
        "package_sha256": digest(packages[0]),
        "source_sha256": archive_sha256,
        "files": files,
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
    # No on-server backup file: tar is streamed directly to the local backup root.
    try:
        run("systemctl", "stop", "extore-worker.service", stdout=sys.stderr)
        run("systemctl", "stop", "extore-api.service", stdout=sys.stderr)
        summary = database_summary()
        save("snapshot.json", {"sha": sha, "database": summary})
        value = load("prepared.json")
        value["old_database"] = summary
        save("prepared.json", value)
        paths = RUNTIME_PATHS + ["var/lib/extore", value["old_pacman_entry"]]
        if (STATE / "current.json").exists():
            paths.append("var/lib/extore-deploy/current.json")
        run(
            "tar",
            "--acls",
            "--xattrs",
            "--numeric-owner",
            "-czf",
            "-",
            "-C",
            "/",
            *paths,
        )
    finally:
        run("systemctl", "start", *SERVICES, stdout=sys.stderr)


def verify(sha, value=None):
    value = value or prepared(sha)
    sites = list(Path("/usr/lib/extore/.venv/lib").glob("python*/site-packages"))
    if len(sites) != 1:
        raise RuntimeError("Expected exactly one installed Python site directory")
    for name, expected in value["files"].items():
        if digest(sites[0] / name) != expected:
            raise RuntimeError(f"Installed source checksum mismatch: {name}")
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
            api = json.loads(
                capture(
                    "curl",
                    "-fsS",
                    "--max-time",
                    "3",
                    "http://127.0.0.1:8095/openapi.json",
                )
            )
            if (
                health == {"status": "ok"}
                and api["info"]["version"] == value["version"]
            ):
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
        "package": capture("pacman", "-Q", "extore"),
        "database": database_summary(),
    }


def activate(sha):
    value = prepared(sha)
    if sha != main_sha():
        return {"status": "superseded", "sha": sha}
    if load("snapshot.json")["sha"] != sha:
        raise RuntimeError("Deployment snapshot is missing")
    run("systemctl", "stop", "extore-worker.service", stdout=sys.stderr)
    run("systemctl", "stop", "extore-api.service", stdout=sys.stderr)
    run("pacman", "-U", "--noconfirm", str(WORK / value["package"]), stdout=sys.stderr)
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
            "Schema changed; preserve data and recover manually from the local backup"
        )
    # Restore program/config files only. Never overwrite new business transactions.
    shutil.rmtree("/usr/lib/extore")
    for entry in Path("/var/lib/pacman/local").iterdir():
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
        "/",
        *RUNTIME_PATHS,
        value["old_pacman_entry"],
        stdin=sys.stdin.buffer,
        stdout=sys.stderr,
    )
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
        "prepare": prepare,
        "snapshot": snapshot,
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
