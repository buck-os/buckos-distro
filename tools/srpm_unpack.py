#!/usr/bin/env python3
"""Unpack a source RPM into an rpmbuild topdir layout.

A .src.rpm payload is a flat bag of files: the .spec plus every Source
and Patch. rpmbuild wants them split into SPECS/ and SOURCES/, so we
unpack flat and then sort.

Output layout:
    <out>/SPECS/<name>.spec
    <out>/SOURCES/*            (tarballs, patches, sysusers files, ...)
    <out>/BUILD/  <out>/BUILDROOT/  <out>/RPMS/  <out>/SRPMS/   (empty)
"""

import argparse
import os
import shutil
import sys

from _rpm import extract_rpm, require_tool

TOPDIR_SUBDIRS = ("BUILD", "BUILDROOT", "RPMS", "SOURCES", "SPECS", "SRPMS")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--srpm", required=True, help="path to the .src.rpm")
    ap.add_argument("--out", required=True, help="topdir to create")
    ap.add_argument(
        "--expect-spec",
        default=None,
        help="fail unless this spec basename is present (guards against "
        "a mirror serving the wrong package)",
    )
    args = ap.parse_args()

    require_tool("rpm2archive")
    require_tool("tar")

    out = os.path.abspath(args.out)
    staging = os.path.join(out, ".flat")
    for sub in TOPDIR_SUBDIRS:
        os.makedirs(os.path.join(out, sub), exist_ok=True)
    os.makedirs(staging, exist_ok=True)

    extract_rpm(args.srpm, staging)

    specs = []
    for entry in sorted(os.listdir(staging)):
        src = os.path.join(staging, entry)
        if not os.path.isfile(src):
            continue
        target_dir = "SPECS" if entry.endswith(".spec") else "SOURCES"
        if entry.endswith(".spec"):
            specs.append(entry)
        shutil.move(src, os.path.join(out, target_dir, entry))

    shutil.rmtree(staging, ignore_errors=True)

    if not specs:
        sys.exit("no .spec file found in {}".format(args.srpm))
    if len(specs) > 1:
        # Legal but rare. Ambiguity here silently builds the wrong thing.
        sys.exit(
            "{} contains multiple spec files ({}); pass --expect-spec to "
            "disambiguate".format(args.srpm, ", ".join(specs))
        )
    if args.expect_spec and specs[0] != args.expect_spec:
        sys.exit(
            "expected spec '{}' but {} contains '{}'".format(
                args.expect_spec, args.srpm, specs[0]
            )
        )

    print("unpacked {} -> SPECS/{}".format(args.srpm, specs[0]), file=sys.stderr)


if __name__ == "__main__":
    main()
