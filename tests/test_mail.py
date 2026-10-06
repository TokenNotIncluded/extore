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
    quit_error = None
    send_error = None

    def __init__(self, host, port, *, timeout, context=None):
        self.calls.append(("connect", host, port, timeout, context))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.calls.append(("close",))
        if self.quit_error is not None:
            raise self.quit_error

    def ehlo(self):
        self.calls.append(("ehlo",))
        return 250, b"OK"

    def starttls(self, *, context):
        self.calls.append(("starttls", context))
        if self.fail_tls:
            raise smtplib.SMTPNotSupportedError("synthetic server does not support TLS")

    def login(self, username, password):
        self.calls.append(("login", username, password))

    def send_message(self, message, *, from_addr, to_addrs):
        self.calls.append(("send", message, from_addr, to_addrs))
        if self.send_error is not None:
            raise self.send_error
        return self.refused or {}


@pytest.fixture
def smtp(monkeypatch):
    calls = []
    monkeypatch.setattr(FakeSMTP, "calls", calls)
    monkeypatch.setattr(FakeSMTP, "fail_tls", False)
    monkeypatch.setattr(FakeSMTP, "refused", None)
    monkeypatch.setattr(FakeSMTP, "quit_error", None)
    monkeypatch.setattr(FakeSMTP, "send_error", None)
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


@pytest.mark.parametrize(
    "failure",
    [
        smtplib.SMTPResponseException(550, b"QUIT failed"),
        smtplib.SMTPResponseException(421, b"QUIT failed"),
        smtplib.SMTPServerDisconnected("Disconnected during QUIT"),
        OSError("Connection cleanup failed"),
    ],
)
def test_smtp_quit_failure_after_acceptance_does_not_resend(smtp, monkeypatch, failure):
    configure()
    message_id = enqueue()
    monkeypatch.setattr(FakeSMTP, "quit_error", failure)
    assert asyncio.run(mail.process_outbox_once()) is True
    current = row(message_id)
    assert current["state"] == "delivered" and current["attempts"] == 1
    assert current["payload"] == current["error"] == ""
    assert asyncio.run(mail.process_outbox_once()) is False
    assert sum(call[0] == "send" for call in smtp) == 1


def test_non_io_exit_errors_are_not_swallowed(smtp, monkeypatch):
    failure = ValueError("Unexpected error")
    monkeypatch.setattr(FakeSMTP, "quit_error", failure)
    with pytest.raises(type(failure), match=str(failure)):
        mail._send(
            CONFIG, {"to": RECIPIENT, "subject": "Subject", "body": TOKEN}, "test"
        )


def test_accepted_mail_is_not_resent_when_stdlib_smtp_close_has_an_io_failure(
    monkeypatch,
):
    configure(mode="ssl", port=465, username="", password="")
    message_id = enqueue()
    # A real, disconnected stdlib instance exercises __exit__/docmd/close without
    # creating a socket. Only protocol I/O and the accepted send are simulated.
    server = smtplib.SMTP_SSL(local_hostname="extore.test")
    commands, sent, closed = [], [], []
    replies = iter([(250, b"extore.test"), (221, b"Bye")])
    monkeypatch.setattr(server, "putcmd", lambda *args: commands.append(args))
    monkeypatch.setattr(server, "getreply", lambda: next(replies))
    monkeypatch.setattr(
        server, "send_message", lambda *args, **kwargs: sent.append(kwargs) or {}
    )

    class BrokenReader:
        def close(self):
            closed.append(True)
            raise OSError("Synthetic close failure")

    server.file = BrokenReader()
    monkeypatch.setattr(smtplib, "SMTP_SSL", lambda *args, **kwargs: server)
    assert asyncio.run(mail.process_outbox_once()) is True
    current = row(message_id)
    assert current["state"] == "delivered" and current["attempts"] == 1
    assert current["payload"] == current["error"] == ""
    assert [command[0].lower() for command in commands] == ["ehlo", "quit"]
    assert len(sent) == len(closed) == 1
    assert server.file is server.sock is None
    assert asyncio.run(mail.process_outbox_once()) is False
    assert len(sent) == 1


