"""Verify frozen LMM Go code against native Extore, using isolated temporary state.

Usage:
    python3 tests/native_lmm_sdk_probe.py --lmm-source /path/to/frozen/source \
        --lmm-repo /path/to/lmm-api-source --lmm-ref cdc2ad56 \
        --report output/rust-migration/native-lmm-sdk.json

The Go overlay leaves the frozen source untouched. Only the test owner session is
seeded directly; product, consent, token, stock and synchronization use real APIs.
"""

import argparse
import hashlib
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lmm-source", required=True, type=Path)
    parser.add_argument("--lmm-repo", required=True, type=Path)
    parser.add_argument("--lmm-ref", required=True)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    binary = (args.binary or root / "target/debug/extore").resolve()
    source = args.lmm_source.resolve()
    module = source / "services/extensions"
    report_path = args.report.resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    reference = subprocess.check_output(
        ["git", "-C", str(args.lmm_repo), "rev-parse", args.lmm_ref + "^{commit}"],
        text=True,
    ).strip()
    sources = {}
    for relative in (
        "services/extensions/go.mod",
        "services/extensions/go.sum",
        "services/extensions/internal/extore/client.go",
        "services/extensions/internal/extore/connection.go",
        "services/extensions/internal/modules/store/extore_sync.go",
        "services/extensions/internal/modules/store/extore_sync_test.go",
    ):
        frozen = (source / relative).read_bytes()
        committed = subprocess.check_output(
            ["git", "-C", str(args.lmm_repo), "show", reference + ":" + relative]
        )
        if frozen != committed:
            raise RuntimeError("Frozen candidate differs from commit: " + relative)
        sources[relative] = hashlib.sha256(frozen).hexdigest()

    report = {
        "status": "failed",
        "lmm_commit": reference,
        "source_sha256": sources,
        "rust_binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "test_source_sha256": hashlib.sha256(
            Path(__file__).with_suffix(".go").read_bytes()
        ).hexdigest(),
    }
    with tempfile.TemporaryDirectory(prefix="extore-lmm-native-") as temporary:
        temp = Path(temporary)
        data = temp / "data"
        origin = "https://extore.native.test"
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = dict(
            os.environ,
            EXTORE_DATA=str(data),
            EXTORE_ORIGIN=origin,
            EXTORE_STATIC=str(root / "extore/static"),
            EXTORE_LISTEN=f"127.0.0.1:{port}",
        )
        subprocess.run([str(binary), "init"], env=env, capture_output=True, check=True)
        owner = secrets.token_urlsafe(32)
        now = time.time()
        with sqlite3.connect(data / "extore.sqlite3") as db:
            db.execute(
                "INSERT INTO sessions(digest,role,staff_id,expires,created,id,last_seen,ip,ua,revoked,shop_id,auth_at,auth_method) "
                "VALUES (?,'admin',NULL,?,?,?,?,?,?,0,NULL,?,'legacy')",
                (
                    hashlib.sha256(owner.encode()).hexdigest(),
                    now + 600,
                    now,
                    str(uuid.uuid4()),
                    now,
                    "127.0.0.1",
                    "native-sdk-fixture",
                    now,
                ),
            )
        config = temp / "config.json"
        config.write_text(
            json.dumps(
                {
                    "origin": origin,
                    "native_url": f"http://127.0.0.1:{port}",
                    "owner_cookie": owner,
                    "report_path": str(temp / "facts.json"),
                }
            )
        )
        config.chmod(0o600)
        overlay = temp / "overlay.json"
        overlay.write_text(
            json.dumps(
                {
                    "Replace": {
                        str(
                            module
                            / "internal/modules/store/native_extore_bridge_test.go"
                        ): str(Path(__file__).with_suffix(".go"))
                    }
                }
            )
        )
        with (temp / "server.log").open("wb") as log:
            process = subprocess.Popen(
                [str(binary), "serve"], env=env, stdout=log, stderr=subprocess.STDOUT
            )
            try:
                for _ in range(100):
                    if process.poll() is not None:
                        raise RuntimeError(
                            "Native fixture server exited before readiness"
                        )
                    try:
                        with urllib.request.urlopen(
                            f"http://127.0.0.1:{port}/health", timeout=0.3
                        ) as response:
                            if json.load(response).get("runtime") == "rust":
                                break
                    except OSError:
                        pass
                    time.sleep(0.05)
                else:
                    raise RuntimeError("Native fixture readiness timed out")
                result = subprocess.run(
                    [
                        "go",
                        "test",
                        "-mod=readonly",
                        "-overlay=" + str(overlay),
                        "./internal/modules/store",
                        "-run",
                        "^TestNativeRustExtoreConnection$",
                        "-count=1",
                        "-v",
                    ],
                    cwd=module,
                    env=dict(
                        os.environ, EXTORE_NATIVE_PROBE_CONFIG=str(config), GOWORK="off"
                    ),
                    capture_output=True,
                    text=True,
                    timeout=240,
                )
                print(result.stdout, end="")
                print(result.stderr, end="")
                report["go_test_exit_code"] = result.returncode
                report["go_test_output"] = result.stdout + result.stderr
                if result.returncode == 0:
                    report.update(json.loads((temp / "facts.json").read_text()))
                report_path.write_text(
                    json.dumps(report, ensure_ascii=False, indent=2) + "\n"
                )
                if result.returncode:
                    raise SystemExit(result.returncode)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    print("Native LMM cross-stack report:", report_path)


if __name__ == "__main__":
    main()
