"""TLS-only account mail with an encrypted, durable, bounded outbox.

SMTP is disabled until the super administrator configures it. Account requests
only enqueue mail inside their existing database transaction; the worker sends
outside that transaction. Delivery is at least once, so links and verification
tokens in messages must themselves be single use.
"""

import asyncio
import ipaddress
import math
import re
import secrets
import smtplib
import ssl
import time
import uuid
from email.headerregistry import Address
from email.message import EmailMessage
from email.policy import SMTP

from .db import audit, db, set_setting, setting

SETTINGS_KEY = "smtp_config"
MAX_QUEUED = 1000
MAX_BODY_BYTES = 64 * 1024
MAX_ATTEMPTS = 8
MAX_LIFETIME = 7 * 86400
LEASE_SECONDS = 120
SMTP_TIMEOUT = 10

SCHEMA = """
CREATE TABLE IF NOT EXISTS mail_outbox (
    id TEXT PRIMARY KEY,
    tenant_id TEXT,
    payload TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK(state IN ('pending', 'sending', 'delivered', 'dead')),
    attempts INTEGER NOT NULL DEFAULT 0,
    due REAL NOT NULL,
    created REAL NOT NULL,
    expires REAL NOT NULL,
    lease_until REAL,
    lease_token TEXT,
    error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS mail_outbox_due ON mail_outbox(state, due);
"""


def init_schema(c):
    # Do not use executescript: it commits an enclosing account/migration txn.
    c.execute(SCHEMA.split(";", 1)[0])
    c.execute("CREATE INDEX IF NOT EXISTS mail_outbox_due ON mail_outbox(state,due)")


def _seal(value, resource_type, resource_id, tenant_id=None):
    from .secret_store import store_secret

    return store_secret(
        value,
        tenant_id=tenant_id,
        resource_type=resource_type,
        resource_id=resource_id,
    )


def _open(value, resource_type, resource_id, tenant_id=None):
    from .secret_store import open_secret

    return open_secret(
        value,
        tenant_id=tenant_id,
        resource_type=resource_type,
        resource_id=resource_id,
    )


def _text(value, name, maximum, *, trim=False, empty=True):
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"Invalid {name}")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"Invalid {name}")
    value = value.strip() if trim else value
    if not empty and not value:
        raise ValueError(f"Invalid {name}")
    return value


def email_address(value):
    """One ASCII mailbox, never a display-name/list/header provided by a user."""
    value = _text(value, "email address", 254, trim=True, empty=False)
    if not value.isascii() or value.count("@") != 1:
        raise ValueError("Invalid email address")
    local, domain = value.rsplit("@", 1)
    if (
        len(local) > 64
        or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+", local)
        or local.startswith(".")
        or local.endswith(".")
        or ".." in local
        or len(domain) > 253
        or "." not in domain
        or any(not _domain_label(label) for label in domain.split("."))
    ):
        raise ValueError("Invalid email address")
    return local + "@" + domain.lower()


