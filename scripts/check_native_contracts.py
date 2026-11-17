"""Run reproducible contracts against independent native Rust HTTP processes.

The Python modules prepare isolated fixtures and inspect persistence. Python
monkeypatches cannot change Rust process clocks, constants, or implementation
functions; those exclusions are explicit below and have separate native checks.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

GROUPS = {
    "commerce": (
        [
            "test_commerce_oauth.py",
            "test_commerce_listing.py",
            "test_commerce_schema.py",
            "test_marketplace_product.py",
            "test_commerce_recovery_key.py",
        ],
        # The legacy SDK bridge substitutes an in-process Python transport.
        ["-k", "not sdk"],
    ),
    "catalog": (
        [
            "test_product_variants.py",
            "test_card_variants.py",
            "test_text_stock_http.py",
            "test_card_batches.py",
            "test_product_lifecycle.py",
            "test_product_purges.py",
            "test_product_links.py",
        ],
        # These race fixtures patch the Python service before its DB transaction.
        # Native authentication executes under BEGIN IMMEDIATE; revocation is
        # additionally covered by native signed-device and long-poll tests.
        ["-k", "not ancestor_revocation_before_management_transaction"],
    ),
    "metadata": (
        [
            "test_progress_board.py",
            "test_task_flow_schema_http.py",
            "test_processor_contributions.py",
        ],
        [
            "--deselect",
            "tests/test_progress_board.py::test_safety_caps_reject_instead_of_silently_truncating[products]",
            "--deselect",
            "tests/test_progress_board.py::test_safety_caps_reject_instead_of_silently_truncating[workers]",
        ],
    ),
    "scopes": (
        ["test_scope_auth.py", "test_scope_auth_security.py"],
        # These three legacy fixtures patch Python's clock or lower process-local
        # request caps. They cannot alter the separate native service.
        [
            "-k",
            "not scope_clock_bounds_and_retired_nonce_cannot_create_a_new_request and not scope_request_total_cap_is_enforced_without_creating_business_rows and not scope_request_key_cap_counts_approved_but_releases_denied_requests",
        ],
    ),
    "files": (
        [
            "test_files.py",
            "test_tenant_files.py",
            "test_rich_files.py",
            "test_processor_profiles.py",
            "test_processor_workflow_profiles.py",
        ],
        # Python-only lowered constants, worker calls, or positive blob-read spies.
        [
            "-k",
            "not upload_limits_are_atomic and not chunked_multipart_stream and not collection_upload_keeps_per_card_quota and not processor_defines_real_steps and not shop_owners_list_and_download_only_their_files and not customer_receipt_cannot_download_another_shop_output",
        ],
    ),
    "fulfillment": (
        [
            "test_redemption.py",
            "test_structured_outputs.py",
            "test_batch_redemption.py",
            "test_batch_partial.py",
            "test_retry_modes.py",
            "test_task_outcomes.py",
            "test_card_entitlements.py",
            "test_progress_steps.py",
        ],
        # Direct Python models/SDKs, injected failures, worker implementations,
        # fake quotas, and old-version Python schema setup have separate gates.
        [
            "-k",
            "not sdk and not script and not exception_after_mutation and not file_binding_is_rolled_back and not render_errors and not revision_text_uses_quota and not capacity_is and not revision_policy_rejects and not review_requires_reason and not review_does_not_include and not cli_link_quota and not invalid_product_plans and not invalid_support_email and not product_defaults and not completed_steps_are and not retry_request_does_not and not schema21_upgrade",
        ],
    ),
    "automation": (
        ["test_automation_api.py"],
        [
            "-k",
            "not wait_rechecks_revocation_without_holding and not wait_claims_new_arrival_without",
        ],
    ),
    "flow": (
        ["test_task_flow_http_integration.py"],
        # In-process ASGI adapters, Python fake clocks, and injected finalizers.
        [
            "-k",
            "not real_http_aladdin and not question_timeout and not processing_timeout and not finalizer_duplicate",
        ],
    ),
    "native": (
        [
            "test_native_admin_cli.py",
            "test_native_processor_worker.py",
            "test_native_automation_poll.py",
            "test_native_private_worker.py",
            "test_native_board_bounds.py",
        ],
        [],
    ),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", action="append", choices=GROUPS)
    parser.add_argument(
        "--build",
        action="store_true",
        help="Run locked Rust unit tests and build first",
    )
    parser.add_argument("--output", type=Path, default=Path("output/native-contracts"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output if args.output.is_absolute() else root / args.output
    output.mkdir(parents=True, exist_ok=True)
    if args.build:
        for command in (["cargo", "test", "--locked"], ["cargo", "build", "--locked"]):
            subprocess.run(command, cwd=root, check=True)
    if not (root / "target/debug/extore").is_file():
        parser.error("Build the native server first, or pass --build")
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(root / "tests"), str(root))))
    results = []
    for name in args.group or GROUPS:
        files, filters = GROUPS[name]
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "native_http_plugin",
            *(f"tests/{f}" for f in files),
            *filters,
            "-q",
            "--tb=short",
        ]
        start = time.monotonic()
        log_path = output / f"{name}.log"
        with log_path.open("w") as log:
            result = subprocess.run(
                command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT
            )
        summary = log_path.read_text().splitlines()[-1:]
        results.append(
            {
                "group": name,
                "command": command,
                "exit_code": result.returncode,
                "seconds": round(time.monotonic() - start, 2),
                "summary": summary,
                "log": str(log_path.relative_to(output)),
            }
        )
        (output / "results.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2) + "\n"
        )
        print(f"{name}: {summary[0] if summary else result.returncode}", flush=True)
    return int(any(item["exit_code"] for item in results))


if __name__ == "__main__":
    raise SystemExit(main())
