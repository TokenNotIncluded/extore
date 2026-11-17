"""Run the HTTP contracts against a separate native Rust process.

Python only prepares isolated fixtures and checks persisted state. Enable with
PYTHONPATH=tests:. pytest -p native_http_plugin; no Python ASGI server runs.
"""

import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import fastapi.testclient
import httpx
import pytest
import starlette.testclient

_transport_port = None


class LoopbackTransport(httpx.BaseTransport):
    def __init__(self):
        self.transport = httpx.HTTPTransport()

    def handle_request(self, request):
        target = request.url.copy_with(
            scheme="http", host="127.0.0.1", port=_transport_port
        )
        forwarded = httpx.Request(
            request.method,
            target,
            headers=request.headers,
            stream=request.stream,
            extensions=request.extensions,
        )
        return self.transport.handle_request(forwarded)

    def close(self):
        self.transport.close()


class NativeTestClient(httpx.Client):
    __test__ = False

    def __init__(self, app=None, base_url="http://localhost:8000", **kwargs):
        for ignored in (
            "raise_server_exceptions",
            "backend",
            "backend_options",
            "root_path",
            "client",
        ):
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


fastapi.testclient.TestClient = NativeTestClient
starlette.testclient.TestClient = NativeTestClient


@pytest.fixture(scope="session", autouse=True)
def native_contract_server():
    global _transport_port
    from extore.db import init

    init()
    root = Path(__file__).resolve().parents[1]
    # conftest creates a unique directory before importing the legacy fixtures.
    fixture_data = Path(os.environ["EXTORE_DATA"]).resolve()
    binary = root / "target/debug/extore"
    if not binary.exists():
        raise RuntimeError("Build native service with cargo build --locked first")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        _transport_port = sock.getsockname()[1]
    env = dict(
        os.environ,
        EXTORE_LISTEN=f"127.0.0.1:{_transport_port}",
        EXTORE_STATIC=str(root / "extore/static"),
    )
    logdir = root / "output/rust-migration"
    logdir.mkdir(parents=True, exist_ok=True)
    prefix = f"native-contract-{os.getpid()}"
    http_log = logdir / f"{prefix}-http.log"
    with (logdir / f"{prefix}-init.log").open("wb") as initlog:
        subprocess.run(
            [str(binary), "init"],
            env=env,
            stdout=initlog,
            stderr=subprocess.STDOUT,
            check=True,
        )
    with http_log.open("wb") as log:
        process = subprocess.Popen(
            [str(binary), "serve"],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError(f"Native HTTP server failed; see {http_log}")
                try:
                    response = httpx.get(
                        f"http://127.0.0.1:{_transport_port}/health",
                        timeout=0.3,
                    )
                    if (
                        response.status_code == 200
                        and response.json().get("runtime") == "rust"
                    ):
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.05)
            else:
                raise RuntimeError("Native HTTP readiness timed out")
            yield process
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            if fixture_data.parent == Path(
                tempfile.gettempdir()
            ).resolve() and fixture_data.name.startswith("extore-tests-"):
                shutil.rmtree(fixture_data)