def _domain_label(value):
    return bool(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", value))


def _host(value):
    value = _text(value, "SMTP host", 253, trim=True)
    if not value:
        return ""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        try:
            value = value.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise ValueError("Invalid SMTP host") from exc
        if len(value) > 253 or any(
            not _domain_label(label) for label in value.split(".")
        ):
            raise ValueError("Invalid SMTP host")
    else:
        if address.is_unspecified or address.is_multicast:
            raise ValueError("Invalid SMTP host")
        value = str(address)
    return value


def _load(c):
    encrypted = setting(c, SETTINGS_KEY)
    if encrypted is None:
        return {
            "enabled": False,
            "host": "",
            "port": 587,
            "mode": "starttls",
            "sender": "",
            "from_name": "",
            "username": "",
            "password": "",
        }
    values = _open(encrypted, "smtp-config", "global")
    if not isinstance(values, dict):
        raise ValueError("Invalid encrypted SMTP configuration")
    return _validate(values)


def _validate(values):
    allowed = {
        "enabled",
        "host",
        "port",
        "mode",
        "sender",
        "from_name",
        "username",
        "password",
    }
    if not isinstance(values, dict) or set(values) != allowed:
        raise ValueError("Invalid SMTP configuration fields")
    values = dict(values)
    if type(values["enabled"]) is not bool:
        raise ValueError("Invalid SMTP enabled value")
    if type(values["port"]) is not int or not 1 <= values["port"] <= 65535:
        raise ValueError("Invalid SMTP port")
    if values["mode"] not in ("ssl", "starttls"):
        raise ValueError("SMTP requires TLS (ssl or starttls)")
    values["host"] = _host(values["host"])
    values["sender"] = _text(values["sender"], "SMTP sender", 254, trim=True)
    if values["sender"]:
        values["sender"] = email_address(values["sender"])
    for name, maximum in (("from_name", 120), ("username", 320), ("password", 4096)):
        values[name] = _text(values[name], "SMTP " + name, maximum)
    if values["password"] == "[redacted]":
        raise ValueError("Omit the SMTP password to preserve it")
    if bool(values["username"]) != bool(values["password"]):
        raise ValueError("SMTP username and password must be configured together")
    if values["enabled"] and not (values["host"] and values["sender"]):
        raise ValueError("Configure the SMTP host and sender before enabling mail")
    return values


def _metadata(values):
    return {
        "configured": bool(values["host"] and values["sender"]),
        **{
            key: values[key]
            for key in ("enabled", "host", "port", "mode", "sender", "from_name")
        },
        "has_username": bool(values["username"]),
        "has_password": bool(values["password"]),
    }


def configure(c, values, actor=None):
    """Patch global SMTP settings; callers must require super-administrator auth."""
    if not isinstance(values, dict):
        raise ValueError("Invalid SMTP configuration")
    old = _load(c)
    result = _validate({**old, **values})
    encrypted = _seal(result, "smtp-config", "global")
    set_setting(c, SETTINGS_KEY, encrypted)
    if actor is not None:
        audit(c, actor, "smtp.configure", "global")
    return _metadata(result)


def get_settings(c=None):
    """Only return non-secret metadata, including when called from the CLI."""
    if c is None:
        with db() as connection:
            return _metadata(_load(connection))
    return _metadata(_load(c))


def _validate_bodies(body, html=None):
    if (
        not isinstance(body, str)
        or "\x00" in body
        or not body.strip()
        or (
            html is not None
            and (not isinstance(html, str) or "\x00" in html or not html.strip())
        )
    ):
        raise ValueError("Invalid mail body")
    total = len(body.encode("utf-8")) + (
        len(html.encode("utf-8")) if html is not None else 0
    )
    if total > MAX_BODY_BYTES:
        raise ValueError("Invalid mail body")


def enqueue(c, to, subject, body, *, html=None, expires=None, shop_id=None):
    """Store an encrypted message atomically with its account/invitation change."""
    config = _load(c)
    if not config["enabled"]:
        raise ValueError("SMTP mail is not enabled")
    recipient = email_address(to)
    subject = _text(subject, "mail subject", 200, trim=True, empty=False)
    _validate_bodies(body, html)
    if shop_id is not None:
        shop_id = _text(shop_id, "shop id", 128, empty=False)
    now = time.time()
    expires = now + 86400 if expires is None else expires
    if (
        type(expires) not in (int, float)
        or not math.isfinite(expires)
        or not now < expires <= now + MAX_LIFETIME
    ):
        raise ValueError("Invalid mail expiration")
    pending = c.execute(
        "SELECT count(*) FROM mail_outbox WHERE state IN ('pending','sending') AND expires>?",
        (now,),
    ).fetchone()[0]
    if pending >= MAX_QUEUED:
        raise ValueError("Mail queue is full")
    message_id = str(uuid.uuid4())
    values = {"to": recipient, "subject": subject, "body": body}
    if html is not None:
        values["html"] = html
    payload = _seal(
        values,
        "mail",
        message_id,
        shop_id,
    )
    c.execute(
        "INSERT INTO mail_outbox(id,tenant_id,payload,due,created,expires) VALUES(?,?,?,?,?,?)",
        (message_id, shop_id, payload, now, now, expires),
    )
    return message_id


def _ehlo(server):
    reply = server.ehlo()
    if not isinstance(reply, (tuple, list)) or len(reply) != 2:
        raise smtplib.SMTPHeloError(-1, b"Invalid EHLO response")
    code, response = reply
    # smtplib.ehlo returns rejection codes without raising an exception.
    if type(code) is not int or code != 250:
        raise smtplib.SMTPHeloError(code, response)


def _send(config, payload, message_id):
    from .mail_templates import plain_message

    # Certificate/hostname verification cannot be disabled by SMTP configuration.
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    message = EmailMessage(policy=SMTP)
    message["From"] = Address(
        display_name=config["from_name"], addr_spec=config["sender"]
    )
    message["To"] = payload["to"]
    message["Subject"] = payload["subject"]
    message["Message-ID"] = f"<{message_id}@{config['sender'].rsplit('@', 1)[1]}>"
    message.set_content(payload["body"])
    html = payload.get("html")
    if html is None:
        candidate = plain_message(payload["subject"], payload["body"])
        # Existing outbox messages can already fill the entire body limit. Keep
        # them deliverable as plain text instead of expanding mail without bound.
        if (
            len(payload["body"].encode("utf-8")) + len(candidate.encode("utf-8"))
            <= MAX_BODY_BYTES
        ):
            html = candidate
    if html is not None:
        message.add_alternative(html, subtype="html")
    if config["mode"] == "ssl":
        server = smtplib.SMTP_SSL(
            config["host"], config["port"], timeout=SMTP_TIMEOUT, context=context
        )
    else:
        server = smtplib.SMTP(config["host"], config["port"], timeout=SMTP_TIMEOUT)
    accepted = False
    send_failure = None
    try:
        with server:
            try:
                _ehlo(server)
                if config["mode"] == "starttls":
                    # Never fall back to cleartext credentials/message on TLS failure.
                    server.starttls(context=context)
                    _ehlo(server)
                if config["username"]:
                    server.login(config["username"], config["password"])
                refused = server.send_message(
                    message, from_addr=config["sender"], to_addrs=[payload["to"]]
                )
                if refused:
                    raise smtplib.SMTPRecipientsRefused(refused)
                accepted = True
            except Exception as exc:
                send_failure = exc
                raise
    except (smtplib.SMTPException, OSError):
        # QUIT can fail after acceptance. It must neither resend accepted mail
        # nor replace a failure from TLS/auth/MAIL/RCPT/DATA with its own reply.
        if send_failure is not None:
            raise send_failure
        if not accepted:
            raise


def _error(exc):
    # Provider exceptions often contain the password, recipient, or entire message.
    if isinstance(exc, ssl.SSLError):
        return "tls_failed"
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "authentication_failed"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "recipient_rejected"
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return "sender_rejected"
    if isinstance(exc, (smtplib.SMTPException, OSError)):
        return "transport_failed"
    return "invalid_mail_payload"


def _permanent_smtp_failure(exc):
    """Stop this message on structured 5xx replies, without judging its mailbox."""

    def permanent(code):
        return type(code) is int and 500 <= code < 600

    if isinstance(exc, smtplib.SMTPResponseException):
        return permanent(exc.smtp_code)
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        responses = exc.recipients
        # Extore sends to one recipient. Unknown/mixed replies remain bounded
        # retries; never parse provider text that may contain private data.
        return (
            isinstance(responses, dict)
            and bool(responses)
            and all(
                isinstance(response, (tuple, list))
                and len(response) == 2
                and permanent(response[0])
                for response in responses.values()
            )
        )
    return False


async def process_outbox_once():
    """Claim at most one message, deliver outside SQLite, and fence stale workers."""
    now = time.time()
    with db() as c:
        c.execute(
            "DELETE FROM mail_outbox WHERE id IN (SELECT id FROM mail_outbox "
            "WHERE state IN ('delivered','dead') AND created<? ORDER BY created LIMIT 100)",
            (now - 30 * 86400,),
        )
        c.execute(
            "UPDATE mail_outbox SET state='dead',payload='',error='expired',"
            "lease_until=NULL,lease_token=NULL WHERE state IN ('pending','sending') AND expires<=?",
            (now,),
        )
        c.execute(
            "UPDATE mail_outbox SET state=CASE WHEN attempts>=? THEN 'dead' ELSE 'pending' END,"
            "payload=CASE WHEN attempts>=? THEN '' ELSE payload END,"
            "error='delivery_interrupted',lease_until=NULL,lease_token=NULL "
            "WHERE state='sending' AND lease_until<=?",
            (MAX_ATTEMPTS, MAX_ATTEMPTS, now),
        )
        config = _load(c)
        if not config["enabled"]:
            return False
        row = c.execute(
            "SELECT * FROM mail_outbox WHERE state='pending' AND due<=? AND expires>? "
            "ORDER BY due,created LIMIT 1",
            (now, now),
        ).fetchone()
        if row is None:
            return False
        row = dict(row)
        lease = secrets.token_urlsafe(24)
        c.execute(
            "UPDATE mail_outbox SET state='sending',attempts=attempts+1,"
            "lease_until=?,lease_token=? WHERE id=? AND state='pending'",
            (now + LEASE_SECONDS, lease, row["id"]),
        )
    error = ""
    permanent_failure = False
    try:
        payload = _open(row["payload"], "mail", row["id"], row["tenant_id"])
        if not isinstance(payload, dict) or set(payload) not in (
            {"to", "subject", "body"},
            {"to", "subject", "body", "html"},
        ):
            raise ValueError("Invalid encrypted mail payload")
        # Recheck fields before setting headers even if an encryption key is compromised.
        payload["to"] = email_address(payload["to"])
        payload["subject"] = _text(
            payload["subject"], "mail subject", 200, trim=True, empty=False
        )
        if "html" in payload and payload["html"] is None:
            raise ValueError("Invalid encrypted mail body")
        _validate_bodies(payload["body"], payload.get("html"))
        await asyncio.to_thread(_send, config, payload, row["id"])
    except Exception as exc:
        error = _error(exc)
        permanent_failure = _permanent_smtp_failure(exc)
    # Cancellation/crash leaves the claim to expire; a stale worker cannot overwrite
    # a claim made later by another process. SMTP cannot guarantee exactly-once send.
    with db() as c:
        if not error:
            c.execute(
                "UPDATE mail_outbox SET state='delivered',payload='',error='',"
                "lease_until=NULL,lease_token=NULL WHERE id=? AND state='sending' AND lease_token=?",
                (row["id"], lease),
            )
        else:
            attempts = row["attempts"] + 1
            due = time.time() + min(3600, 30 * 2**attempts)
            dead = (
                error == "invalid_mail_payload"
                or permanent_failure
                or attempts >= MAX_ATTEMPTS
                or due >= row["expires"]
            )
            c.execute(
                "UPDATE mail_outbox SET state=?,payload=CASE WHEN ? THEN '' ELSE payload END,"
                "due=?,error=?,lease_until=NULL,lease_token=NULL "
                "WHERE id=? AND state='sending' AND lease_token=?",
                (
                    "dead" if dead else "pending",
                    dead,
                    due,
                    error,
                    row["id"],
                    lease,
                ),
            )
    return True
