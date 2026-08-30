#!/usr/bin/env python3
"""Compress an image rootfs into a squashfs, using the target's mksquashfs.

Same argument tools/initramfs_build.py makes about dracut, applied to
squashfs-tools: the host's mksquashfs is whatever the build machine happens
to have, and the compressor it was built with is not a detail.  A squashfs
written with a zstd the target's kernel cannot decompress mounts nowhere,
and an image built on one machine and one built on another are not the same
bytes.  So mksquashfs comes from a pinned target buildroot.  Releases whose
packaged tool predates pseudo-file xattrs compile a pinned newer source in
that buildroot before creating the image.

Trips into the sandbox:

  1. in the buildroot, untar the rootfs into the work area
  2. with --selinux-relabel, in the *image*, ask its own matchpathcon what
     each of its paths should be labelled
  3. back in the buildroot, run mksquashfs over that tree
  4. back in the buildroot, delete the tree

Steps 1, 3 and 4 do not enter the unpacked tree the way the initramfs
build enters it.  They do not need to: mksquashfs only ever *reads* the
rootfs, so the tool comes from the buildroot and the tree is just an
argument.  Step 2 is the exception and chroots one level deeper for the
same reason dracut does -- the policy that decides a label belongs to the
distro being built, not to the machine building it.

Step 4 is not tidiness.  The unpacked tree is full of files owned by ids
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

from _deb import fakeroot_command, stage_fakeroot_runtime
from _isolation import (
    ISOLATION_MODES,
    require_target_execution,
    resolve_isolation,
    run_isolated,
)
from _rpm import make_dirs_writable, reproducible_env, scratch_dir

# Names inside the work area.  All three are referenced from shell, so
# they are names rather than expressions built twice.
_ROOTFS = "image.tar"
_ROOT = "root"
_IMAGE = "squashfs.img"
_SOURCE = "squashfs-tools.tar"
_BUILT_MKSQUASHFS = "mksquashfs"


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


# ── SELinux labelling ────────────────────────────────────────────────
#
# An image built here has no security.selinux on anything, because
# setxattr("security.selinux") is EPERM inside a nested user namespace and
# every stage of this build runs in one.  The kernel makes an exception for
# security.capability -- its v3 format records a rootid, so a namespace can
# set one -- and no such exception for labels.  So rpm-plugin-selinux sets
# nothing during the rootfs transaction and mksquashfs has nothing to copy.
#
# The consequence is not a degraded boot but no boot: with a policy shipped
# and nothing labelled, systemd cannot label /run/systemd/units, fails to
# allocate its manager object, and freezes as PID 1.  The image therefore
# used to boot with selinux=0.
#
# The way out is to stop asking the kernel.  Two facts, each checked rather
# than assumed:
#
#   * mksquashfs 4.6.1 takes per-file xattrs in a pseudo-file --
#     `path x name=value` -- and writes them straight into the image's
#     xattr table.  No setxattr(2) is involved, so the namespace never
#     comes into it.  (-xattrs-add is the one that applies a single value
#     to every file; this is the other one.)
#   * Computing what each label should be is a pure lookup against the
#     policy, which needs no privilege at all -- and the image ships its
#     own policy and its own matchpathcon, so the answer comes from the
#     distro being built rather than from the build host.  Same argument
#     initramfs_build.py makes for using the image's own dracut.
#
# 22912 paths take about a second.
_MATCHPATHCON_CANDIDATES = ("/usr/sbin/matchpathcon", "/usr/bin/matchpathcon")

# Where the path list and the answers live inside the work area.
_PATHS = "selinux-paths.txt"
_CONTEXTS = "selinux-contexts.txt"
_PSEUDO = "selinux-pseudo.txt"


def image_paths(rootfs):
    """Every path in the image, from the tarball rather than the tree.

    The tar is the authoritative manifest and reading it costs nothing, so
    the list comes from there rather than from walking the unpacked tree.

    That is not just convenience.  Walking the tree has to happen inside
    the sandbox, where /proc, /sys and /dev are bind-mounted into the
    chroot -- a plain `find /` there picks up the *host's* procfs and
    returns 298000 paths for a 22912-file image.  Pruning them is possible
    and fragile; not generating them is neither.
    """
    import tarfile

    paths = []
    with tarfile.open(rootfs) as tar:
        for member in tar:
            name = member.name
            # `tar -C root .` names the root directory ".", which is the
            # one member that is not a path *inside* the image.  Left
            # alone it becomes "/." -- root again, spelled so the guard
            # below does not recognise it -- and the pseudo-file then
            # carries an xattr line for a file mksquashfs does not think
            # exists.  4.6.1 accepted that line; 4.7.4, which Fedora 45
            # ships, fails the whole image with
            #
            #   FATAL ERROR: File "." does not exist, can not add Pseudo
            #   xattr to it.
            #
            # so the normalisation is the fix rather than a tidy-up.
            if name in (".", "./"):
                name = "/"
            elif name.startswith("./"):
                name = name[1:]
            elif not name.startswith("/"):
                name = "/" + name
            if name not in ("/", "/."):
                paths.append(name)
    return paths


def _matchpathcon_script(work):
    """Ask the image what each of its own paths should be labelled."""
    paths = os.path.join(work, _PATHS)
    out = os.path.join(work, _CONTEXTS)
    return "\n".join([
        "set -e",
        _resolve("MATCHPATHCON", _MATCHPATHCON_CANDIDATES),
        # -d '\n' so a filename containing a space is one argument.  The
        # image has none today, but a path list is exactly the place where
        # assuming that quietly mislabels a file instead of failing.
        "xargs -a {} -d '\\n' -n 2000 \"$MATCHPATHCON\" > {}".format(
            shlex.quote(paths), shlex.quote(out)
        ),
        "test -s {}".format(shlex.quote(out)),
    ])


def write_pseudo(contexts, pseudo):
    """Turn matchpathcon's answers into mksquashfs pseudo-file lines.

    Returns (written, skipped).  A path containing whitespace or a
    backslash is skipped rather than guessed at: the pseudo grammar splits
    on spaces and gives the backslash meaning of its own, so emitting one
    would label some other path, and mislabelling is worse than leaving a
    file unlabelled.  Pseudo-file paths are relative to the source tree;
    an absolute path is rejected by some mksquashfs releases.  Two systemd
    unit files with escaped names currently land here --
    `system-systemd\\x2dcryptsetup.slice` and its veritysetup sibling --
    and both are unit files that never execute.
    """
    written = skipped = 0
    with open(contexts) as src, open(pseudo, "w") as dst:
        for line in src:
            path, _, context = line.rstrip("\n").partition("\t")
            if not path or path in (".", "/", "/.") or not context:
                continue
            if any(char in path for char in " \t\\"):
                skipped += 1
                continue
            pseudo_path = path.lstrip("/")
            if not pseudo_path:
                continue
            dst.write("{} x security.selinux={}\n".format(pseudo_path, context))
            written += 1
    return written, skipped


def _build_mksquashfs_script(source, work, output):
    """Build a current mksquashfs with the target buildroot's compiler."""
    source_tree = os.path.join(work, "squashfs-tools-source")
    build_dir = os.path.join(source_tree, "squashfs-tools")
    return "\n".join([
        "set -e",
        "rm -rf {}".format(shlex.quote(source_tree)),
        "mkdir -p {}".format(shlex.quote(source_tree)),
        "tar -xf {} -C {} --strip-components=1".format(
            shlex.quote(source), shlex.quote(source_tree)
        ),
        "make -C {} CONFIG=1 GZIP_SUPPORT=0 ZSTD_SUPPORT=1 "
        "COMP_DEFAULT=zstd XATTR_SUPPORT=1 USE_PREBUILT_MANPAGES=1 "
        "EXTRA_CFLAGS='-g0 -ffile-prefix-map={}=/usr/src/squashfs-tools' "
        "mksquashfs".format(shlex.quote(build_dir), work),
        "cp {} {}".format(
            shlex.quote(os.path.join(build_dir, "mksquashfs")),
            shlex.quote(output),
        ),
        "chmod 0755 {}".format(shlex.quote(output)),
    ])