@pytest.mark.parametrize("code", [421, 550])
def test_stdlib_ehlo_rejection_is_classified_before_tls_or_authentication(
    monkeypatch, code
):
    configure()
    message_id = enqueue()
    server = smtplib.SMTP(local_hostname="extore.test")
    commands = []
    replies = iter([(code, b"Rejected"), (221, b"Bye")])
    monkeypatch.setattr(server, "putcmd", lambda *args: commands.append(args))
    monkeypatch.setattr(server, "getreply", lambda: next(replies))
    monkeypatch.setattr(smtplib, "SMTP", lambda *args, **kwargs: server)
    assert asyncio.run(mail.process_outbox_once()) is True
    current = row(message_id)
    assert current["state"] == ("dead" if code == 550 else "pending")
    assert current["attempts"] == 1 and current["error"] == "transport_failed"
    assert [command[0].lower() for command in commands] == ["ehlo", "quit"]


@pytest.mark.parametrize("code", [451, 554, None, True, "550", 200])
def test_post_tls_ehlo_rejection_cannot_proceed_to_credentials(smtp, monkeypatch, code):
    configure()
    message_id = enqueue()

    def ehlo(self):
        self.calls.append(("ehlo",))
        if sum(call[0] == "ehlo" for call in self.calls) == 1:
            return 250, b"OK"
        return code, b"Rejected"

    monkeypatch.setattr(FakeSMTP, "ehlo", ehlo)
    assert asyncio.run(mail.process_outbox_once()) is True
    current = row(message_id)
    assert current["state"] == ("dead" if code == 554 else "pending")
    assert current["error"] == "transport_failed" and current["attempts"] == 1
    assert [entry[0] for entry in smtp] == [
        "connect",
        "ehlo",
        "starttls",
        "ehlo",
        "close",
    ]


@pytest.mark.parametrize(
    ("failure", "quit_code", "state"),
    [
        (smtplib.SMTPDataError(554, b"Rejected"), 421, "dead"),
        (smtplib.SMTPDataError(451, b"Try later"), 550, "pending"),
        (smtplib.SMTPRecipientsRefused({RECIPIENT: (550, b"Rejected")}), 421, "dead"),
        (
            smtplib.SMTPRecipientsRefused({RECIPIENT: (450, b"Try later")}),
            550,
            "pending",
        ),
    ],
)
@pytest.mark.parametrize("io_cleanup", [False, True])
def test_smtp_quit_error_cannot_replace_the_original_send_failure(
    smtp, monkeypatch, failure, quit_code, state, io_cleanup
):
    configure()
    message_id = enqueue()
    monkeypatch.setattr(FakeSMTP, "send_error", failure)
    monkeypatch.setattr(
        FakeSMTP,
        "quit_error",
        OSError("Cleanup failed")
        if io_cleanup
        else smtplib.SMTPResponseException(quit_code, b"QUIT failed"),
    )
    assert asyncio.run(mail.process_outbox_once()) is True
    current = row(message_id)
    assert current["state"] == state and current["attempts"] == 1
    assert current["error"] == (
        "recipient_rejected"
        if isinstance(failure, smtplib.SMTPRecipientsRefused)
        else "transport_failed"
    )
    assert bool(current["payload"]) is (state == "pending")


@pytest.mark.parametrize(
    "failure",
    [smtplib.SMTPResponseException(550, b"QUIT failed"), OSError("Cleanup failed")],
)
def test_smtp_quit_rejection_cannot_replace_a_certificate_failure(
    smtp, monkeypatch, failure
):
    configure()
    message_id = enqueue()

    def fail_tls(self, **kwargs):
        raise ssl.SSLCertVerificationError("Untrusted certificate")

    monkeypatch.setattr(FakeSMTP, "starttls", fail_tls)
    monkeypatch.setattr(FakeSMTP, "quit_error", failure)
    assert asyncio.run(mail.process_outbox_once()) is True
    current = row(message_id)
    assert current["state"] == "pending" and current["error"] == "tls_failed"
    assert not any(call[0] in ("login", "send") for call in smtp)


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
        raise smtplib.SMTPAuthenticationError(454, (PASSWORD + TOKEN).encode())

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


