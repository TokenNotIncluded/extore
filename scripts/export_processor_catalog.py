#!/usr/bin/env python3
"""Refresh or verify the Rust metadata snapshot from the pinned reviewed processor package."""

import argparse
import importlib
import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "processors/official"))
catalog = importlib.import_module("extore_processors").catalog

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "--check",
    action="store_true",
    help="fail if the checked-in Rust catalog has drifted",
)
args = parser.parse_args()
target = root / "src/catalog/processor-catalog.json"
expected = json.dumps(catalog(), ensure_ascii=False, indent=2) + "\n"
if args.check:
    if not target.is_file() or target.read_text() != expected:
        raise SystemExit(
            "Rust processor catalog is stale; run scripts/export_processor_catalog.py"
        )
    print("Rust processor catalog matches the pinned reviewed package.")
else:
    target.write_text(expected)
    print("Updated src/catalog/processor-catalog.json")
