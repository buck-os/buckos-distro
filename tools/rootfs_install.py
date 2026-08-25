#!/usr/bin/env python3
"""Install a pinned set of rpms into a root filesystem, with a real rpmdb.

The other half of tools/buildroot_assemble.py, and deliberately not the
same mechanism.

A *buildroot* is a place to run a compiler.  It is unpacked with
`rpm2archive | tar`, has no package database, and runs no scriptlets --
all correct, because none of that affects what gcc produces, and skipping
it keeps the tree a pure function of its pinned inputs.

A *rootfs* is a system that has to boot and then be maintainable.  That
needs the two things the buildroot deliberately does without:

  the rpmdb    without /usr/lib/sysimage/rpm the image cannot be queried,
               updated, or verified.  An image you cannot `dnf update` is
               a demo, not a distro.
  scriptlets   %post is where systemd unit presets are applied, ldconfig
               builds its cache, depmod writes modules.dep, and sysusers
               creates the accounts /var expects.  Skip them and the
               image either fails to boot or boots into something subtly
               broken.

So this shells out to rpm proper -- `rpm --root` inside the buildroot
chroot, one transaction -- rather than unpacking payloads.  rpm decides
install order, runs the scriptlets, and writes its own database.  We never
reimplement any of that (SPEC.md section 1).

Which rpm matters: the *buildroot's*, not the host's.  Fedora 43 keeps its
database in sqlite while the host's rpm 4.16 still defaults to bdb, so a
database built by the host would be one the target cannot read.  Running
inside the chroot is what makes the produced rpmdb the target distro's
own.

Why the output is a tarball and not a directory
-----------------------------------------------
Because the ownership is the point, and a directory cannot carry it here.

rpm installs files owned by mail, tss, systemd-network and a dozen others.
Inside the namespace those are real ids; outside, they land in this user's
subordinate range (see tools/_isolation.py), and nothing outside the
namespace can chown them back.  A directory artifact full of files the
build daemon does not own is one Buck can hash but cannot delete -- the
next build fails in `remove_dir_all` with no hint of why.

The alternative, chowning everything to the builder on the way out, throws
away exactly the metadata that makes this a rootfs rather than a heap of
unpacked payloads: an image where /usr/bin/passwd is not setuid root and
/var/spool/mail is not group mail does not work correctly.

A tar archive holds the ownership as metadata inside a single file the
builder owns.  Downstream image rules unpack it inside their own namespace.
This is the same reason container layers and stage3 tarballs are tarballs.
"""

import argparse
import os
import shlex
import shutil
import sys

from _isolation import ISOLATION_MODES, resolve_isolation, run_isolated
from _rpm import (
    make_dirs_writable,
    reproducible_env,
    scratch_dir,
    stage_rpms,
)


def collect_rpms(paths):
    """Expand --rpm arguments, which may be files or directories.

    Directories are how built packages arrive: srpm_build emits a whole
    RPMS/ directory because one spec produces many subpackages, and the
    caller usually wants all of them.

    A plain file is taken at its word rather than checked for a .rpm
    suffix.  Buck names an http_file's output after the *target*, so a
    pinned upstream rpm arrives as
    `__rpm-glibc-2.42-4.fc43-x86_64-43__/rpm-glibc-...-43` with no
    extension at all -- filtering on the name would reject every pinned
    package in the seed.  Inside a directory the suffix filter does apply,
    because there the name is the payload's own and the directory holds
    build leftovers alongside the rpms.
    """
    found = []
    for path in paths:
        if os.path.isdir(path):
            for root, _dirs, names in os.walk(path):
                found += [
                    os.path.join(root, n) for n in names if n.endswith(".rpm")
                ]
        elif os.path.isfile(path):
            found.append(path)
        else:
            sys.exit("no such rpm or directory of rpms: {}".format(path))
    if not found:
        sys.exit(
            "no rpms to install.  A rootfs with no packages is an empty "
            "directory, which is never what the caller meant."
        )
    # Sorted so the transaction is presented identically regardless of the
    # order Buck passed the deps in.  rpm still computes its own install
    # order from the dependency graph; this only fixes the input.
    return sorted(set(found))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True,
                    help="tar archive of the rootfs to create (see above)")
    ap.add_argument("--rpm", action="append", default=[], metavar="PATH",
                    help="rpm file or directory of rpms (repeatable)")
    ap.add_argument("--buildroot-tree", default=None,
                    help="tree providing the rpm that runs the transaction")
    ap.add_argument("--isolation", default="auto", choices=ISOLATION_MODES)
    ap.add_argument("--work", default=None,
                    help="scratch directory; a temp dir is used if omitted")
    ap.add_argument("--keep-work", action="store_true",
                    help="do not delete the scratch area, for debugging")
    ap.add_argument("--nodeps", action="store_true",
                    help="skip rpm's dependency check (see below)")
    ap.add_argument("--source-date-epoch", default="1700000000")
    args = ap.parse_args()

    isolation = resolve_isolation(args.isolation)
    rpms = collect_rpms(args.rpm)

    # Same reasoning as the replay: a caller-supplied --work would resolve
    # against the action's cwd, which is the project root, so two concurrent
    # rootfs actions would write into the source tree and into each other.
    if args.work:
        work = os.path.abspath(args.work)
        shutil.rmtree(work, ignore_errors=True)
        os.makedirs(work, exist_ok=True)
    else:
        work = scratch_dir("buckos-distro-rootfs-")

    out = os.path.abspath(args.out)
    try:
        _install(args, isolation, rpms, work, out)
    finally:
        if not args.keep_work and not args.work:
            # Best effort only.  The tree rpm built is removed from inside
            # the namespace, where the subordinate ids are ours; anything
            # that survives that is owned by an id we cannot touch out here,
            # and failing the build over litter in /tmp would hide whatever
            # actually went wrong.
            shutil.rmtree(work, ignore_errors=True)


