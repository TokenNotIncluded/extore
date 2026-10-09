"""Trusted local deployment coordinator; never executed from a runner checkout."""

import argparse
import concurrent.futures
import fcntl
import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

CONFIG = Path("/etc/extore-deploy/config.json")
LOCK = Path("/var/lib/extore-deploy/deploy.lock")
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")


def require_sha(sha):
    if not SHA_PATTERN.fullmatch(sha):
        raise ValueError("Expected a full lowercase Git commit SHA")
    return sha


def emit(message):
    print(message, flush=True)


def command(config, operation, sha):
    argv = [
        "ssh",
        "-F",
        "/dev/null",
        "-i",
        config["identity"],
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=20",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        f"UserKnownHostsFile={config['known_hosts']}",
    ]
    if config.get("proxy_command"):
        argv.extend(["-o", "ProxyCommand=" + config["proxy_command"]])
    return argv + [config["target"], f"{operation} {require_sha(sha)}"]


def remote(config, operation, sha, **kwargs):
    return subprocess.run(command(config, operation, sha), check=True, **kwargs)


def rpc(config, operation, sha):
    result = remote(
        config, operation, sha, stdout=subprocess.PIPE, text=True, timeout=1100
    )
    return json.loads(result.stdout)


def digest(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def verify_backup(path, target):
    """Read the whole archive and open an independently restored database."""
    with gzip.open(path, "rb") as compressed:
        while compressed.read(1024 * 1024):
            pass
    with tarfile.open(path, "r:gz") as archive:
        names = set()
        for member in archive:
            parts = Path(member.name).parts
            if member.name.startswith("/") or ".." in parts or member.name in names:
                raise RuntimeError("Unsafe or duplicate backup archive path")
            names.add(member.name)
            if member.isfile():
                with archive.extractfile(member) as file:
                    while file.read(1024 * 1024):
                        pass
        for required in [
            "var/lib/extore/extore.sqlite3",
            "var/lib/extore/issuance.key",
            "var/lib/extore/master-secrets.key",
            "etc/extore/extore.env",
        ]:
            if required not in names:
                raise RuntimeError("A required recovery file is missing")
            if not archive.getmember(required).isfile():
                raise RuntimeError("Recovery files must be regular files")
        for name in ["extore.sqlite3", "extore.sqlite3-wal", "extore.sqlite3-shm"]:
            member_name = "var/lib/extore/" + name
            if member_name in names:
                member = archive.getmember(member_name)
                if not member.isfile():
                    raise RuntimeError("Recovery database must be a regular file")
                with (
                    archive.extractfile(member) as source,
                    (target / name).open("wb") as output,
                ):
                    shutil.copyfileobj(source, output)
                (target / name).chmod(0o600)
    with sqlite3.connect(target / "extore.sqlite3") as connection:
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise RuntimeError("Restored backup database failed integrity_check")
        return {
            "user_version": connection.execute("PRAGMA user_version").fetchone()[0],
            "table_count": connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()[0],
        }


def public_check(config, value):
    origin = config["origin"].rstrip("/")

    def fetch(path):
        return subprocess.check_output(
            [
                "curl",
                "-fsS",
                "--retry",
                "4",
                "--retry-all-errors",
                "--retry-delay",
                "2",
                "--max-time",
                "15",
                origin + path,
            ]
        )

    if json.loads(fetch("/health")) != {"status": "ok"}:
        raise RuntimeError("Public health check failed")
    if json.loads(fetch("/openapi.json"))["info"]["version"] != value["version"]:
        raise RuntimeError("Public API version mismatch")

    def check_asset(item):
        name, expected = item
        if name.startswith("extore/static/"):
            path = (
                "/AGENTS.md"
                if name == "extore/static/AGENTS.md"
                else "/static/" + name.removeprefix("extore/static/")
            )
            actual = hashlib.sha256(
                fetch(path + "?deploy=" + value["sha"][:12])
            ).hexdigest()
            if actual != expected:
                raise RuntimeError(f"Public static checksum mismatch: {name}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(check_asset, value["files"].items()))


def handover(backup, config):
    owner = config.get("backup_owner")
    if owner:
        for path in backup.rglob("*"):
            path.chmod(0o700 if path.is_dir() else 0o600)
            shutil.chown(path, owner, owner)
        shutil.chown(backup, owner, owner)


def make_backup(value, config, backup_root):
    sha = value["sha"]
    backup = backup_root / (time.strftime("%Y%m%d-%H%M%S") + "-" + sha[:12])
    backup.mkdir(parents=True, mode=0o700)
    archive = backup / "recovery.tar.gz"
    (backup / "prepared.json").write_text(
        json.dumps(value, sort_keys=True, indent=2) + "\n"
    )
    try:
        emit(
            "Creating a consistent snapshot, restoring services, then transferring it locally"
        )
        with archive.open("wb") as output:
            remote(config, "snapshot", sha, stdout=output, timeout=360)
        with tempfile.TemporaryDirectory(
            prefix="restore-check-", dir=backup
        ) as directory:
            restored = verify_backup(archive, Path(directory))
        archive_hash = digest(archive)
        info = rpc(config, "snapshot_info", sha)
        if archive_hash != info["archive_sha256"]:
            raise RuntimeError("Local and server backup checksums differ")
        (backup / "SHA256SUMS").write_text(f"{archive_hash}  recovery.tar.gz\n")
        (backup / "backup-verification.json").write_text(
            json.dumps(
                {
                    "sha": sha,
                    "archive_sha256": archive_hash,
                    "restored_database": restored,
                    "database": info["database"],
                },
                sort_keys=True,
                indent=2,
            )
            + "\n"
        )
        rpc(config, "acknowledge", sha)
        value["old_database"] = info["database"]
        emit(f"Backup archive and independently restored SQLite verified: {backup}")
        return backup, restored, archive_hash
    finally:
        handover(backup, config)


def deploy(sha, config):
    require_sha(sha)
    backup_root = Path(config["backup_root"]).resolve()
    if not backup_root.is_relative_to("/home/lightjunction/Backups"):
        raise RuntimeError("Backups must stay under /home/lightjunction/Backups")
    emit(f"Preparing main {sha}")
    try:
        value = rpc(config, "prepare", sha)
        if value["status"] == "pending_backup":
            emit(
                "Archiving a previous interrupted snapshot before preparing this deployment"
            )
            make_backup(value, config, backup_root)
            rpc(config, "cleanup", value["sha"])
            value = rpc(config, "prepare", sha)
    except Exception:
        emit("Preparation failed; preserving any unverified server recovery snapshot")
        try:
            rpc(config, "cleanup", sha)
        except Exception:
            emit("Preparation cleanup needs attention")
        raise
    if value["status"] == "superseded":
        emit("A newer main commit exists; this deployment was skipped")
        return "superseded"
    if value["status"] == "current":
        public_check(config, value)
        rpc(config, "cleanup", sha)
        emit(f"Already deployed and verified: {sha}")
        return "current"
    activated = False
    backup = None
    try:
        backup, restored, archive_hash = make_backup(value, config, backup_root)
        archive = backup / "recovery.tar.gz"
        activated = True
        result = rpc(config, "activate", sha)
        if result["status"] == "superseded":
            activated = False
            emit("A newer main commit exists; activation was skipped")
            return "superseded"
        public_check(config, value)
        final = rpc(config, "confirm", sha)
        report = {
            "sha": sha,
            "version": value["version"],
            "backup_sha256": archive_hash,
            "restored_database": restored,
            "before": value["old_database"],
            "after": final["database"],
            "package": final["package"],
            "public_static_files_verified": sum(
                name.startswith("extore/static/") for name in value["files"]
            ),
        }
        (backup / "result.json").write_text(
            json.dumps(report, sort_keys=True, indent=2) + "\n"
        )
        emit(
            f"Deployed {final['package']} at {sha}; API, worker, database and public assets verified"
        )
        return "deployed"
    except Exception:
        if activated:
            emit(
                "Deployment failed; restoring program files if the database schema is unchanged"
            )
            try:
                with archive.open("rb") as source:
                    remote(
                        config,
                        "rollback",
                        sha,
                        stdin=source,
                        stdout=subprocess.PIPE,
                        timeout=360,
                    )
                emit("Previous program files restored; current business data preserved")
            except Exception:
                emit(
                    f"Automatic rollback could not finish; recovery archive: {archive}"
                )
        raise
    finally:
        try:
            cleanup = rpc(config, "cleanup", sha)
            if cleanup["status"] == "pending_backup":
                emit(
                    "Unverified recovery snapshot retained on the server for the next transfer"
                )
        except Exception:
            emit("Remote cleanup needs attention")
            raise
        finally:
            if backup is not None:
                handover(backup, config)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sha", nargs="?")
    parser.add_argument("--socket", action="store_true")
    args = parser.parse_args()
    sha = args.sha
    if args.socket:
        raw = sys.stdin.buffer.readline(42)
        if len(raw) != 41 or not raw.endswith(b"\n"):
            raise ValueError("Expected exactly one commit SHA")
        sha = raw[:-1].decode("ascii")
    require_sha(sha or "")
    if os.geteuid() != 0:
        raise RuntimeError("The coordinator must run as root")
    os.umask(0o077)
    config = json.loads(CONFIG.read_text())
    LOCK.parent.mkdir(mode=0o700, exist_ok=True)
    with LOCK.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        status = deploy(sha, config)
    emit(f"EXTORE_DEPLOY_RESULT={status}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        emit(f"Deployment failed: {type(error).__name__}: {error}")
        emit("EXTORE_DEPLOY_RESULT=failed")
        sys.exit(1)
