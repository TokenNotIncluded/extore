"""Attested package transport never bypasses source, recovery or activation gates."""

import gzip
import hashlib
import io
import json
import runpy
import shutil
import stat
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
SHA = "a" * 40
SOURCE_TAR = b"independently generated deterministic source tar"
SOURCE = gzip.compress(SOURCE_TAR, mtime=0)
SOURCE_HASH = hashlib.sha256(SOURCE_TAR).hexdigest()
BUNDLE = b'{"signed":"fixture"}\n'


def frame(package=b"package", *, update=None, trailing=b"", bundle=BUNDLE):
    header = {
        "format": 1,
        "sha": SHA,
        "source_sha256": SOURCE_HASH,
        "package_sha256": hashlib.sha256(package).hexdigest(),
        "size": len(package),
        "provenance_size": len(bundle),
    }
    header.update(update or {})
    return (
        json.dumps(header, separators=(",", ":")).encode()
        + b"\n"
        + bundle
        + package
        + trailing
    )


@pytest.fixture
def endpoint(tmp_path, monkeypatch):
    module = runpy.run_path(str(REPO / "scripts/deploy/server.py"))
    namespace = module["stage_package"].__globals__
    state, work, root = tmp_path / "state", tmp_path / "build/work", tmp_path / "root"
    state.mkdir()
    work.parent.mkdir()
    live = root / "usr/lib/extore/program"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"live runtime untouched")
    data = root / "var/lib/extore"
    data.mkdir(parents=True)
    (data / "extore.sqlite3").write_bytes(b"live business database untouched")
    for key, value in {
        "STATE": state,
        "WORK": work,
        "ROOT": root,
        "DATA": data,
    }.items():
        monkeypatch.setitem(namespace, key, value)
    monkeypatch.setitem(namespace, "main_sha", lambda: SHA)
    calls = []

    def provenance(package, bundle, sha):
        assert stat.S_IMODE(package.stat().st_mode) == 0o600
        assert stat.S_IMODE(bundle.stat().st_mode) == 0o600
        calls.append((sha, package.read_bytes(), bundle.read_bytes()))

    monkeypatch.setitem(namespace, "require_provenance", provenance)
    return SimpleNamespace(
        ns=namespace,
        state=state,
        work=work,
        root=root,
        data=data,
        live=live,
        calls=calls,
    )


def upload(endpoint, monkeypatch, data):
    monkeypatch.setitem(
        endpoint.ns,
        "sys",
        SimpleNamespace(
            stdin=SimpleNamespace(buffer=io.BytesIO(data)), stderr=sys.stderr
        ),
    )
    return endpoint.ns["stage_package"](SHA)


def unchanged(endpoint):
    assert endpoint.live.read_bytes() == b"live runtime untouched"
    assert (
        endpoint.data / "extore.sqlite3"
    ).read_bytes() == b"live business database untouched"


def test_exact_upload_is_private_atomic_and_has_no_installation(endpoint, monkeypatch):
    result = upload(endpoint, monkeypatch, frame())
    assert result == {
        "status": "staged",
        "sha": SHA,
        "source_sha256": SOURCE_HASH,
        "package_sha256": hashlib.sha256(b"package").hexdigest(),
        "size": 7,
    }
    assert endpoint.calls == [(SHA, b"package", BUNDLE)]
    metadata = json.loads((endpoint.state / "staged-package.json").read_text())
    assert metadata["provenance_sha256"] == hashlib.sha256(BUNDLE).hexdigest()
    assert (endpoint.state / "staged-package.tar.zst").read_bytes() == b"package"
    assert (endpoint.state / "staged-provenance.jsonl").read_bytes() == BUNDLE
    assert all(
        stat.S_IMODE((endpoint.state / name).stat().st_mode) == 0o600
        for name in endpoint.ns["STAGE_FILES"]
    )
    assert not list(endpoint.state.glob("staged-upload-*"))
    assert not endpoint.work.exists()
    unchanged(endpoint)


