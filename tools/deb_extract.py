#!/usr/bin/env python3
"""Unpack one binary Debian package into an installroot."""

import argparse
import glob
import os
import shutil
import sys

from _deb import compatible_binary_version, deb_fields, extract_deb, source_identity
from _rpm import make_dirs_writable


def select_deb(
    deb_dir: str,
    package: str,
    architecture: str = "",
    source_name: str = "",
    source_version: str = "",
) -> str:
    package_candidates = []
    available = []
    patterns = ("*.deb", "*.ddeb")
    for pattern in patterns:
        for path in sorted(glob.glob(os.path.join(deb_dir, pattern))):
            fields = deb_fields(path)
            name = fields["Package"]
            available.append("{}:{}={}".format(
                name,
                fields["Architecture"],
                fields["Version"],
            ))
            if name == package:
                package_candidates.append((path, fields))

    if not package_candidates:
        raise ValueError(
            "no deb for binary package {!r} in {}\navailable: {}".format(
                package, deb_dir, ", ".join(sorted(available)) or "(none)",
            )
        )

    candidates = package_candidates
    if architecture:
        candidates = [item for item in candidates if item[1]["Architecture"] == architecture]
        if not candidates:
            found = sorted({item[1]["Architecture"] for item in package_candidates})
            raise ValueError(
                "wrong architecture for {!r}: expected {}, got {}".format(
                    package, architecture, ", ".join(found),
                )
            )

    if source_name:
        matching = []
        found = []
        for item in candidates:
            actual_name, actual_version = source_identity(item[1])
            found.append("{}={}".format(actual_name, actual_version))
            if actual_name == source_name:
                matching.append(item)
        candidates = matching
        if not candidates:
            raise ValueError(
                "wrong source for {!r}: expected {}, got {}".format(
                    package, source_name, ", ".join(sorted(set(found))),
                )
            )

    if source_version:
        matching = []
        found = []
        for item in candidates:
            _actual_name, actual_source_version = source_identity(item[1])
            found.append(actual_source_version)
            if compatible_binary_version(actual_source_version, source_version):
                matching.append(item)
        candidates = matching
        if not candidates:
            raise ValueError(
                "incompatible version for {!r}: expected source version {}, got {}".format(
                    package, source_version, ", ".join(sorted(set(found))),
                )
            )

    if len(candidates) > 1:
        raise ValueError(
            "ambiguous deb for {!r}: {}".format(
                package,
                ", ".join(os.path.basename(path) for path, _fields in candidates),
            )
        )
    return candidates[0][0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--deb", default=None)
    parser.add_argument("--deb-dir", default=None)
    parser.add_argument("--select", default=None)
    parser.add_argument("--architecture", default="")
    parser.add_argument("--source-name", default="")
    parser.add_argument("--source-version", default="")
    parser.add_argument("--out", default=None)
    parser.add_argument("--out-deb", default=None)
    args = parser.parse_args()

    if bool(args.deb) == bool(args.deb_dir):
        sys.exit("pass exactly one of --deb or --deb-dir")
    if args.deb_dir and not args.select:
        sys.exit("--deb-dir requires --select")
    if not args.out and not args.out_deb:
        sys.exit("pass --out, --out-deb, or both")

    try:
        if args.deb:
            directory = os.path.dirname(os.path.abspath(args.deb))
            path = select_deb(
                directory,
                args.select or deb_fields(args.deb)["Package"],
                args.architecture,
                args.source_name,
                args.source_version,
            )
            if os.path.abspath(path) != os.path.abspath(args.deb):
                sys.exit("selected a different package than --deb: {}".format(path))
        else:
            path = select_deb(
                args.deb_dir,
                args.select,
                args.architecture,
                args.source_name,
                args.source_version,
            )
    except ValueError as error:
        sys.exit(str(error))

    if args.out_deb:
        out_deb = os.path.abspath(args.out_deb)
        os.makedirs(os.path.dirname(out_deb), exist_ok=True)
        shutil.copyfile(path, out_deb)
        print(
            "buckos-distro: selected {}".format(os.path.basename(path)),
            file=sys.stderr,
        )

    if args.out:
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
