import os
import tempfile

os.environ["EXTORE_DATA"] = tempfile.mkdtemp(prefix="extore-tests-")
os.environ["EXTORE_ORIGIN"] = "http://localhost:8000"
import pytest
from fastapi.testclient import TestClient
from starlette.responses import Response

from extore.app import app
from extore.db import db
from extore.security import create_session


@pytest.fixture(autouse=True)
def clean():
    from extore.db import init

    init()
    with db() as c:
        for table in (
            "mail_outbox",
            "account_tokens",
            "shop_integration_keys",
            "api_requests",
            "outbox",
            "events",
            "audit",
            "callback_nonces",
            "rate_limits",
            "challenges",
            "credentials",
            "sessions",
            "owner_cli_action_challenges",
            "owner_cli_password_challenges",
            "owner_cli_approval_challenges",
            "owner_cli_challenges",
            "owner_cli_requests",
            "owner_cli_devices",
            "cli_challenges",
            "cli_bind_tickets",
            "cli_devices",
            "upload_reservations",
            "staff",
            "receipt_batch_cards",
            "receipt_batches",
            "grants",
            "processor_card_bindings",
            "processor_product_bindings",
            "processor_profile_revisions",
            "processor_profiles",
            "jobs",
            "cards",
            "products",
            "shops",
            "settings",
        ):
            c.execute("DELETE FROM " + table)


@pytest.fixture
def client():
    with TestClient(
        app,
        base_url="http://localhost:8000",
        headers={"Origin": "http://localhost:8000"},
    ) as client:
        yield client


@pytest.fixture
def owner(client):
    with db() as c:
        value = create_session(c, Response(), "admin")
    client.cookies.set("extore_session", value)
    return client


@pytest.fixture
def setup_product(owner):
    def create(**kwargs):
        config = {
            "name": "测试商品",
            "parameters": [
                {
                    "key": "email",
                    "label": {"zh-CN": "邮箱", "en": "Email"},
                    "type": "email",
                    "description": {"zh-CN": "**请确认邮箱**"},
                }
            ],
            **kwargs,
        }
        r = owner.post("/api/admin/products", json=config)
        assert r.status_code == 200, r.text
        pid = r.json()["id"]
        r = owner.post("/api/admin/cards", json={"product_id": pid, "count": 1})
        return pid, r.json()["codes"][0]

    return create
