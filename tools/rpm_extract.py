#!/usr/bin/env python3
"""Unpack a binary rpm into an installroot.

Two modes:

    --rpm PATH              unpack exactly this rpm (prebuilt_rpm)
    --rpm-dir DIR --select N  pick the rpm for binary package N out of a
                            directory of rpms and unpack it (rpm_subpackage)

The --rpm-dir form exists because an srpm_build produces every subpackage
in one action.  The whole directory is a Buck input (RE materializes only
declared inputs), and the selection happens here.

No rpmdb, no scriptlets: rpm2archive | tar, so this needs no root and no
database state.  A package whose %post matters cannot be satisfied this
way -- see SPEC.md section 7.
"""

import argparse
import glob
import os
import shutil
import sys

from _rpm import extract_rpm


def select_rpm(rpm_dir, name):
    """Find the rpm file for binary package `name` in `rpm_dir`.

    rpm filenames are NAME-VERSION-RELEASE.ARCH.rpm, so a plain prefix
    match is wrong: selecting "zlib" would also match "zlib-devel".  The
    character after the name must begin the version field, i.e. it is a
    hyphen followed by a digit.
    """
    candidates = []
    for path in sorted(glob.glob(os.path.join(rpm_dir, "*.rpm"))):
        base = os.path.basename(path)
        if base.endswith(".src.rpm"):
            continue
        rest = base[len(name):]
        if not base.startswith(name) or not rest.startswith("-"):
            continue
        if len(rest) > 1 and rest[1].isdigit():
            candidates.append(path)

    if not candidates:
        available = sorted(
            os.path.basename(p) for p in glob.glob(os.path.join(rpm_dir, "*.rpm"))
        )
        sys.exit(
            "no rpm for binary package {!r} in {}\navailable: {}".format(
                name, rpm_dir, ", ".join(available) or "(none)"
            )
        )
    if len(candidates) > 1:
        sys.exit(
            "ambiguous rpm for {!r}: {}".format(
                name, ", ".join(os.path.basename(p) for p in candidates)
            )
        )
    return candidates[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpm", default=None, help="a single rpm to unpack")
    ap.add_argument("--rpm-dir", default=None,
                    help="directory of rpms to select from")
    ap.add_argument("--select", default=None,
                    help="binary package name to pick out of --rpm-dir")
    ap.add_argument("--out", required=True, help="installroot to create")
    args = ap.parse_args()

    if bool(args.rpm) == bool(args.rpm_dir):
        sys.exit("pass exactly one of --rpm or --rpm-dir")
    if args.rpm_dir and not args.select:
        sys.exit("--rpm-dir requires --select")

    rpm_path = args.rpm or select_rpm(args.rpm_dir, args.select)

    out = os.path.abspath(args.out)
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out, exist_ok=True)
    extract_rpm(rpm_path, out)

    print(
        "buckos-distro: unpacked {} -> {}".format(
            os.path.basename(rpm_path), args.out
        ),
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
