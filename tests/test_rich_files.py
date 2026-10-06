import io
import json
import uuid
import warnings
from concurrent.futures import ThreadPoolExecutor

import pytest
from PIL import Image
from test_files import batch, download, exchange, make_product, set_staff, submit

from extore.db import db
from extore.image_validation import image_content_type


def raster(format="PNG", *, size=(4, 3), color="red"):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format=format)
    return buffer.getvalue()


def field(key, type="images", *, required=True, max_items=3):
    return {
        "key": key,
        "label": {"zh-CN": key},
        "type": type,
        "required": required,
        **({"max_items": max_items} if type == "images" else {}),
    }


def product(owner, **values):
    return make_product(
        owner,
        parameters=[field("source")],
        outputs=[field("result")],
        **values,
    )


def upload(
    owner, token_or_job, *, key="source", content=None, mime="application/octet-stream"
):
    output = isinstance(token_or_job, dict)
    return owner.post(
        "/api/manage/files/upload" if output else "/api/files/upload",
        data={
            "job_id" if output else "token": token_or_job["id"]
            if output
            else token_or_job,
            "field_key": "result" if output and key == "source" else key,
        },
        files={
            "file": ("untrusted.name", raster() if content is None else content, mime)
        },
    )


def uploaded(owner, token_or_job, **kwargs):
    response = upload(owner, token_or_job, **kwargs)
    assert response.status_code == 200, response.text
    return response.json()


def collection(ids):
    return json.dumps(ids)


def managed_job(owner, job):
    response = owner.get(
        "/api/manage/jobs",
        params={"product_id": job["product_id"], "job_id": job["id"]},
    )
    assert response.status_code == 200, response.text
    return response.json()[0]