def test_upload_uses_a_bundle_extension_accepted_by_github_cli(endpoint, monkeypatch):
    verifier = endpoint.ns["require_provenance"]

    def verify_package(package, bundle, sha):
        # gh attestation verify rejects other suffixes before parsing the bundle.
        assert bundle.suffix in {".json", ".jsonl"}, (
            "Unsupported GitHub CLI bundle extension"
        )
        return verifier(package, bundle, sha)

    monkeypatch.setitem(endpoint.ns, "require_provenance", verify_package)
    result = upload(endpoint, monkeypatch, frame())
    assert result["status"] == "staged"
    assert endpoint.calls == [(SHA, b"package", BUNDLE)]
    unchanged(endpoint)


@pytest.mark.parametrize(
    "update",
    [
        {"format": True},
        {"format": 1.0},
        {"format": 2},
        {"sha": "b" * 40},
        {"source_sha256": "A" * 64},
        {"package_sha256": "x" * 64},
        {"size": True},
        {"size": 0},
        {"size": -1},
        {"size": 128 * 1024 * 1024 + 1},
        {"size": "7"},
        {"size": 7.0},
        {"provenance_size": False},
        {"provenance_size": 0},
        {"provenance_size": 256 * 1024 + 1},
        {"unexpected": "field"},
    ],
)
def test_invalid_header_never_writes_a_package(endpoint, monkeypatch, update):
    with pytest.raises(RuntimeError):
        upload(endpoint, monkeypatch, frame(update=update))
    assert not any(
        (endpoint.state / name).exists() for name in endpoint.ns["STAGE_FILES"]
    )
    assert not endpoint.calls
    unchanged(endpoint)


@pytest.mark.parametrize(
    "data",
    [
        b"{}",
        b"\xff\n",
        b"[]\n",
        b" " * 4096 + b"\n",
        frame().replace(b'{"format":1,', b'{"format":1,"format":1,', 1),
        frame().replace(b'"format":1,', b"", 1),
        frame()[:-1],
        frame(trailing=b"extra"),
        frame(update={"package_sha256": "0" * 64}),
    ],
)
def test_invalid_framing_preserves_the_previous_verified_stage(
    endpoint, monkeypatch, data
):
    upload(endpoint, monkeypatch, frame(b"previous package"))
    prior = {
        name: (endpoint.state / name).read_bytes()
        for name in endpoint.ns["STAGE_FILES"]
    }
    with pytest.raises(RuntimeError):
        upload(endpoint, monkeypatch, data)
    assert {name: (endpoint.state / name).read_bytes() for name in prior} == prior
    assert not list(endpoint.state.glob("staged-upload-*"))
    unchanged(endpoint)


@pytest.mark.parametrize("when", ["before", "after"])
def test_stale_main_never_commits_an_upload(endpoint, monkeypatch, when):
    values = iter(["b" * 40] if when == "before" else [SHA, "b" * 40])
    monkeypatch.setitem(endpoint.ns, "main_sha", lambda: next(values))
    with pytest.raises(RuntimeError, match="main|Main"):
        upload(endpoint, monkeypatch, frame())
    assert not any(
        (endpoint.state / name).exists() for name in endpoint.ns["STAGE_FILES"]
    )
    unchanged(endpoint)


def test_unverified_recovery_blocks_upload_before_consuming_it(endpoint, monkeypatch):
    (endpoint.state / "recovery.tar.gz").write_bytes(b"only recovery copy")
    (endpoint.state / "snapshot.json").write_text(
        json.dumps({"sha": SHA, "offhost_verified": False})
    )
    with pytest.raises(RuntimeError, match="unverified recovery"):
        upload(endpoint, monkeypatch, frame())
    assert not endpoint.calls
    assert (endpoint.state / "recovery.tar.gz").read_bytes() == b"only recovery copy"
    unchanged(endpoint)


def test_failed_attestation_leaves_no_committed_upload(endpoint, monkeypatch):
    def denied(*args):
        raise RuntimeError("attestation denied")

    monkeypatch.setitem(endpoint.ns, "require_provenance", denied)
    with pytest.raises(RuntimeError, match="attestation denied"):
        upload(endpoint, monkeypatch, frame())
    assert not any(
        (endpoint.state / name).exists() for name in endpoint.ns["STAGE_FILES"]
    )
    unchanged(endpoint)


