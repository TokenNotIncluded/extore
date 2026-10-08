"""Commerce CLI uses synthetic transports and private, disposable input files."""

import argparse
import io
import json
import socket
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from urllib.parse import urlencode

import httpx
import pytest

from extore import cli
from extore import commerce_client as sdk
from extore import commerce_commands as commands
from extore.http_proxy import ProxySettings

ISSUER = "https://extore.example.test"
CLIENT = "synthetic-commerce-cli"
REDIRECT = "https://shop.example.test/extore/callback?tenant=synthetic"
ACCESS = "SYNTHETIC-PRIVATE-ACCESS-0000000000"
REFRESH = "SYNTHETIC-PRIVATE-REFRESH-000000000"
NEXT_REFRESH = "SYNTHETIC-PRIVATE-NEXT-REFRESH-0000"
CODE = "SYNTHETIC-PRIVATE-CALLBACK-CODE-000"
CARD = "SYNTHETIC-PRIVATE-CARD-00000000000"
REVISION = "a" * 64


@pytest.fixture(autouse=True)
def forbid_external_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Commerce CLI tests must use the in-process transport")

    monkeypatch.setattr(socket, "create_connection", forbidden)


def private_file(path, content):
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)
    return path


def private_json(path, value):
    return private_file(path, json.dumps(value))


def token_document(**changes):
    return {
        "schema": "extore.commerce-token.v1",
        **asdict(
            sdk.TokenSet(
                ISSUER,
                CLIENT,
                "synthetic-grant",
                ("cards.issue", "products.read"),
                900,
                2000000000,
                ACCESS,
                REFRESH,
            )
        ),
        **changes,
    }


def authorization_document():
    def forbidden(request):
        raise AssertionError("Authorization preparation must not make HTTP requests")

    with sdk.CommerceClient(
        ISSUER,
        CLIENT,
        proxy_settings=ProxySettings(),
        transport=httpx.MockTransport(forbidden),
    ) as client:
        transaction = client.authorize(
            REDIRECT, scopes=("products.read", "cards.issue")
        )
    return {
        "schema": "extore.commerce-authorization.v1",
        "transaction": asdict(transaction),
        "authorization_url": transaction.url,
    }


def callback(document):
    return (
        REDIRECT.split("?", 1)[0]
        + "?"
        + urlencode(
            {
                "tenant": "synthetic",
                "state": document["transaction"]["state"],
                "iss": ISSUER,
                "code": CODE,
            }
        )
    )


def request_document(**changes):
    return {
        "product_id": "synthetic-product",
        "variant_id": "standard",
        "count": 1,
        "idempotency_key": "synthetic-order-00001",
        "expected_revision": REVISION,
        **changes,
    }


def listing():
    return {
        "schema": "extore.product-listing.v1",
        "id": "synthetic-product",
        "shop_id": "synthetic-shop",
        "revision": REVISION,
        "redemption_url": ISSUER + "/",
        "semantics": {
            "price": "reference",
            "inventory": "not_exported",
            "payment": "external_sales_platform",
            "redemption": "extore",
        },
        "product": {"id": "synthetic-product", "name": "Synthetic product"},
        "variants": [{"id": "standard", "price": "25.00", "attributes": {}}],
    }


def response_for(request):
    path = request.url.path
    if path == "/.well-known/oauth-authorization-server":
        return {
            "issuer": ISSUER,
            "authorization_endpoint": ISSUER + "/oauth/authorize",
            "token_endpoint": ISSUER + "/api/integrations/commerce/token",
            "revocation_endpoint": ISSUER + "/api/integrations/commerce/revoke",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": ["products.read", "cards.issue"],
            "authorization_response_iss_parameter_supported": True,
        }
    if path == "/api/integrations/commerce/token":
        return {
            "token_type": "Bearer",
            "access_token": ACCESS,
            "refresh_token": NEXT_REFRESH,
            "scope": "cards.issue products.read",
            "expires_in": 900,
            "grant_id": "synthetic-grant",
            "grant_expires": 2000000000,
        }
    if path == "/api/integrations/commerce/revoke":
        return None
    if path == "/api/integrations/commerce/products":
        return {
            "schema": "extore.commerce-catalog.v1",
            "issuer": ISSUER,
            "grant_id": "synthetic-grant",
            "shop": {"id": "synthetic-shop", "name": "Synthetic shop"},
            "products": [listing()],
        }
    if path == "/api/integrations/commerce/products/synthetic-product":
        return listing()
    if path == "/api/integrations/commerce/cards":
        return {
            "schema": "extore.card-batch.v1",
            "grant_id": "synthetic-grant",
            "product_id": "synthetic-product",
            "variant_id": "standard",
            "batch_id": "synthetic-batch",
            "count": 1,
            "codes": [CARD],
            "created_at": 1900000000,
            "recovery_expires": 1900000300,
            "quota": {"max_count": 10, "issued_count": 1, "remaining": 9},
            "variant": {
                "id": "standard",
                "name": "Standard",
                "price": "25.00",
                "attributes": {},
            },
        }
    raise AssertionError("Unexpected synthetic request")


