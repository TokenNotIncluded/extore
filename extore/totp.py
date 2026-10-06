"""TOTP and recovery-code helpers; callers persist consumption atomically.

TOTP secrets must be encrypted at rest.  A successful ``match_counter`` does
not consume a code by itself: the account transaction must store the returned
counter before it issues a session.  Recovery-code digests have the same
requirement: delete the matching account-scoped digest in that transaction.
"""

import base64
import binascii
import hashlib
import hmac
import math
import secrets
from urllib.parse import quote, urlencode

PERIOD = 30
DIGITS = 6
_MAX_COUNTER = (1 << 64) - 1
_BASE32_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")
_HEX_ALPHABET = frozenset("0123456789ABCDEF")


def new_secret() -> str:
    """Return an authenticator-compatible secret containing 160 random bits."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _secret_bytes(secret: str) -> bytes:
    if (
        not isinstance(secret, str)
        or not 32 <= len(secret) <= 104
        or not secret.isascii()
    ):
        raise ValueError("Invalid TOTP secret")
    normalized = secret.upper().rstrip("=")
    if not normalized or any(c not in _BASE32_ALPHABET for c in normalized):
        raise ValueError("Invalid TOTP secret")
    if secret.upper() not in (
        normalized,
        normalized + "=" * (-len(normalized) % 8),
    ):
        raise ValueError("Invalid TOTP secret")
    try:
        decoded = base64.b32decode(normalized + "=" * (-len(normalized) % 8))
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Invalid TOTP secret") from exc
    if not 20 <= len(decoded) <= 64:
        raise ValueError("Invalid TOTP secret")
    if base64.b32encode(decoded).decode("ascii").rstrip("=") != normalized:
        raise ValueError("Invalid TOTP secret")
    return decoded


def provisioning_uri(secret: str, email: str, issuer: str = "Extore") -> str:
    """Build a standard otpauth URI without fetching a third-party QR image."""
    key = base64.b32encode(_secret_bytes(secret)).decode("ascii").rstrip("=")
    for value, maximum in ((email, 254), (issuer, 64)):
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > maximum
            or ":" in value
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
        ):
            raise ValueError("Invalid authenticator label")
    label = f"{quote(issuer, safe='')}:{quote(email, safe='')}"
    query = urlencode(
        {
            "secret": key,
            "issuer": issuer,
            "algorithm": "SHA1",
            "digits": DIGITS,
            "period": PERIOD,
        }
    )
    return f"otpauth://totp/{label}?{query}"


def _code(key: bytes, counter: int) -> str:
    mac = hmac.new(key, counter.to_bytes(8, "big"), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    number = int.from_bytes(mac[offset : offset + 4], "big") & 0x7FFFFFFF
    return f"{number % (10**DIGITS):0{DIGITS}d}"


def match_counter(
    secret: str, code: str, now: int | float, last_counter: int | None = None
) -> int | None:
    """Return the matching unused RFC 6238 counter in the current ±1 window.

    Invalid or replayed input returns ``None``.  All three candidate codes are
    compared with ``compare_digest`` without returning on the first match.
    ``last_counter`` must be read and updated in the caller's same transaction.
    """
    if (
        not isinstance(code, str)
        or len(code) != DIGITS
        or any(c < "0" or c > "9" for c in code)
    ):
        return None
    if isinstance(now, bool) or not isinstance(now, (int, float)):
        return None
    try:
        if not math.isfinite(now) or now < 0:
            return None
        current = int(now // PERIOD)
    except (ValueError, OverflowError):
        return None
    if current >= _MAX_COUNTER:
        return None
    previous = -1 if last_counter is None else last_counter
    if isinstance(previous, bool) or not isinstance(previous, int) or previous < -1:
        return None
    try:
        key = _secret_bytes(secret)
    except ValueError:
        return None
    matched = None
    for counter in (current - 1, current, current + 1):
        if counter < 0:
            continue
        equal = hmac.compare_digest(_code(key, counter), code)
        if equal and counter > previous:
            # In the rare event of a code collision, consume the latest match.
            matched = counter
    return matched


def backup_digest(code: str) -> str:
    """Hash one 128-bit recovery code; reject malformed or truncated inputs."""
    if not isinstance(code, str) or len(code) > 96:
        raise ValueError("Invalid recovery code")
    normalized = code.replace("-", "").replace(" ", "").upper()
    if len(normalized) != 32 or any(c not in _HEX_ALPHABET for c in normalized):
        raise ValueError("Invalid recovery code")
    return hashlib.sha256(
        b"extore-account-recovery-code-v1\0" + normalized.encode("ascii")
    ).hexdigest()


def generate_backup_codes(count: int = 10) -> tuple[list[str], list[str]]:
    """Return raw recovery codes once and the corresponding storage digests."""
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 100:
        raise ValueError("Invalid recovery-code count")
    raw = []
    seen = set()
    while len(raw) < count:
        code = secrets.token_hex(16).upper()
        if code in seen:
            continue
        seen.add(code)
        raw.append("-".join(code[i : i + 8] for i in range(0, 32, 8)))
    return raw, [backup_digest(code) for code in raw]
