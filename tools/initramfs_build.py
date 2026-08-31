#!/usr/bin/env python3
"""Build an initramfs from an image rootfs, using that image's own dracut.

The initramfs is not part of the rootfs, and keeping it out is a decision
tools/rootfs_install.py makes on purpose: kernel-core's %posttrans would
have run kernel-install -> dracut inside the rpm transaction, and it is
bypassed there with KERNEL_INSTALL_BYPASS=1 for three reasons -- it cannot
work (dracut wants /proc/cmdline and a writable tmpdir the transaction does
not have), its output is derived data that would bloat the rootfs tarball,
and it has inputs of its own that deserve to be visible to Buck.  This is
where it happens instead, as a rule with those inputs declared.

Built with the *target's* dracut, not the host's.  That is the same
argument the replay makes about rpm: the host's dracut has the host's
modules, the host's version, and the host's idea of what a kernel needs.
An initramfs for Fedora 45's kernel has to be built by Fedora 45's
dracut, so the image rootfs becomes / and dracut runs inside it.

Three trips into the sandbox rather than one, because the root dracut needs
does not exist until something has unpacked it:

  1. in the buildroot, untar the rootfs into the work area
  2. in *that unpacked tree*, run dracut
  3. back in the buildroot, delete the tree

Step 3 is not tidiness.  The unpacked tree is full of files owned by ids
from this user's subordinate range -- ids that exist only inside the
namespace -- so it can only be deleted from in there.  Left behind, it is
litter the build cannot remove; worse, under a Buck output path it is an
artifact Buck can hash and cannot clean.  Same reasoning as
defs/rules/rootfs.bzl's tarball, arrived at from the other direction.

--no-hostonly is not optional, and it is the one flag worth being loud
about.  dracut defaults to host-only: it inspects the running machine and
includes drivers for *that* hardware.  An image built that way boots on the
build machine and hangs on anything else, with no error -- which is the
worst possible failure for an installer ISO, because it looks like the
image is fine and the target machine is broken.
"""

import argparse
import os
import shlex
import shutil
import sys

from _deb import fakeroot_command, stage_fakeroot_runtime
from _image import find_kernel
from _isolation import (
    ISOLATION_MODES,
    require_target_execution,
    resolve_isolation,
    run_isolated,
    sandbox_path,
)
from _rpm import make_dirs_writable, reproducible_env, scratch_dir

# Where the image tarball, the tree unpacked from it, and the finished
# initramfs live inside the work area.  All three are referenced from
# shell, so they are names rather than expressions built twice.
_ROOTFS = "image.tar"
_ROOT = "root"
_IMAGE = "initramfs.img"


def stage_image_tool(buildroot, work, name):
    """Stage one binary-seeded construction tool outside the image root.

    Debian's source-built coreutils cp cannot preserve ownership on symlinks
    in the user-namespace bind mount used here, even under fakeroot.  The
    binary-seeded buildroot's cp can, and image construction tools are meant
    to come from that buildroot rather than become rootfs payload.  The work
    directory is bind-mounted inside the image at _isolation.SANDBOX_WORK, so
    putting the binary there makes it available without modifying the image;
    address it with sandbox_path() at the point it is handed to a script.
    """
    source = os.path.join(buildroot, "usr", "bin", name)
    if not os.path.isfile(source):
        sys.exit(
            "image-tools buildroot has no /usr/bin/{} for initramfs "
            "construction".format(name)
        )
    directory = os.path.join(work, "image-tools-bin")
    os.makedirs(directory, exist_ok=True)
    destination = os.path.join(directory, name)
    shutil.copy2(source, destination)
    return destination


def _install_image_tool_script(tool, root, name):
    """Replace a tool only in the ephemeral tree used by the generator."""
    destination = os.path.join(root, "usr", "bin", name)
    return "{} --preserve=mode,timestamps {} {}".format(
        shlex.quote(tool),
        shlex.quote(tool),
        shlex.quote(destination),
    )


