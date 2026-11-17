"""Exercise real board limits without changing another process's constants."""

import json
import time
import uuid

import pytest
from test_progress_board import BOARD, PRIVATE, create, seed, snapshot

from extore.db import db


@pytest.mark.parametrize("bound", ["products", "workers"])
def test_real_board_safety_cap_rejects_oversized_projection(owner, bound):
    pid, sid = create(owner)
    if bound == "products":
        with db() as connection:
            connection.executemany(
                "INSERT INTO products(id,config,created,shop_id) VALUES (?,?,?,?)",
                [
                    (str(uuid.uuid4()), json.dumps({"name": PRIVATE}), time.time(), sid)
                    for _ in range(500)
                ],
            )
    else:
        for index in range(501):
            seed(pid, "processing", f"operator-{index}")
    before = snapshot()
    response = owner.get(BOARD, params={"shop_id": sid})
    assert response.status_code == 409, response.text
    assert PRIVATE not in response.text
    assert snapshot() == before
