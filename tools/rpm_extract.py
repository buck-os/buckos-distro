#!/usr/bin/env python3
"""Pick one binary rpm out of a build, and either unpack it or hand it over.

Selecting:

    --rpm PATH                this exact rpm (prebuilt_rpm)
    --rpm-dir DIR --select N  the rpm for binary package N, out of a
                              directory of them (rpm_subpackage, built_rpm)

The --rpm-dir form exists because an srpm_build produces every subpackage
in one action.  The whole directory is a Buck input (RE materializes only
declared inputs), and the selection happens here.

Delivering:

    --out DIR                 unpack it into an installroot
    --out-rpm PATH            copy the .rpm itself

Two outputs because the two consumers want different things from the same
selection.  A *build* dependency wants the unpacked tree, to be overlaid
into a buildroot.  A *rootfs* wants the rpm file, because it runs a real
rpm transaction -- database, scriptlets, triggers -- and an unpacked tree
cannot be installed, only copied over.  Keeping both behind one selection
is the point: `select_rpm` is the rule that says a package named "zlib"
is not the file "zlib-devel-...rpm", and two copies of that rule would
disagree eventually.

No rpmdb, no scriptlets *here*: rpm2archive | tar, so unpacking needs no
root and no database state.  A package whose %post matters cannot be
satisfied that way -- but it can be satisfied by --out-rpm, because what
consumes that does run the transaction.  See SPEC.md section 7.
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
    ap.add_argument("--out", default=None, help="installroot to create")
    ap.add_argument("--out-rpm", default=None,
                    help="copy the selected .rpm here instead of unpacking")
    args = ap.parse_args()

    if bool(args.rpm) == bool(args.rpm_dir):
        sys.exit("pass exactly one of --rpm or --rpm-dir")
    if args.rpm_dir and not args.select:
        sys.exit("--rpm-dir requires --select")
    if bool(args.out) == bool(args.out_rpm):
        sys.exit("pass exactly one of --out or --out-rpm")

    rpm_path = args.rpm or select_rpm(args.rpm_dir, args.select)

    if args.out_rpm:
        out = os.path.abspath(args.out_rpm)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        # copyfile, not copy2: the mtime of an rpm inside a Buck output is
        # whatever the build wrote, and carrying it forward would make this
        # output depend on something the content hash already covers.
        shutil.copyfile(rpm_path, out)
        print(
            "buckos-distro: selected {}".format(os.path.basename(rpm_path)),
            file=sys.stderr,
        )
        return

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
