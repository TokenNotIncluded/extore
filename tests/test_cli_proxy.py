import argparse
import logging

import pytest

from extore import (
    commerce_client,
    commerce_commands,
    customer_cli,
    manage_client,
    owner_client,
)
from extore.http_proxy import (
    ProxySettings,
    make_client,
    resolve_proxy,
    validate_proxy,
)
from extore.manage_client import ManageError


def parser():
    result = argparse.ArgumentParser()
    commands = result.add_subparsers(dest="command", required=True)
    for module in (manage_client, owner_client, customer_cli):
        module.add_parser(commands)
    commerce_commands.add_parser(commands)
    return result


@pytest.mark.parametrize(
    "arguments",
    [
        ["manage", "--proxy", "http://proxy.example:8080", "login"],
        ["manage", "login", "--proxy", "http://proxy.example:8080"],
        ["admin", "--proxy-env", "login", "--origin", "https://store.example"],
        ["admin", "login", "--origin", "https://store.example", "--proxy-env"],
        ["customer", "--proxy-env", "products", "--origin", "https://store.example"],
        ["customer", "products", "--origin", "https://store.example", "--proxy-env"],
    ],
)
def test_proxy_flags_at_both_positions(arguments):
    args = parser().parse_args(arguments)
    settings = resolve_proxy(args, environ={})
    assert settings.mode == ("explicit" if "--proxy" in arguments else "environment")


def test_direct_default_and_explicit_environment_priority():
    environment = {
        "HTTP_PROXY": "http://ignored.example:8080",
        "HTTPS_PROXY": "http://ignored.example:8080",
        "SSL_CERT_FILE": "/untrusted.pem",
    }
    assert resolve_proxy(environ=environment) == ProxySettings()
    environment["EXTORE_PROXY"] = "socks5h://user:private@proxy.example:1080"
    assert resolve_proxy(environ=environment).url == environment["EXTORE_PROXY"]
    args = argparse.Namespace(proxy="https://explicit.example:443")
    assert resolve_proxy(args, environ=environment).url == args.proxy
    args = argparse.Namespace(proxy_env=True)
    assert resolve_proxy(args, environ=environment).mode == "environment"
    assert "private" not in repr(resolve_proxy(environ=environment))
    assert "proxy.example" not in repr(resolve_proxy(environ=environment))


def test_proxy_env_is_only_proxy_variables_and_preserves_bypass():
    result = resolve_proxy(
        argparse.Namespace(proxy_env=True),
        environ={
            "HTTP_PROXY": "upper.example:8080",
            "http_proxy": "lower.example:8080",
            "HTTPS_PROXY": "socks5://proxy.example:1080",
            "ALL_PROXY": "http://fallback.example:3128",
            "NO_PROXY": "localhost,127.0.0.1,.example.org,https://exact.example:8443",
        },
    )
    assert dict(result.routes) == {
        "http://": "http://lower.example:8080",
        "https://": "socks5://proxy.example:1080",
        "all://": "http://fallback.example:3128",
        "all://localhost": None,
        "all://127.0.0.1": None,
        "all://*.example.org": None,
        "https://exact.example:8443": None,
    }
    assert (
        resolve_proxy(
            argparse.Namespace(proxy_env=True), environ={"NO_PROXY": "*"}
        ).routes
        == ()
    )


@pytest.mark.parametrize("module", [manage_client, owner_client, customer_cli])
def test_malformed_proxy_before_any_private_profile(module, tmp_path, monkeypatch):
    monkeypatch.setenv("EXTORE_PROXY", "https://user:very-secret@proxy.example/bad")
    profile = tmp_path / "not-created" / "profile.json"
    args = argparse.Namespace(profile=profile)
    with pytest.raises(ManageError) as caught:
        module.execute(args)
    assert caught.value.code == "invalid_proxy"
    assert "very-secret" not in str(caught.value)
    assert not profile.parent.exists()


def test_cross_level_modes_rejected_locally():
    args = parser().parse_args(
        ["manage", "--proxy-env", "login", "--proxy", "http://proxy.example:3128"]
    )
    with pytest.raises(ManageError, match="^Invalid CLI proxy configuration$"):
        resolve_proxy(args, environ={})


