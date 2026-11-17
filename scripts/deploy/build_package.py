"""Build the exact checked-out main revision as an Arch package for attestation."""

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sha")
    parser.add_argument("output", type=Path)
    parser.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument("--arch-mirror", default="https://geo.mirror.pkgbuild.com")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.sha) or not 1 <= args.jobs <= 8:
        parser.error("Use a full lowercase commit SHA and 1–8 build jobs")
    root = Path(__file__).resolve().parents[2]
    actual = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()
    if actual != args.sha:
        raise RuntimeError(
            "The checked-out source does not match the deployment commit"
        )
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise RuntimeError("The package output directory must be empty")
    archive = output / "extore-source.tar.gz"
    subprocess.run(
        [
            sys.executable,
            str(root / "scripts/source_archive.py"),
            str(archive),
            "--revision",
            args.sha,
        ],
        cwd=root,
        check=True,
    )
    archive_hash = digest(archive)
    # gzip bytes differ across zlib implementations; the deterministic tar
    # content is the source identity independently recomputed by production.
    with gzip.open(archive, "rb") as source:
        source_hash = hashlib.file_digest(source, "sha256").hexdigest()
    recipe = (root / "deploy/arch/PKGBUILD").read_text()
    marker = "sha256sums=('SKIP')"
    if recipe.count(marker) != 1:
        raise RuntimeError("The package source checksum marker is missing or ambiguous")
    (output / "PKGBUILD").write_text(
        recipe.replace(marker, f"sha256sums=('{archive_hash}')")
    )
    tag = f"extore-package-builder:{args.sha[:12]}"
    subprocess.run(
        [
            "docker",
            "build",
            "--build-arg",
            f"ARCH_MIRROR={args.arch_mirror}",
            "--file",
            str(root / "deploy/arch/build.Dockerfile"),
            "--tag",
            tag,
            str(root / "deploy/arch"),
        ],
        cwd=root,
        check=True,
    )
    # The mount contains only this invocation's source archive, recipe and outputs.
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--mount",
            f"type=bind,src={output},dst=/work",
            "--env",
            f"CARGO_BUILD_JOBS={args.jobs}",
            "--env",
            f"EXTORE_OUTPUT_UID={os.getuid()}",
            "--env",
            f"EXTORE_OUTPUT_GID={os.getgid()}",
            tag,
            "bash",
            "-euc",
            "trap 'chown -R \"$EXTORE_OUTPUT_UID:$EXTORE_OUTPUT_GID\" /work' EXIT\n"
            "chown -R builder:builder /work\n"
            "runuser -u builder -- makepkg --noconfirm --cleanbuild --clean\n",
        ],
        cwd=root,
        check=True,
    )
    packages = list(output.glob("extore-*.pkg.tar.zst"))
    if len(packages) != 1:
        raise RuntimeError("Expected exactly one native package")
    package = packages[0]
    record = {
        "sha": args.sha,
        "package_path": str(package),
        "package_sha256": digest(package),
        "source_sha256": source_hash,
        "source_archive_sha256": archive_hash,
        "size": package.stat().st_size,
        "builder_image": subprocess.check_output(
            ["docker", "image", "inspect", "--format={{.Id}}", tag], text=True
        ).strip(),
        "openssl_abi": "3.6.5-1",
    }
    (output / "package.json").write_text(
        json.dumps(record, sort_keys=True, indent=2) + "\n"
    )
    if path := os.environ.get("GITHUB_OUTPUT"):
        with Path(path).open("a") as stream:
            stream.write(f"package_path={package}\nsource_sha256={source_hash}\n")
    print(json.dumps(record, sort_keys=True), flush=True)
    # Only temporary source/working files are removed; retain the attested package
    # and its manifest until the deployment and evidence upload have finished.
    for name in ("src", "pkg"):
        if (output / name).exists():
            shutil.rmtree(output / name)


if __name__ == "__main__":
    main()