def test_provenance_verifier_pins_certificate_repository_workflow_and_sha(
    monkeypatch, tmp_path
):
    module = runpy.run_path(str(REPO / "scripts/deploy/server.py"))
    namespace = module["require_provenance"].__globals__
    calls = []
    verified = [
        {
            "verificationResult": {
                "signature": {
                    "certificate": {"sourceRepositoryIdentifier": "1407071795"}
                }
            }
        }
    ]

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(stdout=json.dumps(verified))

    monkeypatch.setitem(namespace, "run", fake_run)
    module["require_provenance"](tmp_path / "package", tmp_path / "bundle", SHA)
    args, kwargs = calls[0]
    assert args[:3] == ("/usr/bin/gh", "attestation", "verify")
    assert args[args.index("--cert-identity") + 1] == namespace["CERT_IDENTITY"]
    assert args[args.index("--repo") + 1] == "TokenNotIncluded/extore"
    for flag, value in [
        ("--source-ref", "refs/heads/main"),
        ("--source-digest", SHA),
        ("--signer-digest", SHA),
    ]:
        assert args[args.index(flag) + 1] == value
    assert "--deny-self-hosted-runners" in args
    assert "--signer-workflow" not in args and "--custom-trusted-root" not in args
    assert kwargs["timeout"] == 120
    assert kwargs["env"]["GH_CONFIG_DIR"] == str(namespace["STATE"] / "gh-config")
    assert kwargs["env"]["XDG_CACHE_HOME"] == str(namespace["STATE"] / "gh-cache")
    assert not {"GH_TOKEN", "GITHUB_TOKEN", "EXTORE_DATA"} & kwargs["env"].keys()
    for invalid in [
        [],
        {},
        [None],
        [{}],
        [{"verificationResult": {}}],
        [
            {
                "verificationResult": {
                    "signature": {
                        "certificate": {
                            "sourceRepositoryIdentifier": "another-repository"
                        }
                    }
                }
            }
        ],
    ]:
        verified[:] = invalid if isinstance(invalid, list) else [invalid]
        with pytest.raises(RuntimeError):
            module["require_provenance"](tmp_path / "package", tmp_path / "bundle", SHA)


@pytest.mark.parametrize(
    "output,code,valid",
    [
        ("extore 1.0.0 (Rust)\n", 0, True),
        ("extore 0.11.3 (Rust)\n", 0, False),
        ("libssl.so.3: cannot open shared object file\n", 127, False),
        ("x" * 4097, 0, False),
    ],
)
def test_native_probe_is_unprivileged_bounded_and_checks_real_exit_status(
    monkeypatch, tmp_path, output, code, valid
):
    module = runpy.run_path(str(REPO / "scripts/deploy/server.py"))
    original = subprocess.Popen
    children = []

    def popen(args, **kwargs):
        assert args == [
            "runuser",
            "-u",
            "extore-build",
            "--",
            str(tmp_path / "extore"),
            "--version",
        ]
        assert (
            kwargs["stdin"] == subprocess.DEVNULL
            and kwargs["start_new_session"] is True
        )
        assert "EXTORE_DATA" not in kwargs["env"]
        child = original(
            [
                sys.executable,
                "-c",
                f"import sys; sys.stdout.write({output!r}); sys.exit({code})",
            ],
            **kwargs,
        )
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", popen)
    if valid:
        module["probe_native_version"](tmp_path / "extore", "1.0.0")
    else:
        with pytest.raises(RuntimeError):
            module["probe_native_version"](tmp_path / "extore", "1.0.0")
    assert all(child.poll() is not None for child in children)


def test_native_probe_kills_a_stalled_process(monkeypatch, tmp_path):
    module = runpy.run_path(str(REPO / "scripts/deploy/server.py"))
    namespace = module["probe_native_version"].__globals__
    clock = iter([0.0, 16.0])
    monkeypatch.setitem(
        namespace, "time", SimpleNamespace(monotonic=lambda: next(clock))
    )
    original = subprocess.Popen
    children = []

    def popen(args, **kwargs):
        child = original(
            [sys.executable, "-c", "import signal; signal.pause()"], **kwargs
        )
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", popen)
    with pytest.raises(RuntimeError, match="timed out"):
        module["probe_native_version"](tmp_path / "extore", "1.0.0")
    assert all(child.poll() is not None for child in children)


