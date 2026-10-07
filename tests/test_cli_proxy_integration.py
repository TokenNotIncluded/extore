"""Loopback-only proxy acceptance: real sockets, TLS, and signed API admission."""

import argparse
import json
import select
import socket
import socketserver
import ssl
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from extore import customer_cli, manage_client, owner_client
from extore.db import db
from extore.http_proxy import ProxySettings, resolve_proxy
from extore.manage_client import ManageError

ORIGIN = "http://localhost:8000"
REMOTE_HOST = "service.proxy-test.invalid"


@contextmanager
def _running(server):
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()


def _reply(handler, status, payload, headers=None):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    handler.send_response(status)
    for key, value in (headers or {}).items():
        if key.lower() not in ("content-length", "connection", "transfer-encoding"):
            handler.send_header(key, value)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.wfile.write(body)


def _http_server(callback):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def respond(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            status, payload, headers = callback(
                self.command, self.path, self.headers, body
            )
            _reply(self, status, payload, headers)

        def log_message(self, *args):
            pass

    return ThreadingHTTPServer(("127.0.0.1", 0), Handler)


def _parser():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    for module in (manage_client, owner_client, customer_cli):
        module.add_parser(commands)
    return parser


@pytest.mark.parametrize("role", ["manage", "admin"])
def test_first_signed_device_code_uses_real_http_proxy(role, client, tmp_path):
    requests = []

    def relay(method, target, headers, body):
        parsed = urlsplit(target)
        assert parsed.scheme == "http" and parsed.netloc == "localhost:8000"
        requests.append(
            (method, parsed.path, headers.get("Proxy-Authorization") is not None)
        )
        forwarded = {
            key: value
            for key, value in headers.items()
            if key.lower() not in ("proxy-authorization", "connection", "host")
        }
        response = client.request(method, parsed.path, content=body, headers=forwarded)
        return response.status_code, response.content, dict(response.headers)

    profile = tmp_path / (role + ".json")
    with _running(_http_server(relay)) as proxy:
        proxy_url = (
            f"http://test-user:synthetic-proxy-secret@127.0.0.1:{proxy.server_port}"
        )
        command = [
            role,
            "--profile",
            str(profile),
            "--proxy",
            proxy_url,
            "login",
            "--origin",
            ORIGIN,
            "--client-name",
            "Proxy admission fixture",
        ]
        if role == "manage":
            command.extend(
                [
                    "--device-code",
                    "--no-wait",
                    "--existing-link",
                    "--agent-type",
                    "test-fixture",
                ]
            )
        args = _parser().parse_args(command)
        result = (manage_client if role == "manage" else owner_client).execute(args)
    assert result["ok"] is True
    assert (
        (result.get("pending") is True)
        if role == "manage"
        else result["status"] == "pending"
    )
    expected = (
        "/api/cli/device/request" if role == "manage" else "/api/cli/owner/request"
    )
    assert requests[0] == ("POST", expected, True)
    assert all(item[2] for item in requests)
    with db() as connection:
        table = "cli_device_requests" if role == "manage" else "owner_cli_requests"
        assert connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] == 1
    saved = profile.read_text()
    assert "synthetic-proxy-secret" not in saved
    assert proxy_url not in saved