@pytest.mark.parametrize("scheme", ["http", "https", "socks5", "socks5h"])
def test_supported_schemes(scheme):
    value = f"{scheme}://user:private@proxy.example:3128"
    assert validate_proxy(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "ftp://user:private@proxy.example",
        "http://user:private@proxy.example:70000",
        "http://user:private@proxy.example/path",
        "http://user:private@proxy.example?credential=private",
        "http://user:private@proxy.example#private",
    ],
)
def test_malformed_argument_errors_do_not_echo_proxy_credentials(value, capsys):
    with pytest.raises(SystemExit):
        parser().parse_args(["manage", "login", "--proxy", value])
    output = capsys.readouterr().err
    assert "private" not in output
    assert value not in output


def test_invalid_no_proxy_still_rejected_after_wildcard():
    with pytest.raises(ManageError):
        resolve_proxy(
            argparse.Namespace(proxy_env=True),
            environ={"NO_PROXY": "*,user:private@host"},
        )


def test_all_clients_enforce_tls_and_no_redirects_and_never_persist_proxy(monkeypatch):
    configurations = []

    class CapturedClient:
        def __init__(self, **kwargs):
            configurations.append(kwargs)

        def close(self):
            pass

    monkeypatch.setattr("extore.http_proxy.httpx.Client", CapturedClient)
    settings = ProxySettings("explicit", "http://user:private@proxy.example:8080")
    data = {"grants": []}
    clients = [
        manage_client.ManageClient(data, proxy_settings=settings),
        owner_client.OwnerClient(data, proxy_settings=settings),
        customer_cli.CustomerClient(data, proxy_settings=settings),
        commerce_client.CommerceClient(
            "https://store.example", "test-client", proxy_settings=settings
        ),
    ]
    for configuration in configurations:
        assert configuration["proxy"] == settings.url
        assert configuration["verify"] is True
        assert configuration["trust_env"] is False
        assert configuration["follow_redirects"] is False
    assert "proxy" not in data
    assert "private" not in str(data)
    for client in clients:
        client.http.close()


def test_environment_mounts_keep_tls_and_environment_certificates_disabled(monkeypatch):
    captured = {}
    transports = []

    class CapturedTransport:
        def __init__(self, **kwargs):
            transports.append(kwargs)

    monkeypatch.setattr("extore.http_proxy.httpx.HTTPTransport", CapturedTransport)
    monkeypatch.setattr(
        "extore.http_proxy.httpx.Client", lambda **kwargs: captured.update(kwargs)
    )
    settings = resolve_proxy(
        argparse.Namespace(proxy_env=True),
        environ={
            "HTTPS_PROXY": "http://user:private@proxy.example:3128",
            "NO_PROXY": "localhost",
        },
    )
    make_client(settings)
    assert captured["verify"] is True
    assert captured["trust_env"] is False
    assert captured["follow_redirects"] is False
    assert captured["mounts"]["all://localhost"] is None
    assert transports == [
        {
            "proxy": "http://user:private@proxy.example:3128",
            "verify": True,
            "trust_env": False,
        }
    ]


def test_socks_debug_trace_does_not_log_auth_and_keeps_ordinary_logs(
    monkeypatch, caplog
):
    logger = logging.getLogger("httpcore.socks")
    monkeypatch.setattr(logger, "filters", [])
    monkeypatch.setattr("extore.http_proxy.httpx.Client", lambda **kwargs: object())
    make_client(
        ProxySettings("explicit", "socks5h://user:synthetic-secret@proxy.example:1080")
    )
    with caplog.at_level(logging.DEBUG, logger="httpcore.socks"):
        logger.debug(
            "setup_socks5_connection.started auth=(b'user', b'synthetic-secret')"
        )
        logger.debug("setup_socks5_connection.complete")
        logger.debug("connect_tcp.started host='proxy.example' port=1080")
    assert "synthetic-secret" not in caplog.text
    assert "setup_socks5_connection.complete" in caplog.text
    assert "connect_tcp.started" in caplog.text


@pytest.mark.parametrize("role", ["manage", "admin", "customer"])
@pytest.mark.parametrize("before", [True, False])
def test_ca_bundle_flag_positions(role, before):
    command = [role]
    if before:
        command += ["--ca-bundle", "explicit.pem"]
    command += ["login"] if role != "customer" else ["products"]
    if role != "manage":
        command += ["--origin", "https://store.example"]
    if not before:
        command += ["--ca-bundle", "explicit.pem"]
    assert parser().parse_args(command).ca_bundle == "explicit.pem"


