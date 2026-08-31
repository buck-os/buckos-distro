#!/usr/bin/env python3
"""Verify and unpack one Debian source package into a source tree."""

import argparse
import os
import shutil
import sys

from _deb import dsc_files, parse_control, require_tool, run, sha256_file
from _rpm import scratch_dir


def validate_sources(dsc, source_paths, source_name=None, source_version=None):
    with open(dsc, encoding="utf-8") as stream:
        fields = parse_control(stream.read())
    actual_name = fields.get("Source", fields.get("Package"))
    actual_version = fields.get("Version")
    if source_name and actual_name != source_name:
        raise ValueError(
            ".dsc source name mismatch: expected {}, got {}".format(
                source_name, actual_name or "<missing>",
            )
        )
    if source_version and actual_version != source_version:
        raise ValueError(
            ".dsc source version mismatch: expected {}, got {}".format(
                source_version, actual_version or "<missing>",
            )
        )
    expected = dsc_files(fields)

    provided = {}
    for path in source_paths:
        name = os.path.basename(path)
        if name in provided:
            raise ValueError("duplicate source input basename: {!r}".format(name))
        provided[name] = path

    missing = sorted(set(expected) - set(provided))
    extra = sorted(set(provided) - set(expected))
    if missing or extra:
        details = []
        if missing:
            details.append("missing: {}".format(", ".join(missing)))
        if extra:
            details.append("unexpected: {}".format(", ".join(extra)))
        raise ValueError(".dsc source inputs do not match manifest ({})".format("; ".join(details)))

    for name, (digest, size) in sorted(expected.items()):
        path = provided[name]
        actual_size = os.path.getsize(path)
        if actual_size != size:
            raise ValueError(
                "{}: size mismatch: expected {}, got {}".format(name, size, actual_size)
            )
        actual_digest = sha256_file(path)
        if actual_digest != digest:
            raise ValueError(
                "{}: SHA-256 mismatch: expected {}, got {}".format(
                    name, digest, actual_digest
                )
            )
    return provided


def archive_source_tree(source, destination, source_date_epoch):
    """Archive every source node into one Buck-representable artifact."""
    run([
        require_tool("tar"),
        "--create",
        "--format=posix",
        "--sort=name",
        "--numeric-owner",
        "--owner=0",
        "--group=0",
        "--mtime=@{}".format(source_date_epoch),
        "--pax-option=delete=atime,delete=ctime",
        "--file", destination,
        "--directory", source,
        ".",
    ])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsc", required=True)
    parser.add_argument("--file", action="append", default=[])
    parser.add_argument("--out", required=True)
    parser.add_argument("--source-name", required=True)
    parser.add_argument("--source-version", required=True)
    parser.add_argument("--source-date-epoch", default="1700000000")
    args = parser.parse_args()

    try:
        provided = validate_sources(
            args.dsc,
            args.file,
            source_name=args.source_name,
            source_version=args.source_version,
        )
    except ValueError as exc:
        sys.exit("buckos-distro: {}".format(exc))

    work = scratch_dir("buckos-dsc-unpack-", key=os.path.abspath(args.out))
    staged = os.path.join(work, "source")
    unpacked = os.path.join(work, "unpacked")
    os.makedirs(staged)

    dsc_name = os.path.basename(args.dsc)
    shutil.copy2(args.dsc, os.path.join(staged, dsc_name))
    for name, path in sorted(provided.items()):
        shutil.copy2(path, os.path.join(staged, name))

    run([
        require_tool("dpkg-source"),
        "--no-check",
        "--extract",
        os.path.join(staged, dsc_name),
        unpacked,
    ])

    out = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    archive_source_tree(unpacked, out, args.source_date_epoch)
    print(
        "buckos-distro: unpacked {} -> {}".format(dsc_name, args.out),
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
