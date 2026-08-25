#!/usr/bin/env python3
"""Replay a Fedora spec file with rpmbuild inside a Buck action.

This is the heart of the fedora flavor.  The spec is never rewritten: we
hand it to rpmbuild unchanged and control the build purely through
--define, --with/--without, and the contents of the buildroot.  See
SPEC.md section 1.

Outputs:
    --out-rpms         directory of produced binary .rpm files
    --out-installroot  the unpacked BUILDROOT tree, which becomes
                       PackageInfo.prefix and feeds rootfs assembly

Isolation
---------
The buildroot is entered as / -- see tools/_isolation.py for the modes
and why "auto" never degrades to the host.

The rule layer refuses remote execution unless the buildroot is hermetic,
because "none" reads the host's /usr.
"""

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile

from _isolation import resolve_isolation, run_isolated
from _rpm import extract_rpm, reproducible_env, require_tool, run

TOPDIR_SUBDIRS = ("BUILD", "BUILDROOT", "RPMS", "SOURCES", "SPECS", "SRPMS")


def copy_topdir(src, dst):
    """Copy the read-only unpacked topdir into a writable work area.

    Buck artifacts are read-only; rpmbuild writes into BUILD/ and
    BUILDROOT/ under %_topdir, so it needs its own copy.
    """
    shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)
    for sub in TOPDIR_SUBDIRS:
        os.makedirs(os.path.join(dst, sub), exist_ok=True)
    # Clear anything the unpack step left in output dirs.
    for sub in ("RPMS", "SRPMS", "BUILD", "BUILDROOT"):
        path = os.path.join(dst, sub)
        shutil.rmtree(path, ignore_errors=True)
        os.makedirs(path, exist_ok=True)


def find_spec(topdir):
    specs = sorted(glob.glob(os.path.join(topdir, "SPECS", "*.spec")))
    if not specs:
        sys.exit("no spec file under {}/SPECS".format(topdir))
    if len(specs) > 1:
        sys.exit("multiple spec files under {}/SPECS: {}".format(topdir, specs))
    return specs[0]


def compose_buildroot(seed_root, dep_roots, dest):
    """Assemble the build environment into a fresh writable tree.

    Layered lowest-priority first: the flavor seed base, then each
    resolved BuildRequires installroot (a prebuilt_rpm or an
    rpm_subpackage, per SPEC.md section 3a).

    Composed into `dest` rather than overlaid onto `seed_root`: the seed
    is a Buck input artifact shared by every package in the build, so
    mutating it would corrupt concurrent actions and poison the cache.

    Copy, not symlink: rpmbuild's %__os_install_post and the brp-*
    scripts rewrite files in place, and a symlink farm makes them rewrite
    the dependency instead of the output.
    """
    os.makedirs(dest, exist_ok=True)
    layers = ([seed_root] if seed_root else []) + list(dep_roots)
    for layer in layers:
        if not os.path.isdir(layer):
            continue
        shutil.copytree(layer, dest, symlinks=True, dirs_exist_ok=True)
    return dest


def sysroot_env(sysroot, env):
    """Point the ambient toolchain at a composed sysroot.

    Only meaningful for isolation "none".  Under bwrap the sysroot IS /, so
    the normal /usr paths already resolve to it and prefixing with the
    host-side path would point outside the sandbox.

    Best-effort by construction: a spec that hardcodes /usr/lib64 still
    reads the host's copy.  That is the documented cost of host provenance
    (SPEC.md section 3) and the reason a seeded buildroot is the production
    path.
    """
    root = os.path.abspath(sysroot)
    usr = os.path.join(root, "usr")
    overrides = {
        "PATH": os.pathsep.join([
            os.path.join(usr, "bin"),
            os.path.join(usr, "sbin"),
            env.get("PATH", "/usr/bin:/bin"),
        ]),
        "PKG_CONFIG_PATH": os.pathsep.join([
            os.path.join(usr, "lib64", "pkgconfig"),
            os.path.join(usr, "lib", "pkgconfig"),
            os.path.join(usr, "share", "pkgconfig"),
        ]),
        "C_INCLUDE_PATH": os.path.join(usr, "include"),
        "CPLUS_INCLUDE_PATH": os.path.join(usr, "include"),
        "LIBRARY_PATH": os.pathsep.join([
            os.path.join(usr, "lib64"),
            os.path.join(usr, "lib"),
        ]),
    }
    # Keep any inherited search paths rather than shadowing them.
    for key in ("PKG_CONFIG_PATH", "C_INCLUDE_PATH", "CPLUS_INCLUDE_PATH",
                "LIBRARY_PATH"):
        inherited = env.get(key)
        if inherited:
            overrides[key] = overrides[key] + os.pathsep + inherited
    return overrides


