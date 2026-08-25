#!/usr/bin/env python3
"""Assemble a seeded buildroot from a pinned set of binary rpms.

This is where the dependency graph is cut (SPEC.md section 3a).  Every rpm
here is fetched by sha256 from upstream and is *not* built from source; the
set is deliberately small -- roughly Fedora's @buildsys-build group -- and
its size is the repo's honest bootstrap-debt metric.

Payloads are unpacked with rpm2archive | tar, so no scriptlets run.  The
database is then written separately, by a real `rpm --justdb` transaction
run inside the tree itself -- see _register_rpmdb below for why those two
halves are done by different mechanisms.
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
    a symlink inside a transaction.  We unpack payloads and run no
    scriptlets (see the module docstring), so /usr/sbin simply never
    appears and every /sbin/<tool> path dangles one link short.

    That is not a theoretical gap.  redhat-rpm-config's brp-ldconfig, which
    runs in the %install of anything shipping a library, calls
    /sbin/ldconfig by absolute path and fails with "No such file or
    directory" -- with a perfectly good ldconfig sitting in /usr/bin.

    Only created when nothing already occupies the path, so this stays
    correct for releases and distros that predate the merge: there, an rpm
    ships /usr/sbin as a real directory full of real binaries, and turning
    that into a symlink would silently discard them.
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

    --noscripts because the payloads were laid down by tar: a %post that
    expected to run during unpacking has already missed its chance, and
    running it now against a half-configured tree is worse than not
    running it.  That is the remaining honest gap, and it is why
    SCRIPTLET_LINKS above exists.

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
            'exec rpm --justdb --install --noscripts --notriggers '
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