@pytest.mark.parametrize("mutation", ["package", "bundle", "source", "sha", "orphan"])
def test_prepare_revalidates_stage_and_never_falls_back_on_mismatch(
    endpoint, monkeypatch, mutation
):
    upload(endpoint, monkeypatch, frame())
    endpoint.work.mkdir()
    source_hash = SOURCE_HASH
    if mutation == "package":
        (endpoint.state / "staged-package.tar.zst").write_bytes(b"tampered")
    elif mutation == "bundle":
        (endpoint.state / "staged-provenance.jsonl").write_bytes(b"tampered")
    elif mutation == "orphan":
        (endpoint.state / "staged-package.json").unlink()
    elif mutation == "source":
        source_hash = "0" * 64
    else:
        metadata = json.loads((endpoint.state / "staged-package.json").read_text())
        metadata["sha"] = "b" * 40
        (endpoint.state / "staged-package.json").write_text(json.dumps(metadata))
    with pytest.raises(RuntimeError):
        endpoint.ns["staged_package"](SHA, source_hash)
    assert not list(endpoint.work.iterdir())
    unchanged(endpoint)


def source_fixture(base, namespace):
    checkout = base / "source"
    paths = ["extore/static/index.html", "extore/static/native-theme.css"]
    paths += [
        "extore/sdk/" + name + ".py"
        for name in ["__init__", "client", "flow", "script", "instructions"]
    ]
    paths += ["extore/" + name for name in namespace["NATIVE_MODULES"]]
    paths += [
        "processors/official/extore_processors/__init__.py",
        "processors/official/LICENSE",
        "LICENSE",
    ]
    paths += [
        "deploy/arch/" + name
        for name in [
            "extore-api.service",
            "extore-worker.service",
            "extore.sysusers",
            "extore-admin",
            "extore.env",
        ]
    ]
    for name in paths:
        path = checkout / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(("source:" + name + "\n").encode())
    (checkout / "Cargo.toml").write_text('[package]\nname="extore"\nversion="1.0.0"\n')
    (checkout / "deploy/arch/PKGBUILD").write_text(
        "pkgname=extore\npkgver=1.0.0\npkgrel=1\nsha256sums=('SKIP')\n"
    )
    return checkout


def package_fixture(
    checkout, namespace, *, changed=None, architecture="x86_64", extra_directory=None
):
    files = {
        name: source.read_bytes()
        for name, source in namespace["native_source_files"](checkout).items()
    }
    files["usr/lib/extore/bin/extore"] = b"\x7fELFnative-fixture"
    files["etc/extore/extore.env"] = (checkout / "deploy/arch/extore.env").read_bytes()
    files[".PKGINFO"] = (
        f"pkgname = extore\npkgver = 1.0.0-1\narch = {architecture}\n".encode()
    )
    if changed:
        files[changed] = b"modified package bytes"
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, content in files.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(content)
            entry.mode = (
                0o755
                if name in {"usr/lib/extore/bin/extore", "usr/bin/extore-admin"}
                else 0o644
            )
            archive.addfile(entry, io.BytesIO(content))
        alias = tarfile.TarInfo("usr/bin/extore")
        alias.type, alias.linkname = tarfile.SYMTYPE, "../lib/extore/bin/extore"
        archive.addfile(alias)
        if extra_directory:
            entry = tarfile.TarInfo(extra_directory)
            entry.type, entry.mode = tarfile.DIRTYPE, 0o755
            archive.addfile(entry)
    return stream.getvalue()


def archive_reader(monkeypatch):
    # Compression is an external executable; exercise the real tar/parser/checks
    # with transparent bytes so these unit tests do not require host zstd.
    original = subprocess.Popen

    def popen(args, *a, **kw):
        if args[:3] == ["zstd", "-dc", "--"]:
            return original(["cat", args[3]], *a, **kw)
        return original(args, *a, **kw)

    monkeypatch.setattr(subprocess, "Popen", popen)