def _install(args, isolation, rpms, work, out):
    # Everything rpm touches lives under the work area, which is the one
    # path bound read-write into the sandbox at its own absolute name.  The
    # declared output is not: it sits in buck-out/v2/gen, invisible inside
    # the chroot, so `--root` pointed at it would have rpm build the image
    # into a directory that does not exist.  Install here, move at the end.
    target = os.path.join(work, "rootfs")
    tarball = os.path.join(work, "rootfs.tar")
    os.makedirs(target)
    staging = os.path.join(work, "rpms")
    stage_rpms(rpms, staging)

    # A private, writable copy of the buildroot to chroot into.  Copied
    # rather than entered directly because the buildroot is a Buck input
    # artifact shared with every other action in the build: rpm writes to
    # /var/tmp and /var/lib inside its own root during a transaction, and
    # mutating a shared input would corrupt concurrent actions and poison
    # the cache.
    sysroot = None
    if isolation == "none":
        # Host provenance, the development escape hatch.  Worth one line of
        # warning rather than silence: the host's rpm writes the database in
        # whatever backend the host defaults to, and on an older host that is
        # bdb, which the target's rpm cannot open.  The tree will look right
        # and `dnf` inside it will not work.
        print(
            "buckos-distro: WARNING: isolation=none, so the HOST's rpm "
            "writes the rpmdb.  If its db backend differs from the target "
            "release's, the image cannot read its own package database.",
            file=sys.stderr,
        )
    else:
        if not args.buildroot_tree:
            sys.exit(
                "isolation={} needs --buildroot-tree: the rpm that runs "
                "the transaction has to be the target distro's, not the "
                "host's, or the database it writes is one the image "
                "cannot read".format(isolation)
            )
        sysroot = os.path.join(work, "sysroot")
        shutil.copytree(args.buildroot_tree, sysroot, symlinks=True,
                        dirs_exist_ok=True)
        make_dirs_writable(sysroot)

    print(
        "buckos-distro: installing {} rpm(s) into a rootfs "
        "(isolation={}, deps={})".format(
            len(rpms), isolation, "skipped" if args.nodeps else "checked"
        ),
        file=sys.stderr,
        flush=True,
    )
    run_isolated(
        ["/bin/sh", "-c", _transaction_script(args, staging, target, tarball)],
        isolation, work, work, sysroot,
        env=_transaction_env(args),
    )

    if not os.path.isfile(tarball):
        sys.exit("the transaction produced no archive at {}".format(tarball))

    # Moved rather than written in place, so a failed transaction leaves no
    # half-written archive where Buck expects a finished output.
    if os.path.isdir(out):
        shutil.rmtree(out, ignore_errors=True)
    try:
        os.rename(tarball, out)
    except OSError:
        # Different devices: buck-out's tmp and gen trees are normally the
        # same filesystem, but nothing guarantees it.
        shutil.move(tarball, out)

    print(
        "buckos-distro: rootfs installed from {} rpm(s) -> {} ({} bytes)".format(
            len(rpms), os.path.basename(out), os.path.getsize(out)
        ),
        file=sys.stderr,
    )


