"""Root-owned, forced-command deployment endpoint on the production server."""

import argparse
import fcntl
import gzip
import hashlib
import json
import os
import re
import selectors
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
from contextlib import contextmanager
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
HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
MAX_PACKAGE_SIZE = 128 * 1024 * 1024
MAX_PROVENANCE_SIZE = 256 * 1024
STAGE_FIELDS = {
    "format",
    "sha",
    "source_sha256",
    "package_sha256",
    "size",
    "provenance_size",
}
STAGE_FILES = (
    "staged-package.json",
    "staged-package.tar.zst",
    "staged-provenance.jsonl",
)
CERT_IDENTITY = "https://github.com/TokenNotIncluded/extore/.github/workflows/deploy.yml@refs/heads/main"


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


def source_content_digest(path):
    """Bind deterministic tar content independently of the gzip implementation."""
    with gzip.open(path, "rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


@contextmanager
def operation_lock():
    """Keep upload, preparation, recovery and installation in one serial lane."""
    STATE.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(
        STATE / "operation.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    try:
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another deployment operation is running") from None
        yield
    finally:
        os.close(fd)


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate staging header field")
        value[key] = item
    return value


def validate_stage_header(value, sha, *, stored=False):
    fields = STAGE_FIELDS | ({"provenance_sha256"} if stored else set())
    if not isinstance(value, dict) or set(value) != fields:
        raise RuntimeError("Unexpected staging header fields")
    if type(value["format"]) is not int or value["format"] != 1 or value["sha"] != sha:
        raise RuntimeError("Staging format or commit does not match")
    for field in ["source_sha256", "package_sha256"] + (
        ["provenance_sha256"] if stored else []
    ):
        if not isinstance(value[field], str) or not HASH_PATTERN.fullmatch(
            value[field]
        ):
            raise RuntimeError("Invalid staging digest")
    for field, maximum in [
        ("size", MAX_PACKAGE_SIZE),
        ("provenance_size", MAX_PROVENANCE_SIZE),
    ]:
        if type(value[field]) is not int or not 1 <= value[field] <= maximum:
            raise RuntimeError("Invalid staging size")
    return value


def require_provenance(package, bundle, sha):
    """Only the main deployment workflow may attest this exact package and SHA."""
    result = run(
        "/usr/bin/gh",
        "attestation",
        "verify",
        str(package),
        "--bundle",
        str(bundle),
        "--repo",
        "TokenNotIncluded/extore",
        "--cert-identity",
        CERT_IDENTITY,
        "--source-ref",
        "refs/heads/main",
        "--source-digest",
        require_sha(sha),
        "--signer-digest",
        sha,
        "--deny-self-hosted-runners",
        "--format",
        "json",
        stdout=subprocess.PIPE,
        stderr=sys.stderr,
        text=True,
        timeout=120,
        env={
            "HOME": "/root",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "GH_CONFIG_DIR": str(STATE / "gh-config"),
            "XDG_CACHE_HOME": str(STATE / "gh-cache"),
            "LANG": "C.UTF-8",
            "GH_PROMPT_DISABLED": "1",
        },
    )
    verified = json.loads(result.stdout)
    if (
        not isinstance(verified, list)
        or not verified
        or any(not isinstance(item, dict) or not item for item in verified)
    ):
        raise RuntimeError("No verified GitHub deployment attestation")
    for item in verified:
        try:
            repository_id = item["verificationResult"]["signature"]["certificate"][
                "sourceRepositoryIdentifier"
            ]
        except (KeyError, TypeError):
            raise RuntimeError(
                "Verified attestation has no repository identity"
            ) from None
        if str(repository_id) != "1407071795":
            raise RuntimeError("Verified attestation belongs to another repository")


def reject_unverified_recovery():
    if (STATE / "recovery.tar.gz").exists():
        info = load("snapshot.json")
        if info.get("offhost_verified") is not True:
            raise RuntimeError("An unverified recovery snapshot must be preserved")


def receive_exact(stream, target, size):
    remaining = size
    checksum = hashlib.sha256()
    with target.open("xb") as output:
        target.chmod(0o600)
        while remaining:
            chunk = stream.read(min(remaining, 64 * 1024))
            if not chunk:
                raise RuntimeError("Staging upload was truncated")
            output.write(chunk)
            checksum.update(chunk)
            remaining -= len(chunk)
        output.flush()
        os.fsync(output.fileno())
    return checksum.hexdigest()


def stage_package(sha):
    require_sha(sha)
    if sha != main_sha():
        raise RuntimeError("Staging commit is no longer main")
    reject_unverified_recovery()
    stream = sys.stdin.buffer
    line = stream.readline(4097)
    if len(line) > 4096 or not line.endswith(b"\n"):
        raise RuntimeError(
            "Staging header must be one ASCII JSON line of at most 4096 bytes"
        )
    try:
        header = json.loads(line.decode("ascii"), object_pairs_hook=unique_object)
    except (UnicodeDecodeError, ValueError) as error:
        raise RuntimeError("Invalid ASCII JSON staging header") from error
    validate_stage_header(header, sha)
    STATE.mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f"staged-upload-{sha}-", dir=STATE
    ) as temporary:
        temporary = Path(temporary)
        package = temporary / "package.partial"
        bundle = temporary / "provenance.partial"
        provenance_hash = receive_exact(stream, bundle, header["provenance_size"])
        package_hash = receive_exact(stream, package, header["size"])
        if stream.read(1):
            raise RuntimeError("Staging upload has trailing bytes")
        if package_hash != header["package_sha256"]:
            raise RuntimeError("Staging package checksum mismatch")
        require_provenance(package, bundle, sha)
        if sha != main_sha():
            raise RuntimeError("Main changed during the staging upload")
        reject_unverified_recovery()
        metadata = {**header, "provenance_sha256": provenance_hash}
        # Each file is atomically replaced. A crash between replacements remains
        # unusable because prepare rechecks the complete tuple before copying it.
        package.replace(STATE / "staged-package.tar.zst")
        bundle.replace(STATE / "staged-provenance.jsonl")
        save("staged-package.json", metadata)
    return {
        "status": "staged",
        "sha": sha,
        "package_sha256": package_hash,
        "source_sha256": header["source_sha256"],
        "size": header["size"],
    }


def staged_package(sha, source_hash):
    paths = [STATE / name for name in STAGE_FILES]
    if not any(os.path.lexists(path) for path in paths):
        return None
    if any(path.is_symlink() or not path.is_file() for path in paths):
        raise RuntimeError("Incomplete or invalid staged package")
    metadata = validate_stage_header(load("staged-package.json"), sha, stored=True)
    if metadata["source_sha256"] != source_hash:
        raise RuntimeError("Staged source archive does not match the verified checkout")
    for path, length, checksum in [
        (paths[1], metadata["size"], metadata["package_sha256"]),
        (paths[2], metadata["provenance_size"], metadata["provenance_sha256"]),
    ]:
        if path.stat().st_size != length or digest(path) != checksum:
            raise RuntimeError("Staged package or provenance checksum mismatch")
    require_provenance(paths[1], paths[2], sha)
    destination = WORK / "extore-prebuilt.pkg.tar.zst"
    with paths[1].open("rb") as source, destination.open("xb") as output:
        destination.chmod(0o600)
        shutil.copyfileobj(source, output)
    if digest(destination) != metadata["package_sha256"]:
        raise RuntimeError("Copied prebuilt package checksum mismatch")
    return destination


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


def native_source_files(checkout):
    """The noncompiled package payload must be byte-for-byte from this checkout."""
    files = {}
    for directory in [
        "extore/static",
        "extore/sdk",
        "processors/official/extore_processors",
    ]:
        for path in (checkout / directory).rglob("*"):
            if (
                path.is_file()
                and not path.is_symlink()
                and "__pycache__" not in path.parts
                and path.suffix not in {".pyc", ".pyo"}
            ):
                files["usr/lib/extore/" + path.relative_to(checkout).as_posix()] = path
    for name in NATIVE_MODULES:
        files["usr/lib/extore/extore/" + name] = checkout / "extore" / name
    for installed, source in {
        "usr/lib/systemd/system/extore-api.service": "deploy/arch/extore-api.service",
        "usr/lib/systemd/system/extore-worker.service": "deploy/arch/extore-worker.service",
        "usr/lib/sysusers.d/extore.conf": "deploy/arch/extore.sysusers",
        "usr/bin/extore-admin": "deploy/arch/extore-admin",
        "usr/share/licenses/extore/LICENSE": "LICENSE",
        "usr/share/licenses/extore/official-processors-LICENSE": "processors/official/LICENSE",
    }.items():
        files[installed] = checkout / source
    if any(path.is_symlink() or not path.is_file() for path in files.values()):
        raise RuntimeError("Verified checkout is missing a native package source")
    return files


def probe_native_version(binary, version):
    """Load the native binary as the build user, with no database environment."""
    process = subprocess.Popen(
        ["runuser", "-u", BUILD_USER, "--", str(binary), "--version"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"},
    )
    output = bytearray()
    deadline = time.monotonic() + 15
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("Native package version/ABI probe timed out")
                if not selector.select(remaining):
                    raise RuntimeError("Native package version/ABI probe timed out")
                chunk = os.read(process.stdout.fileno(), 4097 - len(output))
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > 4096:
                    raise RuntimeError(
                        "Native package version/ABI probe output is too large"
                    )
        process.wait(timeout=max(0.01, deadline - time.monotonic()))
        if (
            process.returncode != 0
            or output.decode("utf-8", errors="replace").strip()
            != f"extore {version} (Rust)"
        ):
            raise RuntimeError("Native package version or host ABI check failed")
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if process.poll() is None:
            process.wait()
        process.stdout.close()


def validate_prebuilt(package, checkout, manifest, version):
    sources = native_source_files(checkout)
    binary_name = "usr/lib/extore/bin/extore"
    if set(manifest["files"]) != set(sources) | {binary_name}:
        raise RuntimeError("Prebuilt package file set differs from the verified source")
    for name, source in sources.items():
        if manifest["files"][name] != digest(source):
            raise RuntimeError(f"Prebuilt package source mismatch: {name}")
    for name, mode in manifest["file_modes"].items():
        if mode != (0o755 if name in {binary_name, "usr/bin/extore-admin"} else 0o644):
            raise RuntimeError(f"Prebuilt package permissions mismatch: {name}")
    pkgrel = re.search(
        r"(?m)^pkgrel=['\"]?([0-9]+(?:\.[0-9]+)?)['\"]?\s*$",
        (checkout / "deploy/arch/PKGBUILD").read_text(),
    )
    if pkgrel is None:
        raise RuntimeError("Verified PKGBUILD needs a literal package release")
    identity = capture("pacman", "-Qp", str(package))
    if identity != f"extore {version}-{pkgrel.group(1)}":
        raise RuntimeError("Prebuilt package identity differs from the verified source")
    template = "etc/extore/extore.env"
    allowed_directories = {"."}
    for name in [*sources, binary_name, template, *NATIVE_LINKS]:
        allowed_directories.update(
            str(parent) for parent in PurePosixPath(name).parents
        )
    # Do not extract an uploaded tar tree as root. Only copy the already hashed
    # ELF member to a temporary path for an unprivileged loader/version check.
    with tempfile.TemporaryDirectory(prefix="native-abi-", dir=WORK) as temporary:
        directory = Path(temporary)
        shutil.chown(directory, BUILD_USER, BUILD_USER)
        binary = directory / "extore"
        found_template = found_metadata = False
        with subprocess.Popen(
            ["zstd", "-dc", "--", str(package)],
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
        ) as process:
            try:
                with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
                    for member in archive:
                        name = payload_name(member.name)
                        if member.isdir():
                            if (
                                name not in allowed_directories
                                or stat.S_IMODE(member.mode) != 0o755
                            ):
                                raise RuntimeError(
                                    "Unexpected prebuilt package directory"
                                )
                        elif name == template:
                            if (
                                not member.isfile()
                                or stat.S_IMODE(member.mode) != 0o644
                            ):
                                raise RuntimeError(
                                    "Invalid prebuilt configuration template"
                                )
                            with archive.extractfile(member) as source:
                                checksum = hashlib.file_digest(
                                    source, "sha256"
                                ).hexdigest()
                            if checksum != digest(checkout / "deploy/arch/extore.env"):
                                raise RuntimeError(
                                    "Prebuilt configuration template differs from the source"
                                )
                            found_template = True
                        elif name == binary_name:
                            if (
                                not member.isfile()
                                or not 1 <= member.size <= MAX_PACKAGE_SIZE
                            ):
                                raise RuntimeError("Invalid native binary size or type")
                            with (
                                archive.extractfile(member) as source,
                                binary.open("xb") as target,
                            ):
                                shutil.copyfileobj(source, target)
                            if digest(binary) != manifest["files"][binary_name]:
                                raise RuntimeError(
                                    "Extracted native binary checksum mismatch"
                                )
                        elif name == ".PKGINFO":
                            if not member.isfile() or member.size > 64 * 1024:
                                raise RuntimeError("Invalid package metadata")
                            with archive.extractfile(member) as source:
                                info = source.read().decode("utf-8")
                            if re.findall(r"(?m)^arch = (.+)$", info) != ["x86_64"]:
                                raise RuntimeError(
                                    "Unexpected native package architecture"
                                )
                            found_metadata = True
                while process.stdout.read(1024 * 1024):
                    pass
            except BaseException:
                process.kill()
                raise
            if process.wait() != 0:
                raise RuntimeError("Could not extract the native ABI probe")
        if not found_template or not found_metadata or not binary.is_file():
            raise RuntimeError("Incomplete native package ABI probe")
        with binary.open("rb") as source:
            if source.read(4) != b"\x7fELF":
                raise RuntimeError("Prebuilt native executable is not an ELF binary")
        binary.chmod(0o755)
        probe_native_version(binary, version)
    return identity


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
    source_content_sha256 = source_content_digest(WORK / "extore-source.tar.gz")
    package_build = WORK / "PKGBUILD"
    package_build.write_text(
        package_build.read_text().replace(
            "sha256sums=('SKIP')", f"sha256sums=('{archive_sha256}')"
        )
    )
    prebuilt = staged_package(sha, source_content_sha256)
    if prebuilt is None:
        build("makepkg", "--noconfirm", "--cleanbuild", "--clean", cwd=WORK)
        packages = list(WORK.glob("extore-*.pkg.tar.zst"))
    else:
        if runtime != "rust":
            raise RuntimeError("Prebuilt packages require the native Rust source")
        packages = [prebuilt]
    if len(packages) != 1:
        raise RuntimeError("Expected exactly one built Extore package")
    if runtime == "rust":
        manifest = package_manifest(packages[0])
        package_id = (
            validate_prebuilt(packages[0], checkout, manifest, version)
            if prebuilt is not None
            else capture("pacman", "-Qp", str(packages[0]))
        )
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
        package_id = capture("pacman", "-Qp", str(packages[0]))
    value = {
        "sha": sha,
        "version": version,
        "runtime": runtime,
        "package": packages[0].name,
        "package_sha256": digest(packages[0]),
        "package_id": package_id,
        "source_sha256": archive_sha256,
        "source_content_sha256": source_content_sha256,
        **manifest,
        "old_package": capture("pacman", "-Q", "extore"),
        "old_database": database_summary(),
    }
    value["old_pacman_entry"] = (
        "var/lib/pacman/local/extore-" + value["old_package"].split()[1]
    )
    if not (ROOT / value["old_pacman_entry"]).is_dir():
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
    staged = STATE / "staged-package.json"
    if staged.exists():
        validate_stage_header(load("staged-package.json"), sha, stored=True)
    elif any(os.path.lexists(STATE / name) for name in STAGE_FILES[1:]):
        raise RuntimeError("Unidentified staged package must not be removed")
    for name in ["recovery.tar.gz", "recovery.partial"]:
        (STATE / name).unlink(missing_ok=True)
    if WORK.exists():
        shutil.rmtree(WORK)
    cache = WORK.parent / "cache"
    if cache.exists():
        shutil.rmtree(cache)
    for name in ["prepared.json", "snapshot.json"]:
        (STATE / name).unlink(missing_ok=True)
    for name in STAGE_FILES:
        (STATE / name).unlink(missing_ok=True)
    for temporary in STATE.glob(f"staged-upload-{sha}-*"):
        if temporary.is_dir() and not temporary.is_symlink():
            shutil.rmtree(temporary)
    return {"status": "cleaned", "sha": sha}


SCHEMA_23_ADDITIONS = {
    "commerce_bindings": (
        "table",
        "CREATE TABLE commerce_bindings (id TEXT PRIMARY KEY,shop_id TEXT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,key_digest TEXT UNIQUE NOT NULL,label TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL,revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)))",
    ),
    "commerce_bindings_shop": (
        "index",
        "CREATE INDEX commerce_bindings_shop ON commerce_bindings(shop_id,revoked)",
    ),
}


def rollback_schema_compatible(previous, current):
    """Only the verified 22-to-23 catalogue-binding addition may differ."""
    versions = (previous.get("user_version"), current.get("user_version"))
    if current["schema_sha256"] == previous["schema_sha256"]:
        return versions[0] == versions[1] or versions == (22, 23)
    if versions != (22, 23):
        return False
    with sqlite3.connect(
        f"file:{DATA / 'extore.sqlite3'}?mode=ro", uri=True
    ) as connection:
        schema = connection.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name"
        ).fetchall()
    additions = {
        name: (kind, sql) for kind, name, sql in schema if name in SCHEMA_23_ADDITIONS
    }
    if additions.keys() != SCHEMA_23_ADDITIONS.keys():
        return False
    for name, (expected_kind, expected_sql) in SCHEMA_23_ADDITIONS.items():
        kind, sql = additions[name]
        if (
            kind != expected_kind
            or "".join(sql.split()).lower() != "".join(expected_sql.split()).lower()
        ):
            return False
    baseline = [row for row in schema if row[1] not in SCHEMA_23_ADDITIONS]
    return (
        hashlib.sha256(json.dumps(baseline).encode()).hexdigest()
        == previous["schema_sha256"]
    )


def rollback(sha):
    value = prepared(sha)
    run("systemctl", "stop", *SERVICES, stdout=sys.stderr)
    if not rollback_schema_compatible(value["old_database"], database_summary()):
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
        "stage_package": stage_package,
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
    with operation_lock():
        result = operations[operation](sha)
    if result is not None:
        print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
