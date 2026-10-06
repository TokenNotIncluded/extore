import base64
import hashlib
import hmac
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from extore import totp

RFC_SECRET = base64.b32encode(b"12345678901234567890").decode("ascii")


@pytest.fixture(autouse=True)
def clean():
    # These pure cryptographic helpers neither need nor initialize a database.
    yield


@pytest.mark.parametrize(
    ("timestamp", "code"),
    [
        (59, "287082"),
        (1111111109, "081804"),
        (1111111111, "050471"),
        (1234567890, "005924"),
        (2000000000, "279037"),
        (20000000000, "353130"),
    ],
)
def test_rfc_6238_sha1_vectors_reduced_to_six_digits(timestamp, code):
    # RFC 6238 Appendix B uses 8 digits; reduction preserves the final 6.
    assert totp.match_counter(RFC_SECRET, code, timestamp) == timestamp // 30


def independent_code(counter):
    mac = hmac.digest(b"12345678901234567890", counter.to_bytes(8, "big"), "sha1")
    offset = mac[-1] & 15
    truncated = int.from_bytes(mac[offset : offset + 4], "big") & 0x7FFFFFFF
    return str(truncated % 1_000_000).zfill(6)


@pytest.mark.parametrize("offset", (-1, 0, 1))
def test_accepts_exactly_one_neighboring_step(offset):
    assert (
        totp.match_counter(RFC_SECRET, independent_code(100 + offset), 3000)
        == 100 + offset
    )


@pytest.mark.parametrize("offset", (-2, 2, -5, 5))
def test_rejects_codes_outside_window(offset):
    assert totp.match_counter(RFC_SECRET, independent_code(100 + offset), 3000) is None


def test_consumed_counter_and_older_counters_cannot_replay():
    code = independent_code(100)
    assert totp.match_counter(RFC_SECRET, code, 3000, 99) == 100
    assert totp.match_counter(RFC_SECRET, code, 3000, 100) is None
    assert totp.match_counter(RFC_SECRET, code, 3030, 100) is None
    assert totp.match_counter(RFC_SECRET, independent_code(99), 3000, 100) is None
    assert totp.match_counter(RFC_SECRET, independent_code(101), 3000, 100) == 101


def test_future_window_acceptance_blocks_older_code():
    assert totp.match_counter(RFC_SECRET, independent_code(101), 3000, 99) == 101
    assert totp.match_counter(RFC_SECRET, independent_code(100), 3000, 101) is None


def test_fractional_time_and_epoch_boundary_are_supported():
    assert totp.match_counter(RFC_SECRET, independent_code(0), 0, -1) == 0
    assert totp.match_counter(RFC_SECRET, independent_code(1), 29.999) == 1
    assert totp.match_counter(RFC_SECRET, independent_code(2), 59.999) == 2
    assert totp.match_counter(RFC_SECRET, independent_code(2), 60) == 2


@pytest.mark.parametrize(
    "code", [None, 287082, "", "12345", "1234567", "１２３４５６", "12 345", " 287082"]
)
def test_malformed_codes_are_rejected(code):
    assert totp.match_counter(RFC_SECRET, code, 59) is None


@pytest.mark.parametrize(
    "now", [None, "59", True, -1, float("nan"), float("inf"), float("-inf"), 10**1000]
)
def test_malformed_times_are_rejected(now):
    assert totp.match_counter(RFC_SECRET, "287082", now) is None


@pytest.mark.parametrize("last_counter", [True, "1", 1.0, -2])
def test_malformed_stored_counters_fail_closed(last_counter):
    assert totp.match_counter(RFC_SECRET, "287082", 59, last_counter) is None


@pytest.mark.parametrize(
    "secret",
    [
        None,
        b"12345678901234567890",
        "",
        "A" * 16,
        "A" * 32 + "=",
        "A" * 200,
        "!" * 32,
        "ı" * 32,
    ],
)
def test_malformed_or_short_secrets_fail_closed(secret):
    assert totp.match_counter(secret, "287082", 59) is None
    with pytest.raises(ValueError):
        totp.provisioning_uri(secret, "owner@example.com")