@pytest.mark.parametrize(
    "network,matching,nonmatching",
    [
        ("10.0.0.0/8", "10.12.34.56", "11.12.34.56"),
        ("192.168.1.42/24", "192.168.1.7", "192.168.2.7"),
        ("2001:db8::/32", "2001:db8::1234", "2001:db9::1234"),
    ],
)
def test_cidr_routes_match_only_literal_ips(network, matching, nonmatching):
    import httpx

    settings = resolve_proxy(
        argparse.Namespace(proxy_env=True),
        environ={"ALL_PROXY": "http://proxy.example:3128", "NO_PROXY": network},
    )
    with make_client(settings) as client:

        def route(host):
            host = f"[{host}]" if ":" in host else host
            return client._transport_for_url(httpx.URL(f"https://{host}/"))

        assert route(matching) is client._transport
        assert route(nonmatching) is not client._transport
        assert route("service.example") is not client._transport


@pytest.mark.parametrize(
    "value", ["10.0.0.0/99", "host.example/8", "user:secret@host/8", "2001:db8::/999"]
)
def test_invalid_cidr_redacted(value):
    with pytest.raises(ManageError) as caught:
        resolve_proxy(argparse.Namespace(proxy_env=True), environ={"NO_PROXY": value})
    assert caught.value.code == "invalid_proxy"
    assert value not in str(caught.value)
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("module", [manage_client, owner_client, customer_cli])
def test_invalid_ca_before_profile_and_redacted(module, tmp_path, monkeypatch):
    ca = tmp_path / "private-path-secret.pem"
    ca.write_text("invalid certificate")
    monkeypatch.setenv("EXTORE_CA_BUNDLE", str(ca))
    profile = tmp_path / "not-created" / "profile.json"
    with pytest.raises(ManageError) as caught:
        module.execute(argparse.Namespace(profile=profile))
    assert caught.value.code == "invalid_ca_bundle"
    assert str(ca) not in str(caught.value)
    assert "secret" not in str(caught.value)
    assert not profile.parent.exists()


def test_explicit_ca_context_preserves_verification_and_ignores_ambient(monkeypatch):
    import ssl

    import certifi

    monkeypatch.setenv("SSL_CERT_FILE", "/untrusted-ambient.pem")
    settings = resolve_proxy(
        argparse.Namespace(ca_bundle=certifi.where()),
        environ={"EXTORE_CA_BUNDLE": "/ignored.pem"},
    )
    assert settings.tls_context.verify_mode == ssl.CERT_REQUIRED
    assert settings.tls_context.check_hostname is True
    assert certifi.where() not in repr(settings)
    with make_client(settings) as client:
        assert client.follow_redirects is False


@pytest.mark.parametrize("scheme", ["https", "HTTPS"])
def test_ca_bundle_applies_to_https_proxy_and_target(monkeypatch, scheme):
    import certifi

    captured = {}
    transports = []

    class CapturedTransport:
        def __init__(self, **kwargs):
            transports.append(kwargs)

    monkeypatch.setattr("extore.http_proxy.httpx.HTTPTransport", CapturedTransport)
    monkeypatch.setattr(
        "extore.http_proxy.httpx.Client", lambda **kwargs: captured.update(kwargs)
    )
    settings = resolve_proxy(
        argparse.Namespace(proxy_env=True),
        environ={
            "EXTORE_CA_BUNDLE": certifi.where(),
            "HTTPS_PROXY": f"{scheme}://user:synthetic-secret@proxy.example:443",
        },
    )
    make_client(settings)
    assert captured["verify"] is settings.tls_context
    assert transports[0]["verify"] is settings.tls_context
    assert transports[0]["proxy"].ssl_context is settings.tls_context
    assert captured["trust_env"] is False
    assert "synthetic-secret" not in repr(settings)


@pytest.mark.parametrize("before", [True, False])
def test_commerce_ca_bundle_flag_positions(before):
    command = ["commerce"]
    if before:
        command += ["--ca-bundle", "explicit.pem"]
    command += [
        "metadata",
        "--origin",
        "https://store.example",
        "--client-id",
        "test-client",
        "--output",
        "output.json",
    ]
    if not before:
        command += ["--ca-bundle", "explicit.pem"]
    assert parser().parse_args(command).ca_bundle == "explicit.pem"
