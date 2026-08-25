#!/usr/bin/env python3
"""Compress an image rootfs into a squashfs, using the target's mksquashfs.

Same argument tools/initramfs_build.py makes about dracut, applied to
squashfs-tools: the host's mksquashfs is whatever the build machine
happens to have, and the compressor it was built with is not a detail.  A
squashfs written with a zstd the target's kernel cannot decompress mounts
nowhere, and an image built on one machine and one built on another are
not the same bytes.  So mksquashfs comes out of the image-tools buildroot,
which is pinned by the lockfile like everything else.

Two trips into the sandbox rather than one, plus a third to clean up:

  1. in the buildroot, untar the rootfs into the work area
  2. still in the buildroot, run mksquashfs over that tree
  3. back in the buildroot, delete the tree

Step 2 does not enter the unpacked tree the way the initramfs build enters
it.  It does not need to: mksquashfs only ever *reads* the rootfs, so the
tool comes from the buildroot and the tree is just an argument.  dracut is
the opposite -- it runs the image's own binaries against the image's own
modules -- which is why that build chroots one level deeper.

Step 3 is not tidiness.  The unpacked tree is full of files owned by ids
from this user's subordinate range -- ids that exist only inside the
namespace -- so it can only be deleted from in there.  The same reasoning,
and the same failure if skipped, as initramfs_build.py's cleanup.

The ownership round-trip is worth stating plainly, because it is what
makes the image correct.  tar restores uid 12 as namespace-uid 12, which
is a subordinate id on disk; mksquashfs, running in a namespace with the
same mapping, stats it back as 12 and records 12 in the squashfs.  The
mapping is deterministic, so the numbers that came out of the rpm payload
are the numbers that end up in the image.

There is no LiveOS/ directory inside the result, and that is a decision --
see the comment on the ext4 alternative in defs/rules/image.bzl.  dracut's
dmsquash-live module treats a squashfs without one as the root filesystem
itself, which is the only variant an unprivileged build can produce: the
alternative needs a sized ext4 image and a loop mount.
"""

import argparse
import os
import shlex
import shutil
import sys

from _isolation import ISOLATION_MODES, resolve_isolation, run_isolated
from _rpm import make_dirs_writable, reproducible_env, scratch_dir

# Names inside the work area.  All three are referenced from shell, so
# they are names rather than expressions built twice.
_ROOTFS = "image.tar"
_ROOT = "root"
_IMAGE = "squashfs.img"


def stage_rootfs(rootfs, work):
    """Put the image tarball somewhere the sandbox can actually see it.

    Buck hands us the tarball under buck-out/v2/gen, and nothing mounts
    that inside the chroot -- only the work area is bind-mounted, at its
    own absolute path.  Hardlinked so a half-gigabyte image costs a
    directory entry; see stage_rootfs in tools/initramfs_build.py, which
    solves the identical problem and explains the fallback.
    """
    dest = os.path.join(work, _ROOTFS)
    try:
        os.link(rootfs, dest)
    except OSError:
        shutil.copy2(rootfs, dest)
    return dest


def _unpack_script(rootfs, root):
    """Untar the image, in the buildroot, as the namespace's root."""
    return "\n".join([
        "set -e",
        "mkdir -p {}".format(shlex.quote(root)),
        # --numeric-owner: the buildroot's /etc/passwd is not the image's,
        # so resolving names here would map the image's users through the
        # wrong table.  The numbers in the archive are the truth, and they
        # are what mksquashfs reads back in the next step.
        #
        # No --same-owner needed: inside the namespace we are root, which
        # is when GNU tar restores ownership by default.
        "tar -xf {} -C {} --numeric-owner".format(
            shlex.quote(rootfs), shlex.quote(root)
        ),
        # A sanity check with a useful message, rather than letting
        # mksquashfs cheerfully compress an empty directory.
        "test -d {}/usr".format(shlex.quote(root)),
    ])


