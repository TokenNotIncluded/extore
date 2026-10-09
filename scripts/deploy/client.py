"""GitHub-hosted deployment coordinator using repository Secrets over SSH."""

import argparse
import concurrent.futures
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
from pathlib import Path

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


def output(name, value):
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as file:
        file.write(f"{name}={value}\n")


def save_state(directory, value):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    (directory / "state.json").write_text(json.dumps(value, sort_keys=True) + "\n")


def load_state(directory):
    return json.loads((directory / "state.json").read_text())


def make_backup(value, config, directory):
    """Verify a snapshot and encrypt it; acknowledgement follows artifact upload."""
    sha = value["sha"]
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    archive = directory / "recovery.tar.gz"
    emit("Creating a consistent snapshot; services resume before the SSH transfer")
    with archive.open("wb") as file:
        remote(config, "snapshot", sha, stdout=file, timeout=360)
    with tempfile.TemporaryDirectory(prefix="restore-check-", dir=directory) as restore:
        restored = verify_backup(archive, Path(restore))
    info = rpc(config, "snapshot_info", sha)
    archive_hash = digest(archive)
    if archive_hash != info["archive_sha256"]:
        raise RuntimeError("Runner and server backup checksums differ")
    value["old_database"] = info["database"]
    artifact = directory / "artifact"
    artifact.mkdir(mode=0o700)
    encrypted = artifact / "recovery.tar.gz.age"
    subprocess.run(
        [
            "age",
            "--recipient",
            config["backup_recipient"],
            "--output",
            str(encrypted),
            str(archive),
        ],
        check=True,
        timeout=120,
    )
    (artifact / "SHA256SUMS").write_text(f"{digest(encrypted)}  recovery.tar.gz.age\n")
    (artifact / "manifest.json").write_text(
        json.dumps(
            {"sha": sha, "version": value["version"], "archive_sha256": archive_hash},
            sort_keys=True,
        )
        + "\n"
    )
    save_state(
        directory,
        {"value": value, "restored_database": restored, "archive_sha256": archive_hash},
    )
    output("backup", "true")
    emit(
        "Snapshot checksum and independently restored SQLite verified; encrypted artifact ready"
    )


def require_artifact(artifact_id):
    if not re.fullmatch(r"[1-9][0-9]*", artifact_id or ""):
        raise RuntimeError("A successfully uploaded recovery artifact is required")


def recover(sha, config, directory):
    value = rpc(config, "pending", sha)
    if value["status"] == "pending_backup":
        emit("Preserving the recovery snapshot left by an interrupted deployment")
        make_backup(value, config, directory)
    else:
        output("backup", "false")


def drain(config, directory, artifact_id):
    require_artifact(artifact_id)
    sha = load_state(directory)["value"]["sha"]
    rpc(config, "acknowledge", sha)
    rpc(config, "cleanup", sha)
    shutil.rmtree(directory)
    emit(
        "Previous recovery snapshot uploaded and verified; server temporary copy cleared"
    )


def prepare(sha, config, directory):
    emit(f"Preparing main {require_sha(sha)}")
    try:
        value = rpc(config, "prepare", sha)
        if value["status"] == "pending_backup":
            raise RuntimeError(
                "An interrupted snapshot must be recovered before preparing"
            )
        if value["status"] == "current":
            public_check(config, value)
            rpc(config, "cleanup", sha)
            emit(f"Already deployed and verified: {sha}")
        elif value["status"] == "superseded":
            emit("A newer main commit exists; this deployment was skipped")
        else:
            make_backup(value, config, directory)
        output("status", value["status"])
    except Exception:
        emit("Preparation failed; unverified server recovery snapshots are retained")
        rpc(config, "cleanup", sha)
        raise


def activate(sha, config, directory, artifact_id):
    require_artifact(artifact_id)
    state = load_state(directory)
    value = state["value"]
    if value["sha"] != require_sha(sha):
        raise RuntimeError("Prepared deployment does not match the requested commit")
    archive = directory / "recovery.tar.gz"
    if digest(archive) != state["archive_sha256"]:
        raise RuntimeError("Verified recovery archive changed before activation")
    rpc(config, "acknowledge", sha)
    activated = False
    try:
        activated = True
        result = rpc(config, "activate", sha)
        if result["status"] == "superseded":
            activated = False
            emit("A newer main commit exists; activation was skipped")
            return "superseded"
        public_check(config, value)
        final = rpc(config, "confirm", sha)
        count = sum(name.startswith("extore/static/") for name in value["files"])
        report = {
            "sha": sha,
            "version": value["version"],
            "backup_artifact_id": artifact_id,
            "backup_sha256": state["archive_sha256"],
            "restored_database": state["restored_database"],
            "before": value["old_database"],
            "after": final["database"],
            "package": final["package"],
            "public_static_files_verified": count,
        }
        (directory / "result.json").write_text(
            json.dumps(report, sort_keys=True) + "\n"
        )
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as file:
            file.write(
                f"Deployed `{sha}`: `{final['package']}`. API, worker, SQLite and {count} public assets verified. Recovery artifact: `{artifact_id}`.\n"
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
                    f"Automatic rollback could not finish; encrypted recovery artifact: {artifact_id}"
                )
        raise
    finally:
        rpc(config, "cleanup", sha)


def configuration(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    host = os.environ["EXTORE_DEPLOY_HOST"]
    user = os.environ["EXTORE_DEPLOY_USER"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]*", host) or not re.fullmatch(
        r"[a-z_][a-z0-9_-]*", user
    ):
        raise ValueError("Invalid deployment host or SSH user")
    identity = directory / "id_ed25519"
    identity.write_text(os.environ["EXTORE_DEPLOY_SSH_KEY"].strip() + "\n")
    identity.chmod(0o600)
    known_hosts = directory / "known_hosts"
    known_hosts.write_text(os.environ["EXTORE_DEPLOY_KNOWN_HOSTS"].strip() + "\n")
    known_hosts.chmod(0o600)
    return {
        "identity": str(identity),
        "known_hosts": str(known_hosts),
        "target": f"{user}@{host}",
        "origin": "https://extore.lmm.best",
        "backup_recipient": os.environ["EXTORE_BACKUP_RECIPIENT"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "stage", choices=["recover", "drain", "prepare", "activate", "clean"]
    )
    parser.add_argument("sha")
    args = parser.parse_args()
    require_sha(args.sha)
    os.umask(0o077)
    work = Path(os.environ["RUNNER_TEMP"]) / "extore-deploy"
    if args.stage == "clean":
        if work.exists():
            shutil.rmtree(work)
        return
    config = configuration(work / "ssh")
    if args.stage == "recover":
        recover(args.sha, config, work / "recovery")
    elif args.stage == "drain":
        drain(config, work / "recovery", os.environ.get("EXTORE_BACKUP_ARTIFACT_ID"))
    elif args.stage == "prepare":
        prepare(args.sha, config, work / "deployment")
    else:
        status = activate(
            args.sha,
            config,
            work / "deployment",
            os.environ.get("EXTORE_BACKUP_ARTIFACT_ID"),
        )
        emit(f"EXTORE_DEPLOY_RESULT={status}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        emit(f"Deployment failed: {type(error).__name__}: {error}")
        emit("EXTORE_DEPLOY_RESULT=failed")
        sys.exit(1)
