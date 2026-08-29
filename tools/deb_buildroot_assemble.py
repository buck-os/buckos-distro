#!/usr/bin/env python3
"""Assemble a Debian buildroot from SHA-256-pinned binary packages."""

import argparse
import os
import shutil

from _deb import ensure_base_files, extract_deb, register_debs
from _rpm import make_dirs_writable


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--deb", action="append", default=[])
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    out = os.path.abspath(args.out)
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    ensure_base_files(out)

    for deb in args.deb:
        extract_deb(deb, out)
        make_dirs_writable(out)

    ensure_base_files(out)
    register_debs(args.deb, out)


if __name__ == "__main__":
    main()