def _certificate(tmp_path):
    now = datetime.now(timezone.utc)
    authority_key = ec.generate_private_key(ec.SECP256R1())
    authority_name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "Loopback fixture CA")]
    )
    authority = (
        x509.CertificateBuilder()
        .subject_name(authority_name)
        .issuer_name(authority_name)
        .public_key(authority_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(authority_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, REMOTE_HOST)]))
        .issuer_name(authority_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(REMOTE_HOST)]), critical=False
        )
        .sign(authority_key, hashes.SHA256())
    )
    ca_path, cert_path, key_path = (
        tmp_path / name for name in ("ca.pem", "server.pem", "server.key")
    )
    ca_path.write_bytes(authority.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    return ca_path, context


def _tunnel(first, second):
    with second:
        while True:
            readable, _, _ = select.select([first, second], [], [], 3)
            if not readable:
                return
            for source in readable:
                try:
                    chunk = source.recv(65536)
                except OSError:
                    return
                if not chunk:
                    return
                try:
                    (second if source is first else first).sendall(chunk)
                except OSError:
                    return


def _connect_proxy(destination, captured):
    class Handler(BaseHTTPRequestHandler):
        def do_CONNECT(self):
            captured.append(self.path)
            with socket.create_connection(destination, timeout=3) as target:
                self.send_response(200)
                self.end_headers()
                self.wfile.flush()
                _tunnel(self.connection, target)
            self.close_connection = True

        def log_message(self, *args):
            pass

    return ThreadingHTTPServer(("127.0.0.1", 0), Handler)


def _deny_local_remote_dns(monkeypatch):
    original = socket.getaddrinfo

    def resolve(host, *args, **kwargs):
        assert host not in (REMOTE_HOST, "wrong.proxy-test.invalid"), (
            "remote DNS leaked outside the proxy"
        )
        return original(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)


def test_https_connect_verifies_real_certificate_and_hostname(tmp_path, monkeypatch):
    ca_path, context = _certificate(tmp_path)
    requests, connects = [], []

    def respond(method, target, headers, body):
        requests.append((method, target))
        return 200, {"ok": True}, {}

    server = _http_server(respond)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    _deny_local_remote_dns(monkeypatch)
    with (
        _running(server),
        _running(_connect_proxy(server.server_address, connects)) as proxy,
    ):
        settings = ProxySettings("explicit", f"http://127.0.0.1:{proxy.server_port}")
        origin = f"https://{REMOTE_HOST}:{server.server_port}"
        # The synthetic CA is trusted only inside this test. Production still uses verify=True.
        with customer_cli.CustomerClient(proxy_settings=settings) as customer:
            with pytest.raises(ManageError, match="^Cannot connect to Extore$"):
                customer.json(origin, "GET", "/proof")
        monkeypatch.setattr("certifi.where", lambda: str(ca_path))
        monkeypatch.setenv("SSL_CERT_FILE", "/must-not-override-fixture-ca.pem")
        with customer_cli.CustomerClient(proxy_settings=settings) as customer:
            assert customer.json(origin, "GET", "/proof") == {"ok": True}
            with pytest.raises(ManageError, match="^Cannot connect to Extore$"):
                customer.json(
                    f"https://wrong.proxy-test.invalid:{server.server_port}",
                    "GET",
                    "/proof",
                )
    assert requests == [("GET", "/proof")]
    assert connects == [
        f"{REMOTE_HOST}:{server.server_port}",
        f"{REMOTE_HOST}:{server.server_port}",
        f"wrong.proxy-test.invalid:{server.server_port}",
    ]


def test_no_proxy_bypasses_real_proxy_and_redirects_never_forward_bearers():
    direct_requests, proxy_requests = [], []

    def direct(method, target, headers, body):
        direct_requests.append(target)
        return 200, {"ok": True}, {}

    with _running(_http_server(direct)) as destination:
        origin = f"http://127.0.0.1:{destination.server_port}"

        def proxied(method, target, headers, body):
            proxy_requests.append((target, headers.get("Authorization")))
            return 302, {}, {"Location": origin + "/stolen"}

        with _running(_http_server(proxied)) as proxy:
            proxy_url = f"http://127.0.0.1:{proxy.server_port}"
            settings = resolve_proxy(
                argparse.Namespace(proxy_env=True),
                environ={"HTTP_PROXY": proxy_url, "NO_PROXY": "127.0.0.1"},
            )
            with customer_cli.CustomerClient(proxy_settings=settings) as customer:
                assert customer.json(origin, "GET", "/direct") == {"ok": True}
            assert proxy_requests == []
            with manage_client.ManageClient(
                {}, proxy_settings=ProxySettings("explicit", proxy_url)
            ) as manager:
                with pytest.raises(ManageError) as caught:
                    manager._json(
                        origin,
                        "GET",
                        "/redirect",
                        grant={"access_token": "synthetic-private-bearer"},
                    )
            assert caught.value.status == 302
    assert direct_requests == ["/direct"]
    assert proxy_requests == [(origin + "/redirect", "Bearer synthetic-private-bearer")]


def _read_exact(stream, size):
    value = stream.read(size)
    if len(value) != size:
        raise ConnectionError("incomplete synthetic SOCKS handshake")
    return value


def _socks_proxy(destination, captured):
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            assert _read_exact(self.rfile, 1) == b"\x05"
            methods = _read_exact(self.rfile, _read_exact(self.rfile, 1)[0])
            assert 2 in methods
            self.wfile.write(b"\x05\x02")
            self.wfile.flush()
            assert _read_exact(self.rfile, 1) == b"\x01"
            user = _read_exact(self.rfile, _read_exact(self.rfile, 1)[0])
            password = _read_exact(self.rfile, _read_exact(self.rfile, 1)[0])
            assert (user, password) == (b"fixture", b"synthetic-socks-secret")
            self.wfile.write(b"\x01\x00")
            self.wfile.flush()
            assert _read_exact(self.rfile, 3) == b"\x05\x01\x00"
            assert _read_exact(self.rfile, 1) == b"\x03"
            host = _read_exact(self.rfile, _read_exact(self.rfile, 1)[0]).decode()
            port = int.from_bytes(_read_exact(self.rfile, 2), "big")
            captured.append((host, port))
            with socket.create_connection(destination, timeout=3) as target:
                self.wfile.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
                self.wfile.flush()
                _tunnel(self.connection, target)

    return socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)


