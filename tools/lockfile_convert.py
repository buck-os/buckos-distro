#!/usr/bin/env python3
"""Convert lockfiles to the format selected in Buck configuration."""

import argparse

from _lockfile import (
    configured_compression,
    convert_lockfile,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lockfiles", nargs="+")
    parser.add_argument(
        "--compression",
        choices=("none", "gzip"),
        default=None,
        help="override [buckos.lockfiles] compression",
    )
    args = parser.parse_args(argv)
    compression = args.compression or configured_compression()
    for path in args.lockfiles:
        destination = convert_lockfile(path, compression)
        print("{} -> {}".format(path, destination))


if __name__ == "__main__":
    main()
