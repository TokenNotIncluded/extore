"""The upload transport cannot acknowledge or install an unconfirmed package."""

import hashlib
import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLIENT = runpy.run_path(str(ROOT / "scripts/deploy/client.py"))
SHA = "a" * 40
SOURCE = "b" * 64


def files(tmp_path):
    package = tmp_path / "extore.pkg.tar.zst"
    package.write_bytes(b"signed package bytes")
    bundle = tmp_path / "bundle.json"
    bundle.write_bytes(b'{"verified-on-server":true}\n')
    return package, bundle


def test_upload_frames_provenance_before_exact_package_bytes(tmp_path, monkeypatch):
    package, bundle = files(tmp_path)
    calls = []

    def remote(config, operation, sha, **kwargs):
        calls.append(operation)
        assert operation == "stage_package" and sha == SHA
        stream = kwargs["stdin"]
        header = json.loads(stream.readline())
        assert set(header) == {
            "format",
            "sha",
            "source_sha256",
            "package_sha256",
            "size",
            "provenance_size",
        }
        assert header["format"] == 1 and header["sha"] == SHA
        assert header["source_sha256"] == SOURCE
        assert stream.read(header["provenance_size"]) == bundle.read_bytes()
        body = stream.read(header["size"])
        assert body == package.read_bytes() and stream.read() == b""
        assert header["package_sha256"] == hashlib.sha256(body).hexdigest()
        return SimpleNamespace(
            stdout=json.dumps(
                {
                    "status": "staged",
                    **{
                        key: header[key]
                        for key in ("sha", "source_sha256", "package_sha256", "size")
                    },
                }
            ).encode()
        )

    monkeypatch.setitem(CLIENT["stage_package"].__globals__, "remote", remote)
    result = CLIENT["stage_package"]({}, SHA, package, SOURCE, bundle)
    assert result["status"] == "staged"
    assert calls == ["stage_package"]


@pytest.mark.parametrize(
    "field", ["status", "sha", "package_sha256", "source_sha256", "size"]
)
def test_upload_requires_exact_server_confirmation(tmp_path, monkeypatch, field):
    package, bundle = files(tmp_path)

    def remote(*args, **kwargs):
        header = json.loads(kwargs["stdin"].readline())
        reply = {
            "status": "staged",
            **{
                key: header[key]
                for key in ("sha", "source_sha256", "package_sha256", "size")
            },
        }
        reply[field] = "unexpected"
        return SimpleNamespace(stdout=json.dumps(reply).encode())

    monkeypatch.setitem(CLIENT["stage_package"].__globals__, "remote", remote)
    with pytest.raises(RuntimeError, match="exact uploaded package"):
        CLIENT["stage_package"]({}, SHA, package, SOURCE, bundle)


@pytest.mark.parametrize("kind", ["source", "package", "provenance"])
def test_invalid_local_material_never_uses_remote_access(tmp_path, monkeypatch, kind):
    package, bundle = files(tmp_path)
    namespace = CLIENT["stage_package"].__globals__
    monkeypatch.setitem(
        namespace, "remote", lambda *args, **kwargs: pytest.fail("No remote access")
    )
    source = SOURCE
    if kind == "source":
        source = "not-a-checksum"
    elif kind == "package":
        monkeypatch.setitem(namespace, "MAX_PACKAGE_BYTES", 1)
    else:
        bundle.write_bytes(b"")
    with pytest.raises(ValueError):
        CLIENT["stage_package"]({}, SHA, package, source, bundle)


def test_failed_upload_cannot_prepare_acknowledge_or_activate(tmp_path, monkeypatch):
    package, bundle = files(tmp_path)
    namespace = CLIENT["prepare"].__globals__
    calls = []

    def upload(*args):
        raise RuntimeError("provenance rejected")

    monkeypatch.setitem(namespace, "stage_package", upload)
    monkeypatch.setitem(
        namespace, "rpc", lambda config, operation, sha: calls.append(operation)
    )
    with pytest.raises(RuntimeError, match="provenance rejected"):
        CLIENT["prepare"](
            SHA, {}, tmp_path, package=package, source_sha256=SOURCE, provenance=bundle
        )
    assert calls == ["cleanup"]
