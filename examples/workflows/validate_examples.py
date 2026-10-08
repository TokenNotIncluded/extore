"""Validate the checked-in examples against the server's canonical contract."""

import json
from pathlib import Path

from extore.task_flow_definition import validate_definition


def main(root=Path(__file__).parent):
    definitions = sorted(root.glob("*/flow.json"))
    if not definitions:
        raise ValueError("No workflow examples found")
    for path in definitions:
        directory = path.parent
        definition = json.loads(path.read_text(encoding="utf-8"))
        product = json.loads((directory / "product.json").read_text(encoding="utf-8"))
        canonical = validate_definition(definition, product)
        assert canonical is not None
        assert validate_definition(canonical, product) == canonical
        print(f"{directory.name}: valid ({len(canonical['nodes'])} nodes)")


if __name__ == "__main__":
    main()
