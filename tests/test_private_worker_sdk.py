import hashlib
import hmac
import io
import json
import time

import pytest

from extore.sdk.client import (
    FlowScope,
    PrivateWorkerClient,
    signature_v2,
    verify_flow_event,
)

SCOPE = FlowScope("shop-a", "product-a", "job-a", 1, "process", 2, "action-a")
ORIGIN = "https://worker.example"
SECRET = "isolated-fixture-secret"
FIELDS = (
    "shop_id",
    "product_id",
    "job_id",
    "attempt",
    "node_id",
    "flow_epoch",
    "action_id",
)


def headers(body, *, path="/process", direction="extore-to-worker"):
    ts, nonce = str(int(time.time())), "N" * 32
    body_digest = hashlib.sha256(body).hexdigest()
    return {
        "X-Extore-Version": "2",
        "X-Extore-Direction": direction,
        "X-Extore-Audience": ORIGIN,
        "X-Extore-Timestamp": ts,
        "X-Extore-Nonce": nonce,
        "X-Extore-Body-SHA256": body_digest,
        "X-Extore-Signature": signature_v2(
            SECRET,
            SCOPE,
            direction=direction,
            audience=ORIGIN,
            method="POST",
            path=path,
            timestamp=ts,
            nonce=nonce,
            body_digest=body_digest,
        ),
        **{
            "X-Extore-" + name.replace("_", "-"): str(getattr(SCOPE, name))
            for name in FIELDS
        },
    }


class Opener:
    def __init__(self):
        self.requests = []

    def open(self, request, *, timeout):
        assert timeout == 30
        self.requests.append(request)
        if request.method == "GET":
            return io.BytesIO(b"fixture-file")
        return io.BytesIO(b'{"ok":true,"id":"fixture-file"}')


def test_sdk_v2_signature_matches_independent_protocol_vector():
    values = [
        "extore-private-worker-signature-v2",
        "worker-to-extore",
        ORIGIN,
        "POST",
        "/result",
        "123",
        "N" * 32,
        "0" * 64,
        *[getattr(SCOPE, name) for name in FIELDS],
    ]
    key = hmac.new(
        SECRET.encode(),
        b"extore-private-worker-key-v2\0worker-to-extore",
        hashlib.sha256,
    ).digest()
    expected = hmac.new(
        key,
        json.dumps(
            values,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    assert (
        signature_v2(
            SECRET,
            SCOPE,
            direction="worker-to-extore",
            audience=ORIGIN,
            method="POST",
            path="/result",
            timestamp="123",
            nonce="N" * 32,
            body_digest="0" * 64,
        )
        == expected
    )


def test_sdk_v2_update_retry_body_is_stable_but_transport_nonce_changes():
    opener = Opener()
    client = PrivateWorkerClient(ORIGIN, SECRET, opener=opener)
    for _ in range(2):
        assert client.update(
            SCOPE, "result-1", state="succeeded", output={"answer": "fixture"}
        )["ok"]
    first, second = opener.requests
    assert first.data == second.data
    assert first.get_header("X-extore-nonce") != second.get_header("X-extore-nonce")
    body = json.loads(first.data)
    assert (
        body["update"]["flow_epoch"] == 2 and body["update"]["action_id"] == "action-a"
    )
    assert SECRET not in str(first.headers) and SECRET.encode() not in first.data


def test_sdk_v2_files_share_exact_execution_scope(tmp_path):
    opener = Opener()
    client = PrivateWorkerClient(ORIGIN, SECRET, opener=opener)
    source = tmp_path / "delivery.docx"
    source.write_bytes(b"fixture-file")
    assert client.upload(SCOPE, "delivery_file", source, result_id="upload-1")["ok"]
    assert opener.requests[0].full_url.endswith("/files/delivery_file/upload-1/upload")
    assert client.download(SCOPE, "brief_file", "file-1") == b"fixture-file"
    assert opener.requests[1].full_url.endswith("/files/brief_file/file-1/download")
    assert (
        opener.requests[1].get_header("X-extore-body-sha256")
        == hashlib.sha256(b"").hexdigest()
    )
    with pytest.raises(ValueError):
        client.upload(SCOPE, "../escape", source, result_id="upload-1")
    symlink = tmp_path / "linked.docx"
    symlink.symlink_to(source)
    with pytest.raises(OSError):
        client.upload(SCOPE, "delivery_file", symlink, result_id="upload-2")


def test_sdk_v2_dispatch_verification_binds_direction_path_scope_and_body():
    body = json.dumps(
        {
            "version": 2,
            "type": "flow.process.requested",
            "scope": {name: getattr(SCOPE, name) for name in FIELDS},
            "deadline": time.time() + 60,
            "params": {"request": "fixture"},
        }
    ).encode()
    signed = headers(body)
    assert (
        verify_flow_event(SECRET, body, signed, audience=ORIGIN, path="/process")[
            "version"
        ]
        == 2
    )
    cases = [
        (body, signed, "/other"),
        (body + b" ", signed, "/process"),
        (body, headers(body, direction="worker-to-extore"), "/process"),
        (body, {**signed, "X-Extore-Flow-Epoch": "3"}, "/process"),
    ]
    for raw, candidate, path in cases:
        with pytest.raises(ValueError, match="Invalid private-worker event"):
            verify_flow_event(SECRET, raw, candidate, audience=ORIGIN, path=path)


def test_sdk_v2_dispatch_body_scope_and_deadline_are_checked():
    value = {
        "version": 2,
        "type": "flow.process.requested",
        "scope": {name: getattr(SCOPE, name) for name in FIELDS},
        "deadline": time.time() + 60,
    }
    for body in (
        {**value, "scope": {**value["scope"], "flow_epoch": 3}},
        {**value, "deadline": time.time() - 1},
    ):
        raw = json.dumps(body).encode()
        with pytest.raises(ValueError):
            verify_flow_event(
                SECRET, raw, headers(raw), audience=ORIGIN, path="/process"
            )


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', b'{"x":NaN}', b"[]"])
def test_sdk_v2_rejects_ambiguous_signed_json(raw):
    with pytest.raises(ValueError, match="Invalid private-worker event"):
        verify_flow_event(SECRET, raw, headers(raw), audience=ORIGIN, path="/process")


@pytest.mark.parametrize(
    "origin",
    [
        "http://worker.example",
        "https://u:p@worker.example",
        "https://worker.example/path",
        "https://worker.example?query=1",
        "https://worker.example/#secret",
        "https://worker.example\n",
    ],
)
def test_sdk_v2_rejects_unsafe_origins(origin):
    with pytest.raises(ValueError):
        PrivateWorkerClient(origin, SECRET)


def test_sdk_v2_rejects_other_execution_before_transport():
    opener = Opener()
    client = PrivateWorkerClient(ORIGIN, SECRET, opener=opener)
    with pytest.raises(ValueError):
        client.update(SCOPE, "result-1", state="succeeded", flow_epoch=3)
    with pytest.raises(ValueError):
        FlowScope("shop", "product", "job", True, "node", 1, "action")
    assert opener.requests == []