def arguments(tmp_path, operation, *options, output_name="output.json"):
    parser = argparse.ArgumentParser(prog="extore")
    parents = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(parents)
    return parser.parse_args(
        [
            "commerce",
            operation,
            "--origin",
            ISSUER,
            "--client-id",
            CLIENT,
            "--output",
            str(tmp_path / output_name),
            *map(str, options),
        ]
    )


@pytest.fixture
def transport(monkeypatch):
    requests = []
    constructions = []
    clients = []
    custom_handler = [None]

    def handler(request):
        requests.append(request)
        if custom_handler[0] is not None:
            return custom_handler[0](request)
        value = response_for(request)
        return httpx.Response(204) if value is None else httpx.Response(200, json=value)

    def create(issuer, client_id, *, proxy_settings=None, **kwargs):
        constructions.append((issuer, client_id, proxy_settings))
        client = sdk.CommerceClient(
            issuer,
            client_id,
            # Capture the forwarded policy, but a configured proxy would replace
            # HTTPX's test transport. The synthetic peer always stays in process.
            proxy_settings=ProxySettings(),
            transport=httpx.MockTransport(handler),
        )
        clients.append(client)
        return client

    monkeypatch.setattr(commands, "CommerceClient", create)
    yield requests, constructions, custom_handler
    for client in clients:
        client.close()


def safe_status(capsys, operation, output):
    captured = capsys.readouterr()
    assert captured.err == ""
    status = json.loads(captured.out)
    assert set(status) <= {
        "ok",
        "operation",
        "output",
        "bytes",
        "count",
        "product_count",
        "previous_refresh_consumed",
    }
    assert status["ok"] is True
    assert status["operation"] == operation
    assert status["output"] == str(output)
    assert status["bytes"] == output.stat().st_size
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    for secret in (ACCESS, REFRESH, NEXT_REFRESH, CODE, CARD):
        assert secret not in captured.out
    return status, json.loads(output.read_text())


def failure(args, capsys, *, code=None, absent=True):
    with pytest.raises(SystemExit) as exited:
        commands.run(args)
    assert exited.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)
    assert set(error) == {"ok", "error", "message"}
    assert error["ok"] is False
    if code is not None:
        assert error["error"] == code
    for secret in (ACCESS, REFRESH, NEXT_REFRESH, CODE, CARD, "PRIVATE_SENTINEL"):
        assert secret not in captured.err
    if absent:
        assert not args.output.exists()
    return error


def test_global_help_and_dispatch_never_initialize_business_db(
    tmp_path, monkeypatch, transport, capsys
):
    def forbidden(*args, **kwargs):
        raise AssertionError("Commerce CLI must not initialize the local business DB")

    monkeypatch.setattr(cli, "init", forbidden)
    with pytest.raises(SystemExit) as exited:
        cli.main(["commerce", "--help"])
    assert exited.value.code == 0
    assert "authorize" in capsys.readouterr().out
    output = tmp_path / "metadata.json"
    assert (
        cli.main(
            [
                "commerce",
                "metadata",
                "--origin",
                ISSUER,
                "--client-id",
                CLIENT,
                "--output",
                str(output),
            ]
        )
        == 0
    )
    _, value = safe_status(capsys, "metadata", output)
    assert value["issuer"] == ISSUER


@pytest.mark.parametrize("operation", ["metadata", "authorize", "revoke"])
def test_private_output_is_required_even_for_nonsecret_and_empty_results(
    operation, capsys
):
    parser = argparse.ArgumentParser()
    commands.add_parser(parser.add_subparsers(dest="command", required=True))
    with pytest.raises(SystemExit) as exited:
        parser.parse_args(
            ["commerce", operation, "--origin", ISSUER, "--client-id", CLIENT]
        )
    assert exited.value.code == 2
    assert "--output" in capsys.readouterr().err


