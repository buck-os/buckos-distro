#!/usr/bin/env python3
"""Convert lockfiles to the format selected in Buck configuration."""

import argparse
import os
import tempfile

from _lockfile import (
    GZIP_SUFFIX,
    PLAIN_SUFFIX,
    configured_compression,
    dump_lockfile,
    load_lockfile,
    strip_lockfile_suffix,
)


def convert(path, compression):
    if compression not in ("none", "gzip"):
        raise ValueError(
            "unsupported lockfile compression {!r}".format(compression)
        )
    source = os.path.abspath(path)
    suffix = GZIP_SUFFIX if compression == "gzip" else PLAIN_SUFFIX
    destination = strip_lockfile_suffix(source) + suffix
    if source != destination and os.path.exists(destination):
        raise ValueError("destination already exists: {}".format(destination))

    value = load_lockfile(source)
    fd, temporary = tempfile.mkstemp(
        prefix=".lockfile-",
        suffix=suffix,
        dir=os.path.dirname(destination),
    )
    os.close(fd)
    try:
        dump_lockfile(value, temporary)
        if load_lockfile(temporary) != value:
            raise ValueError("converted lockfile did not round trip")
        os.replace(temporary, destination)
        if source != destination:
            os.unlink(source)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return destination


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
        destination = convert(path, compression)
        print("{} -> {}".format(path, destination))


if __name__ == "__main__":
    main()
