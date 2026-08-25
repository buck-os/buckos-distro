#!/usr/bin/env python3
"""Assemble a seeded buildroot from a pinned set of binary rpms.

This is where the dependency graph is cut (SPEC.md section 3a).  Every rpm
here is fetched by sha256 from upstream and is *not* built from source; the
set is deliberately small -- roughly Fedora's @buildsys-build group -- and
its size is the repo's honest bootstrap-debt metric.

Payloads are unpacked with rpm2archive | tar; the database is then written
separately, by a real `rpm --justdb` transaction run inside the tree
itself.  See _register_rpmdb below for why those two halves are done by
different mechanisms, and why the scriptlets run with the second one
rather than the first.
"""

import argparse
import os
import shutil
import sys

from _isolation import ISOLATION_MODES, resolve_isolation, run_isolated
from _rpm import (
    extract_rpm,
    make_dirs_writable,
    reproducible_env,
    scratch_dir,
    stage_rpms,
)

# Directories rpm's own macros and the FHS assume exist.  Several brp-*
# scripts and %__os_install_post steps fail on a missing /tmp or /dev.
SKELETON = (
    "dev",
    "proc",
    "sys",
    "tmp",
    "var/tmp",
    "var/lib/rpm",
    "builddir",
)


# Compat symlinks that a real rpm transaction creates from a scriptlet
# rather than from a payload, so unpacking payloads alone never produces
# them.  Mapped to what they should point at, relative to the link.
SCRIPTLET_LINKS = {
    "usr/sbin": "bin",
}


def _repair_dangling_bindir_links(out):
    """Create the bin/sbin compat links no payload ships.

    Fedora 43 finished the sbin merge: ldconfig is /usr/bin/ldconfig, and
    /usr/sbin is a symlink to bin.  But `filesystem` ships only /sbin ->
    usr/sbin in its payload -- the /usr/sbin -> bin link itself is created
    by its %pretrans lua scriptlet, because rpm cannot swap a directory for
    a symlink inside a transaction.  Unpacking payloads alone therefore
    never produces /usr/sbin, and every /sbin/<tool> path dangles one link
    short.

    That is not a theoretical gap.  redhat-rpm-config's brp-ldconfig, which
    runs in the %install of anything shipping a library, calls
    /sbin/ldconfig by absolute path and fails with "No such file or
    directory" -- with a perfectly good ldconfig sitting in /usr/bin.

    A fallback rather than the main path, now that _register_rpmdb runs
    scriptlets: on the normal route `filesystem`'s own %pretrans creates
    this link and the loop below finds it already there.  What is left for
    it is the route where no transaction happens at all -- isolation
    "none", which skips rpmdb registration entirely because there is no
    tree to chroot into -- and that route still needs a buildroot whose
    /sbin/ldconfig resolves.

    Only created when nothing already occupies the path, which is also
    what keeps it correct for releases and distros that predate the merge:
    there, an rpm ships /usr/sbin as a real directory full of real
    binaries, and turning that into a symlink would silently discard them.
    """
    for rel, target in SCRIPTLET_LINKS.items():
        link = os.path.join(out, rel)
        if os.path.lexists(link):
            continue
        # Only if the destination is really there -- a link to nothing
        # would just move the failure one step later.
        if not os.path.isdir(os.path.join(os.path.dirname(link), target)):
            continue
        os.symlink(target, link)


# sqlite's write-ahead log and its shared-memory index.  Both are runtime
# scaffolding rather than database content: sqlite recreates them on the
# next open, and rpm checkpoints the WAL into the main file when it closes
# the transaction cleanly.  They are removed because their contents are not
# reproducible even when the database itself is, and this tree is hashed by
# Buck -- see the epoch discussion in _register_rpmdb.
_DB_TRANSIENTS = ("rpmdb.sqlite-shm", "rpmdb.sqlite-wal", ".rpm.lock")


def _clean_db_transients(dbdir):
    """Drop sqlite scratch files, but never a WAL with data still in it."""
    for name in _DB_TRANSIENTS:
        path = os.path.join(dbdir, name)
        if not os.path.exists(path):
            continue
        # A non-empty WAL means rpm did not checkpoint, and deleting it
        # would silently discard committed rows.  Leave it and accept a
        # non-reproducible tree over a corrupt one.
        if name.endswith("-wal") and os.path.getsize(path) > 0:
            print(
                "buckos-distro: WARNING: {} is non-empty; leaving it in "
                "place. The buildroot is correct but not bit-for-bit "
                "reproducible.".format(path),
                file=sys.stderr,
            )
            continue
        os.unlink(path)


