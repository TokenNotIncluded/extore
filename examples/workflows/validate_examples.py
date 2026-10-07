"""Validate the checked-in examples against the server's canonical contract."""

import json
from pathlib import Path

from extore.task_flow_definition import validate_definition


def main():
    root = Path(__file__).parent
    for directory in sorted(root.iterdir()):
        if not directory.is_dir():
            continue
        definition = json.loads((directory / "flow.json").read_text(encoding="utf-8"))
        product = json.loads((directory / "product.json").read_text(encoding="utf-8"))
        canonical = validate_definition(definition, product)
        assert canonical is not None
        assert validate_definition(canonical, product) == canonical
        print(f"{directory.name}: valid ({len(canonical['nodes'])} nodes)")


if __name__ == "__main__":
    main()
