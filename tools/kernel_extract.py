#!/usr/bin/env python3
"""Lift the kernel out of a rootfs tarball.

A bootloader needs vmlinuz as a file it can read; the rootfs is a tar.
This is the whole job, and it is deliberately its own rule rather than a
step inside the ISO action.

Doing it separately is what keeps the expensive part of the pipeline from
depending on the cheap part.  Extracting the kernel needs no privilege, no
namespace and no distro tooling -- it is a read of the tar index and a copy
of one member -- so it stays a plain, cacheable, RE-eligible action.  The
initramfs, by contrast, has to unpack the whole image and run dracut inside
it.  Fusing them would make a bootloader-config change re-run dracut.

It also emits the kernel version, because everything downstream needs it:
dracut takes it as --kver, the modules live under it, and the bootloader's
menu entry names it.  Reading it out of the path once, here, means nothing
else has to parse a directory name.
"""

import argparse
import os
import sys

from _image import extract_member, find_kernel


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rootfs", required=True,
                    help="the image rootfs tarball to read")
    ap.add_argument("--out", required=True,
                    help="where to write vmlinuz")
    ap.add_argument("--out-kver", default=None,
                    help="where to write the kernel version string")
    ap.add_argument("--kver", default=None,
                    help="which kernel, when the image has more than one")
    args = ap.parse_args()

    kver, member = find_kernel(args.rootfs, args.kver)

    out = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    extract_member(args.rootfs, member, out)

    if args.out_kver:
        kver_out = os.path.abspath(args.out_kver)
        os.makedirs(os.path.dirname(kver_out), exist_ok=True)
        # No trailing newline: this file is read straight into a command
        # line as --kver, and a stray newline there produces a module
        # directory that does not exist.
        with open(kver_out, "w") as fh:
            fh.write(kver)

    print("buckos-distro: kernel {} -> {} ({} bytes)".format(
        kver, out, os.path.getsize(out)), file=sys.stderr)


if __name__ == "__main__":
    main()