def test_authorization_secrets_only_enter_private_export(tmp_path, transport, capsys):
    args = arguments(tmp_path, "authorize", "--redirect-uri", REDIRECT)
    assert commands.run(args) == 0
    _, value = safe_status(capsys, "authorize", args.output)
    assert value["schema"] == "extore.commerce-authorization.v1"
    transaction = value["transaction"]
    assert transaction["scopes"] == ["products.read"]
    assert transaction["_consumed"] is False
    assert len(transaction["code_verifier"]) >= 43
    assert value["authorization_url"] == transaction["url"]
    assert transport[0] == []


def test_authorization_scope_and_selected_products_reach_the_sdk(
    tmp_path, transport, capsys
):
    args = arguments(
        tmp_path,
        "authorize",
        "--redirect-uri",
        REDIRECT,
        "--scope",
        "cards.issue",
        "--scope",
        "products.read",
        "--product-id",
        "synthetic-product",
    )
    assert commands.run(args) == 0
    _, value = safe_status(capsys, "authorize", args.output)
    assert value["transaction"]["scopes"] == ["cards.issue", "products.read"]
    assert "product_ids=synthetic-product" in value["authorization_url"]
    assert transport[0] == []


@pytest.mark.parametrize("kind", ["existing", "symlink", "missing-parent"])
def test_unsafe_output_is_rejected_before_client_construction(
    tmp_path, transport, capsys, kind
):
    args = arguments(tmp_path, "metadata")
    if kind == "existing":
        args.output.write_text("PRIVATE_SENTINEL")
    elif kind == "symlink":
        target = tmp_path / "private-target"
        target.write_text("PRIVATE_SENTINEL")
        args.output.symlink_to(target)
    else:
        args.output = tmp_path / "PRIVATE_SENTINEL" / "output.json"
    failure(args, capsys, absent=False)
    assert transport[1] == []
    if kind != "missing-parent":
        assert args.output.read_text() == "PRIVATE_SENTINEL"


@pytest.mark.parametrize("source", ["token", "authorization", "request"])
def test_world_readable_input_is_rejected_before_client_construction(
    tmp_path, transport, capsys, source
):
    path = tmp_path / "PRIVATE_SENTINEL.json"
    if source == "token":
        private_json(path, token_document())
        args = arguments(tmp_path, "products", "--token-file", path)
    elif source == "authorization":
        document = authorization_document()
        private_json(path, document)
        cb = private_file(tmp_path / "callback.txt", callback(document))
        args = arguments(
            tmp_path, "token", "--authorization-file", path, "--callback-file", cb
        )
    else:
        token = private_json(tmp_path / "token.json", token_document())
        private_json(path, request_document())
        args = arguments(
            tmp_path, "cards", "--token-file", token, "--request-file", path
        )
    path.chmod(0o644)
    failure(args, capsys)
    assert transport[1] == []


def test_input_symlinks_are_not_followed(tmp_path, transport, capsys):
    target = private_json(tmp_path / "target.json", token_document())
    link = tmp_path / "PRIVATE_SENTINEL-link.json"
    link.symlink_to(target)
    failure(arguments(tmp_path, "products", "--token-file", link), capsys)
    assert transport[1] == []
    assert json.loads(target.read_text())["_refresh_consumed"] is False


def test_input_hardlinks_cannot_be_used_as_independent_credentials(
    tmp_path, transport, capsys
):
    target = private_json(tmp_path / "target.json", token_document())
    alias = tmp_path / "token-alias.json"
    alias.hardlink_to(target)
    failure(arguments(tmp_path, "products", "--token-file", alias), capsys)
    assert transport[1] == []
    assert target.read_bytes() == alias.read_bytes()


@pytest.mark.parametrize(
    "change",
    [
        {"issuer": "https://other.example.test"},
        {"client_id": "different-client"},
        {"private_unknown": "PRIVATE_SENTINEL"},
    ],
)
def test_token_binding_and_exact_schema_are_checked_before_network(
    tmp_path, transport, capsys, change
):
    path = private_json(tmp_path / "token.json", token_document(**change))
    failure(arguments(tmp_path, "products", "--token-file", path), capsys)
    assert transport[1] == []


def test_duplicate_json_keys_are_rejected_before_client_construction(
    tmp_path, transport, capsys
):
    value = json.dumps(token_document())
    path = private_file(
        tmp_path / "token.json", '{"access_token":"PRIVATE_SENTINEL",' + value[1:]
    )
    failure(arguments(tmp_path, "products", "--token-file", path), capsys)
    assert transport[1] == []