def stage_rootfs(rootfs, work):
    """Put the image tarball somewhere the sandbox can actually see it.

    Buck hands us the tarball under buck-out/v2/gen, and nothing mounts
    that inside the chroot -- only the work area is bind-mounted, and at
    _isolation.SANDBOX_WORK rather than at the name used out here.  Passing
    the gen path straight to tar therefore
    fails with "Cannot open: No such file or directory" about a file that
    plainly exists outside, which reads like a permissions problem and is
    not one.

    Hardlinked, so a half-gigabyte image costs a directory entry rather
    than a copy.  That depends on the scratch root sharing a device with
    buck-out, which is the default's whole reason for being /var/tmp --
    see scratch_dir in tools/_rpm.py -- so the copy is the fallback for
    hosts where it does not, not the normal path.  Sharing the inode is
    safe because the tarball is a Buck input and only ever read.

    Same reasoning as stage_rpms in tools/rootfs_install.py, which solves
    the identical problem for the transaction's packages.
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
        # wrong table.  The numbers in the archive are the truth.
        #
        # No --same-owner needed: inside the namespace we are root, which
        # is when GNU tar restores ownership by default.
        "tar -xf {} -C {} --numeric-owner".format(
            shlex.quote(rootfs), shlex.quote(root)
        ),
        # A sanity check with a useful message, rather than letting dracut
        # fail later about a module directory.
        "test -d {}/usr/lib/modules".format(shlex.quote(root)),
    ])


def _dracut_script(args, kver, image):
    """Run dracut inside the unpacked image."""
    cmd = [
        "/usr/bin/dracut",
        "--force",
        "--kver", kver,
        # See the module docstring: the difference between an ISO that
        # boots anywhere and one that boots here.
        "--no-hostonly",
        # Without this the kernel command line of the *build* machine is
        # baked in as the image's default.
        "--no-hostonly-cmdline",
        # dracut's own reproducibility switch: stable mtimes and a stable
        # member order inside the cpio, so two builds of the same inputs
        # produce the same bytes.
        "--reproducible",
    ]
    for module in args.add_module:
        cmd += ["--add", module]
    for module in args.omit_module:
        cmd += ["--omit", module]
    if args.no_compress:
        cmd.append("--no-compress")
    cmd += args.dracut_arg
    cmd.append(image)

    return "\n".join([
        "set -e",
        # dracut writes its temp tree under TMPDIR and expects it to exist
        # inside the image; /tmp is the tmpfs _chroot_script mounts.
        "export TMPDIR=/tmp",
        " ".join(shlex.quote(part) for part in cmd),
        "test -s {}".format(shlex.quote(image)),
    ])


def _initramfs_tools_script(args, kver, image):
    hook = {
        "live-boot": "/usr/share/initramfs-tools/scripts/live",
        "casper": "/usr/share/initramfs-tools/scripts/casper",
    }[args.generator]
    return "\n".join([
        "set -e",
        "test -e {} || {{ echo {} >&2; exit 1; }}".format(
            shlex.quote(hook),
            shlex.quote("buckos-distro: {} initramfs hook missing at {}".format(args.generator, hook)),
        ),
        "export TMPDIR=/tmp",
        "/usr/sbin/update-initramfs -c -k {}".format(shlex.quote(kver)),
        "cp {} {}".format(
            shlex.quote("/boot/initrd.img-{}".format(kver)),
            shlex.quote(image),
        ),
        "test -s {}".format(shlex.quote(image)),
    ])


def _cleanup_script(root):
    """Remove the unpacked tree from inside the namespace that owns it."""
    return "set -e\nrm -rf {}".format(shlex.quote(root))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rootfs", required=True,
                    help="image rootfs tarball to build an initramfs for")
    ap.add_argument("--out", required=True, help="initramfs image to write")
    ap.add_argument("--buildroot-tree", default=None,
                    help="tree providing the tar that unpacks the image")
    ap.add_argument("--isolation", default="auto", choices=ISOLATION_MODES)
    ap.add_argument("--kver", default=None,
                    help="which kernel, when the image has more than one")
    ap.add_argument("--add-module", action="append", default=[],
                    metavar="NAME", help="dracut --add (repeatable)")
    ap.add_argument("--omit-module", action="append", default=[],
                    metavar="NAME", help="dracut --omit (repeatable)")
    ap.add_argument("--dracut-arg", action="append", default=[],
                    metavar="ARG",
                    help="passed through to dracut verbatim (repeatable)")
    ap.add_argument("--generator", default="dracut",
                    choices=("dracut", "live-boot", "casper"))
    ap.add_argument("--no-compress", action="store_true",
                    help="leave the cpio uncompressed")
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
            "initramfs needs a sandbox: dracut has to run with the image "
            "as /, and isolation=none would run the host's dracut against "
            "the host's modules instead"
        )
    if not args.buildroot_tree:
        sys.exit(
            "--buildroot-tree is required: something has to unpack the "
            "image before there is a root for dracut to run in"
        )

    rootfs = os.path.abspath(args.rootfs)
    kver, _member = find_kernel(rootfs, args.kver)

    # Same reasoning as the replay and the rootfs install: a relative
    # --work would resolve against the action's cwd, which is the project
    # root, so two concurrent actions would write into the source tree.
    if args.work:
        work = os.path.abspath(args.work)
        shutil.rmtree(work, ignore_errors=True)
        os.makedirs(work, exist_ok=True)
    else:
        work = scratch_dir("buckos-distro-initramfs-")

    out = os.path.abspath(args.out)
    try:
        _build(args, isolation, rootfs, kver, work, out)
    finally:
        if not args.keep_work and not args.work:
            # Best effort: the tree is removed from inside the namespace by
            # step 3, and anything surviving that is owned by an id we
            # cannot touch out here.  Failing the build over litter in /tmp
            # would hide whatever actually went wrong.
            shutil.rmtree(work, ignore_errors=True)


def _build(args, isolation, rootfs, kver, work, out):
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
    fakeroot = stage_fakeroot_runtime(sysroot, work, isolation,
                                      required=False)

    # The scripts below name their paths as the sandbox sees them; `root`
    # and `image` keep their host spelling here, where this process copies
    # the finished initramfs out.
    def inside(path):
        return sandbox_path(path, work, isolation)

    image_cp = None
    if args.generator != "dracut":
        # mkinitramfs resets PATH, so a PATH override cannot select the
        # construction copy of cp.  Replace cp only in the ephemeral unpacked
        # tree.  The source-built cp remains in the input rootfs tarball and
        # therefore in the resulting image.
        image_cp = stage_image_tool(sysroot, work, "cp")

    staged = stage_rootfs(rootfs, work)

    print(
        "buckos-distro: unpacking the image to build an initramfs for "
        "{}".format(kver),
        file=sys.stderr,
        flush=True,
    )
    unpack = _unpack_script(inside(staged), inside(root))
    if image_cp:
        unpack += "\n" + _install_image_tool_script(
            inside(image_cp), inside(root), "cp")
    run_isolated(
        fakeroot_command(
            fakeroot,
            ["/bin/sh", "-c", unpack],
        ),
        isolation, work, work, sysroot, env=env,
    )

    print(
        "buckos-distro: running the image's own {} generator (kver={}, "
        "add={}, omit={})".format(
            args.generator,
            kver,
            ",".join(args.add_module) or "-",
            ",".join(args.omit_module) or "-",
        ),
        file=sys.stderr,
        flush=True,
    )
    try:
        # The unpacked image is the sysroot here, so /proc, /sys, /dev and
        # /tmp land inside *it* -- which is what dracut needs and what a
        # nested chroot would not have.
        script = _dracut_script(args, kver, inside(image))
        if args.generator != "dracut":
            script = _initramfs_tools_script(args, kver, inside(image))
        run_isolated(
            fakeroot_command(
                fakeroot,
                ["/bin/sh", "-c", script],
                load=True,
            ),
            isolation, work, work, root, env=env,
        )
    finally:
        # In a finally so a dracut failure still leaves the work area
        # deletable; without this a broken build needs manual cleanup with
        # ids the user does not have.
        run_isolated(
            ["/bin/sh", "-c", _cleanup_script(inside(root))],
            isolation, work, work, sysroot, env=env,
        )

    if not os.path.isfile(image):
        sys.exit("dracut produced no image at {}".format(image))

    # Moved rather than written in place, so a failed run leaves no
    # half-written output where Buck expects a finished one.
    os.makedirs(os.path.dirname(out), exist_ok=True)
    try:
        os.rename(image, out)
    except OSError:
        shutil.move(image, out)

    print(
        "buckos-distro: initramfs for {} -> {} ({} bytes)".format(
            kver, os.path.basename(out), os.path.getsize(out)
        ),
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