@pytest.mark.parametrize(
    "format,mime",
    [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")],
)
@pytest.mark.parametrize("type", ["image", "images"])
def test_image_upload_derives_mime_from_pixels_not_name_or_mime(
    owner, format, mime, type
):
    item = make_product(owner, parameters=[field("source", type)])
    token = exchange(owner, item["id"])
    result = uploaded(owner, token, content=raster(format), mime="text/html")
    assert result["content_type"] == mime
    params = {
        "source": collection([result["id"]]) if type == "images" else result["id"]
    }
    job = submit(owner, token, params)
    assert managed_job(owner, job)["files"][0]["content_type"] == mime
    assert owner.get(f"/api/manage/files/{result['id']}/download").content == raster(
        format
    )


@pytest.mark.parametrize(
    "payload",
    [
        b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
        b"<html>not an image</html>",
        b"\x89PNG\r\n\x1a\ninvalid compressed pixels",
        b"GIF89a" + b"\x00" * 30,
        b"",
    ],
)
def test_invalid_or_disallowed_images_never_leave_files_or_reservations(owner, payload):
    item = product(owner)
    token = exchange(owner, item["id"])
    response = upload(owner, token, content=payload, mime="image/png")
    assert response.status_code == 400, response.text
    assert "PNG" in response.json()["detail"]
    with db() as c:
        assert c.execute("SELECT count(*) FROM job_files").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM upload_reservations").fetchone()[0] == 0


@pytest.mark.parametrize("format", ["PNG", "JPEG", "WEBP"])
@pytest.mark.parametrize("trim", ["half", 1, 4])
def test_truncated_images_rejected_before_storage(owner, format, trim):
    item = product(owner)
    token = exchange(owner, item["id"])
    content = raster(format)
    response = upload(
        owner,
        token,
        content=content[: len(content) // 2] if trim == "half" else content[:-trim],
        mime="image/png",
    )
    assert response.status_code == 400, response.text
    with db() as c:
        assert c.execute("SELECT count(*) FROM job_files").fetchone()[0] == 0


def test_pixel_limit_checked_before_decode(monkeypatch):
    import extore.image_validation as validation

    content = raster(size=(11, 1))
    monkeypatch.setattr(validation, "MAX_IMAGE_PIXELS", 10)
    monkeypatch.setattr(
        Image.Image,
        "load",
        lambda self: pytest.fail("pixels decoded before size rejection"),
    )
    with pytest.raises(ValueError, match="1600"):
        image_content_type(io.BytesIO(content))


def webp_chunk(kind, payload):
    chunk = kind + len(payload).to_bytes(4, "little") + payload
    if len(payload) & 1:
        chunk += b"\x00"
    return b"RIFF" + (len(chunk) + 4).to_bytes(4, "little") + b"WEBP" + chunk


@pytest.mark.parametrize("kind", [b"VP8X", b"VP8 ", b"VP8L"])
def test_webp_dimensions_rejected_before_native_decoder_allocation(monkeypatch, kind):
    if kind == b"VP8X":
        payload = b"\x00" * 4 + (4999).to_bytes(3, "little") * 2
    elif kind == b"VP8 ":
        payload = b"\x00\x00\x00\x9d\x01\x2a" + (5000).to_bytes(2, "little") * 2
    else:
        payload = b"\x2f" + (4999 | (4999 << 14)).to_bytes(4, "little")
    monkeypatch.setattr(
        Image,
        "open",
        lambda *args, **kwargs: pytest.fail("native decoder opened oversized WebP"),
    )
    with pytest.raises(ValueError, match="1600"):
        image_content_type(io.BytesIO(webp_chunk(kind, payload)))


def test_webp_container_chunk_limit_rejects_before_decoder(monkeypatch):
    chunks = (b"JUNK" + b"\x00" * 4) * 1025
    content = b"RIFF" + (len(chunks) + 4).to_bytes(4, "little") + b"WEBP" + chunks
    monkeypatch.setattr(
        Image,
        "open",
        lambda *args, **kwargs: pytest.fail("decoder opened excessive chunk sequence"),
    )
    with pytest.raises(ValueError):
        image_content_type(io.BytesIO(content))


def test_decoder_warning_is_rejected_and_does_not_escape(monkeypatch):
    def warning(*args, **kwargs):
        warnings.warn("untrusted decoder warning", UserWarning)
        pytest.fail("warning was not treated as an error")

    monkeypatch.setattr(Image, "open", warning)
    stream = io.BytesIO(raster())
    with pytest.raises(ValueError, match="PNG"):
        image_content_type(stream)
    assert not stream.closed and stream.tell() == 0


def test_valid_lossless_and_extended_webp_are_supported(owner):
    item = product(owner)
    token = exchange(owner, item["id"])
    for settings in ({"lossless": True}, {"icc_profile": b"untrusted ICC metadata"}):
        buffer = io.BytesIO()
        Image.new("RGB", (4, 3), "red").save(buffer, format="WEBP", **settings)
        result = uploaded(owner, token, content=buffer.getvalue())
        assert result["content_type"] == "image/webp"


@pytest.mark.parametrize("format", ["PNG", "WEBP"])
def test_animation_rejected_without_decoding_frame_sequence(format):
    content = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(
        content,
        format=format,
        save_all=True,
        append_images=[Image.new("RGB", (2, 2), "blue")],
        duration=100,
        loop=0,
    )
    with pytest.raises(ValueError, match="静态"):
        image_content_type(io.BytesIO(content.getvalue()))


def test_collection_round_trip_preserves_order_purges_drafts_and_destroy(owner):
    item = product(owner)
    token = exchange(owner, item["id"])
    first = uploaded(owner, token)
    second = uploaded(owner, token, content=raster("JPEG"))
    draft = uploaded(owner, token)
    selected = [second["id"], first["id"]]
    job = submit(owner, token, {"source": collection(selected)})
    assert json.loads(managed_job(owner, job)["params"]["source"]) == selected
    with db() as c:
        assert (
            c.execute("SELECT id FROM job_files WHERE id=?", (draft["id"],)).fetchone()
            is None
        )
        assert all(
            row["bound"] == 1 for row in c.execute("SELECT bound FROM job_files")
        )
    assert batch(owner, job, "claim").status_code == 200
    outputs = [
        uploaded(owner, job, content=raster(format))
        for format in ("PNG", "JPEG", "WEBP")
    ]
    discarded = uploaded(owner, job)
    order = [outputs[2]["id"], outputs[0]["id"], outputs[1]["id"]]
    response = batch(owner, job, "succeed", output={"result": collection(order)})
    assert response.status_code == 200, response.text
    assert download(owner, token, order[0]).status_code == 409
    reveal = owner.post("/api/receipt/reveal", json={"token": token})
    assert reveal.status_code == 200, reveal.text
    assert json.loads(reveal.json()["output"]["result"]) == order
    assert [value["id"] for value in reveal.json()["files"]] == order
    assert [value["content_type"] for value in reveal.json()["files"]] == [
        "image/webp",
        "image/png",
        "image/jpeg",
    ]
    for descriptor in outputs:
        assert download(owner, token, descriptor["id"]).status_code == 200
        assert download(owner, token, descriptor["id"]).status_code == 200
    assert download(owner, token, discarded["id"]).status_code == 404
    assert owner.post("/api/receipt/destroy", json={"token": token}).status_code == 200
    with db() as c:
        assert c.execute("SELECT count(*) FROM job_files").fetchone()[0] == 0


def test_collection_once_reveal_and_download_each_file_only_once(owner):
    item = product(owner, view_policy="once")
    token = exchange(owner, item["id"])
    source = uploaded(owner, token)
    job = submit(owner, token, {"source": collection([source["id"]])})
    assert batch(owner, job, "claim").status_code == 200
    outputs = [uploaded(owner, job) for _ in range(2)]
    assert (
        batch(
            owner,
            job,
            "succeed",
            output={"result": collection([item["id"] for item in outputs])},
        ).status_code
        == 200
    )
    assert owner.post("/api/receipt/reveal", json={"token": token}).status_code == 200
    assert owner.post("/api/receipt/reveal", json={"token": token}).status_code == 410
    for output in outputs:
        with ThreadPoolExecutor(max_workers=3) as pool:
            responses = list(
                pool.map(lambda _: download(owner, token, output["id"]), range(3))
            )
        assert [response.status_code for response in responses].count(200) == 1
        assert [response.status_code for response in responses].count(410) == 2


@pytest.mark.parametrize(
    "shape",
    ["duplicate", "malformed", "url", "too_many", "non_string", "required_empty"],
)
def test_invalid_collection_references_fail_without_reserving_card(owner, shape):
    item = product(owner)
    token = exchange(owner, item["id"])
    source = uploaded(owner, token)
    values = {
        "duplicate": collection([source["id"], source["id"]]),
        "malformed": "not-json",
        "url": collection(["https://example.com/image.png"]),
        "too_many": collection([str(uuid.uuid4()) for _ in range(4)]),
        "non_string": collection([7]),
        "required_empty": "[]",
    }
    response = owner.post(
        "/api/redeem", json={"token": token, "params": {"source": values[shape]}}
    )
    assert response.status_code == 400, response.text
    with db() as c:
        assert c.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
        assert c.execute("SELECT state FROM cards").fetchone()[0] == "ready"


def test_optional_empty_collections_bind_and_release_no_attachments(owner):
    item = make_product(
        owner,
        parameters=[field("source", required=False)],
        outputs=[field("result", required=False)],
    )
    token = exchange(owner, item["id"])
    draft = uploaded(owner, token)
    job = submit(owner, token, {"source": "[]"})
    assert managed_job(owner, job)["params"]["source"] == "[]"
    assert batch(owner, job, "claim").status_code == 200
    assert batch(owner, job, "succeed", output={"result": "[]"}).status_code == 200
    reveal = owner.post("/api/receipt/reveal", json={"token": token})
    assert reveal.status_code == 200, reveal.text
    assert reveal.json()["output"] == {"result": "[]"}
    assert not reveal.json().get("files")
    with db() as c:
        assert (
            c.execute("SELECT id FROM job_files WHERE id=?", (draft["id"],)).fetchone()
            is None
        )


def test_collection_references_cannot_cross_cards_fields_or_attempts(owner):
    item = make_product(
        owner,
        parameters=[field("source", required=False), field("other", required=False)],
    )
    first = exchange(owner, item["id"])
    second = exchange(owner, item["id"])
    source = uploaded(owner, first)
    for token, key in ((second, "source"), (first, "other")):
        response = owner.post(
            "/api/redeem",
            json={"token": token, "params": {key: collection([source["id"]])}},
        )
        assert response.status_code == 403, response.text
    with db() as c:
        c.execute("UPDATE job_files SET attempt=2 WHERE id=?", (source["id"],))
    response = owner.post(
        "/api/redeem",
        json={"token": first, "params": {"source": collection([source["id"]])}},
    )
    assert response.status_code == 403, response.text


def test_retry_reuses_collection_in_order_and_invalidates_output_attempt(owner):
    item = product(owner)
    token = exchange(owner, item["id"])
    inputs = [uploaded(owner, token) for _ in range(2)]
    ids = [inputs[1]["id"], inputs[0]["id"]]
    job = submit(owner, token, {"source": collection(ids)})
    assert batch(owner, job, "claim").status_code == 200
    old = uploaded(owner, job)
    assert batch(owner, job, "fail", retryable=True).status_code == 200
    job = submit(owner, token, {"source": collection(ids)})
    assert job["attempt"] == 2
    assert json.loads(managed_job(owner, job)["params"]["source"]) == ids
    with db() as c:
        assert (
            c.execute("SELECT id FROM job_files WHERE id=?", (old["id"],)).fetchone()
            is None
        )
        assert all(
            row["attempt"] == 2 for row in c.execute("SELECT attempt FROM job_files")
        )
    assert batch(owner, job, "claim").status_code == 200
    output = uploaded(owner, job)
    with db() as c:
        c.execute("UPDATE job_files SET attempt=1 WHERE id=?", (output["id"],))
    assert (
        batch(
            owner, job, "succeed", output={"result": collection([output["id"]])}
        ).status_code
        == 403
    )


def test_collection_upload_keeps_per_card_quota_atomic(owner, monkeypatch):
    import extore.files as files

    item = product(owner)
    token = exchange(owner, item["id"])
    monkeypatch.setattr(files, "MAX_CARD_FILES", 2)
    assert upload(owner, token).status_code == 200
    assert upload(owner, token).status_code == 200
    assert upload(owner, token).status_code == 413
    with db() as c:
        assert c.execute("SELECT count(*) FROM job_files").fetchone()[0] == 2
        assert c.execute("SELECT count(*) FROM upload_reservations").fetchone()[0] == 0


def test_changed_generic_file_schema_cannot_bypass_image_byte_validation(owner):
    item = make_product(owner)
    token = exchange(owner, item["id"])
    fake = uploaded(owner, token, content=b"not raster pixels", mime="image/png")
    response = owner.put(
        f"/api/admin/products/{item['id']}",
        json={**item, "parameters": [field("source", "image")]},
    )
    assert response.status_code == 200, response.text
    response = owner.post(
        "/api/redeem", json={"token": token, "params": {"source": fake["id"]}}
    )
    assert response.status_code == 400, response.text
    with db() as c:
        assert c.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_changed_generic_raster_schema_canonicalizes_mime(owner):
    item = make_product(owner)
    token = exchange(owner, item["id"])
    source = uploaded(owner, token, content=raster("JPEG"), mime="text/plain")
    response = owner.put(
        f"/api/admin/products/{item['id']}",
        json={**item, "parameters": [field("source", "image")]},
    )
    assert response.status_code == 200, response.text
    job = submit(owner, token, {"source": source["id"]})
    assert managed_job(owner, job)["files"][0]["content_type"] == "image/jpeg"


def test_collection_output_rejects_foreign_job_and_input_files(owner):
    item = product(owner)
    tokens = [exchange(owner, item["id"]) for _ in range(2)]
    sources = [uploaded(owner, token) for token in tokens]
    jobs = [
        submit(owner, token, {"source": collection([source["id"]])})
        for token, source in zip(tokens, sources)
    ]
    assert all(batch(owner, job, "claim").status_code == 200 for job in jobs)
    output = uploaded(owner, jobs[0])
    assert (
        batch(
            owner, jobs[1], "succeed", output={"result": collection([output["id"]])}
        ).status_code
        == 403
    )
    assert (
        batch(
            owner, jobs[0], "succeed", output={"result": collection([sources[0]["id"]])}
        ).status_code
        == 403
    )


def test_collection_output_cannot_bypass_staff_product_or_claim_scope(owner):
    item = product(owner)
    token = exchange(owner, item["id"])
    source = uploaded(owner, token)
    job = submit(owner, token, {"source": collection([source["id"]])})
    assert batch(owner, job, "claim").status_code == 200
    other = product(owner)
    staff = set_staff(owner, other["id"], ["queue.view", "queue.process"])
    assert upload(owner, job).status_code == 403
    with db() as c:
        c.execute("UPDATE staff SET product_id=? WHERE id=?", (item["id"], staff))
    assert upload(owner, job).status_code == 409