def test_token_exchange_consumes_durable_state_before_request(
    tmp_path, transport, capsys
):
    document = authorization_document()
    auth = private_json(tmp_path / "authorization.json", document)
    cb = private_file(tmp_path / "callback.txt", callback(document) + "\n")
    args = arguments(
        tmp_path, "token", "--authorization-file", auth, "--callback-file", cb
    )

    def handler(request):
        assert json.loads(auth.read_text())["transaction"]["_consumed"] is True
        assert stat.S_IMODE(auth.stat().st_mode) == 0o600
        assert args.output.exists() and args.output.stat().st_size == 0
        assert CODE.encode() in request.read()
        return httpx.Response(200, json=response_for(request))

    transport[2][0] = handler
    assert commands.run(args) == 0
    _, value = safe_status(capsys, "token", args.output)
    assert value["schema"] == "extore.commerce-token.v1"
    assert value["access_token"] == ACCESS
    assert value["refresh_token"] == NEXT_REFRESH
    assert value["_refresh_consumed"] is False
    assert document["transaction"]["code_verifier"] not in json.dumps(value)


def test_lost_token_response_cannot_replay_consumed_state(tmp_path, transport, capsys):
    document = authorization_document()
    auth = private_json(tmp_path / "authorization.json", document)
    cb = private_file(tmp_path / "callback.txt", callback(document))
    args = arguments(
        tmp_path, "token", "--authorization-file", auth, "--callback-file", cb
    )

    def lost(request):
        assert json.loads(auth.read_text())["transaction"]["_consumed"] is True
        raise httpx.ReadError("PRIVATE_SENTINEL " + CODE, request=request)

    transport[2][0] = lost
    failure(args, capsys)
    assert len(transport[0]) == 1
    failure(args, capsys)
    assert len(transport[0]) == 1


def test_callback_stdin_is_supported_without_argument_secret(
    tmp_path, monkeypatch, transport, capsys
):
    document = authorization_document()
    auth = private_json(tmp_path / "authorization.json", document)
    monkeypatch.setattr("sys.stdin", io.StringIO(callback(document) + "\n"))
    args = arguments(
        tmp_path, "token", "--authorization-file", auth, "--callback-stdin"
    )
    assert commands.run(args) == 0
    _, value = safe_status(capsys, "token", args.output)
    assert value["refresh_token"] == NEXT_REFRESH


def test_multiline_callback_fails_before_client_construction(
    tmp_path, transport, capsys
):
    document = authorization_document()
    auth = private_json(tmp_path / "authorization.json", document)
    cb = private_file(
        tmp_path / "callback.txt", callback(document) + "\nPRIVATE_SENTINEL\n"
    )
    failure(
        arguments(
            tmp_path, "token", "--authorization-file", auth, "--callback-file", cb
        ),
        capsys,
    )
    assert transport[1] == []
    assert json.loads(auth.read_text())["transaction"]["_consumed"] is False


def test_callback_with_wrong_state_does_not_consume_valid_authorization(
    tmp_path, transport, capsys
):
    document = authorization_document()
    auth = private_json(tmp_path / "authorization.json", document)
    cb = private_file(
        tmp_path / "callback.txt",
        callback(document).replace(
            document["transaction"]["state"], "PRIVATE_SENTINEL-wrong-state"
        ),
    )
    failure(
        arguments(
            tmp_path, "token", "--authorization-file", auth, "--callback-file", cb
        ),
        capsys,
    )
    assert transport[0] == []
    assert json.loads(auth.read_text())["transaction"]["_consumed"] is False


def test_valid_authorization_denial_is_durably_consumed_without_exchange(
    tmp_path, transport, capsys
):
    document = authorization_document()
    auth = private_json(tmp_path / "authorization.json", document)
    denied = (
        REDIRECT.split("?", 1)[0]
        + "?"
        + urlencode(
            {
                "tenant": "synthetic",
                "state": document["transaction"]["state"],
                "iss": ISSUER,
                "error": "access_denied",
                "error_description": "PRIVATE_SENTINEL",
            }
        )
    )
    cb = private_file(tmp_path / "callback.txt", denied)
    failure(
        arguments(
            tmp_path, "token", "--authorization-file", auth, "--callback-file", cb
        ),
        capsys,
        code="access_denied",
    )
    assert transport[0] == []
    assert json.loads(auth.read_text())["transaction"]["_consumed"] is True