def _mksquashfs_script(args, root, image, pseudo=None, mksquashfs=None):
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
    if pseudo:
        cmd += ["-pf", pseudo]
    # -e consumes everything after it, so it goes last.
    if args.exclude:
        cmd.append("-e")
        cmd += args.exclude

    resolve = (
        "MKSQUASHFS={}\ntest -x \"$MKSQUASHFS\"".format(shlex.quote(mksquashfs))
        if mksquashfs
        else _resolve("MKSQUASHFS", _MKSQUASHFS_CANDIDATES)
    )
    return "\n".join([
        "set -e",
        resolve,
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
    ap.add_argument("--mksquashfs-source", default=None,
                    help="source archive to compile when the packaged "
                         "mksquashfs lacks pseudo-file xattrs")
    ap.add_argument("--isolation", default="auto", choices=ISOLATION_MODES)
    ap.add_argument("--compressor", default="zstd",
                    help="mksquashfs -comp")
    ap.add_argument("--block-size", default="",
                    help="mksquashfs -b; its default if empty")
    ap.add_argument("--processors", default="",
                    help="mksquashfs -processors; its default if empty")
    ap.add_argument("--exclude", action="append", default=[], metavar="PATH",
                    help="path to omit, relative to the rootfs (repeatable)")
    ap.add_argument("--selinux-relabel", action="store_true",
                    help="write security.selinux into the image, computed "
                         "from the image's own policy")
    ap.add_argument("--work", default=None,
                    help="scratch directory; a temp dir is used if omitted")
    ap.add_argument("--keep-work", action="store_true",
                    help="do not delete the scratch area, for debugging")
    ap.add_argument("--source-date-epoch", default="1700000000")
    ap.add_argument("--target-cpu", default="x86_64")
    args = ap.parse_args()

    require_target_execution(args.target_cpu)
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


def _relabel(args, isolation, rootfs, root, work, env):
    """Compute the image's SELinux labels and return a pseudo-file for them.

    The lookup runs with the *unpacked image* as /, not the buildroot: the
    policy that decides these labels is the image's own, and so is the
    matchpathcon that reads it.  Building an image with the build host's
    policy would be the same mistake as building it with the host's dracut.
    """
    paths = image_paths(rootfs)
    listing = os.path.join(work, _PATHS)
    with open(listing, "w") as handle:
        handle.write("\n".join(paths) + "\n")

    print(
        "buckos-distro: labelling {} paths with the image's own "
        "policy".format(len(paths)),
        file=sys.stderr,
        flush=True,
    )
    run_isolated(
        ["/bin/sh", "-c", _matchpathcon_script(work)],
        isolation, work, work, root, env=env,
    )

    pseudo = os.path.join(work, _PSEUDO)
    written, skipped = write_pseudo(os.path.join(work, _CONTEXTS), pseudo)
    print(
        "buckos-distro: {} labels, {} paths skipped as unquotable".format(
            written, skipped
        ),
        file=sys.stderr,
        flush=True,
    )
    if not written:
        sys.exit(
            "--selinux-relabel produced no labels. The image has to ship a "
            "policy (selinux-policy-targeted) and matchpathcon "
            "(libselinux-utils) for its own contexts to be readable."
        )
    return pseudo


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
    env["FAKEROOTDONTTRYCHOWN"] = "1"
    fakeroot = stage_fakeroot_runtime(sysroot, work, required=False)

    mksquashfs = None
    if args.mksquashfs_source:
        source = os.path.join(work, _SOURCE)
        shutil.copy2(os.path.abspath(args.mksquashfs_source), source)
        mksquashfs = os.path.join(work, _BUILT_MKSQUASHFS)
        print(
            "buckos-distro: building mksquashfs with pseudo-file xattr support",
            file=sys.stderr,
            flush=True,
        )
        run_isolated(
            ["/bin/sh", "-c", _build_mksquashfs_script(source, work, mksquashfs)],
            isolation, work, work, sysroot, env=env,
        )

    staged = stage_rootfs(rootfs, work)

    print("buckos-distro: unpacking the image to compress it",
          file=sys.stderr, flush=True)
    run_isolated(
        fakeroot_command(
            fakeroot,
            ["/bin/sh", "-c", _unpack_script(staged, root)],
        ),
        isolation, work, work, sysroot, env=env,
    )

    pseudo = None
    if args.selinux_relabel:
        pseudo = _relabel(args, isolation, rootfs, root, work, env)

    print(
        "buckos-distro: mksquashfs (comp={}, exclude={})".format(
            args.compressor, ",".join(args.exclude) or "-"
        ),
        file=sys.stderr,
        flush=True,
    )
    try:
        run_isolated(
            fakeroot_command(
                fakeroot,
                ["/bin/sh", "-c", _mksquashfs_script(
                    args, root, image, pseudo, mksquashfs
                )],
                load=True,
            ),
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
