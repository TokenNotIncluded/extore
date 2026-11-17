"""HTTP contract checks against the compiled Rust binary and disposable SQLite data.

Run with .venv/bin/python tests/rust_contract.py. No Python application is started.
"""

import base64
import hashlib
import json
import os
import secrets
import socket
import sqlite3
import struct
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import uuid
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

ROOT = Path(__file__).resolve().parents[1]


class RustContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="extore-rust-contract-")
        cls.data = Path(cls.directory.name)
        cls.binary = Path(
            os.getenv("EXTORE_NATIVE_BINARY", ROOT / "target/debug/extore")
        ).resolve()
        if not cls.binary.exists():
            raise RuntimeError(
                "Build the native binary with cargo build before running contracts"
            )
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        cls.origin = f"http://127.0.0.1:{port}"
        cls.env = {
            **os.environ,
            "EXTORE_DATA": str(cls.data),
            "EXTORE_ORIGIN": cls.origin,
            "EXTORE_LISTEN": f"127.0.0.1:{port}",
            "EXTORE_STATIC": str(ROOT / "extore/static"),
            "EXTORE_UPLOAD_FILE_BYTES": str(1024 * 1024),
        }
        initialized = subprocess.run(
            [str(cls.binary), "init"], env=cls.env, capture_output=True, timeout=30
        )
        if initialized.returncode:
            raise RuntimeError(
                "Native database initialization failed: " + initialized.stderr.decode()
            )
        cls.log = (cls.data / "server.log").open("wb")
        cls.server = subprocess.Popen(
            [str(cls.binary), "serve"], env=cls.env, stdout=cls.log, stderr=cls.log
        )
        for _ in range(100):
            try:
                with urllib.request.urlopen(
                    cls.origin + "/health", timeout=1
                ) as response:
                    assert response.headers["x-extore-runtime"] == "rust"
                    return
            except (OSError, urllib.error.URLError):
                if cls.server.poll() is not None:
                    raise RuntimeError("Native server exited during startup")
                time.sleep(0.05)
        raise RuntimeError("Native server failed to start")

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        try:
            cls.server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.server.kill()
            cls.server.wait(timeout=5)
        cls.log.close()
        cls.directory.cleanup()

    def db(self):
        connection = sqlite3.connect(self.data / "extore.sqlite3", timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def setUp(self):
        self.shop, self.session = self.merchant("Contract A")
        self.other_shop, self.other_session = self.merchant("Contract B")

    def merchant(self, name):
        sid, raw, now = str(uuid.uuid4()), secrets.token_urlsafe(32), time.time()
        with self.db() as c:
            c.execute(
                "INSERT INTO shops(id,name,created,verified) VALUES (?,?,?,1)",
                (sid, name, now),
            )
            c.execute(
                "INSERT INTO sessions(digest,role,expires,created,id,last_seen,shop_id,auth_at,auth_method) "
                "VALUES (?,'admin',?,?,?,?,?,?,?)",
                (
                    hashlib.sha256(raw.encode()).hexdigest(),
                    now + 3600,
                    now,
                    str(uuid.uuid4()),
                    now,
                    sid,
                    now,
                    "passkey",
                ),
            )
        return sid, raw

    def request(
        self, path, method="GET", body=None, session=None, raw=None, content_type=None
    ):
        headers = {"Origin": self.origin}
        if session:
            headers["Cookie"] = "extore_session=" + session
        if body is not None:
            raw, content_type = json.dumps(body).encode(), "application/json"
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(
            self.origin + path, data=raw, method=method, headers=headers
        )
        try:
            response = urllib.request.urlopen(request, timeout=20)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            data = response.read()
            parsed = (
                json.loads(data)
                if response.headers.get_content_type() == "application/json"
                else data
            )
            return response.status, parsed, response.headers

    def api(self, path, method="GET", body=None, session=None, expected=200):
        status, data, _ = self.request(
            path, method, body, self.session if session is None else session
        )
        self.assertEqual(status, expected, data)
        return data

    def product(self, **values):
        return self.api(
            "/api/admin/products", "POST", {"name": "Contract product", **values}
        )

    def exchange(self, pid):
        issued = self.api("/api/admin/cards", "POST", {"product_id": pid, "count": 1})
        token = self.api("/api/exchange", "POST", {"code": issued["codes"][0]})["token"]
        return token

    def upload(
        self,
        fields,
        content=b"source",
        *,
        output=False,
        name="a.txt",
        mime="text/plain",
        expected=200,
        session=None,
    ):
        boundary = "rust-contract-" + secrets.token_hex(12)
        chunks = []
        for key, value in fields:
            chunks.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
            )
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\nContent-Type: {mime}\r\n\r\n'.encode()
        )
        chunks.extend([content, f"\r\n--{boundary}--\r\n".encode()])
        status, data, _ = self.request(
            "/api/manage/files/upload" if output else "/api/files/upload",
            "POST",
            session=self.session if session is None else session,
            raw=b"".join(chunks),
            content_type=f"multipart/form-data; boundary={boundary}",
        )
        self.assertEqual(status, expected, data)
        return data

    def file_product(self, **values):
        return self.product(
            parameters=[{"key": "source", "label": {"en": "Source"}, "type": "file"}],
            outputs=[{"key": "result", "label": {"en": "Result"}, "type": "file"}],
            **values,
        )

    def prepare(self, **values):
        p = self.file_product(**values)
        token = self.exchange(p["id"])
        source = self.upload([("token", token), ("field_key", "source")])
        job = self.api(
            "/api/redeem", "POST", {"token": token, "params": {"source": source["id"]}}
        )
        return p, token, job, source

    def action(self, job, action, **values):
        return self.api(
            "/api/manage/batch",
            "POST",
            {
                "product_id": job["product_id"],
                "ids": [job["id"]],
                "action": action,
                **values,
            },
        )

    def complete(self, job):
        self.action(job, "claim")
        self.upload(
            [("job_id", job["id"]), ("field_key", "result"), ("attempt", "99")],
            output=True,
            expected=409,
        )
        out = self.upload(
            [("job_id", job["id"]), ("field_key", "result")],
            b"delivery bytes",
            output=True,
        )
        self.action(job, "succeed", output={"result": out["id"]})
        return out

    def test_native_shell_and_accessible_theme(self):
        status, html, headers = self.request("/")
        self.assertEqual(status, 200)
        self.assertEqual(headers["x-extore-runtime"], "rust")
        self.assertIn(b"native-theme.css", html)
        self.assertIn(b"skip-link", html)
        self.assertEqual(self.request("/admin?product=missing")[0], 200)
        self.assertEqual(self.request("/static/../../Cargo.toml")[0], 404)

    def test_processor_profiles_hide_secrets_and_freeze_card_revisions(self):
        p = self.product(
            mode="script",
            processor_id="resource_link",
            processor_config={
                "resource_url": "https://example.com/PRIVATE-fixture",
                "message": "editable",
            },
        )
        self.assertEqual(p["processor_config"], {"message": "editable"})
        bound = self.api(f"/api/admin/processor-profiles/bindings/{p['id']}")["profile"]
        self.assertEqual(bound["bound_revision"], 1)
        self.api(
            f"/api/admin/processor-profiles/{bound['id']}",
            session=self.other_session,
            expected=404,
        )
        self.api("/api/admin/cards", "POST", {"product_id": p["id"], "count": 1})
        edited = self.api(
            f"/api/admin/processor-profiles/{bound['id']}",
            "PUT",
            {
                "configuration": {"resource_url": "", "message": "changed"},
                "workflow": {
                    "secrets": {"TOKEN": "fixture-secret"},
                    "variables": {"REGION": "cn"},
                },
            },
        )
        self.assertEqual(edited["revision"], 2)
        self.assertEqual(edited["workflow"]["configured_secret_names"], ["TOKEN"])
        for url in [
            "/api/admin/products",
            "/api/admin/processor-profiles",
            f"/api/admin/processor-profiles/{bound['id']}",
        ]:
            encoded = json.dumps(self.api(url))
            self.assertNotIn("PRIVATE-fixture", encoded)
            self.assertNotIn("fixture-secret", encoded)
            self.assertNotIn("ciphertext", encoded)
        self.assertEqual(
            self.api(f"/api/admin/processor-profiles/bindings/{p['id']}")["profile"][
                "bound_revision"
            ],
            1,
        )
        self.api(
            f"/api/admin/processor-profiles/bindings/{p['id']}",
            "PUT",
            {"profile_id": bound["id"]},
        )
        self.api("/api/admin/cards", "POST", {"product_id": p["id"], "count": 1})
        with self.db() as c:
            revisions = [
                r[0]
                for r in c.execute(
                    "SELECT revision FROM processor_card_bindings WHERE product_id=? ORDER BY revision",
                    (p["id"],),
                )
            ]
            self.assertEqual(revisions, [1, 2])
            encoded = " ".join(
                r[0]
                for r in c.execute(
                    "SELECT ciphertext FROM processor_profile_revisions WHERE profile_id=?",
                    (bound["id"],),
                )
            )
            self.assertNotIn("PRIVATE-fixture", encoded)
            self.assertNotIn("fixture-secret", encoded)
        self.api(
            f"/api/admin/processor-profiles/{bound['id']}",
            "PUT",
            {"workflow": {"secrets": {"TOKEN": ""}}},
        )
        self.assertEqual(
            self.api(f"/api/admin/processor-profiles/{bound['id']}")["workflow"][
                "configured_secret_names"
            ],
            ["TOKEN"],
        )
        self.api(
            f"/api/admin/processor-profiles/{bound['id']}",
            "PUT",
            {"workflow": {"secrets": {"LD_PRELOAD": ""}}},
            expected=422,
        )
        self.api(
            f"/api/admin/processor-profiles/{bound['id']}",
            "PUT",
            {"workflow": {"delete_secrets": ["TOKEN"]}},
        )
        self.assertEqual(
            self.api(f"/api/admin/processor-profiles/{bound['id']}")["workflow"][
                "configured_secret_names"
            ],
            [],
        )

    def test_file_scope_reveal_and_once_atomic_consumption(self):
        p, token, job, source = self.prepare(view_policy="once")
        out = self.complete(job)
        self.api(
            "/api/files/download",
            "POST",
            {"token": token, "file_id": out["id"]},
            expected=409,
        )
        self.api(
            "/api/files/download",
            "POST",
            {"token": token, "file_id": source["id"]},
            expected=403,
        )
        self.api(
            f"/api/manage/files/{source['id']}/download",
            session=self.other_session,
            expected=403,
        )
        self.api("/api/receipt/reveal", "POST", {"token": token})

        def download(_):
            return self.request(
                "/api/files/download",
                "POST",
                {"token": token, "file_id": out["id"]},
                self.session,
            )

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(download, range(4)))
        self.assertEqual(sorted(r[0] for r in results), [200, 410, 410, 410])
        winner = next(r for r in results if r[0] == 200)
        self.assertEqual(winner[1], b"delivery bytes")
        self.assertEqual(
            winner[2]["content-security-policy"], "default-src 'none'; sandbox"
        )
        with self.db() as c:
            row = c.execute(
                "SELECT content,consumed FROM job_files WHERE id=?", (out["id"],)
            ).fetchone()
            self.assertIsNone(row[0])
            self.assertEqual(row[1], 1)
        items = self.api(f"/api/manage/files?job_id={job['id']}")
        self.assertTrue(all("content" not in f for f in items))

    def test_image_decoder_multipart_and_shop_quota(self):
        p = self.product(
            parameters=[{"key": "image", "label": {"en": "Image"}, "type": "image"}]
        )
        token = self.exchange(p["id"])
        fields = [("token", token), ("field_key", "image")]
        self.upload(fields, b"<svg onload=alert(1)>", mime="image/png", expected=400)

        def chunk(name, data):
            return (
                struct.pack(">I", len(data))
                + name
                + data
                + struct.pack(">I", zlib.crc32(name + data) & 0xFFFFFFFF)
            )

        png = (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00\xff"))
            + chunk(b"IEND", b"")
        )
        image = self.upload(fields, png, mime="text/plain")
        self.assertEqual(image["content_type"], "image/png")
        self.upload(fields, png[:-1], mime="image/png", expected=400)
        self.upload(fields + [("field_key", "image")], png, expected=400)
        self.upload(fields, b"x" * (1024 * 1024 + 1), expected=413)
        other = self.file_product()
        token2 = self.exchange(other["id"])
        with self.db() as c:
            c.execute("UPDATE shops SET storage_limit_bytes=1 WHERE id=?", (self.shop,))
        self.upload(
            [("token", token2), ("field_key", "source")], b"over quota", expected=507
        )
        usage = self.api("/api/admin/storage")
        self.assertNotIn("disk_free_bytes", usage)
        self.assertNotIn("disk_reserve_bytes", usage)

    def test_pipeline_review_signature_claim_and_revoke(self):
        p = self.product()
        private = Ed25519PrivateKey.generate()

        def b64(data):
            return base64.urlsafe_b64encode(data).decode().rstrip("=")

        key = b64(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
        unsigned = {
            "public_key": key,
            "client_name": "Rust contract worker",
            "nonce": b64(int(time.time()).to_bytes(8, "big") + secrets.token_bytes(24)),
            "kind": "shop.pipeline",
            "shop_id": self.shop,
            "product_ids": [p["id"]],
            "permissions": ["queue.view", "queue.process"],
            "authorization_id": None,
            "expected_revision": None,
            "reason": "Contract test",
        }
        canonical = json.dumps(
            unsigned, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        )
        signature = b64(
            private.sign(
                f"extore-cli-scope-request-v1\n{self.origin}\n{canonical}".encode()
            )
        )
        status, req, _ = self.request(
            "/api/cli/scopes/request", "POST", {**unsigned, "signature": signature}
        )
        self.assertEqual(status, 200, req)
        self.api(
            "/api/manage/device/options",
            "POST",
            {"user_code": req["user_code"]},
            session=self.other_session,
            expected=403,
        )
        options = self.api(
            "/api/manage/device/options", "POST", {"user_code": req["user_code"]}
        )
        approved = {
            "user_code": req["user_code"],
            "product_ids": options["selected"]["product_ids"],
            "permissions": options["selected"]["permissions"],
            "expires": options["selected"]["expires"],
            "review_digest": options["review_digest"],
        }
        self.api(
            "/api/manage/device/approve",
            "POST",
            {**approved, "permissions": ["queue.view", "queue.process", "queue.retry"]},
            expected=403,
        )
        with self.db() as c:
            c.execute(
                "UPDATE products SET config=json_set(config,'$.name','Changed after review') WHERE id=?",
                (p["id"],),
            )
        self.api("/api/manage/device/approve", "POST", approved, expected=409)
        options = self.api(
            "/api/manage/device/options", "POST", {"user_code": req["user_code"]}
        )
        approved["review_digest"] = options["review_digest"]
        self.api("/api/manage/device/approve", "POST", approved)
        with self.db() as c:
            self.assertEqual(
                c.execute(
                    "SELECT COUNT(*) FROM pipeline_authorizations WHERE shop_id=?",
                    (self.shop,),
                ).fetchone()[0],
                0,
            )
        proof = b64(
            private.sign(
                f"extore-cli-scope-claim-v1\n{self.origin}\n{req['request_id']}\n{req['challenge']}\n{key}".encode()
            )
        )
        status, claimed, _ = self.request(
            "/api/cli/scopes/claim",
            "POST",
            {"request_id": req["request_id"], "public_key": key, "signature": proof},
        )
        self.assertEqual(status, 200, claimed)
        auth = claimed["authorization"]
        self.assertEqual(auth["product_ids"], [p["id"]])
        self.assertEqual(auth["permissions"], ["queue.view", "queue.process"])
        status, repeated, _ = self.request(
            "/api/cli/scopes/claim",
            "POST",
            {"request_id": req["request_id"], "public_key": key, "signature": proof},
        )
        self.assertEqual(status, 200, repeated)
        self.assertEqual(repeated["authorization"]["id"], auth["id"])
        with self.db() as c:
            staff = c.execute(
                "SELECT max_uses,max_cli_uses,cli_uses FROM staff WHERE id=?",
                (claimed["bindings"][0]["staff_id"],),
            ).fetchone()
            self.assertEqual(tuple(staff), (0, 1, 1))
        self.api(
            f"/api/admin/pipeline-authorizations/{auth['id']}",
            "DELETE",
            {"expected_revision": auth["revision"] + 1},
            expected=409,
        )
        self.api(
            f"/api/admin/pipeline-authorizations/{auth['id']}",
            "DELETE",
            {"expected_revision": auth["revision"]},
        )
        self.assertEqual(self.api("/api/admin/pipeline-authorizations"), [])

    def test_instruction_edits_preserve_other_configuration_and_scope(self):
        p = self.product(description="Keep this")
        self.api("/api/admin/factory", "PUT", {"factory_slogan": "Review carefully"})
        self.api(
            f"/api/manage/workshop?product_id={p['id']}",
            "PUT",
            {"workshop_slogan": "Keep **Markdown**"},
        )
        view = self.api(f"/api/manage/instructions?product_id={p['id']}")
        self.assertEqual(view["factory_slogan"], "Review carefully")
        self.assertEqual(view["workshop_slogan"], "Keep **Markdown**")
        self.assertEqual(len(view["revision"]), 64)
        self.api(
            f"/api/manage/instructions?product_id={p['id']}",
            session=self.other_session,
            expected=403,
        )
        with self.db() as c:
            config = json.loads(
                c.execute(
                    "SELECT config FROM products WHERE id=?", (p["id"],)
                ).fetchone()[0]
            )
            self.assertEqual(config["description"], "Keep this")


if __name__ == "__main__":
    unittest.main(verbosity=2)
