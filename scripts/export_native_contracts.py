"""Refresh checked-in public contracts used by the native service.

The compatibility SDK supplies the public protocol definitions at development
time. The production Rust process embeds these JSON assets and imports no Python.
"""

import json
import os
import sys
import tempfile
import tomllib
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
with tempfile.TemporaryDirectory(prefix="extore-contract-export-") as data:
    os.environ["EXTORE_DATA"] = data
    os.environ["EXTORE_ORIGIN"] = "https://extore.lmm.best"
    from extore.app import app
    from extore.processor_contributions import contribution_contract
    from extore.task_flow_schema import definition_schema

    directory = root / "src/api"
    directory.mkdir(parents=True, exist_ok=True)
    document = app.openapi()
    document["info"]["version"] = tomllib.loads((root / "Cargo.toml").read_text())[
        "package"
    ]["version"]
    document["info"]["x-runtime"] = "rust"
    exports = {
        "openapi.json": document,
        "task-flow-schema.json": definition_schema(),
        "processor-contributions.json": contribution_contract(),
    }
    for filename, document in exports.items():
        target = directory / filename
        target.write_text(
            json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        )
        print(filename, target.stat().st_size)
