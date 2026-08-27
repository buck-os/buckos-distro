#!/usr/bin/env python3
"""Unpack one binary Debian package into an installroot."""

import argparse
import glob
import os
import shutil
import sys

from _deb import deb_field, extract_deb
from _rpm import make_dirs_writable


def select_deb(deb_dir, package):
    candidates = []
    available = []
    patterns = ("*.deb", "*.ddeb")
    for pattern in patterns:
        for path in sorted(glob.glob(os.path.join(deb_dir, pattern))):
            name = deb_field(path, "Package")
            available.append(name)
            if name == package:
                candidates.append(path)

    if not candidates:
        sys.exit(
            "no deb for binary package {!r} in {}\navailable: {}".format(
                package, deb_dir, ", ".join(sorted(available)) or "(none)"
            )
        )
    if len(candidates) > 1:
        sys.exit(
            "ambiguous deb for {!r}: {}".format(
                package, ", ".join(os.path.basename(path) for path in candidates)
            )
        )
    return candidates[0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--deb", default=None)
    parser.add_argument("--deb-dir", default=None)
    parser.add_argument("--select", default=None)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    if bool(args.deb) == bool(args.deb_dir):
        sys.exit("pass exactly one of --deb or --deb-dir")
    if args.deb_dir and not args.select:
        sys.exit("--deb-dir requires --select")

    path = args.deb or select_deb(args.deb_dir, args.select)
    out = os.path.abspath(args.out)
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    extract_deb(path, out)
    make_dirs_writable(out)
    print(
        "buckos-distro: unpacked {} -> {}".format(os.path.basename(path), args.out),
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
