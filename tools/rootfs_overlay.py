#!/usr/bin/env python3
"""Append deterministic files to a rootfs tar archive."""

import argparse
import io
import os
import shutil
import tarfile


def parse_file(value):
    destination, separator, remainder = value.partition(":")
    mode, separator2, source = remainder.partition(":")
    if not separator or not separator2 or not destination or not source:
        raise argparse.ArgumentTypeError("expected DESTINATION:MODE:SOURCE")
    destination = destination.lstrip("/")
    if not destination or destination == ".." or destination.startswith("../"):
        raise argparse.ArgumentTypeError("destination must stay inside the rootfs")
    try:
        parsed_mode = int(mode, 8)
    except ValueError as error:
        raise argparse.ArgumentTypeError("mode must be octal") from error
    return destination, parsed_mode, source


def append_file(archive, destination, mode, source, mtime):
    with open(source, "rb") as stream:
        data = stream.read()
    info = tarfile.TarInfo(destination)
    info.size = len(data)
    info.mode = mode
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = mtime
    archive.addfile(info, io.BytesIO(data))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rootfs", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--file", action="append", default=[], type=parse_file)
    parser.add_argument("--source-date-epoch", default="1700000000", type=int)
    args = parser.parse_args()

    out = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    shutil.copyfile(args.rootfs, out)
    with tarfile.open(out, "a") as archive:
        for destination, mode, source in sorted(args.file):
            append_file(
                archive,
                destination,
                mode,
                source,
                args.source_date_epoch,
            )


if __name__ == "__main__":
    main()