def _register_rpmdb(out, rpms, isolation, source_date_epoch):
    """Populate the tree's rpmdb from the same rpms whose payloads it holds.

    Why a real transaction rather than unpacking the headers ourselves:
    the database is what rpmbuild consults for BuildRequires, so a
    hand-written one is a reimplementation of rpm semantics that has to
    stay correct as rpm changes -- exactly what SPEC.md section 1 forbids.

    Why `--justdb` rather than a plain install: the payloads are already
    on disk, unpacked by tar as the invoking user.  A real install would
    chown them into the subordinate id range, and Buck -- which does not
    own those ids -- could then neither delete nor re-materialize its own
    output directory.  --justdb writes the database and touches nothing
    else, so the tree stays deletable and stays a directory artifact that
    other actions can chroot into.

    Why rpm from inside the tree rather than the host's: Fedora 43 ships
    rpm 6, whose database is sqlite at /usr/lib/sysimage/rpm.  A host rpm
    of a different vintage writes a different format in a different place,
    and the resulting tree would be one no rpmbuild inside it can read.

    Scriptlets *do* run here, and that is a deliberate reversal.  The
    argument for --noscripts was that the payloads were laid down by tar,
    so a %post expecting to run during unpacking had already missed its
    chance and firing it against a half-configured tree was worse than not
    firing it.  The premise was wrong in one important way: by the time
    this function runs the tree is not half-configured, it is complete.
    Every payload is already extracted.  A %post that wants to walk
    /usr/lib and build a cache finds the whole of /usr/lib there.

    What that buys, measured on the image-tools set rather than assumed:

      * `filesystem`'s %pretrans creates /usr/sbin -> bin for real, so the
        fabrication below stops being load-bearing.
      * systemd's sysusers scriptlets populate /etc/passwd and /etc/group
        -- dbus, systemd-coredump, systemd-oom, systemd-timesync.  A
        package whose build needs one of those ids to exist now finds it
        instead of failing somewhere unhelpful.

    Reproducibility was the thing worth checking before believing any of
    it, because sysusers allocates uids dynamically from the top of a
    range and an allocation that moved between runs would rehash the tree
    and invalidate every package built against it.  Two clean runs produce
    a byte-identical rpmdb and an identical tree: rpm orders the
    transaction from the dependency graph, so the allocation order is a
    function of the package set and not of the clock.

    --notriggers stays.  A trigger fires on *another* package's
    installation, so in a single transaction that installs everything at
    once the firing order is a property of rpm's internal ordering rather
    than of anything this repo decides -- and nothing in the seed set
    needs one.  Turning them on is a change that should come with a case.

    No --nodeps.  The seed set is dependency-closed by construction --
    tools/solve.py computes its closure -- so rpm checking that claim is
    free verification, and if it ever fails it means the solver is wrong.

    SOURCE_DATE_EPOCH is load-bearing here, not decoration.  rpm records
    an install time per package and a transaction id for the set; left to
    the clock they change every run, the tree's hash changes with them,
    and since every package in the distro is built against this tree, one
    unpinned timestamp invalidates the entire downstream cache.  rpm 6
    honours the epoch for both fields and derives per-package times as
    epoch + n, so install order is still recorded -- just not wall-clock.
    """
    if not rpms:
        return
    if isolation == "none":
        # Nothing to chroot into means the host's rpm would write the
        # host's database format into the tree.  Better to ship a tree
        # with no database, which callers already handle, than one whose
        # database its own rpm cannot open.
        print(
            "buckos-distro: isolation=none, skipping rpmdb registration; "
            "`rpm -q` inside this buildroot will not work.",
            file=sys.stderr,
        )
        return

    work = scratch_dir("buckos-distro-buildroot-")
    try:
        staging = os.path.join(work, "rpms")
        stage_rpms(rpms, staging)

        # Handed to rpm as a glob expanded by the shell inside the sandbox,
        # not as argv out here: 292 absolute paths is close enough to the
        # kernel's 128 KB single-argument limit to be a latent failure.
        script = (
            'set -e\n'
            'exec rpm --justdb --install --notriggers '
            '--nosignature "$1"/*.rpm\n'
        )
        run_isolated(
            ["/bin/sh", "-c", script, "sh", staging],
            isolation,
            work=work,
            chdir=work,
            sysroot=out,
            env=reproducible_env(
                {
                    "PATH": "/usr/bin:/usr/sbin:/bin:/sbin",
                    "HOME": "/builddir",
                },
                source_date_epoch=source_date_epoch,
            ),
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)
        # The sandbox binds the work area at its own absolute path, which
        # means it had to create that path *inside* the tree as a mount
        # point.  The mount is gone with the namespace but the empty
        # directory is not, and its name contains mkdtemp's random suffix
        # -- so leaving it would make this output differ on every build
        # for no reason a reader could ever guess.
        leftover = os.path.join(out, os.path.relpath(work, "/"))
        shutil.rmtree(leftover, ignore_errors=True)

    _clean_db_transients(os.path.join(out, "usr", "lib", "sysimage", "rpm"))

    # The transaction created the database as the namespace's root, which
    # is us, but rpm sets restrictive modes on some of what it writes.
    make_dirs_writable(out)

    _check_ownership(out)