def build_rpmbuild_cmd(spec, topdir, args, buildroot_dir):
    cmd = [args.rpmbuild, "-bb"]

    # Extra flavor macros are *loaded on top of* the normal macro path.
    # Not --macros=, which replaces %_macrofiles wholesale and would drop
    # redhat-rpm-config's own macros.
    if args.macros:
        cmd += ["--load", os.path.abspath(args.macros)]

    # %_topdir must be absolute -- rpmbuild resolves sources relative to it.
    cmd += ["--define", "_topdir {}".format(os.path.abspath(topdir))]

    # Keep the build inside the action's sandbox.
    cmd += ["--define", "_tmppath {}".format(os.path.abspath(os.path.join(topdir, "tmp")))]

    if args.dist_tag:
        cmd += ["--define", "dist {}".format(args.dist_tag)]
    if args.fedora_release:
        cmd += ["--define", "fedora {}".format(args.fedora_release)]
    if args.target_cpu:
        cmd += ["--target", args.target_cpu]

    # Hardening / global flag injection.  Macro-level, not per-compile --
    # a documented limitation of replay (SPEC.md section 1).
    for flag_expr in args.define or []:
        cmd += ["--define", flag_expr]

    # USE flags map onto rpm's own conditional-build mechanism so the
    # spec is not edited (SPEC.md section 5).
    for name in args.with_ or []:
        cmd += ["--with", name]
    for name in args.without or []:
        cmd += ["--without", name]

    if args.nocheck:
        cmd += ["--nocheck"]

    # The buildroot is assembled by unpacking rpm payloads, so it has no
    # rpmdb (SPEC.md section 3a).  rpmbuild's BuildRequires check queries
    # that database rather than the filesystem, so without --nodeps every
    # single BuildRequires fails -- "gcc is needed by gzip" with
    # /usr/bin/gcc sitting right there in the tree.
    #
    # Dropping the check is not dropping the dependencies.  Which packages
    # are in the buildroot is decided by tools/solve.py, pinned by sha256
    # in the lockfile, and reviewed as a diff; rpm re-deriving the same
    # answer from a database we would have to fabricate adds nothing.  It
    # does cost one thing worth naming: rpm no longer catches a buildroot
    # the solver got wrong, so a missing BuildRequires now surfaces as a
    # compile error rather than a clear dependency error.
    cmd += ["--nodeps"]

    if buildroot_dir:
        cmd += ["--buildroot", os.path.abspath(buildroot_dir)]

    cmd.append(spec)
    return cmd


