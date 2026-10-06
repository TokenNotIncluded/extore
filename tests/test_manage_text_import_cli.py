import io
import json
import stat

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
