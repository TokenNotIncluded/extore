"""Runtime schema errors must not be treated as empty data or unused quota."""

import sqlite3
from contextlib import contextmanager

import pytest

from extore import card_entitlements, files, storage, text_cards, worker
from extore.db import db


@pytest.mark.parametrize(
    "read",
    [
        text_cards.allocated_bytes,
        storage._flow_allocated,
        card_entitlements.allocated_bytes,
        lambda c: card_entitlements.frozen_policy(c, "missing-card"),
        lambda c: card_entitlements.last_delivery(
            c, {"id": "missing-job", "state": "queued"}
        ),
        lambda c: card_entitlements.has_previous_delivery(c, {"id": "missing-job"}),
        lambda c: card_entitlements.select_delivery(
            c, {"id": "missing-job", "state": "queued"}
        ),
        lambda c: files._with_flow_scope(c, {"id": "missing-file"}),
    ],
    ids=[
        "text-quota",
        "flow-quota",
        "revision-quota",
        "policy",
        "last-delivery",
        "previous-delivery",
        "select-delivery",
        "file-scope",
    ],
)
def test_runtime_readers_reject_missing_schema(read):
    with sqlite3.connect(":memory:") as connection:
        connection.row_factory = sqlite3.Row
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            read(connection)


def test_initialized_empty_database_keeps_normal_empty_results():
    with db() as connection:
        assert text_cards.allocated_bytes(connection) == 0
        assert storage._flow_allocated(connection) == 0
        assert card_entitlements.allocated_bytes(connection) == 0
        assert card_entitlements.frozen_policy(connection, "missing-card") is None
        row = {"id": "missing-job", "state": "queued"}
        assert card_entitlements.last_delivery(connection, row) is None
        assert not card_entitlements.has_previous_delivery(connection, row)


def test_worker_maintenance_rejects_missing_schema(monkeypatch):
    @contextmanager
    def uninitialized_db():
        with sqlite3.connect(":memory:") as connection:
            yield connection

    monkeypatch.setattr(worker, "db", uninitialized_db)
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        worker.automation_maintenance_once()
