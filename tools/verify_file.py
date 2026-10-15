#!/usr/bin/env python3
"""Copy a declared file only after checking its pinned SHA-256 digest."""

import argparse
import hashlib
from pathlib import Path
import shutil


def verify_copy(source, destination, sha256):
    digest = hashlib.sha256()
    with open(source, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual != sha256:
        raise ValueError("SHA-256 mismatch for {}: expected {}, got {}".format(source, sha256, actual))
    Path(destination).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args()
    try:
        verify_copy(args.source, args.out, args.sha256)
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