@pytest.mark.parametrize("scheme", ["socks5", "socks5h"])
def test_authenticated_socks_resolves_destination_at_proxy(
    scheme, tmp_path, monkeypatch, caplog
):
    pytest.importorskip("socksio")
    ca_path, context = _certificate(tmp_path)
    server = _http_server(lambda *args: (200, {"ok": True}, {}))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    monkeypatch.setattr("certifi.where", lambda: str(ca_path))
    _deny_local_remote_dns(monkeypatch)
    destinations = []
    with (
        _running(server),
        _running(_socks_proxy(server.server_address, destinations)) as proxy,
    ):
        url = f"{scheme}://fixture:synthetic-socks-secret@127.0.0.1:{proxy.server_address[1]}"
        settings = ProxySettings("explicit", url)
        with caplog.at_level("DEBUG", logger="httpcore.socks"):
            with customer_cli.CustomerClient(proxy_settings=settings) as customer:
                assert customer.json(
                    f"https://{REMOTE_HOST}:{server.server_port}", "GET", "/proof"
                ) == {"ok": True}
    assert destinations == [(REMOTE_HOST, server.server_port)]
    assert "synthetic-socks-secret" not in caplog.text
    assert "connect_tcp.started" in caplog.text


@pytest.mark.parametrize("status", [401, 407])
def test_proxy_rejection_does_not_echo_private_proxy_challenge(status):
    def rejection(*args):
        return (
            status,
            b"synthetic-private-proxy-password",
            {"Proxy-Authenticate": "Basic realm=private"},
        )

    with _running(_http_server(rejection)) as proxy:
        settings = ProxySettings(
            "explicit",
            f"http://user:synthetic-private-proxy-password@127.0.0.1:{proxy.server_port}",
        )
        with manage_client.ManageClient({}, proxy_settings=settings) as manager:
            with pytest.raises(ManageError) as caught:
                manager._json(ORIGIN, "GET", "/proof")
    assert caught.value.status == status
    assert "synthetic-private-proxy-password" not in json.dumps(caught.value.as_dict())
