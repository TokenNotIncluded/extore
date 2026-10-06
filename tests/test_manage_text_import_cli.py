import io
import json
import stat
import sys

import httpx
import pytest
from test_manage_cli import MockMerchant, arguments

from extore import manage_client as remote
from extore.manage_client import ManageError


class Merchant(MockMerchant):
    def __init__(self):
        super().__init__()
        self.links["first"] = "product-a", ["queue.view", "cards.manage"]
        self.imports = []

    def __call__(self, request):
        if request.url.path == "/api/manage/cards/import-text":
            value = json.loads(request.read())
            self.imports.append(value)
            assert value["product_id"] == "product-a"
            return httpx.Response(
                200,
                json={
                    "codes": ["PRIVATE-CARD-ONE"],
                    "items": [
                        {"code": "PRIVATE-CARD-ONE", "content": "private delivery text"}
                    ],
                    "batch_id": "batch-1",
                    "stats": {"lines": 2, "blank": 0, "duplicates": 1, "created": 1},
                },
            )
        return super().__call__(request)


@pytest.fixture
def cli(tmp_path, monkeypatch):
    profile = tmp_path / "private" / "profile.json"
    merchant = Merchant()
    transport = httpx.MockTransport(merchant)
    monkeypatch.setattr(
        "sys.stdin", io.StringIO("https://merchant.example/staff#first")
    )
    remote.execute(
        arguments("login", "--link-stdin", profile=profile), transport=transport
    )

    def run(*argv, stdin=None):
        if stdin is not None:
            monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
        return remote.execute(arguments(*argv, profile=profile), transport=transport)

    return run, merchant


def test_import_text_saves_full_mapping_only_in_private_output(cli, tmp_path):
    run, merchant = cli
    source = tmp_path / "stock.txt"
    source.write_text("商品一\n商品一\n", encoding="utf-8")
    result = run(
        "cards",
        "import-text",
        "--product",
        "product-a",
        "--variant",
        "word",
        "--file",
        str(source),
    )
    assert merchant.imports[0]["text"] == "商品一\n商品一\n"
    assert merchant.imports[0]["variant_id"] == "word"
    output = __import__("pathlib").Path(result["output"])
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert (
        json.loads(output.read_text())["items"][0]["content"] == "private delivery text"
    )
    assert "PRIVATE-CARD" not in json.dumps(
        result
    ) and "private delivery text" not in json.dumps(result)
    assert result["count"] == 1 and result["stats"]["duplicates"] == 1


def test_import_text_bounded_utf8_and_existing_output_before_post(cli, tmp_path):
    run, merchant = cli
    source = tmp_path / "invalid.txt"
    source.write_bytes(b"\xff")
    with pytest.raises(ManageError, match="UTF-8"):
        run("cards", "import-text", "--product", "product-a", "--file", str(source))
    with pytest.raises(ManageError, match="2 MiB"):
        run(
            "cards",
            "import-text",
            "--product",
            "product-a",
            "--stdin",
            stdin="中" * 700000,
        )
    existing = tmp_path / "exists.json"
    existing.write_text("keep")
    with pytest.raises(ManageError, match="already exists"):
        run(
            "cards",
            "import-text",
            "--product",
            "product-a",
            "--stdin",
            "--output",
            str(existing),
            stdin="private text",
        )
    assert merchant.imports == [] and existing.read_text() == "keep"


def test_import_text_accepts_payload_over_old_json_loader_cap(cli):
    run, merchant = cli
    result = run(
        "cards", "import-text", "--product", "product-a", "--stdin", stdin="x" * 300000
    )
    assert result["ok"] and len(merchant.imports[0]["text"]) == 300000


def test_actual_cli_imports_text_mapping_without_consuming_existing_cards(
    owner, tmp_path, monkeypatch
):
    from fastapi.testclient import TestClient

    from extore.app import app
    from extore.db import db

    response = owner.post(
        "/api/admin/products",
        json={"name": "Isolated stocked text", "mode": "stock", "parameters": []},
    )
    assert response.status_code == 200, response.text
    pid = response.json()["id"]
    response = owner.post(
        "/api/admin/staff",
        json={
            "product_id": pid,
            "name": "Synthetic stock manager",
            "permissions": ["cards.manage"],
        },
    )
    assert response.status_code == 200, response.text
    profile = tmp_path / "profile.json"
    with TestClient(app, base_url="http://localhost:8000") as api:

        def actual(request):
            result = api.request(
                request.method,
                str(request.url),
                content=request.read(),
                headers=dict(request.headers),
            )
            return httpx.Response(
                result.status_code, content=result.content, headers=dict(result.headers)
            )

        transport = httpx.MockTransport(actual)

        def command(*argv):
            return remote.execute(
                arguments(*argv, profile=profile), transport=transport
            )

        monkeypatch.setattr(sys, "stdin", io.StringIO(response.json()["url"]))
        assert command("login", "--link-stdin")["ok"]
        source = tmp_path / "stock.txt"
        source.write_bytes("\ufeff alpha \r\n\r\nalpha\r\nβeta\r\n".encode())
        saved = tmp_path / "mapping.json"
        result = command(
            "cards",
            "import-text",
            "--product",
            pid,
            "--file",
            str(source),
            "--output",
            str(saved),
        )
        assert result["count"] == 2 and result["stats"]["duplicates"] == 1
        assert stat.S_IMODE(saved.stat().st_mode) == 0o600
        mapping = json.loads(saved.read_text())
        assert [item["content"] for item in mapping["items"]] == ["alpha", "βeta"]
        assert all(item["code"] not in json.dumps(result) for item in mapping["items"])
        # A valid, line-bounded import exceeds the old 256k JSON transport cap.
        bulk = tmp_path / "bulk.txt"
        bulk.write_text("\n".join(f"{i}:" + "x" * 9000 for i in range(40)))
        second = command("cards", "import-text", "--product", pid, "--file", str(bulk))
        assert second["count"] == 40
    with db() as c:
        assert (
            c.execute(
                "SELECT count(*) FROM cards WHERE product_id=? AND state='ready'",
                (pid,),
            ).fetchone()[0]
            == 42
        )
        assert (
            c.execute(
                "SELECT count(*) FROM jobs WHERE product_id=?", (pid,)
            ).fetchone()[0]
            == 0
        )