def test_all_window_candidates_use_timing_safe_comparison(monkeypatch):
    compared = []
    original = totp.hmac.compare_digest

    def compare(left, right):
        compared.append((left, right))
        return original(left, right)

    monkeypatch.setattr(totp.hmac, "compare_digest", compare)
    assert totp.match_counter(RFC_SECRET, independent_code(99), 3000) == 99
    assert len(compared) == 3


def test_collision_consumes_latest_matching_step(monkeypatch):
    monkeypatch.setattr(totp, "_code", lambda key, counter: "123456")
    assert totp.match_counter(RFC_SECRET, "123456", 3000, 99) == 101


def test_new_secrets_use_twenty_secure_random_bytes(monkeypatch):
    calls = []

    def token_bytes(size):
        calls.append(size)
        return bytes(range(size))

    monkeypatch.setattr(totp.secrets, "token_bytes", token_bytes)
    secret = totp.new_secret()
    assert calls == [20]
    assert len(secret) == 32
    assert base64.b32decode(secret) == bytes(range(20))


def test_generated_secrets_are_distinct():
    secrets = {totp.new_secret() for _ in range(20)}
    assert len(secrets) == 20


def test_provisioning_uri_encodes_labels_and_all_algorithm_parameters():
    parsed = urlparse(
        totp.provisioning_uri(
            RFC_SECRET.lower(), "shop+owner@example.com", "店铺 & Extore"
        )
    )
    assert (parsed.scheme, parsed.netloc) == ("otpauth", "totp")
    assert unquote(parsed.path) == "/店铺 & Extore:shop+owner@example.com"
    assert parse_qs(parsed.query) == {
        "secret": [RFC_SECRET],
        "issuer": ["店铺 & Extore"],
        "algorithm": ["SHA1"],
        "digits": ["6"],
        "period": ["30"],
    }


@pytest.mark.parametrize(
    ("email", "issuer"),
    [("", "Extore"), ("owner\n@example.com", "Extore"), ("owner@example.com", "a:b")],
)
def test_provisioning_rejects_ambiguous_or_control_character_labels(email, issuer):
    with pytest.raises(ValueError):
        totp.provisioning_uri(RFC_SECRET, email, issuer)


def test_backup_codes_have_128_bits_and_only_digests_are_storage_values():
    raw, digests = totp.generate_backup_codes()
    assert len(raw) == len(digests) == 10
    assert len(set(raw)) == len(set(digests)) == 10
    assert all(len(code) == 35 for code in raw)
    assert all([len(group) for group in code.split("-")] == [8] * 4 for code in raw)
    assert all(len(bytes.fromhex(code.replace("-", ""))) == 16 for code in raw)
    assert digests == [totp.backup_digest(code) for code in raw]
    assert all(code not in digest for code, digest in zip(raw, digests))
    assert digests[0] != hashlib.sha256(raw[0].encode()).hexdigest()


def test_backup_code_formatting_is_case_insensitive():
    code = "01234567-89ABCDEF-FEDCBA98-76543210"
    assert totp.backup_digest(code) == totp.backup_digest(code.lower())
    assert totp.backup_digest(code) == totp.backup_digest(code.replace("-", " "))
    assert totp.backup_digest(code) == totp.backup_digest(code.replace("-", ""))


@pytest.mark.parametrize(
    "code", [None, 123, "", "abcd", "Z" * 32, "Ａ" * 32, "0" * 100]
)
def test_malformed_backup_codes_are_rejected(code):
    with pytest.raises(ValueError):
        totp.backup_digest(code)


@pytest.mark.parametrize("count", [False, None, "10", 0, -1, 101])
def test_backup_code_count_is_bounded(count):
    with pytest.raises(ValueError):
        totp.generate_backup_codes(count)


def test_backup_generation_recovers_from_a_duplicate_random_value(monkeypatch):
    values = iter(["a" * 32, "a" * 32, "b" * 32])
    monkeypatch.setattr(totp.secrets, "token_hex", lambda size: next(values))
    raw, digests = totp.generate_backup_codes(2)
    assert len(set(raw)) == len(set(digests)) == 2
