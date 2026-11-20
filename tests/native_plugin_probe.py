"""Exercise real Extore + SQLite through the two native plugin libraries.

The fixture creates its own user/network namespace, TLS certificate and database.
Only shop identities and browser sessions are seeded. Product creation, OAuth
consent, tokens, catalog reads and revocation use the actual Rust HTTP service.
It never binds a port or changes host resolution in the caller's network namespace.

An optional prebuilt consumer command receives EXTORE_PLUGIN_PROBE_CONFIG, a
temporary mode-0600 JSON file. The report contains no cookies, tokens or keys.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import http.client
import http.server
import json
import os
import secrets
import signal
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path

ORIGIN = "https://localhost.localdomain"
CATALOG = "/api/integrations/commerce/products"
REDIRECT = "https://store.example.com/extore/callback"


def require(value, message):
    if not value:
        raise RuntimeError(message)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class API:
    def __init__(self, certificate):
        context = ssl.create_default_context(cafile=str(certificate))
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=context),
            NoRedirect(),
        )

    def request(
        self,
        method,
        path,
        *,
        body=None,
        form=None,
        cookie=None,
        token=None,
        headers_extra=None,
        want=200,
    ):
        headers = {"Accept": "application/json"}
        if headers_extra:
            headers.update(headers_extra)
        data = None
        if cookie:
            headers.update(Cookie="extore_session=" + cookie, Origin=ORIGIN)
        if token:
            headers["Authorization"] = "Bearer " + token
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = urllib.request.Request(
            ORIGIN + path, data=data, headers=headers, method=method
        )
        try:
            response = self.opener.open(request, timeout=30)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            raw = response.read(16 * 1024 * 1024 + 1)
            # Do not include bodies, queries, credentials or secrets on failure.
            require(
                response.status == want,
                f"{method} {urllib.parse.urlsplit(path).path}: "
                f"HTTP {response.status}, expected {want}",
            )
            require(len(raw) <= 16 * 1024 * 1024, "oversized fixture response")
            return (json.loads(raw) if raw else None), response.headers


class BridgeOutput(ctypes.Structure):
    _fields_ = [
        ("data", ctypes.c_void_p),
        ("length", ctypes.c_size_t),
        ("owner", ctypes.c_size_t),
    ]


class NativeBridge:
    def __init__(self, host, plugin, certificate):
        self.plugin = str(plugin).encode()
        self.certificate = Path(certificate).read_bytes()
        self.library = ctypes.CDLL(str(host))
        self.library.extore_host_abi_version.argtypes = []
        self.library.extore_host_abi_version.restype = ctypes.c_uint32
        self.library.extore_host_open.argtypes = [
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self.library.extore_host_open.restype = ctypes.c_int32
        self.library.extore_host_catalog.argtypes = [
            ctypes.c_size_t,
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.POINTER(BridgeOutput),
        ]
        self.library.extore_host_catalog.restype = ctypes.c_int32
        self.library.extore_host_release.argtypes = [ctypes.c_size_t]
        self.library.extore_host_release.restype = None
        self.library.extore_host_close.argtypes = [ctypes.c_size_t]
        self.library.extore_host_close.restype = ctypes.c_int32
        require(self.library.extore_host_abi_version() == 1, "host ABI differs")
        self.releases = 0
        self.closes = 0

    def open(self, *, path=None, origin=None, expected=0):
        if origin is None:
            origin = ORIGIN
        path = self.plugin if path is None else str(path).encode()
        origin = origin.encode()
        handle = ctypes.c_size_t(0)
        status = self.library.extore_host_open(
            path,
            len(path),
            origin,
            len(origin),
            self.certificate,
            len(self.certificate),
            ctypes.byref(handle),
        )
        require(status == expected, f"open returned {status}, expected {expected}")
        require(
            bool(handle.value) == (status == 0),
            "open ownership did not match its status",
        )
        return handle.value

    def read(self, handle, token, expected=0):
        token = token.encode()
        output = BridgeOutput()
        status = self.library.extore_host_catalog(
            handle, token, len(token), ctypes.byref(output)
        )
        try:
            require(
                status == expected, f"catalog returned {status}, expected {expected}"
            )
            if status:
                require(
                    not output.owner and not output.data and not output.length,
                    "failed catalog retained an output allocation",
                )
                return None
            require(
                output.owner and output.data and 0 < output.length <= 16 << 20,
                "successful catalog has invalid output ownership",
            )
            return json.loads(ctypes.string_at(output.data, output.length))
        finally:
            if output.owner:
                self.library.extore_host_release(output.owner)
                self.releases += 1

    def close(self, handle):
        status = self.library.extore_host_close(handle)
        require(status == 0, f"close returned {status}")
        self.closes += 1


def seed_identities(database):
    """Test identities only; all business data is created through HTTP."""
    now = time.time()
    shops = []
    with sqlite3.connect(database) as db:
        for name in ("alpha", "beta"):
            shop = str(uuid.uuid4())
            cookie = secrets.token_urlsafe(32)
            db.execute(
                "INSERT INTO shops(id,name,email,created,verified) VALUES (?,?,?,?,1)",
                (shop, name, shop + "@example.com", now),
            )
            db.execute(
                "INSERT INTO sessions(digest,role,staff_id,expires,created,id,"
                "last_seen,ip,ua,revoked,shop_id,auth_at,auth_method) "
                "VALUES (?,'admin',NULL,?,?,?,?,?,?,0,?,?,'legacy')",
                (
                    hashlib.sha256(cookie.encode()).hexdigest(),
                    now + 900,
                    now,
                    str(uuid.uuid4()),
                    now,
                    "127.0.0.1",
                    "native-plugin-fixture",
                    shop,
                    now,
                ),
            )
            shops.append({"id": shop, "name": name, "cookie": cookie})
    return shops


def product(api, shop, name):
    value, _ = api.request(
        "POST",
        "/api/admin/products",
        cookie=shop["cookie"],
        body={
            "name": name,
            "description": "Isolated native plugin fixture",
            "public": True,
            "parameters": [],
            "mode": "manual",
            "delivery": "content",
            "view_policy": "repeat",
            "variants": [
                {
                    "id": "fixed",
                    "name": "Fixed delivery",
                    "price": "2.500000",
                    "currency": "CNY",
                    "enabled": True,
                    "marketplace": {
                        "template": "fixed-content",
                        "price_quota": 750000,
                        "fixed_content": "PRIVATE-" + shop["name"] + "-FIXTURE",
                    },
                }
            ],
            "marketplace": {
                "title": name,
                "description": "Explicitly approved template",
                "image_urls": [],
                "links": [],
                "payment_methods": ["balance"],
            },
        },
    )
    require(bool(value.get("id")), "product API returned no identity")
    return value["id"]


def consent(api, shop, product_id):
    client, _ = api.request(
        "POST",
        "/api/admin/commerce/clients",
        cookie=shop["cookie"],
        body={"client_name": "Native plugin fixture", "redirect_uris": [REDIRECT]},
    )
    client_id = client["client_id"]
    verifier = secrets.token_urlsafe(32)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    state = secrets.token_urlsafe(24)
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "scope": "products.read marketplace.sync",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "product_ids": product_id,
        }
    )
    _, headers = api.request("GET", "/oauth/authorize?" + query, want=303)
    request_id = urllib.parse.urlsplit(headers["Location"]).fragment
    require(bool(request_id), "authorization returned no consent request")
    request_path = "/api/admin/commerce/requests/" + request_id
    review, _ = api.request("GET", request_path, cookie=shop["cookie"])
    approved, _ = api.request(
        "POST",
        request_path + "/approve",
        cookie=shop["cookie"],
        body={
            "shop_id": shop["id"],
            "review_digest": review["review_digest"],
            "scopes": ["products.read", "marketplace.sync"],
            "product_ids": [product_id],
            "card_limits": [],
            "grant_expires": time.time() + 3600,
        },
    )
    callback = urllib.parse.parse_qs(
        urllib.parse.urlsplit(approved["redirect_uri"]).query
    )
    require(
        callback.get("state") == [state] and callback.get("iss") == [ORIGIN],
        "callback state or issuer differs",
    )
    credentials, _ = api.request(
        "POST",
        "/api/integrations/commerce/token",
        form={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": callback["code"][0],
            "redirect_uri": REDIRECT,
            "code_verifier": verifier,
        },
    )
    require(
        credentials["scope"] == "products.read marketplace.sync",
        "grant acquired unexpected authority",
    )
    return {
        "client_id": client_id,
        "product_id": product_id,
        "credentials": credentials,
    }


def serve_tls(upstream_port, certificate, key, observations):
    class Forward(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def forward(self):
            length = int(self.headers.get("Content-Length", "0"))
            require(0 <= length <= 5 * 1024 * 1024, "fixture body limit")
            body = self.rfile.read(length) if length else None
            headers = {
                key: value
                for key, value in self.headers.items()
                if key.lower() not in {"connection", "transfer-encoding"}
            }
            upstream = http.client.HTTPConnection(
                "127.0.0.1", upstream_port, timeout=30
            )
            try:
                upstream.request(self.command, self.path, body, headers)
                response = upstream.getresponse()
                data = response.read(16 * 1024 * 1024 + 1)
                require(len(data) <= 16 * 1024 * 1024, "fixture response limit")
                observations.append(
                    (
                        self.command,
                        urllib.parse.urlsplit(self.path).path,
                        response.status,
                    )
                )
                self.send_response(response.status)
                for name, value in response.getheaders():
                    if name.lower() not in {
                        "connection",
                        "transfer-encoding",
                        "content-length",
                    }:
                        self.send_header(name, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            finally:
                upstream.close()

        do_GET = do_POST = do_PUT = do_DELETE = forward

    class IPv6Server(http.server.ThreadingHTTPServer):
        address_family = socket.AF_INET6

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certificate, key)
    servers = []
    for cls, address in (
        (http.server.ThreadingHTTPServer, "127.0.0.1"),
        (IPv6Server, "::1"),
    ):
        server = cls((address, 443), Forward)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
    return servers


@contextmanager
def private_host_mapping(args, temp, report):
    """Bind test DNS only after proving a distinct private mount namespace."""
    if not args.private_hosts:
        yield
        return
    require(
        bool(args.parent_mountns)
        and args.parent_mountns != os.readlink("/proc/self/ns/mnt"),
        "refusing hostname overrides in the caller's mount namespace",
    )
    subprocess.run(["mount", "--make-rprivate", "/"], check=True, capture_output=True)
    before = {path: digest(path) for path in ("/etc/hosts", "/etc/nsswitch.conf")}
    hosts = temp / "private-hosts"
    hosts.write_text(
        "127.0.0.1 localhost localhost.localdomain extore.lmm.best\n"
        "::1 localhost ip6-localhost\n"
        "127.0.0.2 api.lmm.best\n"
    )
    hosts.chmod(0o644)
    nsswitch = temp / "private-nsswitch"
    nsswitch.write_text(
        "\n".join(
            line
            for line in Path("/etc/nsswitch.conf").read_text().splitlines()
            if not line.lstrip().startswith("hosts:")
        )
        + "\nhosts: files\n"
    )
    nsswitch.chmod(0o644)
    mounted = []
    try:
        for source, target in ((hosts, "/etc/hosts"), (nsswitch, "/etc/nsswitch.conf")):
            subprocess.run(
                ["mount", "--bind", str(source), target],
                check=True,
                capture_output=True,
            )
            mounted.append(target)
        for name, address in (
            ("extore.lmm.best", "127.0.0.1"),
            ("api.lmm.best", "127.0.0.2"),
        ):
            resolved = {
                entry[4][0]
                for entry in socket.getaddrinfo(name, 443, type=socket.SOCK_STREAM)
            }
            require(resolved == {address}, "private hostname mapping differs")
        report["isolated_mount_namespace"] = True
        report["private_hostname_resolution"] = "files"
        report["private_hostname_mapping"] = {
            "extore.lmm.best": "127.0.0.1",
            "api.lmm.best": "127.0.0.2",
        }
        yield
    finally:
        failed = []
        for target in reversed(mounted):
            result = subprocess.run(["umount", target], capture_output=True)
            if result.returncode:
                failed.append(target)
        unchanged = {
            path: digest(path) == expected for path, expected in before.items()
        }
        report["private_hosts_cleanup"] = {
            "bind_unmounted": not failed,
            "bound_file_count": len(mounted),
            "original_hosts_unchanged": unchanged["/etc/hosts"],
            "original_nsswitch_unchanged": unchanged["/etc/nsswitch.conf"],
        }
        require(not failed, "private resolver bind cleanup failed")
        require(all(unchanged.values()), "private resolver restoration differs")


def run(args):
    require(
        args.parent_netns != os.readlink("/proc/self/ns/net"),
        "refusing to use the caller's network namespace",
    )
    subprocess.run(["ip", "link", "set", "lo", "up"], check=True, capture_output=True)
    root = Path(__file__).resolve().parents[1]
    binary = args.binary.resolve()
    host = args.host_library.resolve()
    plugin = args.plugin_library.resolve()
    report_path = args.report.resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "status": "failed",
        "scope": "native bridge component test; optional consumer is separate",
        "origin": ORIGIN,
        "service_binary_sha256": digest(binary),
        "host_library_sha256": digest(host),
        "plugin_library_sha256": digest(plugin),
        "test_source_sha256": digest(__file__),
        "fixture_seeded_tables": ["shops", "sessions"],
        "isolated_network_namespace": True,
        "tls_certificate_verification": True,
        "pinned_image_unloaded": False,
    }
    try:
        with (
            tempfile.TemporaryDirectory(prefix="extore-native-plugin-") as temp,
            ExitStack() as cleanup,
        ):
            temp = Path(temp)
            cleanup.enter_context(private_host_mapping(args, temp, report))
            data = temp / "data"
            certificate, ca_key = temp / "ca.pem", temp / "ca-key.pem"
            server_certificate, key = temp / "server.pem", temp / "server-key.pem"
            csr, extensions = temp / "server.csr", temp / "server.ext"
            extensions.write_text(
                f"subjectAltName=DNS:{urllib.parse.urlsplit(ORIGIN).hostname}\n"
                "basicConstraints=critical,CA:FALSE\n"
                "keyUsage=critical,digitalSignature,keyEncipherment\n"
                "extendedKeyUsage=serverAuth\n"
                "subjectKeyIdentifier=hash\n"
                "authorityKeyIdentifier=keyid,issuer\n"
            )
            commands = [
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-nodes",
                    "-newkey",
                    "rsa:2048",
                    "-days",
                    "1",
                    "-subj",
                    "/CN=Extore fixture CA",
                    "-addext",
                    "basicConstraints=critical,CA:TRUE",
                    "-addext",
                    "keyUsage=critical,keyCertSign,cRLSign",
                    "-addext",
                    "subjectKeyIdentifier=hash",
                    "-addext",
                    "authorityKeyIdentifier=keyid:always",
                    "-keyout",
                    str(ca_key),
                    "-out",
                    str(certificate),
                ],
                [
                    "openssl",
                    "req",
                    "-new",
                    "-nodes",
                    "-newkey",
                    "rsa:2048",
                    "-subj",
                    f"/CN={urllib.parse.urlsplit(ORIGIN).hostname}",
                    "-keyout",
                    str(key),
                    "-out",
                    str(csr),
                ],
                [
                    "openssl",
                    "x509",
                    "-req",
                    "-in",
                    str(csr),
                    "-CA",
                    str(certificate),
                    "-CAkey",
                    str(ca_key),
                    "-CAcreateserial",
                    "-days",
                    "1",
                    "-sha256",
                    "-extfile",
                    str(extensions),
                    "-out",
                    str(server_certificate),
                ],
            ]
            for command in commands:
                subprocess.run(command, check=True, capture_output=True)
            ca_key.chmod(0o600)
            key.chmod(0o600)
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("EXTORE_")
            }
            env.update(
                EXTORE_DATA=str(data),
                EXTORE_ORIGIN=ORIGIN,
                EXTORE_STATIC=str(root / "extore/static"),
                EXTORE_LISTEN=f"127.0.0.1:{port}",
            )
            subprocess.run(
                [str(binary), "init"],
                env=env,
                check=True,
                capture_output=True,
                timeout=60,
            )
            shops = seed_identities(data / "extore.sqlite3")
            observations = []
            servers = []
            with (temp / "server.log").open("wb") as server_log:
                process = subprocess.Popen(
                    [str(binary), "serve"],
                    env=env,
                    stdout=server_log,
                    stderr=subprocess.STDOUT,
                )
                try:
                    for _ in range(100):
                        require(process.poll() is None, "native service exited")
                        try:
                            conn = http.client.HTTPConnection(
                                "127.0.0.1", port, timeout=0.2
                            )
                            conn.request("GET", "/health")
                            response = conn.getresponse()
                            health = json.loads(response.read())
                            conn.close()
                            if health.get("runtime") == "rust":
                                break
                        except (OSError, ValueError):
                            pass
                        time.sleep(0.05)
                    else:
                        raise RuntimeError("native service readiness timeout")
                    servers = serve_tls(port, server_certificate, key, observations)
                    api = API(certificate)
                    for shop in shops:
                        pid = product(api, shop, shop["name"] + " approved")
                        shop.update(consent(api, shop, pid))
                        shop["unapproved_product_id"] = product(
                            api, shop, shop["name"] + " unapproved"
                        )
                    alpha, beta = shops
                    bridge = NativeBridge(host, plugin, certificate)
                    bridge.open(path="relative.so", expected=-2)
                    bridge.open(path=temp / "missing.so", expected=-1)
                    # A real shared object without zplugin's entry must fail.
                    bridge.open(path=host, expected=-1)
                    bridge.open(origin="http://localhost.localdomain", expected=-2)
                    bridge.open(origin=ORIGIN + ":444", expected=-2)
                    bridge.open(origin=ORIGIN + "/catalog", expected=-2)
                    handle = bridge.open()
                    try:
                        wrong_thread_status = []
                        thread = threading.Thread(
                            target=lambda: wrong_thread_status.append(
                                bridge.library.extore_host_close(handle)
                            )
                        )
                        thread.start()
                        thread.join(timeout=10)
                        require(
                            wrong_thread_status == [-1001],
                            "foreign-thread close was not rejected",
                        )
                        bridge.read(handle, "", expected=-2)
                        bridge.read(handle, "invalid_token", expected=1001)
                        result = bridge.read(
                            handle, alpha["credentials"]["access_token"]
                        )
                        require(
                            result["shop"]["id"] == alpha["id"],
                            "catalog shop differs from grant",
                        )
                        require(
                            [item["id"] for item in result["products"]]
                            == [alpha["product_id"]],
                            "catalog escaped its approved product set",
                        )
                        require(
                            beta["product_id"] not in json.dumps(result),
                            "catalog exposed the other shop",
                        )
                    finally:
                        bridge.close(handle)
                    # Fresh per-request instances share the verified image only.
                    before = sum(item == ("GET", CATALOG, 200) for item in observations)
                    for _ in range(12):
                        handle = bridge.open()
                        try:
                            bridge.read(handle, alpha["credentials"]["access_token"])
                        finally:
                            bridge.close(handle)
                    after = sum(item == ("GET", CATALOG, 200) for item in observations)
                    require(
                        after - before == 12,
                        "a single native call repeated a business request",
                    )
                    handle = bridge.open(origin="https://second.example")
                    bridge.close(handle)
                    handle = bridge.open()
                    try:
                        result = bridge.read(
                            handle, beta["credentials"]["access_token"]
                        )
                        require(
                            result["shop"]["id"] == beta["id"],
                            "new instance retained another origin's client",
                        )
                    finally:
                        bridge.close(handle)
                    api.request(
                        "GET",
                        CATALOG + "/" + beta["product_id"],
                        token=alpha["credentials"]["access_token"],
                        want=403,
                    )
                    api.request(
                        "POST",
                        "/api/integrations/commerce/cards",
                        token=alpha["credentials"]["access_token"],
                        headers_extra={"Idempotency-Key": "native-plugin-read-only"},
                        body={
                            "product_id": alpha["product_id"],
                            "variant_id": "fixed",
                            "count": 1,
                        },
                        want=403,
                    )
                    report.update(
                        {
                            "actual_rust_health": True,
                            "authorized_catalog": True,
                            "unapproved_product_excluded": True,
                            "other_shop_excluded_from_catalog": True,
                            "cross_shop_http_detail_denied": True,
                            "invalid_token_denied": True,
                            "read_scope_cannot_issue_cards": True,
                            "wrong_thread_close_denied": True,
                            "missing_or_wrong_library_rejected": True,
                            "repeated_instances": 12,
                            "one_http_catalog_per_native_call": True,
                        }
                    )
                    with sqlite3.connect(data / "extore.sqlite3") as database:
                        require(
                            database.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
                            == 0,
                            "catalog-only plugin caused card issuance",
                        )
                        used = database.execute(
                            "SELECT COUNT(*) FROM commerce_grants WHERE last_used IS NOT NULL"
                        ).fetchone()[0]
                        require(used == 2, "actual SQLite grant usage was not recorded")
                    report["catalog_calls_did_not_issue_cards"] = True
                    report["actual_sqlite_grant_usage_recorded"] = True
                    config = {
                        "origin": ORIGIN,
                        "native_url": f"http://127.0.0.1:{port}",
                        "owner_cookie": alpha["cookie"],
                        "owner_cookie_b": beta["cookie"],
                        "root_ca_file": str(certificate),
                        "host_library": str(host),
                        "plugin_library": str(plugin),
                        "host_sha256": digest(host),
                        "plugin_sha256": digest(plugin),
                        "shop_a": alpha,
                        "shop_b": beta,
                        "report_path": str(temp / "consumer-facts.json"),
                    }
                    config_path = temp / "consumer-config.json"
                    config_path.write_text(json.dumps(config))
                    config_path.chmod(0o600)
                    if args.consumer:
                        result = subprocess.run(
                            args.consumer,
                            env=dict(
                                os.environ, EXTORE_PLUGIN_PROBE_CONFIG=str(config_path)
                            ),
                            capture_output=True,
                            timeout=args.consumer_timeout_seconds,
                        )
                        output_path = report_path.with_suffix(".consumer.log")
                        output_path.write_bytes(result.stdout + result.stderr)
                        output_path.chmod(0o600)
                        report["consumer_exit_code"] = result.returncode
                        require(result.returncode == 0, "consumer command failed")
                        facts_path = Path(config["report_path"])
                        require(
                            facts_path.is_file(), "consumer wrote no acceptance facts"
                        )
                        # Consumer owns a secret-free evidence schema.
                        report["consumer"] = json.loads(facts_path.read_text())
                    api.request(
                        "DELETE",
                        "/api/admin/commerce/grants/"
                        + alpha["credentials"]["grant_id"],
                        cookie=alpha["cookie"],
                    )
                    handle = bridge.open()
                    try:
                        bridge.read(
                            handle, alpha["credentials"]["access_token"], expected=1001
                        )
                    finally:
                        bridge.close(handle)
                    report.update(
                        {
                            "revoked_token_denied": True,
                            "output_release_calls": bridge.releases,
                            "successful_close_calls": bridge.closes,
                            "actual_catalog_http_statuses": {
                                str(status): sum(
                                    item == ("GET", CATALOG, status)
                                    for item in observations
                                )
                                for status in (200, 401, 403)
                            },
                            "status": "passed",
                        }
                    )
                finally:
                    for server, thread in servers:
                        server.shutdown()
                        server.server_close()
                        thread.join(timeout=5)
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    report["fixture_cleanup"] = {
                        "rust_service_pid": process.pid,
                        "rust_service_returncode": process.returncode,
                        "tls_listeners_closed": len(servers),
                    }
    except Exception as error:
        # Assertions deliberately carry only static labels, paths and statuses.
        report["failure"] = str(error)
        raise
    finally:
        report_path.write_text(json.dumps(report, indent=2) + "\n")
        report_path.chmod(0o600)
        print(json.dumps({"status": report["status"], "report": str(report_path)}))


def terminate_fixture(_signal, _frame):
    raise InterruptedError("fixture termination requested")


def main():
    global ORIGIN
    signal.signal(signal.SIGTERM, terminate_fixture)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--host-library", required=True, type=Path)
    parser.add_argument("--plugin-library", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument(
        "--origin", default=ORIGIN, choices=(ORIGIN, "https://extore.lmm.best")
    )
    parser.add_argument(
        "--private-hosts",
        action="store_true",
        help="Map formal Extore/LMM names inside a private mount namespace",
    )
    parser.add_argument("--inside-netns", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--parent-netns", help=argparse.SUPPRESS)
    parser.add_argument("--parent-mountns", help=argparse.SUPPRESS)
    parser.add_argument(
        "--consumer-timeout-seconds",
        type=int,
        default=180,
        help="Consumer limit, 1..480 seconds (default: 180)",
    )
    parser.add_argument("--consumer", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    ORIGIN = args.origin
    if args.private_hosts and ORIGIN != "https://extore.lmm.best":
        parser.error("--private-hosts requires --origin https://extore.lmm.best")
    if not 1 <= args.consumer_timeout_seconds <= 480:
        parser.error("--consumer-timeout-seconds must be between 1 and 480")
    if not args.inside_netns:
        command = ["unshare", "--user", "--map-root-user", "--net"]
        if args.private_hosts:
            command += ["--mount", "--propagation", "private"]
        command += [
            "--",
            sys.executable,
            str(Path(__file__).resolve()),
            "--inside-netns",
            "--parent-netns",
            os.readlink("/proc/self/ns/net"),
            "--parent-mountns",
            os.readlink("/proc/self/ns/mnt"),
            *sys.argv[1:],
        ]
        caller_files = (
            {path: digest(path) for path in ("/etc/hosts", "/etc/nsswitch.conf")}
            if args.private_hosts
            else None
        )
        process = subprocess.Popen(command)
        try:
            code = process.wait(timeout=args.consumer_timeout_seconds + 120)
        except BaseException:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise
        finally:
            if caller_files is not None:
                unchanged = {
                    path: digest(path) == expected
                    for path, expected in caller_files.items()
                }
                if args.report.is_file():
                    report = json.loads(args.report.read_text())
                    report["caller_hosts_file_unchanged"] = unchanged["/etc/hosts"]
                    report["caller_nsswitch_file_unchanged"] = unchanged[
                        "/etc/nsswitch.conf"
                    ]
                    args.report.write_text(json.dumps(report, indent=2) + "\n")
                    args.report.chmod(0o600)
                require(
                    all(unchanged.values()),
                    "caller resolver files changed during isolated fixture",
                )
        raise SystemExit(code)
    require(bool(args.parent_netns), "missing namespace ownership proof")
    run(args)


if __name__ == "__main__":
    main()
