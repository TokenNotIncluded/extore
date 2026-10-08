"""Authenticated encryption for tenant credentials; no implicit key rotation."""

import base64
import hashlib
import json
import os
import stat

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .config import DATA

KEY_FILE = "master-secrets.key"
MAX_SECRET_BYTES = 200000


class SecretStoreError(RuntimeError):
    """Errors intentionally contain no key, plaintext, or ciphertext."""


def _read_key():
    try:
        fd = os.open(DATA / KEY_FILE, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise SecretStoreError("Secret encryption key is unavailable") from None
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise SecretStoreError("Secret encryption key permissions are invalid")
        value = os.read(fd, 33)
        if len(value) != 32:
            raise SecretStoreError("Secret encryption key is invalid")
        return value
    finally:
        os.close(fd)


def init_schema(c):
    """Call in the initialization transaction, before any credential writes."""
    c.execute(
        "CREATE TABLE IF NOT EXISTS secret_store_state "
        "(id INTEGER PRIMARY KEY CHECK(id=1),fingerprint TEXT NOT NULL)"
    )
    existing = c.execute(
        "SELECT fingerprint FROM secret_store_state WHERE id=1"
    ).fetchone()
    if existing is None:
        # An absent marker must not turn a lost encryption key into a new key.
        # Check existing encrypted consumers during recovery/migration too.
        tables = {
            row[0]
            for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        encrypted = False
        for table, column in (
            ("processor_profile_revisions", "ciphertext"),
            ("text_card_payloads", "ciphertext"),
            ("card_task_flows", "snapshot_ciphertext"),
            ("task_flow_steps", "payload_ciphertext"),
            ("task_flow_dispatches", "payload_ciphertext"),
            ("commerce_issuances", "response_ciphertext"),
            ("proxy_identities", "private_key"),
            ("shops", "totp_secret"),
            ("shops", "pending_totp_secret"),
            ("mail_outbox", "payload"),
            ("settings", "value"),
        ):
            if table in tables:
                encrypted |= (
                    c.execute(
                        f"SELECT 1 FROM {table} WHERE {column} LIKE 'v1.%' LIMIT 1"
                    ).fetchone()
                    is not None
                )
        if encrypted and not (DATA / KEY_FILE).exists():
            raise SecretStoreError(
                "Existing credentials require the original encryption key"
            )
        try:
            fd = os.open(
                DATA / KEY_FILE,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as file:
                file.write(os.urandom(32))
                file.flush()
                os.fsync(file.fileno())
            directory = os.open(DATA, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    key = _read_key()
    fingerprint = hashlib.sha256(b"extore-secret-key-v1\0" + key).hexdigest()
    if existing is not None and existing["fingerprint"] != fingerprint:
        raise SecretStoreError("Secret encryption key does not match this database")
    if existing is None:
        c.execute("INSERT INTO secret_store_state VALUES (1,?)", (fingerprint,))


def _context(tenant_id, resource_type, resource_id):
    if tenant_id is not None and not isinstance(tenant_id, str):
        raise SecretStoreError("Invalid secret scope")
    values = [tenant_id, resource_type, resource_id]
    if any(
        not isinstance(v, str) or not v or len(v) > 200 or any(ord(ch) < 32 for ch in v)
        for v in values[1:]
    ) or (tenant_id is not None and (not tenant_id or len(tenant_id) > 200)):
        raise SecretStoreError("Invalid secret scope")
    aad = json.dumps(
        ["extore-secret-v1", *values], ensure_ascii=True, separators=(",", ":")
    ).encode()
    tenant = json.dumps(tenant_id, separators=(",", ":")).encode()
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"extore-tenant-secret-v1",
        info=tenant,
    ).derive(_read_key())
    return key, aad


def store_secret(value, *, tenant_id, resource_type, resource_id):
    """Return a versioned ciphertext; safe inside an existing DB transaction."""
    if not isinstance(value, (str, dict)):
        raise SecretStoreError("Secret must be text or an object")
    try:
        raw = json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
    except (TypeError, ValueError, RecursionError):
        raise SecretStoreError("Invalid secret value") from None
    if len(raw) > MAX_SECRET_BYTES:
        raise SecretStoreError("Secret exceeds the storage limit")
    key, aad = _context(tenant_id, resource_type, resource_id)
    nonce = os.urandom(12)
    encrypted = AESGCM(key).encrypt(nonce, raw, aad)
    return "v1." + base64.urlsafe_b64encode(nonce + encrypted).decode().rstrip("=")


def open_secret(ciphertext, *, tenant_id, resource_type, resource_id):
    """Authenticate the tenant and immutable resource identity before reading."""
    key, aad = _context(tenant_id, resource_type, resource_id)
    try:
        if not isinstance(ciphertext, str) or not ciphertext.startswith("v1."):
            raise ValueError
        encoded = ciphertext[3:]
        if len(encoded) > (MAX_SECRET_BYTES + 28) * 2:
            raise ValueError
        raw = base64.b64decode(
            encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True
        )
        if (
            base64.urlsafe_b64encode(raw).decode().rstrip("=") != encoded
            or len(raw) < 28
        ):
            raise ValueError
        plaintext = AESGCM(key).decrypt(raw[:12], raw[12:], aad)
        value = json.loads(plaintext)
        if not isinstance(value, (str, dict)):
            raise ValueError
        return value
    except Exception:
        raise SecretStoreError("Secret authentication failed") from None