def test_refresh_rotates_once_with_durable_marker_before_request(
    tmp_path, transport, capsys
):
    path = private_json(tmp_path / "token.json", token_document())
    args = arguments(tmp_path, "refresh", "--token-file", path)

    def handler(request):
        assert json.loads(path.read_text())["_refresh_consumed"] is True
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert args.output.exists() and args.output.stat().st_size == 0
        assert REFRESH.encode() in request.read()
        return httpx.Response(200, json=response_for(request))

    transport[2][0] = handler
    assert commands.run(args) == 0
    _, value = safe_status(capsys, "refresh", args.output)
    assert value["refresh_token"] == NEXT_REFRESH
    assert value["_refresh_consumed"] is False
    retry = arguments(
        tmp_path, "refresh", "--token-file", path, output_name="second.json"
    )
    failure(retry, capsys)
    assert len(transport[0]) == 1


def test_refresh_stdin_is_rejected_before_client_construction(
    tmp_path, monkeypatch, transport, capsys
):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(token_document())))
    with pytest.raises(SystemExit) as exited:
        arguments(tmp_path, "refresh", "--token-stdin")
    assert exited.value.code == 2
    captured = capsys.readouterr()
    assert "--token-file" in captured.err
    assert ACCESS not in captured.out + captured.err
    assert REFRESH not in captured.out + captured.err
    assert transport[1] == []


def test_lost_refresh_response_does_not_retry_or_restore_old_pair(
    tmp_path, transport, capsys
):
    path = private_json(tmp_path / "token.json", token_document())

    def lost(request):
        raise httpx.ReadError("PRIVATE_SENTINEL " + REFRESH, request=request)

    transport[2][0] = lost
    args = arguments(tmp_path, "refresh", "--token-file", path)
    failure(args, capsys)
    assert json.loads(path.read_text())["_refresh_consumed"] is True
    failure(args, capsys)
    assert len(transport[0]) == 1


def test_concurrent_refresh_has_nonblocking_private_lock_and_one_request(
    tmp_path, transport, capsys
):
    path = private_json(tmp_path / "token.json", token_document())
    entered, release = threading.Event(), threading.Event()
    first = arguments(tmp_path, "refresh", "--token-file", path)
    second = arguments(
        tmp_path, "refresh", "--token-file", path, output_name="second.json"
    )

    def paused(request):
        entered.set()
        assert release.wait(5), "Synthetic concurrent request was not released"
        return httpx.Response(200, json=response_for(request))

    transport[2][0] = paused
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(commands.run, first)
        try:
            assert entered.wait(5)
            failure(second, capsys, code="input_busy")
            lock = path.with_name(path.name + ".commerce.lock")
            assert stat.S_IMODE(lock.stat().st_mode) == 0o600
            assert len(transport[0]) == 1
        finally:
            release.set()
        assert future.result(timeout=5) == 0
    safe_status(capsys, "refresh", first.output)


def test_refresh_refuses_preexisting_symlinked_lock_without_consumption(
    tmp_path, transport, capsys
):
    path = private_json(tmp_path / "token.json", token_document())
    target = private_file(tmp_path / "PRIVATE_SENTINEL", "original")
    path.with_name(path.name + ".commerce.lock").symlink_to(target)
    failure(arguments(tmp_path, "refresh", "--token-file", path), capsys)
    assert transport[1] == []
    assert target.read_text() == "original"
    assert json.loads(path.read_text())["_refresh_consumed"] is False


@pytest.mark.parametrize("operation", ["products", "product", "revoke"])
def test_token_stdin_supports_read_and_revoke_with_private_output(
    tmp_path, monkeypatch, transport, capsys, operation
):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(token_document())))
    extra = ("--product-id", "synthetic-product") if operation == "product" else ()
    args = arguments(tmp_path, operation, "--token-stdin", *extra)
    assert commands.run(args) == 0
    _, value = safe_status(capsys, operation, args.output)
    assert len(transport[0]) == 1
    if operation == "products":
        assert value["products"][0]["variants"][0]["price"] == "25.00"
    elif operation == "product":
        assert value["schema"] == "extore.product-listing.v1"
    else:
        assert value["ok"] is True