def prepare_fixture(endpoint, monkeypatch, source):
    calls = []

    def build(*args, cwd=None):
        calls.append(args)
        if args[0] == "git":
            shutil.copytree(source, Path(args[-1]))
        elif args[0] == "python":
            Path(args[2]).write_bytes(SOURCE)
        else:
            pytest.fail("A staged package must never trigger makepkg")

    def capture(*args):
        if args[0] == "runuser":
            return SHA
        if args[:2] == ("pacman", "-Qp"):
            return "extore 1.0.0-1"
        if args[:2] == ("pacman", "-Q"):
            return "extore 0.11.3-4"
        pytest.fail(f"Unexpected external command: {args}")

    monkeypatch.setitem(endpoint.ns, "build", build)
    monkeypatch.setitem(endpoint.ns, "capture", capture)
    monkeypatch.setitem(
        endpoint.ns,
        "database_summary",
        lambda: {"user_version": 22, "schema_sha256": "before", "rows": {}},
    )
    monkeypatch.setattr(shutil, "chown", lambda *a, **kw: None)
    (endpoint.root / "var/lib/pacman/local/extore-0.11.3-4").mkdir(parents=True)
    probes = []
    monkeypatch.setitem(
        endpoint.ns,
        "probe_native_version",
        lambda path, version: probes.append((path.read_bytes(), version)),
    )
    archive_reader(monkeypatch)
    return calls, probes


def test_prepare_independently_recreates_source_and_checks_payload_without_install(
    endpoint, monkeypatch, tmp_path
):
    source = source_fixture(tmp_path, endpoint.ns)
    package = package_fixture(source, endpoint.ns)
    upload(endpoint, monkeypatch, frame(package))
    calls, probes = prepare_fixture(endpoint, monkeypatch, source)
    result = endpoint.ns["prepare"](SHA)
    assert result["status"] == "prepared"
    assert result["source_content_sha256"] == SOURCE_HASH
    assert result["source_sha256"] == hashlib.sha256(SOURCE).hexdigest()
    assert result["package_id"] == "extore 1.0.0-1"
    assert result["package_sha256"] == hashlib.sha256(package).hexdigest()
    assert result["package"] == "extore-prebuilt.pkg.tar.zst"
    assert calls[0][0:2] == ("git", "clone") and "--recurse-submodules" in calls[0]
    assert calls[1][0] == "python" and calls[1][-2:] == ("--revision", SHA)
    assert probes == [(b"\x7fELFnative-fixture", "1.0.0")]
    assert len(endpoint.calls) == 2  # Attestation is rechecked at prepare.
    assert not list(endpoint.work.glob("native-abi-*"))
    unchanged(endpoint)


@pytest.mark.parametrize("changed", [False, True])
def test_prepare_binds_tar_content_independently_of_gzip_encoder(
    endpoint, monkeypatch, tmp_path, changed
):
    source = source_fixture(tmp_path, endpoint.ns)
    package = package_fixture(source, endpoint.ns)
    client_archive = gzip.compress(SOURCE_TAR, compresslevel=0, mtime=0)
    server_archive = gzip.compress(
        SOURCE_TAR + (b"changed" if changed else b""), compresslevel=9, mtime=0
    )
    assert client_archive != server_archive
    client_hash = hashlib.sha256(gzip.decompress(client_archive)).hexdigest()
    upload(endpoint, monkeypatch, frame(package, update={"source_sha256": client_hash}))
    calls, _ = prepare_fixture(endpoint, monkeypatch, source)
    previous = endpoint.ns["build"]

    def build(*args, cwd=None):
        previous(*args, cwd=cwd)
        if args[0] == "python":
            Path(args[2]).write_bytes(server_archive)

    monkeypatch.setitem(endpoint.ns, "build", build)
    if changed:
        with pytest.raises(RuntimeError, match="source archive"):
            endpoint.ns["prepare"](SHA)
        assert not (endpoint.state / "prepared.json").exists()
    else:
        result = endpoint.ns["prepare"](SHA)
        assert result["source_content_sha256"] == client_hash
        assert result["source_sha256"] == hashlib.sha256(server_archive).hexdigest()
        assert (
            hashlib.sha256(server_archive).hexdigest()
            in (endpoint.work / "PKGBUILD").read_text()
        )
    assert all(call[0] != "makepkg" for call in calls)
    unchanged(endpoint)


