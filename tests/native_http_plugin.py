"""Run the original HTTP contracts against a separate native Rust process.

Only fixture creation and direct database assertions use the legacy Python modules.
No Python ASGI server runs. Enable explicitly with -p tests.native_http_plugin.
"""
import os
import socket
import subprocess
import time
from pathlib import Path
import httpx
import pytest

_transport_port = None

class LoopbackTransport(httpx.BaseTransport):
    def __init__(self):
        self.transport = httpx.HTTPTransport()
    def handle_request(self, request):
        target = request.url.copy_with(scheme="http", host="127.0.0.1", port=_transport_port)
        forwarded = httpx.Request(request.method, target, headers=request.headers, stream=request.stream, extensions=request.extensions)
        return self.transport.handle_request(forwarded)
    def close(self):
        self.transport.close()

class NativeTestClient(httpx.Client):
    __test__ = False
    def __init__(self, app=None, base_url="http://localhost:8000", **kwargs):
        for ignored in ("raise_server_exceptions", "backend", "backend_options", "root_path", "client"):
            kwargs.pop(ignored, None)
        kwargs.setdefault("follow_redirects", True)
        kwargs.setdefault("timeout", 20)
        kwargs["transport"] = LoopbackTransport()
        super().__init__(base_url=base_url, **kwargs)
    def __enter__(self):
        # The former ASGI lifespan initialized the schema after the clean fixture.
        from extore.db import init
        init()
        return super().__enter__()

import fastapi.testclient
import starlette.testclient
fastapi.testclient.TestClient = NativeTestClient
starlette.testclient.TestClient = NativeTestClient

@pytest.fixture(scope="session", autouse=True)
def native_contract_server():
    global _transport_port
    from extore.db import init
    init()
    root = Path(__file__).resolve().parents[1]
    binary = root / "target/debug/extore"
    if not binary.exists():
        raise RuntimeError("Build native service with cargo build --locked first")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        _transport_port = sock.getsockname()[1]
    env = dict(os.environ, EXTORE_LISTEN=f"127.0.0.1:{_transport_port}", EXTORE_STATIC=str(root / "extore/static"))
    logdir=root/"output/rust-migration"
    logdir.mkdir(parents=True,exist_ok=True)
    with (logdir/"native-contract-init.log").open("wb") as initlog:
        subprocess.run([str(binary),"init"],env=env,stdout=initlog,stderr=subprocess.STDOUT,check=True)
    with (logdir/"native-contract-http.log").open("wb") as log:
        process=subprocess.Popen([str(binary),"serve"],env=env,stdout=log,stderr=subprocess.STDOUT)
        try:
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError("Native HTTP server failed; see output/rust-migration/native-contract-http.log")
                try:
                    r=httpx.get(f"http://127.0.0.1:{_transport_port}/health",timeout=.3)
                    if r.status_code==200 and r.json().get("runtime")=="rust":
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(.05)
            else:
                raise RuntimeError("Native HTTP readiness timed out")
            yield process
        finally:
            process.terminate()
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:process.kill();process.wait()
