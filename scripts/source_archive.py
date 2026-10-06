"""Produce a reproducible deployment archive including pinned Git submodules."""

import argparse
import gzip
import io
import subprocess
import tarfile
from pathlib import Path


def git(directory, *args):
    return subprocess.check_output(["git", "-C", str(directory), *args])


def collect(repository, revision, prefix, target):
    raw = git(repository, "archive", "--format=tar", f"--prefix={prefix}", revision)
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        for member in archive:
            target.addfile(
                member, archive.extractfile(member) if member.isfile() else None
            )
    entries = git(repository, "ls-tree", "-rz", revision).split(b"\0")
    for entry in entries:
        if not entry:
            continue
        metadata, path_bytes = entry.split(b"\t", 1)
        mode, _, commit = metadata.decode().split()
        if mode != "160000":
            continue
        path = path_bytes.decode()
        checkout = repository / path
        if not checkout.is_dir():
            raise RuntimeError(f"Initialize the pinned submodule first: {path}")
        actual = git(checkout, "rev-parse", "HEAD").decode().strip()
        if actual != commit:
            raise RuntimeError(
                f"Submodule does not match the selected source revision: {path}"
            )
        collect(checkout, commit, prefix + path + "/", target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--revision", default="HEAD")
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[1]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("wb") as file:
        with gzip.GzipFile(fileobj=file, mode="wb", filename="", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w|") as archive:
                collect(repository, args.revision, "extore-source/", archive)


if __name__ == "__main__":
    main()
