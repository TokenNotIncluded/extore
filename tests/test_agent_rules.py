from pathlib import Path

from extore.db import db


def test_agent_rules_are_public_markdown_and_reading_creates_no_authorization(client):
    client.headers.pop("origin", None)
    response = client.get("/AGENTS.md")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/markdown; charset=utf-8"
    assert (
        response.content
        == (Path(__file__).parents[1] / "extore" / "static" / "AGENTS.md").read_bytes()
    )
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers
    assert "<html" not in response.text
    with db() as c:
        for table in (
            "cli_scope_requests",
            "cli_device_requests",
            "owner_cli_requests",
        ):
            assert c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_agent_rules_support_head_and_are_not_an_spa_fallback(client):
    response = client.head("/AGENTS.md")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/markdown; charset=utf-8"
    assert response.content == b""
    assert (
        response.headers["content-length"]
        == client.get("/AGENTS.md").headers["content-length"]
    )
    assert client.get("/agents.md").status_code == 404
    assert client.post("/AGENTS.md").status_code == 405