@pytest.mark.parametrize(
    ("failure", "error"),
    [
        (smtplib.SMTPAuthenticationError(535, b"Rejected"), "authentication_failed"),
        (
            smtplib.SMTPSenderRefused(553, b"Invalid sender", CONFIG["sender"]),
            "sender_rejected",
        ),
        (smtplib.SMTPDataError(554, b"5.7.1 Policy rejection"), "transport_failed"),
        (smtplib.SMTPConnectError(554, b"Unavailable"), "transport_failed"),
        (smtplib.SMTPHeloError(501, b"Rejected"), "transport_failed"),
        (smtplib.SMTPResponseException(500, b"Rejected"), "transport_failed"),
        (
            smtplib.SMTPRecipientsRefused({RECIPIENT: (550, b"5.1.1 NoSuchUser")}),
            "recipient_rejected",
        ),
        (
            smtplib.SMTPRecipientsRefused(
                {RECIPIENT: (554, b"5.7.1 Policy rejection")}
            ),
            "recipient_rejected",
        ),
    ],
)
def test_permanent_smtp_failure_stops_only_this_message_and_erases_private_data(
    monkeypatch, failure, error, capsys, caplog
):
    configure()
    message_id = enqueue()

    def fail(*args):
        # Responses can contain recipient addresses, credentials and account links.
        failure.smtp_error = (PASSWORD + RECIPIENT + TOKEN).encode()
        if isinstance(failure, smtplib.SMTPRecipientsRefused):
            failure.recipients = {
                key: (code, failure.smtp_error)
                for key, (code, _) in failure.recipients.items()
            }
        raise failure

    monkeypatch.setattr(mail, "_send", fail)
    assert asyncio.run(mail.process_outbox_once()) is True
    current = row(message_id)
    assert current["state"] == "dead" and current["attempts"] == 1
    assert current["payload"] == "" and current["error"] == error
    assert current["lease_token"] is current["lease_until"] is None
    with db() as c:
        c.execute("UPDATE mail_outbox SET due=0 WHERE id=?", (message_id,))
    assert asyncio.run(mail.process_outbox_once()) is False
    assert row(message_id)["attempts"] == 1

    # A sender/auth/policy rejection must not blacklist the mailbox for other shops.
    next_message = enqueue(shop_id="other-shop")
    sent = []
    monkeypatch.setattr(mail, "_send", lambda *args: sent.append(args))
    assert asyncio.run(mail.process_outbox_once()) is True
    assert row(next_message)["state"] == "delivered"
    assert sent[0][1]["to"] == RECIPIENT
    captured = capsys.readouterr()
    public = json.dumps(current) + caplog.text + captured.out + captured.err
    for secret in (PASSWORD, RECIPIENT, TOKEN):
        assert secret not in public


@pytest.mark.parametrize("code", [550, 554])
def test_refused_recipient_returned_by_smtp_is_not_retried(smtp, monkeypatch, code):
    configure()
    message_id = enqueue()
    monkeypatch.setattr(FakeSMTP, "refused", {RECIPIENT: (code, b"Rejected")})
    assert asyncio.run(mail.process_outbox_once()) is True
    assert row(message_id)["state"] == "dead"
    assert row(message_id)["error"] == "recipient_rejected"
    assert row(message_id)["payload"] == ""
    assert asyncio.run(mail.process_outbox_once()) is False
    assert sum(call[0] == "send" for call in smtp) == 1


