"""Independent hostile-input checks for encrypted merchant workflow settings."""

import json

import pytest
from test_processor_profiles import merchant

from extore.db import db
from extore.processor_profiles import _open_revision

PROFILE_API = "/api/admin/processor-profiles"
SECRET = "independent-workflow-private-sentinel"


@pytest.fixture
def merchant_client():
    client, _ = merchant()
    yield client
    client.close()


def create_profile(client, workflow=None):
    response = client.post(
        PROFILE_API,
        json={
            "processor_id": "resource_link",
            "name": "Independent synthetic profile",
            "configuration": {
                "resource_url": "https://example.test/private-config-sentinel"
            },
            "workflow": workflow
            if workflow is not None
            else {
                "variables": {"BRAND": "Synthetic brand"},
                "secrets": {"ACCESS": SECRET},
            },
        },
    )
    assert response.status_code == 200, response.text
    assert SECRET not in response.text
    return response.json()


def state(profile_id):
    with db() as c:
        return {
            "profile": dict(
                c.execute(
                    "SELECT * FROM processor_profiles WHERE id=?", (profile_id,)
                ).fetchone()
            ),
            "revisions": [
                dict(row)
                for row in c.execute(
                    "SELECT * FROM processor_profile_revisions WHERE profile_id=? "
                    "ORDER BY revision",
                    (profile_id,),
                )
            ],
            "audit": [
                dict(row)
                for row in c.execute(
                    "SELECT * FROM audit WHERE target=? ORDER BY id", (profile_id,)
                )
            ],
        }


def reject_without_mutation(client, profile_id, workflow):
    before = state(profile_id)
    response = client.put(
        f"{PROFILE_API}/{profile_id}",
        json={"name": "Must not partially apply", "workflow": workflow},
    )
    assert response.status_code == 422, response.text
    assert SECRET not in response.text
    assert state(profile_id) == before


@pytest.mark.parametrize(
    "workflow",
    [
        {"variables": {"BRAND": "replace"}, "delete_variables": ["BRAND"]},
        {"secrets": {"ACCESS": "replace"}, "delete_secrets": ["ACCESS"]},
        {"variables": {"ACCESS": "must not reveal old secret"}},
        {"delete_secrets": ["ACCESS", "ACCESS"]},
        {"delete_variables": ["LD_PRELOAD"]},
        None,
        {"variables": None},
        {"secrets": None},
        {"runtime": None},
        {"variables": {"BRAND": None}},
        {"secrets": {"ACCESS": None}},
    ],
)
def test_invalid_merge_never_changes_revision_name_or_audit(merchant_client, workflow):
    profile = create_profile(merchant_client)
    reject_without_mutation(merchant_client, profile["id"], workflow)


@pytest.mark.parametrize("name", ["PATH", "PYTHONPATH", "LD_PRELOAD", "HTTP_PROXY"])
def test_blank_secret_placeholder_cannot_hide_reserved_input_name(
    merchant_client, name
):
    profile = create_profile(merchant_client)
    reject_without_mutation(merchant_client, profile["id"], {"secrets": {name: ""}})


def test_blank_secret_placeholders_cannot_bypass_input_collection_limit(
    merchant_client,
):
    profile = create_profile(merchant_client)
    reject_without_mutation(
        merchant_client,
        profile["id"],
        {"secrets": {f"ITEM_{index}": "" for index in range(65)}},
    )


def test_merged_collection_limit_checks_existing_values(merchant_client):
    profile = create_profile(
        merchant_client,
        {"variables": {f"ITEM_{index}": "x" for index in range(64)}},
    )
    reject_without_mutation(
        merchant_client, profile["id"], {"variables": {"ONE_MORE": "x"}}
    )


def test_merged_byte_budget_checks_values_from_both_collections(merchant_client):
    profile = create_profile(
        merchant_client,
        {
            "variables": {f"VALUE_{index}": "x" * 8192 for index in range(4)},
            "secrets": {f"SECRET_{index}": "x" * 8192 for index in range(4)},
        },
    )
    reject_without_mutation(
        merchant_client, profile["id"], {"variables": {"ONE_MORE_BYTE": "x"}}
    )


def test_value_budget_counts_utf8_bytes(merchant_client):
    profile = create_profile(merchant_client)
    reject_without_mutation(
        merchant_client, profile["id"], {"variables": {"MULTIBYTE": "汉" * 2731}}
    )


def test_json_escape_expansion_is_rejected_before_sealing_or_partial_update(
    merchant_client,
):
    profile = create_profile(merchant_client)
    # Valid individual/raw UTF-8 sizes, but JSON escaping exceeds the encrypted
    # store's 200,000-byte limit. The request remains below the HTTP body limit.
    workflow = {"variables": {f"VALUE_{index}": "\x01" * 8192 for index in range(5)}}
    request_bytes = json.dumps(
        {"name": "Must not partially apply", "workflow": workflow}
    ).encode()
    assert 200000 < len(request_bytes) < 256000
    reject_without_mutation(merchant_client, profile["id"], workflow)


def test_blank_public_value_and_blank_existing_secret_have_distinct_semantics(
    merchant_client,
):
    profile = create_profile(merchant_client)
    response = merchant_client.put(
        f"{PROFILE_API}/{profile['id']}",
        json={
            "workflow": {
                "variables": {"BRAND": ""},
                "secrets": {"ACCESS": " \t"},
                "runtime": {"timeout_seconds": 10},
            }
        },
    )
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["workflow"]["variables"] == {"BRAND": ""}
    assert value["workflow"]["configured_secret_names"] == ["ACCESS"]
    assert value["workflow"]["runtime"] == {
        "timeout_seconds": 10,
        "memory_mb": 256,
        "cpu_seconds": 120,
        "max_output_bytes": 1000000,
    }
    assert SECRET not in response.text
    with db() as c:
        row = c.execute(
            "SELECT * FROM processor_profiles WHERE id=?", (profile["id"],)
        ).fetchone()
        _, stored = _open_revision(c, row, row["revision"], include_workflow=True)
        assert stored["secrets"]["ACCESS"] == SECRET
    deleted = merchant_client.put(
        f"{PROFILE_API}/{profile['id']}",
        json={"workflow": {"delete_secrets": ["ACCESS"]}},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["workflow"]["configured_secret_names"] == []
    assert deleted.json()["workflow"]["variables"] == {"BRAND": ""}


@pytest.mark.parametrize(
    "workflow",
    [
        {"command": ["/bin/sh", "-c", "true"]},
        {"image": "arbitrary-image:latest"},
        {"mounts": ["/:/host"]},
        {"network": "host"},
        {"runtime": {"network": "host"}},
        {"runtime": {"timeout_seconds": True}},
    ],
)
def test_merchant_cannot_set_execution_authority(merchant_client, workflow):
    profile = create_profile(merchant_client)
    reject_without_mutation(merchant_client, profile["id"], workflow)
