import asyncio
import json
import smtplib
import ssl
import threading
import time

import pytest

from extore import mail
from extore.db import db, setting

PASSWORD = "synthetic-smtp-password-do-not-log"
RECIPIENT = "Recipient@example.test"
TOKEN = "synthetic-account-token-do-not-log"
CONFIG = {
    "enabled": True,
    "host": "smtp.example.test",
    "port": 587,
    "mode": "starttls",
    "sender": "store@example.test",
    "from_name": "Extore 店铺",
    "username": "synthetic-login@example.test",
    "password": PASSWORD,
}


@pytest.fixture(autouse=True)
def clean_mail():
    with db() as c:
        mail.init_schema(c)
        c.execute("DELETE FROM mail_outbox")
    yield
    with db() as c:
        c.execute("DELETE FROM mail_outbox")


def configure(**changes):
    with db() as c:
        return mail.configure(c, {**CONFIG, **changes}, "superadmin-test")


def enqueue(**changes):
    values = {
        "to": RECIPIENT,
        "subject": "Verify your Extore account",
        "body": "Verification: https://extore.test/account#" + TOKEN,
        "shop_id": "synthetic-shop",
    }
    with db() as c:
        return mail.enqueue(c, **{**values, **changes})


def row(message_id):
    with db() as c:
        return dict(
            c.execute("SELECT * FROM mail_outbox WHERE id=?", (message_id,)).fetchone()
        )


