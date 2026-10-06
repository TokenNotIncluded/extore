import httpx
import pytest

from extore import source


@pytest.fixture(autouse=True)
def reset_cache(monkeypatch):
    monkeypatch.setattr(source, "_stars", None)
    monkeypatch.setattr(source, "_refresh_after", 0)


def test_public_stars_are_cached_and_do_not_forward_credentials(client, monkeypatch):
    calls = []

    def github(url, **options):
        calls.append((url, options))
        return httpx.Response(
            200, json={"stargazers_count": 7}, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(source.httpx, "get", github)
    assert client.get("/api/source").json() == {"url": source.REPOSITORY, "stars": 7}
    assert client.get("/api/source").json()["stars"] == 7
    assert len(calls) == 1
    assert set(calls[0][1]["headers"]) == {"Accept", "User-Agent"}
    assert calls[0][0] == "https://api.github.com/repos/TokenNotIncluded/extore"


def test_upstream_failure_never_fabricates_a_star_count(client, monkeypatch):
    def unavailable(*args, **kwargs):
        raise httpx.ConnectError("unavailable")

    monkeypatch.setattr(source.httpx, "get", unavailable)
    assert client.get("/api/source").json()["stars"] is None
    monkeypatch.setattr(source, "_stars", 3)
    monkeypatch.setattr(source, "_refresh_after", 0)
    assert client.get("/api/source").json()["stars"] == 3
