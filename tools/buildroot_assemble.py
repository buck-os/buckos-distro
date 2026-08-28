#!/usr/bin/env python3
"""Assemble a seeded buildroot from a pinned set of binary rpms.

This is where the dependency graph is cut (SPEC.md section 3a).  Every rpm
here is fetched by sha256 from upstream and is *not* built from source; the
set is deliberately small -- roughly Fedora's @buildsys-build group -- and
its size is the repo's honest bootstrap-debt metric.

Payloads are unpacked with rpm2archive | tar, and then installed properly
by a real `rpm --install` transaction run inside the tree itself.  The
unpack is a bootstrap step -- it puts an rpm on disk to run the
transaction with -- and the transaction is what makes the result a
buildroot rather than an approximation of one: it writes the database
rpmbuild consults, and it runs the install scriptlets.  See
_register_rpmdb for why those two halves use different mechanisms.
"""

import argparse
import os
import shutil
import sys

from _isolation import (
    ISOLATION_MODES,
    require_target_execution,
    resolve_isolation,
    run_isolated,
)
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
    # update-alternatives keeps its state here and fails without it.
    # Load-bearing since the transaction became a real one: golang-bin's
    # %post registers /usr/bin/go through it, and a package that
    # autodetects Go sees nothing if that registration silently failed.
    "var/lib/alternatives",
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

    Current Fedora releases have finished the sbin merge: ldconfig is
    /usr/bin/ldconfig, and
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

    Only created when nothing already occupies the path -- which today it
    does, since some payload in both the seed and image-tools sets ships
    the link and the loop below finds it present.  Kept anyway: that is a
    property of these package sets rather than a guarantee, the check is
    one lstat, and the failure it prevents is a hardcoded /sbin/ldconfig
    dangling in the middle of somebody's %install.

    Still worth keeping now that _register_rpmdb runs scriptlets and
    %pretrans creates the link for real, because the order is the other
    way round: payloads are unpacked *before* that transaction, and
    anything reading the tree in between sees the gap.

    Skipping an occupied path is also what keeps this correct for releases
    and distros that predate the merge: there, an rpm ships /usr/sbin as a
    real directory full of real binaries, and turning that into a symlink
    would silently discard them.
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

    Why a full install rather than `--justdb`: because the scriptlets are
    not optional.  golang-bin's %post is

        update-alternatives --install /usr/bin/go go \
            /usr/lib/golang/bin/go 90 ...

    and /usr/bin/go ships as a symlink into /etc/alternatives, so without
    it the symlink dangles.  libcap then autodetects Go with `go version`,
    gets nothing, silently omits its captree program, and fails in %files
    on a file nothing said it was skipping.  Every step is a warning or a
    success until the last one.  Under --justdb rpm updates the database
    and declines to run install scriptlets for files it is not installing,
    so that failure was structural rather than incidental.

    This cost the ownership property that --justdb was chosen for.  A real
    install chowns files into the subordinate id range, and Buck -- which
    does not own those ids -- then cannot delete or re-materialize its own
    output.  That is handled by make_dirs_writable in the finally below
    rather than by avoiding the transaction; see the comment there, and
    note it is in the finally precisely because a *failed* transaction
    leaves the tree in that state too.

    One measurement lesson is worth keeping, because the mistake was in
    the method rather than the conclusion.  Dropping --noscripts was first
    justified by observing /usr/sbin -> bin and the systemd sysusers
    entries in /etc/passwd afterwards and attributing both to it -- without
    building the same tree *with* --noscripts to compare.  The control says
    they are there either way: they come from package payloads.  The case
    for scriptlets is golang-bin above, which was the experiment that
    actually distinguished the two.

    Why rpm from inside the tree rather than the host's: current Fedora ships
    rpm 6, whose database is sqlite at /usr/lib/sysimage/rpm.  A host rpm
    of a different vintage writes a different format in a different place,
    and the resulting tree would be one no rpmbuild inside it can read.

    --notriggers stays, and here that is still right.  A trigger fires on
    *another* package's installation, so in a single transaction that
    installs everything at once the firing order is rpm's internal
    ordering rather than anything this repo decides.  The overlay in
    tools/rpmbuild_replay.py does turn them on, and needs to: it installs
    into a tree that already exists, which is the situation a trigger is
    for -- glibc's rebuilds /etc/ld.so.cache, and without it bpftool
    cannot load a library sitting on disk.

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
        #
        # A real transaction, not --justdb, and that is what makes %post
        # run.  The paths tar already laid down get overwritten by rpm with
        # the ownership and modes the package declares, which is the point:
        # the tree is now one rpm built rather than one tar approximated.
        #
        # --excludepath for anything whose name contains a backslash.  rpm
        # would happily create it and buck2 cannot address it, so the two
        # systemd slice units that carry one (see nest_unrepresentable in
        # tools/_rpm.py) are left out of the tree.  Excluded rather than
        # nested here because nesting is tar's trick and rpm has no
        # equivalent -- and a systemd unit in a buildroot never executes,
        # so what is lost is a file nothing reads.
        #
        # Built into "$@" from a file rather than interpolated, because a
        # path containing a backslash is exactly the wrong thing to be
        # quoting by hand.
        script = (
            'set -e\n'
            # Saved before `set --` below wipes the positional parameters.
            'STAGING="$1"\n'
            'rpm -qlp "$STAGING"/*.rpm 2>/dev/null | grep \'\\\\\' | sort -u '
            '> excludes.txt || true\n'
            # The sandbox's own mount points.  _chroot_script bind-mounts
            # /proc, /dev, /sys and /tmp inside the tree so rpm and the
            # scriptlets have a working system to run in -- and the
            # `filesystem` package owns those very directories, so an
            # unexcluded transaction tries to chown a live mount and dies:
            #
            #   error: unpacking of archive failed on file /dev:
            #          cpio: chown failed - Device or resource busy
            #   error: filesystem-3.18-50.fc45.x86_64: install failed
            #
            # Nothing is lost by skipping them.  SKELETON above already
            # creates all four, they hold no package content, and their
            # modes are the sandbox's business rather than the image's --
            # this tree is a chroot to build in, not a filesystem to boot.
            'set -- --excludepath /dev --excludepath /proc'
            ' --excludepath /sys --excludepath /tmp\n'
            'while IFS= read -r p; do\n'
            '  [ -n "$p" ] && set -- "$@" --excludepath "$p"\n'
            'done < excludes.txt\n'
            'if [ -s excludes.txt ]; then\n'
            '  echo "buckos-distro: excluding $(wc -l < excludes.txt) path(s)'
            ' whose names buck2 cannot address" >&2\n'
            'fi\n'
            'rpm --install --notriggers --nosignature "$@" '
            '"$STAGING"/*.rpm\n'
            # A buildroot is an execution environment, not a bootable image;
            # ownership inside it is therefore deliberately normalized to
            # namespace root. Some newer package scriptlets create runtime
            # state owned by service users. Left mapped to subordinate host
            # ids, Buck can hash the output but cannot later delete it on a
            # clean remote worker. Preserve groups and modes, and avoid the
            # sandbox mounts plus the staged input tree.
            'WORK=${STAGING%/rpms}\n'
            'find / -path /dev -prune -o -path /proc -prune -o '
            '-path /sys -prune -o -path /tmp -prune -o '
            '-path "$WORK" -prune -o ! -uid 0 -exec chown -h 0 {} +\n'
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

        # In the finally, not after it, and this is not tidiness.  A real
        # transaction lays down rpm's declared modes, and rpm ships
        # directories nobody can write and files nobody can read --
        # /etc/pki/ca-trust/extracted, /etc/gshadow-.  If the transaction
        # fails partway the tree is left in that state, and then buck2
        # cannot delete its own output on the *next* build: the failure
        # moves to a later command that says only "Permission denied"
        # about a path it never asked for.  Observed, on the first
        # transaction that died on a mount point.
        make_dirs_writable(out)

    _clean_db_transients(os.path.join(out, "usr", "lib", "sysimage", "rpm"))

    # Again, because _clean_db_transients may have exposed more and
    # because the database rpm just wrote has restrictive modes of its own.
    make_dirs_writable(out)

    _check_ownership(out)


def _check_ownership(out):
    """Fail now if ownership normalization missed anything.

    Inside the namespace anything running as root may chown a file to an
    id in the subordinate range; outside, that id belongs to nobody the
    Buck daemon can act as, so the file cannot be deleted or
    re-materialised.

    The transaction normalizes non-root owners before leaving the namespace.
    That is valid for a buildroot, whose package ownership is not shipped,
    and necessary when scriptlets create service-owned runtime state. This
    outer check makes a missed path fail in the action that created it rather
    than the next action that tries to clean the output directory.
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
        "buckos-distro: ownership normalization left {} file(s) owned by "
        "an id this user does not have, so Buck cannot delete its own output "
        "on the next build:\n  {}".format(
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
    ap.add_argument("--target-cpu", default="x86_64")
    args = ap.parse_args()

    require_target_execution(args.target_cpu)

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
