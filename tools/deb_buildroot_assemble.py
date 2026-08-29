#!/usr/bin/env python3
"""Assemble a Debian buildroot from SHA-256-pinned binary packages."""

import argparse
import glob
import os
import shutil

from _deb import extract_deb, register_debs
from _rpm import make_dirs_writable


SKELETON = (
    "builddir",
    "dev",
    "etc",
    "proc",
    "sys",
    "tmp",
    "var/lib/dpkg/info",
    "var/tmp",
)

def ensure_base_files(root):
    for rel in SKELETON:
        path = os.path.join(root, rel)
        os.makedirs(path, exist_ok=True)
    os.chmod(os.path.join(root, "tmp"), 0o1777)
    os.chmod(os.path.join(root, "var", "tmp"), 0o1777)

    info_format = os.path.join(root, "var", "lib", "dpkg", "info", "format")
    if not os.path.exists(info_format):
        with open(info_format, "w", encoding="utf-8") as stream:
            stream.write("1\n")

    for name, target in (
        ("bin", "usr/bin"),
        ("sbin", "usr/sbin"),
        ("lib", "usr/lib"),
        ("lib64", "usr/lib64"),
    ):
        path = os.path.join(root, name)
        target_path = os.path.join(root, target)
        if not os.path.lexists(path) and os.path.exists(target_path):
            os.symlink(target, path)

    passwd = os.path.join(root, "etc", "passwd")
    if not os.path.exists(passwd):
        with open(passwd, "w", encoding="utf-8") as stream:
            stream.write("root:x:0:0:root:/root:/bin/bash\n")
    group = os.path.join(root, "etc", "group")
    if not os.path.exists(group):
        with open(group, "w", encoding="utf-8") as stream:
            stream.write("root:x:0:\n")

    bindir = os.path.join(root, "usr", "bin")
    for name in ("aclocal", "automake"):
        link = os.path.join(bindir, name)
        candidates = sorted(glob.glob(link + "-*"))
        if not os.path.lexists(link) and len(candidates) == 1:
            os.symlink(os.path.basename(candidates[0]), link)


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