def test_smtp_is_disabled_by_default_and_metadata_has_no_credentials(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Default configuration must never connect to SMTP")

    monkeypatch.setattr(mail, "_send", forbidden)
    metadata = mail.get_settings()
    assert metadata["configured"] is metadata["enabled"] is False
    assert not metadata["has_password"] and not metadata["has_username"]
    with pytest.raises(ValueError, match="not enabled"):
        enqueue()
    assert asyncio.run(mail.process_outbox_once()) is False


def test_smtp_credentials_and_account_mail_are_encrypted_at_rest():
    public = configure()
    assert public["has_password"] and public["has_username"]
    assert "password" not in public and "username" not in public
    assert PASSWORD not in json.dumps(public)
    assert CONFIG["username"] not in json.dumps(public)
    message_id = enqueue()
    with db() as c:
        stored = setting(c, mail.SETTINGS_KEY)
        assert stored.startswith("v1.")
        assert PASSWORD not in stored and CONFIG["username"] not in stored
        audit = [dict(r) for r in c.execute("SELECT * FROM audit")]
    message = row(message_id)
    assert message["payload"].startswith("v1.")
    assert RECIPIENT not in message["payload"] and TOKEN not in message["payload"]
    assert PASSWORD not in json.dumps(audit) and TOKEN not in json.dumps(audit)
    assert mail._open(stored, "smtp-config", "global")["password"] == PASSWORD
    assert (
        mail._open(message["payload"], "mail", message_id, "synthetic-shop")["to"]
        == "Recipient@example.test"
    )
    with pytest.raises(RuntimeError, match="authentication failed"):
        mail._open(message["payload"], "mail", message_id, "another-shop")
    with pytest.raises(RuntimeError, match="authentication failed"):
        mail._open(message["payload"], "mail", "another-message", "synthetic-shop")


def test_config_patch_preserves_omitted_password_and_can_disable_delivery():
    configure()
    with db() as c:
        mail.configure(c, {"from_name": "New name"})
        assert mail._load(c)["password"] == PASSWORD
        assert mail._load(c)["username"] == CONFIG["username"]
        mail.configure(c, {"enabled": False})
    assert mail.get_settings()["enabled"] is False
    assert mail.get_settings()["has_password"] is True


@pytest.mark.parametrize(
    "change",
    [
        {"mode": "plain"},
        {"mode": "none"},
        {"enabled": "true"},
        {"port": True},
        {"port": 0},
        {"port": 65536},
        {"host": "https://smtp.example.test"},
        {"host": "smtp.example.test/path"},
        {"host": "user@smtp.example.test"},
        {"host": "smtp.example.test\r\nBcc: thief@example.test"},
        {"host": "0.0.0.0"},
        {"host": "::"},
        {"host": "224.0.0.1"},
        {"host": "-smtp.example.test"},
        {"sender": "Sender <sender@example.test>"},
        {"sender": "a@example.test,b@example.test"},
        {"sender": None},
        {"from_name": "Extore\nBcc: thief@example.test"},
        {"username": "test\x00@example.test"},
        {"password": "[redacted]"},
        {"password": ""},
        {"username": ""},
        {"verify_tls": False},
    ],
)
def test_smtp_configuration_rejects_plaintext_header_injection_and_invalid_fields(
    change,
):
    with pytest.raises(ValueError):
        configure(**change)
    with db() as c:
        assert setting(c, mail.SETTINGS_KEY) is None


@pytest.mark.parametrize(
    "change",
    [
        {"to": "display <user@example.test>"},
        {"to": "a@example.test,b@example.test"},
        {"to": "user@example.test\nBcc: attacker@example.test"},
        {"to": "a..b@example.test"},
        {"to": "a@example.-test"},
        {"subject": "Subject\r\nBcc: attacker@example.test"},
        {"subject": "a" * 201},
        {"body": "\x00"},
        {"body": " "},
        {"body": "文" * (mail.MAX_BODY_BYTES // 3 + 1)},
        {"expires": 0},
        {"expires": float("nan")},
        {"expires": float("inf")},
        {"expires": True},
        {"expires": time.time() + mail.MAX_LIFETIME + 300},
        {"shop_id": "bad\x00tenant"},
    ],
)
def test_invalid_message_is_never_enqueued(change):
    configure()
    with pytest.raises(ValueError):
        enqueue(**change)
    with db() as c:
        assert c.execute("SELECT count(*) FROM mail_outbox").fetchone()[0] == 0


def test_queue_capacity_is_checked_inside_the_callers_transaction(monkeypatch):
    configure()
    monkeypatch.setattr(mail, "MAX_QUEUED", 1)
    enqueue()
    with pytest.raises(ValueError, match="queue is full"):
        enqueue()
    with db() as c:
        assert c.execute("SELECT count(*) FROM mail_outbox").fetchone()[0] == 1


def test_account_and_outbox_changes_roll_back_together():
    configure()
    with pytest.raises(RuntimeError):
        with db() as c:
            c.execute(
                "INSERT INTO settings VALUES ('test_account_transaction','created')"
            )
            mail.enqueue(c, RECIPIENT, "Subject", TOKEN)
            raise RuntimeError("Synthetic account transaction failure")
    with db() as c:
        assert setting(c, "test_account_transaction") is None
        assert c.execute("SELECT count(*) FROM mail_outbox").fetchone()[0] == 0


class FakeSMTP:
    calls = None
    fail_tls = False
    refused = None

    def __init__(self, host, port, *, timeout, context=None):
        self.calls.append(("connect", host, port, timeout, context))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.calls.append(("close",))

    def ehlo(self):
        self.calls.append(("ehlo",))

    def starttls(self, *, context):
        self.calls.append(("starttls", context))
        if self.fail_tls:
            raise smtplib.SMTPNotSupportedError("synthetic server does not support TLS")

    def login(self, username, password):
        self.calls.append(("login", username, password))

    def send_message(self, message, *, from_addr, to_addrs):
        self.calls.append(("send", message, from_addr, to_addrs))
        return self.refused or {}


@pytest.fixture
def smtp(monkeypatch):
    calls = []
    monkeypatch.setattr(FakeSMTP, "calls", calls)
    monkeypatch.setattr(FakeSMTP, "fail_tls", False)
    monkeypatch.setattr(FakeSMTP, "refused", None)
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    return calls


def test_starttls_runs_before_authentication_and_payload_is_erased_on_delivery(smtp):
    configure()
    message_id = enqueue()
    assert asyncio.run(mail.process_outbox_once()) is True
    assert [entry[0] for entry in smtp] == [
        "connect",
        "ehlo",
        "starttls",
        "ehlo",
        "login",
        "send",
        "close",
    ]
    context = smtp[2][1]
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
    _, message, sender, recipients = smtp[5]
    assert sender == "store@example.test" and recipients == [RECIPIENT]
    assert len(message["From"].addresses) == 1
    assert message["From"].addresses[0].addr_spec == "store@example.test"
    assert message["From"].addresses[0].display_name == "Extore 店铺"
    assert TOKEN in message.get_content()
    delivered = row(message_id)
    assert delivered["state"] == "delivered" and delivered["attempts"] == 1
    assert delivered["payload"] == delivered["error"] == ""
    assert delivered["lease_token"] is delivered["lease_until"] is None
    assert asyncio.run(mail.process_outbox_once()) is False


def test_implicit_tls_uses_verified_context_from_the_first_connection(smtp):
    configure(mode="ssl", port=465)
    enqueue()
    assert asyncio.run(mail.process_outbox_once()) is True
    assert [entry[0] for entry in smtp] == ["connect", "ehlo", "login", "send", "close"]
    context = smtp[0][4]
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname


def test_tls_failure_never_sends_credentials_or_message_in_plaintext(smtp, monkeypatch):
    configure()
    message_id = enqueue()
    monkeypatch.setattr(FakeSMTP, "fail_tls", True)
    assert asyncio.run(mail.process_outbox_once()) is True
    assert [entry[0] for entry in smtp] == ["connect", "ehlo", "starttls", "close"]
    pending = row(message_id)
    assert pending["state"] == "pending" and pending["attempts"] == 1
    assert pending["due"] > time.time() and pending["error"] == "transport_failed"


def test_smtp_exception_text_is_not_persisted_and_retries_are_bounded(monkeypatch):
    configure()
    message_id = enqueue()

    def fail(*args):
        raise smtplib.SMTPAuthenticationError(535, (PASSWORD + TOKEN).encode())

    monkeypatch.setattr(mail, "_send", fail)
    for attempt in range(1, mail.MAX_ATTEMPTS + 1):
        with db() as c:
            c.execute("UPDATE mail_outbox SET due=0 WHERE id=?", (message_id,))
        assert asyncio.run(mail.process_outbox_once()) is True
        current = row(message_id)
        assert current["attempts"] == attempt
        assert current["error"] == "authentication_failed"
        assert PASSWORD not in current["error"] and TOKEN not in current["error"]
        assert current["state"] == (
            "dead" if attempt == mail.MAX_ATTEMPTS else "pending"
        )
    assert current["payload"] == ""
    assert asyncio.run(mail.process_outbox_once()) is False


def test_expired_messages_are_never_sent_and_disabled_config_pauses_pending(
    monkeypatch,
):
    configure()
    message_id = enqueue()
    with db() as c:
        mail.configure(c, {"enabled": False})
    monkeypatch.setattr(mail, "_send", lambda *args: pytest.fail("Must not send"))
    assert asyncio.run(mail.process_outbox_once()) is False
    assert row(message_id)["state"] == "pending"
    with db() as c:
        c.execute("UPDATE mail_outbox SET expires=0 WHERE id=?", (message_id,))
    assert asyncio.run(mail.process_outbox_once()) is False
    expired = row(message_id)
    assert expired["state"] == "dead" and expired["error"] == "expired"
    assert expired["payload"] == ""


def test_sending_does_not_hold_sqlite_and_concurrent_worker_cannot_claim_same_mail(
    monkeypatch,
):
    configure()
    message_id = enqueue()
    entered = threading.Event()
    release = threading.Event()

    def send(*args):
        entered.set()
        assert release.wait(3)

    monkeypatch.setattr(mail, "_send", send)

    async def run():
        first = asyncio.create_task(mail.process_outbox_once())
        assert await asyncio.to_thread(entered.wait, 2)
        assert row(message_id)["state"] == "sending"
        with db() as c:
            c.execute("INSERT INTO settings VALUES ('concurrent_mail_test','ok')")
        assert await mail.process_outbox_once() is False
        release.set()
        assert await first is True

    try:
        asyncio.run(run())
    finally:
        release.set()
    assert row(message_id)["state"] == "delivered"


def test_crashed_claim_is_recovered_without_creating_a_second_message(smtp):
    configure()
    message_id = enqueue()
    with db() as c:
        c.execute(
            "UPDATE mail_outbox SET state='sending',attempts=1,lease_until=0,lease_token='crashed' WHERE id=?",
            (message_id,),
        )
    assert asyncio.run(mail.process_outbox_once()) is True
    assert row(message_id)["state"] == "delivered"
    assert row(message_id)["attempts"] == 2
    assert sum(call[0] == "send" for call in smtp) == 1


def test_stale_worker_cannot_overwrite_a_newer_claim(monkeypatch):
    configure()
    message_id = enqueue()

    def replace_claim(*args):
        with db() as c:
            c.execute(
                "UPDATE mail_outbox SET attempts=2,lease_token='newer',error='newer worker' WHERE id=?",
                (message_id,),
            )

    monkeypatch.setattr(mail, "_send", replace_claim)
    assert asyncio.run(mail.process_outbox_once()) is True
    current = row(message_id)
    assert current["state"] == "sending" and current["lease_token"] == "newer"
    assert current["attempts"] == 2 and current["error"] == "newer worker"


def test_resource_replayed_ciphertext_is_not_delivered(monkeypatch):
    configure()
    first, second = enqueue(), enqueue(shop_id="other-shop")
    first_payload = row(first)["payload"]
    with db() as c:
        c.execute(
            "UPDATE mail_outbox SET payload=? WHERE id=?", (first_payload, second)
        )
        c.execute(
            "UPDATE mail_outbox SET state='delivered',payload='' WHERE id=?", (first,)
        )
    monkeypatch.setattr(
        mail, "_send", lambda *args: pytest.fail("Must not send replayed ciphertext")
    )
    assert asyncio.run(mail.process_outbox_once()) is True
    assert row(second)["error"] == "invalid_mail_payload"
    assert row(second)["state"] == "dead" and row(second)["payload"] == ""


def test_display_name_cannot_add_a_second_sender_address(smtp):
    configure(from_name='Other <attacker@example.test>, "second"')
    enqueue()
    assert asyncio.run(mail.process_outbox_once()) is True
    message = next(call[1] for call in smtp if call[0] == "send")
    assert len(message["From"].addresses) == 1
    assert message["From"].addresses[0].addr_spec == "store@example.test"
