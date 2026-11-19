"""Collect original license notices from a resolved, locked Cargo graph.

Run cargo metadata --locked --format-version 1 first. This command does not
download dependencies or compile code. It emits exactly THIRD-PARTY-NATIVE.txt
and THIRD-PARTY-NATIVE.json for the distribution's fixed file inventory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import tomllib


def license_files(directory, explicit=None, recursive=False):
    found = set()
    candidates = directory.rglob("*") if recursive else directory.iterdir()
    for path in candidates:
        if not path.is_file():
            continue
        relative = path.relative_to(directory)
        if path.name.upper().startswith(
            ("LICENSE", "LICENCE", "COPYING", "NOTICE", "UNLICENSE")
        ) or any(part.upper() == "LICENSES" for part in relative.parts[:-1]):
            found.add(path)
    if explicit:
        path = Path(explicit)
        if not path.is_absolute():
            path = directory / path
        if path.is_file():
            found.add(path)
    return sorted(found, key=lambda path: path.relative_to(directory).as_posix())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    metadata = json.loads(args.metadata.read_text())
    workspace = Path(__file__).resolve().parents[1]
    repository = workspace.parent.parent
    lock = tomllib.loads((workspace / "Cargo.lock").read_text())
    checksums = {
        (package["name"], package["version"]): package.get("checksum")
        for package in lock["package"]
    }
    nodes = {node["id"]: node for node in metadata["resolve"]["nodes"]}
    resolved = set()
    pending = list(metadata["workspace_members"])
    while pending:
        package_id = pending.pop()
        if package_id in resolved:
            continue
        resolved.add(package_id)
        pending.extend(dependency["pkg"] for dependency in nodes[package_id]["deps"])
    packages = [
        package for package in metadata["packages"]
        if package["id"] in resolved and package["source"] is not None
    ]
    workspace_packages = [
        package for package in metadata["packages"]
        if package["id"] in metadata["workspace_members"]
    ]
    text = [
        "Extore native commerce libraries: third-party license texts\n",
        "Generated from the locked Cargo graph. Platform and build dependencies "
        "may be included even when their code is absent from an artifact.\n\n",
    ]
    inventory = []

    def append_package(name, version, license_id, directory, files,
                       repository_url, checksum):
        if not license_id or not files:
            raise RuntimeError(f"Missing license metadata or original texts: "
                               f"{name} {version}")
        records = []
        for path in files:
            raw = path.read_bytes()
            if len(raw) > 1024 * 1024:
                raise RuntimeError("Unexpectedly large license file")
            relative = path.relative_to(directory).as_posix()
            records.append({"path": relative,
                            "sha256": hashlib.sha256(raw).hexdigest()})
            text.extend([
                f"\n===== {name} {version}: {relative} =====\n",
                raw.decode("utf-8"), "\n",
            ])
        inventory.append({
            "name": name, "version": version, "license": license_id,
            "repository": repository_url, "registry_checksum": checksum,
            "files": records,
        })

    append_package(
        "extore", workspace_packages[0]["version"], "MIT",
        repository, license_files(repository),
        "https://github.com/TokenNotIncluded/extore", None,
    )
    for package in sorted(packages, key=lambda item: (item["name"], item["version"])):
        key = (package["name"], package["version"])
        checksum = checksums.get(key)
        if not checksum:
            raise RuntimeError(f"Package is not pinned by registry checksum: {key}")
        directory = Path(package["manifest_path"]).parent
        append_package(
            *key, package["license"], directory,
            license_files(directory, package.get("license_file"), recursive=True),
            package["repository"], checksum,
        )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    notice = args.output_dir / "THIRD-PARTY-NATIVE.txt"
    manifest = args.output_dir / "THIRD-PARTY-NATIVE.json"
    notice.write_text("".join(text))
    manifest.write_text(json.dumps({
        "schema": "extore.native-notices.v1",
        "cargo_lock_sha256": hashlib.sha256(
            (workspace / "Cargo.lock").read_bytes()).hexdigest(),
        "notice_sha256": hashlib.sha256(notice.read_bytes()).hexdigest(),
        "packages": inventory,
    }, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "packages": len(inventory),
        "notice_sha256": hashlib.sha256(notice.read_bytes()).hexdigest(),
        "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
    }))


if __name__ == "__main__":
    main()