def collect_rpms(topdir, out_rpms):
    os.makedirs(out_rpms, exist_ok=True)
    found = []
    for path in sorted(glob.glob(os.path.join(topdir, "RPMS", "**", "*.rpm"), recursive=True)):
        dest = os.path.join(out_rpms, os.path.basename(path))
        shutil.copy2(path, dest)
        found.append(dest)
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topdir", required=True, help="unpacked srpm topdir (read-only)")
    # Defaults to a fresh mkdtemp.  Deliberately NOT a path the rule picks:
    # a relative path would resolve against the action's cwd -- the project
    # root -- so it would both pollute the source tree and collide between
    # concurrent srpm_build actions sharing the same name.
    ap.add_argument("--work", default=None,
                    help="writable scratch dir for the build "
                         "(default: a temporary directory)")
    ap.add_argument("--keep-work", action="store_true",
                    help="leave the scratch dir behind for debugging")
    ap.add_argument("--out-rpms", required=True)
    ap.add_argument("--out-installroot", required=True)
    ap.add_argument("--out-manifest", default=None, help="JSON build manifest")

    ap.add_argument("--buildroot-tree", default=None, help="flavor buildroot root")
    ap.add_argument("--provenance", default="host",
                    choices=["host", "binary-seed", "bootstrapped"])
    ap.add_argument("--isolation", default="none",
                    choices=["none", "bwrap", "unshare", "auto"])
    ap.add_argument("--dep-installroot", action="append", default=[],
                    help="dependency installroot to overlay (repeatable)")

    ap.add_argument("--rpmbuild", default="rpmbuild")
    ap.add_argument("--target-cpu", default=None)
    ap.add_argument("--dist-tag", default=None)
    ap.add_argument("--fedora-release", default=None)
    ap.add_argument("--define", action="append", default=[],
                    help="raw rpm --define expression (repeatable)")
    ap.add_argument("--with", dest="with_", action="append", default=[])
    ap.add_argument("--without", action="append", default=[])
    ap.add_argument("--nocheck", action="store_true",
                    help="skip %%check; on by default for bulk rebuilds")
    ap.add_argument("--source-date-epoch", default="1700000000")
    ap.add_argument("--macros", default=None,
                    help="extra rpm macro file, loaded on top of the "
                         "normal macro path")
    ap.add_argument("--env", action="append", default=[], metavar="KEY=VALUE",
                    help="extra environment variable (repeatable)")
    args = ap.parse_args()

    args.isolation = resolve_isolation(args.isolation)

    # Only the unsandboxed path uses the host's rpmbuild.  Under a sandbox
    # the binary comes from inside the buildroot, so checking for one on
    # the host would be checking the wrong file -- and would happily pass
    # on a machine that has no business supplying it.
    if args.isolation == "none":
        require_tool(args.rpmbuild)
    else:
        args.rpmbuild = "/usr/bin/rpmbuild"

    if args.work:
        work = os.path.abspath(args.work)
        shutil.rmtree(work, ignore_errors=True)
        os.makedirs(work, exist_ok=True)
    else:
        work = tempfile.mkdtemp(prefix="buckos-distro-replay-")

    topdir = os.path.join(work, "topdir")
    copy_topdir(args.topdir, topdir)
    os.makedirs(os.path.join(topdir, "tmp"), exist_ok=True)

    spec = find_spec(topdir)

    # The flavor macro file is a repo source, so its path is outside
    # anything the sandbox mounts and `--load` of it fails inside the
    # chroot with nothing but "failed to load macro file".  Copy it into
    # the work area, which is bound at its real path in every isolation
    # mode, and load it from there.
    if args.macros:
        staged_macros = os.path.join(work, os.path.basename(args.macros))
        shutil.copyfile(args.macros, staged_macros)
        args.macros = staged_macros

    # rpmbuild writes the staged install tree here; we then hand it out
    # as PackageInfo.prefix.
    buildroot_dir = os.path.join(work, "installroot")
    os.makedirs(buildroot_dir, exist_ok=True)

    # Compose the environment the compiler will see: the flavor seed as the
    # base, then each resolved BuildRequires installroot on top.  Always
    # into a fresh tree under work/ -- never onto the seed, which is a
    # shared Buck input.
    sysroot = None
    if args.buildroot_tree or args.dep_installroot:
        sysroot = compose_buildroot(
            args.buildroot_tree,
            args.dep_installroot,
            os.path.join(work, "sysroot"),
        )

    env = reproducible_env(source_date_epoch=args.source_date_epoch)
    if sysroot and args.isolation == "none":
        env.update(sysroot_env(sysroot, env))
    elif args.isolation != "none":
        # Inside the chroot the normal paths already resolve to the
        # buildroot, and an inherited PATH would name host directories
        # that either do not exist there or, worse, do.
        env["PATH"] = "/usr/bin:/usr/sbin:/bin:/sbin"
        env["HOME"] = "/builddir"
    for assignment in args.env:
        key, _, value = assignment.partition("=")
        env[key] = value

    cmd = build_rpmbuild_cmd(spec, topdir, args, buildroot_dir)

    print(
        "buckos-distro: replaying {} (provenance={}, isolation={})".format(
            os.path.basename(spec), args.provenance, args.isolation
        ),
        file=sys.stderr,
    )
    run_isolated(cmd, args.isolation, work, topdir, sysroot, env=env)

    rpms = collect_rpms(topdir, args.out_rpms)
    if not rpms:
        sys.exit("rpmbuild produced no binary rpms for {}".format(spec))

    # Derive the installroot by unpacking the produced rpms, NOT by
    # copying rpmbuild's BUILDROOT tree.  Three reasons:
    #   * rpm's default %clean is `rm -rf %{buildroot}`, so the tree is
    #     usually gone by the time we get here;
    #   * the rpms are the actual product -- they have been through the
    #     brp-* scripts (strip, shebang mangling, manpage compression),
    #     so they match what would really be installed;
    #   * subpackage projection (rpm_subpackage) needs per-rpm unpacking
    #     anyway, and this keeps both paths on one code path.
    out_installroot = os.path.abspath(args.out_installroot)
    shutil.rmtree(out_installroot, ignore_errors=True)
    os.makedirs(out_installroot, exist_ok=True)
    for rpm_path in rpms:
        if rpm_path.endswith(".src.rpm"):
            continue
        extract_rpm(rpm_path, out_installroot)

    if args.out_manifest:
        with open(args.out_manifest, "w") as fh:
            json.dump(
                {
                    "spec": os.path.basename(spec),
                    "rpms": [os.path.basename(r) for r in rpms],
                    "provenance": args.provenance,
                    "isolation": args.isolation,
                    "hermetic": args.isolation in ("bwrap", "unshare"),
                    "with": args.with_,
                    "without": args.without,
                },
                fh,
                indent=2,
                sort_keys=True,
            )

    print(
        "buckos-distro: produced {} rpm(s): {}".format(
            len(rpms), ", ".join(os.path.basename(r) for r in rpms)
        ),
        file=sys.stderr,
    )

    # Only on success: a failed replay leaves its BUILD tree behind, which
    # is the whole reason you want to look at it.
    if not args.keep_work and not args.work:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