# Fedora 42 merged /usr/sbin into /usr/bin, so where mksquashfs lives
# depends on which release's buildroot this is -- and PATH cannot settle
# it, because run() replaces the environment wholesale and the PATH that
# survives is the host's, not the sysroot's.  Resolved in the script, so
# the answer comes from the tree actually mounted at /.
_MKSQUASHFS_CANDIDATES = ("/usr/sbin/mksquashfs", "/usr/bin/mksquashfs")


def _resolve(var, candidates):
    """Shell that sets `var` to the first candidate present, or fails."""
    return "\n".join([
        "{}=".format(var),
        'for _c in {}; do'.format(" ".join(shlex.quote(c) for c in candidates)),
        '  if [ -x "$_c" ]; then {}="$_c"; break; fi'.format(var),
        "done",
        'if [ -z "${}" ]; then'.format(var),
        '  echo "buckos-distro: none of {} in the buildroot" >&2'.format(
            " ".join(candidates)
        ),
        "  exit 1",
        "fi",
    ])


def _mksquashfs_script(args, root, image):
    cmd = [
        "$MKSQUASHFS",
        root,
        image,
        # Overwrite rather than append.  Without it a rerun that finds a
        # stale image adds a second copy of the tree to it.
        "-noappend",
        "-comp", args.compressor,
        # An action's stdout is captured and replayed on failure, so a
        # progress bar is thousands of lines of carriage returns in a log
        # nobody can read.
        "-no-progress",
        # Recovery files are written next to the *output*, which here is
        # the work area, and are pure noise for a one-shot build.
        "-no-recovery",
        # The NFS export table is dead weight for an image that is only
        # ever loop-mounted, and it is keyed on inode numbers, which is
        # one more thing that would have to be stable for the bytes to be.
        "-no-exports",
        # No -mkfs-time here, deliberately.  The timestamp that would
        # otherwise be "now" is pinned by SOURCE_DATE_EPOCH in the
        # environment (see reproducible_env), and mksquashfs treats being
        # given both as a conflict rather than a duplicate:
        #
        #   FATAL ERROR: SOURCE_DATE_EPOCH and command line options can't
        #                be used at the same time to set timestamp(s)
        #
        # The env var is the half worth keeping.  -mkfs-time sets only the
        # filesystem creation time, while SOURCE_DATE_EPOCH sets that *and*
        # clamps every file mtime in the image, which is what the build
        # actually needs to be reproducible.
    ]
    if args.block_size:
        cmd += ["-b", args.block_size]
    if args.processors:
        cmd += ["-processors", args.processors]
    # -e consumes everything after it, so it goes last.
    if args.exclude:
        cmd.append("-e")
        cmd += args.exclude

    return "\n".join([
        "set -e",
        _resolve("MKSQUASHFS", _MKSQUASHFS_CANDIDATES),
        # $MKSQUASHFS is the one word left unquoted, on purpose: it is a
        # path this script just resolved, not caller input.
        " ".join(
            part if part == "$MKSQUASHFS" else shlex.quote(part)
            for part in cmd
        ),
        "test -s {}".format(shlex.quote(image)),
    ])


