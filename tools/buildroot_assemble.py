#!/usr/bin/env python3
"""Assemble a seeded buildroot from a pinned set of binary rpms.

This is where the dependency graph is cut (SPEC.md section 3a).  Every rpm
here is fetched by sha256 from upstream and is *not* built from source; the
set is deliberately small -- roughly Fedora's @buildsys-build group -- and
its size is the repo's honest bootstrap-debt metric.

Unpacked with rpm2archive | tar: no rpmdb, no scriptlets.  The tree is
therefore self-contained enough to bind-mount as / under bubblewrap but
does not have a package database, so `rpm -q` inside it will not work and
specs whose %post populates state cannot be satisfied from the seed.
"""

import argparse
import os
import shutil
import sys

from _rpm import extract_rpm, make_dirs_writable

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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="buildroot tree to create")
    ap.add_argument("--rpm", action="append", default=[],
                    help="seed rpm to unpack (repeatable)")
    ap.add_argument("--macros", default=None,
                    help="extra rpm macro file to install into the tree")
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

    print(
        "buckos-distro: assembled buildroot from {} seed rpm(s)".format(
            len(args.rpm)
        ),
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