def _check_ownership(out):
    """Fail now if anything in the tree stopped being ours.

    Running scriptlets is what makes this worth checking.  Inside the
    namespace a scriptlet is root and may chown a file to any id in the
    subordinate range; outside, that id belongs to nobody the Buck daemon
    can act as, so the file cannot be deleted or re-materialised.

    Nothing in the seed or image-tools sets does this today -- both were
    measured at zero.  It is checked anyway because of *when* the damage
    would otherwise surface: not in this build, which would succeed, but
    in the next one, as "Error cleaning up output path ... Permission
    denied" naming a path nobody asked about, from an action that is not
    this one.  That is the same delayed, misattributed failure the
    backslash trap has, and it is worth the one walk to convert it into a
    message that names the file and the cause.

    Not repaired automatically, because the repair would have to run
    inside a namespace this function no longer has, and because a
    buildroot whose ownership a scriptlet cares about is a situation that
    deserves a human rather than a silent chown.
    """
    uid = os.getuid()
    strays = []
    for root, dirnames, filenames in os.walk(out):
        for name in dirnames + filenames:
            path = os.path.join(root, name)
            try:
                if os.lstat(path).st_uid != uid:
                    strays.append(path)
            except OSError:
                continue
        if len(strays) > 10:
            break
    if not strays:
        return

    sys.exit(
        "buckos-distro: a scriptlet left {} file(s) owned by an id this "
        "user does not have, so Buck cannot delete its own output on the "
        "next build:\n  {}\n"
        "Re-run the transaction with --noscripts, or add the offending "
        "package to a set that does not need scriptlets.".format(
            len(strays), "\n  ".join(sorted(strays)[:10])
        )
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="buildroot tree to create")
    ap.add_argument("--rpm", action="append", default=[],
                    help="seed rpm to unpack (repeatable)")
    ap.add_argument("--macros", default=None,
                    help="extra rpm macro file to install into the tree")
    ap.add_argument("--isolation", default="auto", choices=ISOLATION_MODES,
                    help="how to enter the tree to write its rpmdb")
    ap.add_argument("--source-date-epoch", default="1700000000")
    args = ap.parse_args()

    out = os.path.abspath(args.out)
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out, exist_ok=True)

    # Sorted so the tree is byte-identical regardless of the order Buck
    # happened to pass the deps in -- otherwise two identical buildroots
    # hash differently and every downstream action re-runs.
    for rpm_path in sorted(args.rpm):
        extract_rpm(rpm_path, out)

    # Before the skeleton, not just after: the last rpm unpacked can have
    # left the tree root or an intermediate directory read-only, and
    # makedirs would then fail here rather than in the extraction.
    make_dirs_writable(out)

    for rel in SKELETON:
        os.makedirs(os.path.join(out, rel), exist_ok=True)

    _repair_dangling_bindir_links(out)

    # rpm ships /usr/lib and friends as dr-xr-xr-x, and the last rpm
    # unpacked leaves them that way.  Buck2 has to be able to delete its
    # own output directory on the next build, and it does not run as root
    # -- a read-only directory here fails the *following* build with an
    # opaque "Error cleaning up output path ... Permission denied" that
    # points nowhere near this action.
    make_dirs_writable(out)

    if not args.rpm:
        # An empty buildroot is a valid tree, so this cannot fail here --
        # but a replay against it will die deep inside rpmbuild looking for
        # /bin/sh, which is a terrible way to learn the seed set is
        # unpopulated.  Say so now, and leave a marker so the tree is
        # identifiable in buck-out.
        print(
            "buckos-distro: WARNING: buildroot assembled with no seed rpms.\n"
            "  This tree has no shell, no compiler, and no rpm macros; any\n"
            "  replay against it will fail.  Populate seed_rpms first --\n"
            "  see flavors/<flavor>/BUCK.",
            file=sys.stderr,
        )
        with open(os.path.join(out, "EMPTY-SEED-SET"), "w") as fh:
            fh.write("no seed rpms were passed to buildroot_assemble\n")

    if args.macros:
        dest_dir = os.path.join(out, "usr", "lib", "rpm", "macros.d")
        os.makedirs(dest_dir, exist_ok=True)
        shutil.copy2(args.macros, os.path.join(dest_dir, "macros.buckos-distro"))

    # Last, and after the macros: the transaction runs the tree's own rpm,
    # which reads macros.d on startup, so a macro file dropped in
    # afterwards would not have applied to the database being written.
    _register_rpmdb(
        out,
        sorted(args.rpm),
        resolve_isolation(args.isolation),
        args.source_date_epoch,
    )

    print(
        "buckos-distro: assembled buildroot from {} seed rpm(s)".format(
            len(args.rpm)
        ),
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