def _transaction_env(args):
    """The environment rpm's scriptlets inherit.

    KERNEL_INSTALL_BYPASS is the one addition, and it is a design decision
    rather than a workaround.  kernel-core's %posttrans runs
    kernel-install, whose 50-dracut.install builds an initramfs into the
    tree being installed.  We do not want that here for reasons that
    outlast any particular error:

      * it is a separate rule.  The initramfs is its own artifact with its
        own inputs, and a copy generated as a side effect of installing a
        package is one Buck cannot see, cache, or rebuild.
      * it cannot work.  dracut wants /proc/cmdline, the host's
        /sys/module/firmware_class, and a writable TMPDIR at a path that
        exists inside the chroot -- none of which a hermetic transaction
        has any business providing.
      * it would land a hundred megabytes of derived data in the rootfs
        tarball, which the initramfs rule then regenerates and the image
        rule then ignores.

    Bypassing is what osbuild and anaconda do for the same reason: install
    the kernel, build the initramfs deliberately afterwards.  Without it
    rpm reports the failed scriptlet and exits non-zero, so this is also
    the difference between a transaction that completes and one that does
    not.
    """
    env = reproducible_env(source_date_epoch=args.source_date_epoch)
    env["KERNEL_INSTALL_BYPASS"] = "1"
    return env


def _transaction_script(args, staging, target, tarball):
    """Install, archive, and tidy up -- all inside the one namespace.

    Three things have to happen where the subordinate ids are mapped, and
    only the first is obvious.

    The install, because rpm chowns to mail and tss and systemd-network.

    The tar, because that is what turns ownership into metadata.  Run out
    here instead, it would read back whatever the kernel shows an
    unprivileged process -- the raw subordinate ids, 1879048200 rather than
    8 -- and the archive would encode this machine's /etc/subuid rather than
    the image's own users.

    The removal, because nothing outside the namespace can unlink a file
    owned by an id it does not have.  Left behind, that tree is a directory
    the build user can neither read through nor delete; the trap runs it on
    a failed transaction too, which is exactly when it would otherwise be
    left for someone to find later with no idea what made it.

    rpm is handed the glob rather than a list: 289 absolute paths is around
    24 KB of argv today and grows with the image, against a 128 KB kernel
    limit on a single argument string.  Sorted by the shell, so the order
    presented is stable; rpm computes its own install order regardless.
    """
    quoted_target = shlex.quote(target)
    return "\n".join([
        "set -e",
        # Runs on failure too -- see above.
        "trap 'rm -rf {}' EXIT".format(quoted_target),
        "cd {}".format(shlex.quote(staging)),
        # --nosignature: signatures are not the pin here.  Every rpm reaching
        # this point was fetched by sha256 through http_file or produced by a
        # build action in this graph, both of which buck2 enforces; rpm has
        # no keyring in the buildroot and would only warn NOKEY on all of
        # them, which trains the reader to ignore the warning.
        #
        # --nodeps is off by default, unlike the replay.  The buildroot has
        # to use it because it has no rpmdb to check against, but here rpm is
        # building the database as it goes, so the check works -- and it is
        # the only thing that verifies the package set computed in
        # tools/solve.py is actually closed and installable.
        "/usr/bin/rpm --install --root {} --nosignature -v{} *.rpm".format(
            quoted_target, " --nodeps" if args.nodeps else ""
        ),
        # The rpmdb is the difference between an image and a heap of unpacked
        # payloads, and its absence is otherwise invisible until someone runs
        # dnf inside the image.  Checked in here rather than after the fact
        # because out here the path is not readable.
        "test -d {}/usr/lib/sysimage/rpm || echo 'buckos-distro: WARNING: no"
        " rpmdb at usr/lib/sysimage/rpm; check where this release'\"'\"'s rpm"
        " put it' >&2".format(quoted_target),
        # --numeric-owner: resolve nothing through the buildroot's
        # /etc/passwd, which is not the image's.  --sort=name and a pinned
        # mtime so the archive is a function of its inputs.
        #
        # --xattrs, and therefore --format=posix, because the gnu format has
        # nowhere to put them: file capabilities live in security.capability
        # (without it /usr/bin/ping cannot open a raw socket) and SELinux
        # labels in security.selinux (without them a targeted-policy system
        # boots to a relabel or to nothing).  Both are xattrs, not modes, so
        # a tar that drops them produces a tree that looks complete and is
        # not.
        "tar --create --numeric-owner --sort=name"
        " --xattrs --xattrs-include='*' --acls --format=posix"
        " --mtime=@{epoch}"
        " --file {tarball} --directory {target} .".format(
            epoch=shlex.quote(args.source_date_epoch),
            tarball=shlex.quote(tarball),
            target=quoted_target,
        ),
    ])


if __name__ == "__main__":
    main()