def _cleanup_script(root):
    """Remove the unpacked tree from inside the namespace that owns it."""
    return "set -e\nrm -rf {}".format(shlex.quote(root))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rootfs", required=True,
                    help="image rootfs tarball to compress")
    ap.add_argument("--out", required=True, help="squashfs image to write")
    ap.add_argument("--buildroot-tree", default=None,
                    help="tree providing mksquashfs")
    ap.add_argument("--isolation", default="auto", choices=ISOLATION_MODES)
    ap.add_argument("--compressor", default="zstd",
                    help="mksquashfs -comp")
    ap.add_argument("--block-size", default="",
                    help="mksquashfs -b; its default if empty")
    ap.add_argument("--processors", default="",
                    help="mksquashfs -processors; its default if empty")
    ap.add_argument("--exclude", action="append", default=[], metavar="PATH",
                    help="path to omit, relative to the rootfs (repeatable)")
    ap.add_argument("--work", default=None,
                    help="scratch directory; a temp dir is used if omitted")
    ap.add_argument("--keep-work", action="store_true",
                    help="do not delete the scratch area, for debugging")
    ap.add_argument("--source-date-epoch", default="1700000000")
    args = ap.parse_args()

    isolation = resolve_isolation(args.isolation)
    if isolation == "none":
        sys.exit(
            "squashfs needs a sandbox: unpacking a rootfs restores files "
            "owned by ids this user does not have, and isolation=none "
            "would silently write them all as the build user instead"
        )
    if not args.buildroot_tree:
        sys.exit(
            "--buildroot-tree is required: mksquashfs comes from the "
            "image-tools buildroot, not from the host"
        )

    rootfs = os.path.abspath(args.rootfs)

    # Same reasoning as the replay and the initramfs build: a relative
    # --work would resolve against the action's cwd, which is the project
    # root, so two concurrent actions would write into the source tree.
    if args.work:
        work = os.path.abspath(args.work)
        shutil.rmtree(work, ignore_errors=True)
        os.makedirs(work, exist_ok=True)
    else:
        work = scratch_dir("buckos-distro-squashfs-")

    out = os.path.abspath(args.out)
    try:
        _build(args, isolation, rootfs, work, out)
    finally:
        if not args.keep_work and not args.work:
            # Best effort: the tree is removed from inside the namespace by
            # step 3, and anything surviving that is owned by an id we
            # cannot touch out here.  Failing the build over litter in
            # /var/tmp would hide whatever actually went wrong.
            shutil.rmtree(work, ignore_errors=True)


def _build(args, isolation, rootfs, work, out):
    root = os.path.join(work, _ROOT)
    image = os.path.join(work, _IMAGE)

    # A private, writable copy of the buildroot, for the same reason the
    # rootfs install makes one: the buildroot is a Buck input artifact that
    # other actions are reading concurrently, and entering it directly
    # means writing to a shared input.
    sysroot = os.path.join(work, "sysroot")
    shutil.copytree(args.buildroot_tree, sysroot, symlinks=True,
                    dirs_exist_ok=True)
    make_dirs_writable(sysroot)

    env = reproducible_env(source_date_epoch=args.source_date_epoch)

    staged = stage_rootfs(rootfs, work)

    print("buckos-distro: unpacking the image to compress it",
          file=sys.stderr, flush=True)
    run_isolated(
        ["/bin/sh", "-c", _unpack_script(staged, root)],
        isolation, work, work, sysroot, env=env,
    )

    print(
        "buckos-distro: mksquashfs (comp={}, exclude={})".format(
            args.compressor, ",".join(args.exclude) or "-"
        ),
        file=sys.stderr,
        flush=True,
    )
    try:
        run_isolated(
            ["/bin/sh", "-c", _mksquashfs_script(args, root, image)],
            isolation, work, work, sysroot, env=env,
        )
    finally:
        # In a finally so a mksquashfs failure still leaves the work area
        # deletable; without this a broken build needs manual cleanup with
        # ids the user does not have.
        run_isolated(
            ["/bin/sh", "-c", _cleanup_script(root)],
            isolation, work, work, sysroot, env=env,
        )

    if not os.path.isfile(image):
        sys.exit("mksquashfs produced no image at {}".format(image))

    # Moved rather than written in place, so a failed run leaves no
    # half-written output where Buck expects a finished one.
    os.makedirs(os.path.dirname(out), exist_ok=True)
    try:
        os.rename(image, out)
    except OSError:
        shutil.move(image, out)

    print(
        "buckos-distro: squashfs -> {} ({} bytes)".format(
            os.path.basename(out), os.path.getsize(out)
        ),
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