@pytest.mark.parametrize(
    ("failure", "error"),
    [
        (smtplib.SMTPAuthenticationError(454, b"Try later"), "authentication_failed"),
        (
            smtplib.SMTPSenderRefused(450, b"Try later", CONFIG["sender"]),
            "sender_rejected",
        ),
        (smtplib.SMTPDataError(451, b"Try later"), "transport_failed"),
        (smtplib.SMTPResponseException(421, b"Try later"), "transport_failed"),
        (
            smtplib.SMTPRecipientsRefused({RECIPIENT: (450, b"Try later")}),
            "recipient_rejected",
        ),
        (
            smtplib.SMTPRecipientsRefused({RECIPIENT: (452, b"Try later")}),
            "recipient_rejected",
        ),
        (smtplib.SMTPServerDisconnected("Disconnected"), "transport_failed"),
        (OSError("Unavailable"), "transport_failed"),
        (ssl.SSLCertVerificationError("Untrusted certificate"), "tls_failed"),
    ],
)
def test_temporary_smtp_failures_keep_encrypted_payload_and_exponential_retry(
    monkeypatch, failure, error
):
    configure()
    message_id = enqueue()
    now = time.time()
    monkeypatch.setattr(mail.time, "time", lambda: now)

    def fail(*args):
        raise failure

    monkeypatch.setattr(mail, "_send", fail)
    for attempt in (1, 2):
        with db() as c:
            c.execute("UPDATE mail_outbox SET due=0 WHERE id=?", (message_id,))
        assert asyncio.run(mail.process_outbox_once()) is True
        current = row(message_id)
        assert current["state"] == "pending" and current["attempts"] == attempt
        assert current["error"] == error
        assert current["payload"].startswith("v1.")
        assert current["due"] == now + 30 * 2**attempt
        assert current["lease_token"] is current["lease_until"] is None


@pytest.mark.parametrize("code", [None, True, False, "550", b"550", 200, 600, 999])
@pytest.mark.parametrize("recipient_response", [False, True])
def test_unknown_or_malformed_smtp_codes_do_not_mean_permanent_rejection(
    monkeypatch, code, recipient_response
):
    configure()
    message_id = enqueue()

    def fail(*args):
        if recipient_response:
            raise smtplib.SMTPRecipientsRefused({RECIPIENT: (code, b"550 Rejected")})
        raise smtplib.SMTPResponseException(code, b"550 Rejected")

    monkeypatch.setattr(mail, "_send", fail)
    assert asyncio.run(mail.process_outbox_once()) is True
    current = row(message_id)
    assert current["state"] == "pending" and current["attempts"] == 1
    assert current["payload"].startswith("v1.")


@pytest.mark.parametrize(
    "responses",
    [
        {},
        None,
        "550 Rejected",
        {RECIPIENT: None},
        {RECIPIENT: 550},
        {RECIPIENT: (550,)},
        {RECIPIENT: (550, b"Rejected", "extra")},
        {RECIPIENT: {"code": 550}},
        {RECIPIENT: (550, b"Rejected"), "other@example.test": (450, b"Try later")},
        {RECIPIENT: (550, b"Rejected"), "other@example.test": ("550", b"Unknown")},
    ],
)
def test_empty_malformed_or_mixed_recipient_responses_keep_bounded_retry(
    monkeypatch, responses
):
    configure()
    message_id = enqueue()

    def fail(*args):
        raise smtplib.SMTPRecipientsRefused(responses)

    monkeypatch.setattr(mail, "_send", fail)
    assert asyncio.run(mail.process_outbox_once()) is True
    current = row(message_id)
    assert current["state"] == "pending" and current["attempts"] == 1
    assert current["error"] == "recipient_rejected"
    assert current["payload"].startswith("v1.")


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


@pytest.mark.parametrize("permanent_failure", [False, True])
def test_stale_worker_cannot_overwrite_a_newer_claim(monkeypatch, permanent_failure):
    configure()
    message_id = enqueue()

    def replace_claim(*args):
        with db() as c:
            c.execute(
                "UPDATE mail_outbox SET attempts=2,lease_token='newer',error='newer worker' WHERE id=?",
                (message_id,),
            )
        if permanent_failure:
            raise smtplib.SMTPDataError(554, b"Rejected")

    monkeypatch.setattr(mail, "_send", replace_claim)
    assert asyncio.run(mail.process_outbox_once()) is True
    current = row(message_id)
    assert current["state"] == "sending" and current["lease_token"] == "newer"
    assert current["attempts"] == 2 and current["error"] == "newer worker"
    assert current["payload"].startswith("v1.")


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