def test_cards_request_file_preserves_idempotency_header_and_revision(
    tmp_path, transport, capsys
):
    token = private_json(tmp_path / "token.json", token_document())
    body = private_json(tmp_path / "request.json", request_document())
    args = arguments(tmp_path, "cards", "--token-file", token, "--request-file", body)
    assert commands.run(args) == 0
    status, value = safe_status(capsys, "cards", args.output)
    assert status["count"] == 1
    assert value["codes"] == [CARD]
    request = transport[0][0]
    assert request.headers["Idempotency-Key"] == "synthetic-order-00001"
    sent = json.loads(request.read())
    assert "idempotency_key" not in sent
    assert sent["expected_revision"] == REVISION
    assert json.loads(token.read_text())["_refresh_consumed"] is False


def test_cards_request_stdin_uses_file_tokens_without_echoing_codes(
    tmp_path, monkeypatch, transport, capsys
):
    token = private_json(tmp_path / "token.json", token_document())
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(request_document())))
    args = arguments(tmp_path, "cards", "--token-file", token, "--request-stdin")
    assert commands.run(args) == 0
    _, value = safe_status(capsys, "cards", args.output)
    assert value["codes"] == [CARD]


@pytest.mark.parametrize(
    "change",
    [
        {"count": True},
        {"count": 101},
        {"expected_revision": "not-a-revision"},
        {"idempotency_key": "PRIVATE_SENTINEL\n"},
        {"unexpected": "PRIVATE_SENTINEL"},
    ],
)
def test_invalid_issuance_body_never_constructs_client_or_issues_cards(
    tmp_path, transport, capsys, change
):
    token = private_json(tmp_path / "token.json", token_document())
    request = private_json(tmp_path / "request.json", request_document(**change))
    failure(
        arguments(tmp_path, "cards", "--token-file", token, "--request-file", request),
        capsys,
    )
    assert transport[1] == []
    assert json.loads(token.read_text())["_refresh_consumed"] is False


def test_only_one_stdin_source_is_allowed(tmp_path, transport, capsys):
    failure(arguments(tmp_path, "cards", "--token-stdin", "--request-stdin"), capsys)
    assert transport[1] == []


def test_oversized_stdin_is_rejected_without_credentials_or_client(
    tmp_path, monkeypatch, transport, capsys
):
    monkeypatch.setattr(
        "sys.stdin", io.StringIO("PRIVATE_SENTINEL" + "a" * commands.INPUT_LIMIT)
    )
    failure(arguments(tmp_path, "products", "--token-stdin"), capsys)
    assert transport[1] == []


def test_proxy_is_explicit_and_never_persisted_or_echoed(
    tmp_path, monkeypatch, transport, capsys
):
    monkeypatch.setenv("HTTPS_PROXY", "http://ignored.example.test:8080")
    monkeypatch.delenv("EXTORE_PROXY", raising=False)
    direct = arguments(tmp_path, "metadata")
    assert commands.run(direct) == 0
    safe_status(capsys, "metadata", direct.output)
    assert transport[1][0][2].mode == "direct"
    explicit = arguments(
        tmp_path,
        "metadata",
        "--proxy",
        "http://user:PRIVATE_SENTINEL@proxy.example.test:8080",
        output_name="proxy.json",
    )
    assert commands.run(explicit) == 0
    _, value = safe_status(capsys, "metadata", explicit.output)
    assert transport[1][-1][2].mode == "explicit"
    assert "PRIVATE_SENTINEL" not in json.dumps(value)


def test_untrusted_server_error_and_transport_details_are_sanitized(
    tmp_path, transport, capsys
):
    token = private_json(tmp_path / "token.json", token_document())
    transport[2][0] = lambda request: httpx.Response(
        401,
        json={
            "error": "invalid_token",
            "error_description": "PRIVATE_SENTINEL " + ACCESS,
        },
    )
    error = failure(arguments(tmp_path, "products", "--token-file", token), capsys)
    assert error["error"] == "invalid_token"
    assert json.loads(token.read_text())["_refresh_consumed"] is False


def test_failed_token_export_keeps_consumption_marker_and_removes_empty_output(
    tmp_path, monkeypatch, transport, capsys
):
    path = private_json(tmp_path / "token.json", token_document())

    def broken_write(self, value):
        raise OSError("PRIVATE_SENTINEL " + NEXT_REFRESH)

    monkeypatch.setattr(commands.PrivateOutput, "write", broken_write)
    failure(arguments(tmp_path, "refresh", "--token-file", path), capsys)
    assert json.loads(path.read_text())["_refresh_consumed"] is True
    assert len(transport[0]) == 1