def test_prepare_without_staged_input_preserves_source_build(
    endpoint, monkeypatch, tmp_path
):
    source = source_fixture(tmp_path, endpoint.ns)
    package = package_fixture(source, endpoint.ns)
    calls, probes = prepare_fixture(endpoint, monkeypatch, source)
    previous = endpoint.ns["build"]

    def build(*args, cwd=None):
        if args[0] == "makepkg":
            calls.append(args)
            assert cwd == endpoint.work
            (cwd / "extore-1.0.0-1-x86_64.pkg.tar.zst").write_bytes(package)
        else:
            previous(*args, cwd=cwd)

    monkeypatch.setitem(endpoint.ns, "build", build)
    result = endpoint.ns["prepare"](SHA)
    assert result["status"] == "prepared"
    assert result["package"] == "extore-1.0.0-1-x86_64.pkg.tar.zst"
    assert calls[-1] == ("makepkg", "--noconfirm", "--cleanbuild", "--clean")
    assert not endpoint.calls and not probes
    unchanged(endpoint)


@pytest.mark.parametrize(
    "changed",
    [
        "usr/lib/extore/extore/static/index.html",
        "usr/lib/extore/extore/sdk/client.py",
        "usr/lib/systemd/system/extore-worker.service",
        "etc/extore/extore.env",
    ],
)
def test_prepare_rejects_payload_not_from_fresh_source(
    endpoint, monkeypatch, tmp_path, changed
):
    source = source_fixture(tmp_path, endpoint.ns)
    upload(
        endpoint,
        monkeypatch,
        frame(package_fixture(source, endpoint.ns, changed=changed)),
    )
    _, probes = prepare_fixture(endpoint, monkeypatch, source)
    with pytest.raises(RuntimeError, match="source"):
        endpoint.ns["prepare"](SHA)
    assert not probes and not (endpoint.state / "prepared.json").exists()
    unchanged(endpoint)


@pytest.mark.parametrize("change", ["source_hash", "architecture", "directory", "abi"])
def test_prepare_failures_cannot_install_or_compile_silently(
    endpoint, monkeypatch, tmp_path, change
):
    source = source_fixture(tmp_path, endpoint.ns)
    package = package_fixture(
        source,
        endpoint.ns,
        architecture="aarch64" if change == "architecture" else "x86_64",
        extra_directory="etc/unexpected" if change == "directory" else None,
    )
    upload(
        endpoint,
        monkeypatch,
        frame(
            package,
            update={"source_sha256": "0" * 64} if change == "source_hash" else None,
        ),
    )
    calls, probes = prepare_fixture(endpoint, monkeypatch, source)
    if change == "abi":

        def unavailable(*args):
            raise RuntimeError("Host ABI cannot load the binary")

        monkeypatch.setitem(endpoint.ns, "probe_native_version", unavailable)
    with pytest.raises(RuntimeError):
        endpoint.ns["prepare"](SHA)
    assert all(call[0] != "makepkg" for call in calls)
    assert not (endpoint.state / "prepared.json").exists()
    unchanged(endpoint)


def test_operation_lock_is_exclusive_and_released_on_failure(endpoint):
    with pytest.raises(ValueError):
        with endpoint.ns["operation_lock"]():
            with pytest.raises(RuntimeError, match="operation is running"):
                with endpoint.ns["operation_lock"]():
                    pytest.fail("concurrent deployment was admitted")
            raise ValueError("failed operation")
    with endpoint.ns["operation_lock"]():
        assert stat.S_IMODE((endpoint.state / "operation.lock").stat().st_mode) == 0o600


def test_cleanup_respects_recovery_and_matching_sha(endpoint, monkeypatch):
    upload(endpoint, monkeypatch, frame())
    endpoint.work.mkdir()
    (endpoint.state / "recovery.tar.gz").write_bytes(b"required backup")
    (endpoint.state / "snapshot.json").write_text(
        json.dumps({"sha": SHA, "offhost_verified": False})
    )
    assert endpoint.ns["cleanup"](SHA)["status"] == "pending_backup"
    assert all((endpoint.state / name).exists() for name in endpoint.ns["STAGE_FILES"])
    (endpoint.state / "snapshot.json").write_text(
        json.dumps({"sha": SHA, "offhost_verified": True})
    )
    with pytest.raises(RuntimeError):
        endpoint.ns["cleanup"]("b" * 40)
    assert all((endpoint.state / name).exists() for name in endpoint.ns["STAGE_FILES"])
    assert endpoint.ns["cleanup"](SHA)["status"] == "cleaned"
    assert not any(
        (endpoint.state / name).exists() for name in endpoint.ns["STAGE_FILES"]
    )
    assert not endpoint.work.exists()
    unchanged(endpoint)
