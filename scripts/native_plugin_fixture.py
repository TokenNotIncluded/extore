"""Isolated native-plugin fixture using Extore's real HTTP OAuth and revoke paths.

Only shops, products and a recently authenticated administrator session are seeded.
Clients, requests, PKCE grants and tokens are created by the existing Rust handlers.
This helper never reads production state and does not print credentials.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import tempfile
import time
from urllib.parse import parse_qs, urlencode, urlsplit


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class ExtoreFixture:
    def __init__(self, binary: str | Path, origin: str | None = None):
        self.binary = Path(binary).resolve(strict=True)
        self._configured_origin = origin
        self._temporary = None
        self._server = None
        self._log = None
        self.shop_a = "plugin-shop-a"
        self.shop_b = "plugin-shop-b"
        self.products = {self.shop_a: "plugin-product-a", self.shop_b: "plugin-product-b"}

    def __enter__(self):
        self._temporary = tempfile.TemporaryDirectory(prefix="extore-native-plugin-")
        self.root = Path(self._temporary.name)
        self.data_dir = self.root / "state"
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]
        self.http_origin = f"http://127.0.0.1:{self.port}"
        self.origin = self._configured_origin or self.http_origin
        env = os.environ.copy()
        env.update(EXTORE_DATA=str(self.data_dir), EXTORE_ORIGIN=self.origin,
                   EXTORE_LISTEN=f"127.0.0.1:{self.port}")
        self._log = (self.root / "server.log").open("w+")
        os.chmod(self.root / "server.log", 0o600)
        try:
            subprocess.run([str(self.binary), "init"], env=env, check=True,
                           stdout=self._log, stderr=subprocess.STDOUT, timeout=30)
            self._seed()
            self._server = subprocess.Popen([str(self.binary), "serve"], env=env,
                                            stdout=self._log, stderr=subprocess.STDOUT)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if self._server.poll() is not None:
                    raise RuntimeError("isolated Extore server exited during startup")
                try:
                    status, _, body = self.request("GET", "/health")
                    if status == 200 and body["runtime"] == "rust":
                        return self
                except (OSError, http.client.HTTPException):
                    pass
                time.sleep(0.05)
            raise TimeoutError("isolated Extore server did not become healthy")
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        if self._server is not None and self._server.poll() is None:
            self._server.terminate()
            try:
                self._server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._server.kill()
                self._server.wait()
        if self._log is not None:
            self._log.close()
        if self._temporary is not None:
            self._temporary.cleanup()

    def connection(self):
        connection = sqlite3.connect(self.data_dir / "extore.sqlite3")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _seed(self):
        self._session = secrets.token_urlsafe(32)
        now = time.time()
        with self.connection() as db:
            for shop in [self.shop_a, self.shop_b]:
                db.execute("INSERT INTO shops(id,name,enabled,created,verified) VALUES (?,?,1,?,1)",
                           (shop, f"Fixture {shop}", now))
                config = {
                    "name": f"Fixture {shop}", "description": "catalog version one",
                    "webhook_secret": "PRIVATE_WEBHOOK_SENTINEL",
                    "script": "PRIVATE_SCRIPT_SENTINEL",
                    "processor_config": {"api_key": "PRIVATE_PROCESSOR_SENTINEL"},
                    "variants": [{"id": "default", "name": "Default", "description": "",
                                  "price": "12.3400", "currency": "CNY", "enabled": True,
                                  "attributes": {"color": "blue",
                                                 "api_key": "PRIVATE_VARIANT_SENTINEL"}}],
                }
                db.execute("INSERT INTO products(id,config,created,shop_id) VALUES (?,?,?,?)",
                           (self.products[shop], json.dumps(config), now, shop))
            db.execute(
                "INSERT INTO sessions(digest,role,expires,created,id,auth_at,auth_method)"
                " VALUES (?,'admin',?,?,?,?, 'passkey')",
                (digest(self._session), now + 3600, now, secrets.token_hex(16), now),
            )

    def request(self, method: str, path: str, *, body=None, form=None,
                admin: bool = False, bearer: str | None = None):
        headers = {"Origin": self.origin}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if form is not None:
            payload = urlencode(form).encode("ascii")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if admin:
            headers["Cookie"] = "extore_session=" + self._session
        if bearer:
            headers["Authorization"] = "Bearer " + bearer
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=25)
        try:
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            raw = response.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024:
                raise AssertionError("fixture HTTP response exceeded the catalog limit")
            result = json.loads(raw) if raw else None
            return response.status, dict(response.getheaders()), result
        finally:
            connection.close()

    def authorize(self, shop: str | None = None, scopes: list[str] | None = None):
        shop = shop or self.shop_a
        scopes = scopes or ["products.read", "marketplace.sync"]
        redirect = "https://plugin-fixture.invalid/callback"
        status, _, client = self.request(
            "POST", "/api/admin/commerce/clients?" + urlencode({"shop_id": shop}),
            admin=True, body={"client_name": "Native plugin fixture",
                              "redirect_uris": [redirect]})
        assert status == 200, f"client registration failed: HTTP {status}"
        verifier = secrets.token_urlsafe(32)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        state = secrets.token_urlsafe(24)
        params = {"response_type": "code", "client_id": client["id"],
                  "redirect_uri": redirect, "scope": " ".join(scopes),
                  "state": state, "code_challenge": challenge,
                  "code_challenge_method": "S256", "product_ids": self.products[shop]}
        status, headers, _ = self.request("GET", "/oauth/authorize?" + urlencode(params))
        assert status == 303, f"PKCE authorization failed: HTTP {status}"
        location = next(v for k, v in headers.items() if k.lower() == "location")
        request_id = urlsplit(location).fragment
        base = "/api/admin/commerce/requests/" + request_id
        status, _, review = self.request("GET", base + "?" + urlencode({"shop_id": shop}), admin=True)
        assert status == 200, f"consent review failed: HTTP {status}"
        status, _, approval = self.request("POST", base + "/approve", admin=True, body={
            "shop_id": shop, "review_digest": review["review_digest"],
            "scopes": scopes, "product_ids": [self.products[shop]], "card_limits": [],
            "grant_expires": time.time() + 86400})
        assert status == 200, f"consent approval failed: HTTP {status}"
        callback = parse_qs(urlsplit(approval["redirect_uri"]).query)
        assert callback["state"] == [state] and callback["iss"] == [self.origin]
        status, _, tokens = self.request("POST", "/api/integrations/commerce/token", form={
            "grant_type": "authorization_code", "client_id": client["id"],
            "code": callback["code"][0], "code_verifier": verifier,
            "redirect_uri": redirect})
        assert status == 200, f"PKCE token exchange failed: HTTP {status}"
        return {"client_id": client["id"], "shop_id": shop, **tokens}

    def revoke(self, authorization):
        status, _, _ = self.request("POST", "/api/integrations/commerce/revoke", form={
            "client_id": authorization["client_id"],
            "token": authorization["refresh_token"],
            "token_type_hint": "refresh_token"})
        assert status == 200, f"revocation failed: HTTP {status}"

    def config(self):
        return {"schema_version": 1, "data_dir": str(self.data_dir), "origin": self.origin}
