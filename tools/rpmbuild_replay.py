#!/usr/bin/env python3
"""Replay a Fedora spec file with rpmbuild inside a Buck action.

This is the heart of the fedora flavor.  The spec is never rewritten: we
hand it to rpmbuild unchanged and control the build purely through
--define, --with/--without, and the contents of the buildroot.  See
SPEC.md section 1.

Two stages, selected with --stage:

    bb  (default)  build binary rpms
        --out-rpms         directory of produced binary .rpm files
        --out-installroot  the unpacked BUILDROOT tree, which becomes
                           PackageInfo.prefix and feeds rootfs assembly

    br             run %generate_buildrequires and stop
        --out-buildrequires  JSON: the spec's full BuildRequires set and
                             which of them only exist once the generator
                             has run

The probe exists because a spec's dependencies are not always in the
spec.  Anything packaged with rust-packaging, go-rpm-macros or
pyproject-rpm-macros computes its BuildRequires from a lockfile at build
time, so repodata cannot answer what it needs -- see SPEC.md section 3a.
Buck cannot grow an edge mid-build either, so the probe runs at lock
time and its answer is recorded in the lockfile.

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
import shlex
import shutil
import sys

from _isolation import resolve_isolation, run_isolated
from _rpm import (
    extract_rpm,
    overlay_tree,
    stage_rpms,
    reproducible_env,
    require_tool,
    run,
    scratch_dir,
)

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

    Replace, not merge, within a layer: a dep_root is nearly always a
    locally built copy of something the seed already pins -- that is the
    whole point of a bootstrap stage -- so its paths must supersede the
    seed's rather than collide with them.  overlay_tree does that; plain
    copytree cannot.
    """
    os.makedirs(dest, exist_ok=True)
    layers = ([seed_root] if seed_root else []) + list(dep_roots)
    for layer in layers:
        if not os.path.isdir(layer):
            continue
        overlay_tree(layer, dest)
    return dest



def install_deps(sysroot, dep_rpms, args, work, env):
    """Install the resolved BuildRequires into the composed sysroot.

    compose_buildroot has already overlaid their *files*; this is the rpm
    transaction that makes the sysroot agree with itself.  Three things
    only a transaction supplies:

      * the database.  rpmbuild resolves BuildRequires against it, not
        against the filesystem, so an overlay alone fails on a package
        whose files are demonstrably present:

            error: Failed build dependencies:
                autoconf is needed by xz-1:5.8.1-4.fc43.x86_64

      * %post.  golang-bin registers /usr/bin/go through
        update-alternatives, and /usr/bin/go ships as a symlink into
        /etc/alternatives.  Without the scriptlet it dangles, libcap
        autodetects no Go, silently omits its captree program and fails in
        %files on a file nothing said it was skipping.

      * ownership and modes, as the package declares them rather than as
        the overlay copied them.

    --replacepkgs --replacefiles, because superseding is the normal case
    here rather than an error.  A bootstrap stage rebuilds something the
    base already carries at the same NEVRA, so rpm sees a reinstall of a
    package it knows and a file conflict against the copy the overlay just
    wrote -- the tree describing itself.  Replacing is the intent: this
    build's zlib-ng-compat is the locally built one, not the seed's.

    --nodeps, because this is a partial view by construction.  These
    packages' own dependencies are in the base, already installed and
    already in the database; the closure was decided by the solver and
    this is installation, not resolution.

    Triggers stay *on*, unlike everywhere else in this repo, and the
    reason is ldconfig.  glibc ships a file trigger that rebuilds
    /etc/ld.so.cache whenever a library lands, and a package installing
    into a non-default directory relies on it: llvm20-libs puts
    libLLVM.so.20.1 under /usr/lib64/llvm20/lib64 and drops a conf file
    naming that path.  With --notriggers the cache is never refreshed, and
    bpftool -- which libcap-ng runs during %build -- dies with

        bpftool: error while loading shared libraries: libLLVM.so.20.1:
                 cannot open shared object file

    on a library that is sitting in the sysroot.  The base buildroot gets
    away without triggers because everything in it lives on the default
    search path; an overlay is exactly where that stops being true.

    The sandbox's mount points are excluded for the same reason
    tools/buildroot_assemble.py excludes them: /proc, /dev, /sys and /tmp
    are live mounts inside the chroot, and the package that owns them
    cannot chown a mount point -- "cpio: chown failed - Device or resource
    busy".  They are in the base already, so nothing is lost.
    """
    # Refused rather than attempted without a sandbox.  run_isolated's
    # "none" mode runs the command directly, with no chroot, so `sysroot`
    # would be ignored and this would install the overlay into the
    # developer's own machine -- with --replacefiles, over their own
    # packages.  Nothing routes host provenance here today, since --dep-rpm
    # is only emitted for a seeded buildroot, and that is precisely why the
    # guard belongs here: the day something does, the failure is silent and
    # off-target.
    if args.isolation == "none":
        sys.exit(
            "install_deps needs a sandbox: isolation=none would install "
            "these {} package(s) onto the host rather than into {}".format(
                len(dep_rpms), sysroot)
        )

    staging = os.path.join(work, "deprpms")
    stage_rpms([os.path.abspath(path) for path in dep_rpms], staging)

    script = (
        "set -e\n"
        'exec rpm --install --nosignature --nodeps '
        '--replacepkgs --replacefiles '
        '--excludepath /dev --excludepath /proc '
        '--excludepath /sys --excludepath /tmp '
        '"$1"/*.rpm\n'
    )
    run_isolated(
        ["/bin/sh", "-c", script, "sh", staging],
        args.isolation, work=work, chdir=work, sysroot=sysroot, env=env,
    )


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


def spec_macro_args(topdir, args):
    """The macro state a spec must be read under, for any rpm tool.

    Shared with rpmspec rather than inlined into the rpmbuild command,
    because the two have to agree.  A spec that guards BuildRequires with
    `%if 0%{?fedora} >= 43` or `%{with foo}` -- most of them do -- reports
    a different dependency set under a different macro state, and the
    dynamic-BuildRequires probe subtracts one tool's answer from the
    other's.  Any disagreement between them shows up as a phantom
    dependency, or as a real one going missing.
    """
    cmd = []

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

    return cmd


def build_rpmbuild_cmd(spec, topdir, args, buildroot_dir):
    cmd = [args.rpmbuild, "-" + args.stage]
    cmd += spec_macro_args(topdir, args)

    if args.nocheck:
        cmd += ["--nocheck"]

    # On by request only.  This used to be unconditional, because the
    # buildroot was payloads unpacked by tar with no rpmdb at all, and
    # rpmbuild answers BuildRequires from the database rather than from
    # the filesystem -- so every single one failed, "gcc is needed by
    # gzip" with /usr/bin/gcc sitting right there in the tree.
    #
    # tools/buildroot_assemble.py now registers the seed set with a real
    # `rpm --justdb` transaction, so the database agrees with the tree and
    # the check is worth having: it is what turns a buildroot the solver
    # got wrong from a confusing compile error deep in %build into a named
    # missing dependency before %prep starts.  It is also the mechanism a
    # dynamic-BuildRequires probe needs, since `rpmbuild -br` reports what
    # is missing by consulting exactly this database.
    if args.nodeps:
        cmd += ["--nodeps"]

    # -br stops before %build, so there is no install tree to direct.
    if buildroot_dir and args.stage == "bb":
        cmd += ["--buildroot", os.path.abspath(buildroot_dir)]

    cmd.append(spec)
    return cmd


# rpmbuild's own exit code for "%generate_buildrequires produced dependencies
# that are not installed".  Not a failure in probe mode -- it is the answer.
BUILDREQUIRES_UNMET = 11

# What `rpmbuild -br` writes when the generator asked for something the
# buildroot does not have.  The name is rpm's, not ours: <nvr>.buildreqs,
# with the nosrc suffix because the sources are left out of a header that
# exists only to carry a dependency list.  Written together with exit 11.
_NOSRC_GLOB = "*.buildreqs.nosrc.rpm"

# What it writes otherwise -- which is *two* different situations, not one.
# A spec with no %generate_buildrequires has nothing to compute, so `-br`
# degrades to plain `-bs`.  A spec that has one whose output is already
# installed gets past the check and goes on to write an ordinary source
# package too, with the generated dependencies merged into its header.
# Both look identical from out here, which is why "did a generator run" is
# answered by comparing the header against the spec rather than by which
# file appeared.  Either way its Requires are the BuildRequires, so one
# code path reads all three cases.
_SRC_GLOB = "*.src.rpm"


def probe_buildrequires(spec, topdir, work, args, sysroot, env):
    """Run %generate_buildrequires and report what it asked for.

    Dynamic BuildRequires are computed by a shell script in the spec, so
    they cannot be read out of repodata -- the only way to learn them is
    to run the script (SPEC.md section 3a).  `rpmbuild -br` does exactly
    that and stops: it runs %prep and %generate_buildrequires, merges what
    came out into a source header, and stops before %build.  If any of the
    generated dependencies is missing from the buildroot it exits 11 and
    the header is a `.buildreqs.nosrc.rpm`; otherwise it exits 0 with an
    ordinary `.src.rpm`.  Both carry the same list, so both answer.

    Two queries, because the interesting number is a difference.  The
    header carries the *union* of the spec's static BuildRequires and
    whatever the generator emitted -- a source header's Requires are its
    BuildRequires, which is why one query answers both.  `rpmspec -q
    --buildrequires` carries the static ones alone, since it parses the
    spec without running anything.  Subtracting gives the dynamic set.

    The difference is not merely which capabilities appear, either: it is
    also how tightly they are versioned.  python-appdirs's header asks for
    `python3dist(pip) >= 19` where Fedora's own repodata records a bare
    `python3dist(pip)`, so the probe is strictly better information even
    for a package whose dependency *names* were already known.

    Everything runs inside the buildroot rather than out here.  The header
    was written by the buildroot's rpm, and rpm 6 headers are not
    reliably readable by whatever rpm the host happens to ship -- and the
    host is not supposed to be part of the answer anyway.
    """
    requires_out = os.path.join(work, "br-all.txt")
    static_out = os.path.join(work, "br-static.txt")
    rc_out = os.path.join(work, "br-rc.txt")
    header_out = os.path.join(work, "br-header.txt")

    build = build_rpmbuild_cmd(spec, topdir, args, None)
    query = [args.rpmspec, "-q", "--buildrequires"]
    query += spec_macro_args(topdir, args) + [spec]

    script = (
        "set -e\n"
        # The static query goes first, and unconditionally, because it is
        # the half that always answers.  rpmspec parses the spec and runs
        # nothing, so it needs no buildroot -- and it evaluates %ifarch,
        # which is the whole reason this ordering matters.
        #
        # A source header records the BuildRequires that were in force
        # when the srpm was built, and Fedora's srpms are arch-neutral, so
        # an %ifarch-guarded BuildRequires is simply absent from repodata.
        # libcap-ng is the case that found this: its spec asks for clang,
        # bpftool, libbpf-devel and audit-libs-devel inside
        # `%ifarch %{bpf_supported_arches}`, none of which repodata
        # mentions, so the solver built a buildroot without them and
        # rpmbuild refused to start.
        #
        # Which is exactly when the -br below cannot run: rpm checks the
        # static BuildRequires before it will execute a generator.  Asking
        # rpmspec first means the probe still returns the list that fixes
        # the buildroot, instead of failing with a message about a missing
        # header.
        "{query} > {staticout}\n"
        # Exit 11 is an answer, not a failure: it means the generator
        # asked for something the buildroot does not have, which is
        # exactly what a probe is for.  Recorded rather than swallowed --
        # a lock run wants to know that the set it just learned was
        # unsatisfiable in the buildroot it was learned in.
        "rc=0\n"
        "{build} || rc=$?\n"
        'if [ "$rc" -ne 0 ] && [ "$rc" -ne {unmet} ]; then exit "$rc"; fi\n'
        'echo "$rc" > {rcout}\n'
        # Globbed rather than reconstructed from the NVR, which would mean
        # re-deriving %dist and the epoch out here.  Exactly one of each
        # kind is written, and the nosrc one wins when both exist: it is
        # the one whose header the generator contributed to.
        "set -- {srpms}/{nosrc} {srpms}/{src}\n"
        'for f in "$@"; do\n'
        '  if [ -e "$f" ]; then\n'
        '    echo "$f" > {headerout}\n'
        '    rpm -qp --requires "$f" > {allout}\n'
        "    break\n"
        "  fi\n"
        "done\n"
        # No header is a partial answer rather than an error.  rpm wrote
        # none because it stopped at the static dependency check, so the
        # dynamic set is unknown -- but the static set above is known, and
        # it is the one that unblocks the next solve.  Saying so and
        # carrying on beats failing the probe of every %ifarch package.
        "if [ ! -s {headerout} ]; then\n"
        '  echo "buckos-distro: rpmbuild -br stopped before writing a '
        'source header, so only the static BuildRequires were learned. '
        'Usually an unmet static BuildRequires -- re-solve with these and '
        'probe again to reach the dynamic ones." >&2\n'
        "fi\n"
    ).format(
        build=_join(build),
        query=_join(query),
        unmet=BUILDREQUIRES_UNMET,
        srpms=shlex.quote(os.path.join(topdir, "SRPMS")),
        nosrc=_NOSRC_GLOB,
        src=_SRC_GLOB,
        rcout=shlex.quote(rc_out),
        headerout=shlex.quote(header_out),
        allout=shlex.quote(requires_out),
        staticout=shlex.quote(static_out),
    )
    run_isolated(["/bin/sh", "-c", script], args.isolation,
                 work=work, chdir=topdir, sysroot=sysroot, env=env)

    def _slurp(path):
        return open(path).read().strip() if os.path.exists(path) else ""

    header = _slurp(header_out)
    unmet = _slurp(rc_out) == str(BUILDREQUIRES_UNMET)
    static_set = set(_read_capabilities(static_out))
    # Without a header there is nothing to subtract, so the union *is* the
    # static set and the dynamic set is unknown rather than empty.  Those
    # are different claims and the caller has to be able to tell them
    # apart -- `probed` below is what says which one this is.
    probed = bool(header)
    all_caps = _read_capabilities(requires_out) if probed else sorted(static_set)
    dynamic = sorted({c for c in all_caps if c not in static_set})
    return {
        "package": args.package_name,
        "spec": os.path.basename(spec),
        # Did a generator contribute to this answer?  Read off the
        # difference, not off which file rpm wrote: a spec whose generated
        # dependencies are all already installed produces an ordinary
        # .src.rpm, indistinguishable by name from a spec that has no
        # generator at all -- and python-appdirs is exactly that case, with
        # four capabilities in its header that its spec does not declare.
        #
        # The nosrc header is still conclusive on its own, since rpm only
        # writes one after running a generator; it just is not the only
        # way to have run one.
        #
        # A generator that emits nothing the spec has not already declared
        # reads as False here.  That is unobservable from outside and
        # harmless: it means the probe and the spec agree, which is the
        # only thing the flag is consulted for.
        "generated": bool(dynamic) or header.endswith(".nosrc.rpm"),
        "unmet": unmet,
        # False when rpmbuild stopped before writing a header, which means
        # `dynamic` is "not looked at" rather than "none".  A re-probe
        # after the static set has been solved in is what turns it True.
        "probed": probed,
        "buildrequires": sorted(set(all_caps)),
        "static": sorted(static_set),
        "dynamic": dynamic,
    }


def _join(argv):
    return " ".join(shlex.quote(str(c)) for c in argv)


def _read_capabilities(path):
    """Capability names from one rpm query, minus rpm's own.

    rpmlib(...) entries describe header features the *reader* must
    support, not packages anything can install, so a solver handed one
    reports an unresolvable dependency on something that does not exist.

    Whitespace is collapsed because the two queries have to be compared
    to each other and only agree up to it: both print `name op version`,
    and a difference in spacing alone would read as a dynamic dependency
    that the generator never emitted.
    """
    caps = []
    with open(path) as fh:
        for line in fh:
            cap = " ".join(line.split())
            if not cap or cap.startswith("rpmlib("):
                continue
            caps.append(cap)
    return caps


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
    # rpmbuild's own stage letters.  "bb" builds binary rpms; "br" stops
    # after %generate_buildrequires and reports the dependency set instead
    # of producing anything.  One driver rather than two because every
    # line above and below this one -- topdir copy, macro staging,
    # buildroot composition, sandbox setup -- is identical for both, and
    # a probe that set any of them up differently would be answering a
    # question about a build that never happens.
    ap.add_argument("--stage", default="bb", choices=["bb", "br"])
    ap.add_argument("--out-rpms", default=None)
    ap.add_argument("--out-installroot", default=None)
    ap.add_argument("--out-manifest", default=None, help="JSON build manifest")
    ap.add_argument("--out-buildrequires", default=None,
                    help="JSON dependency report (--stage br)")

    ap.add_argument("--buildroot-tree", default=None, help="flavor buildroot root")
    ap.add_argument("--provenance", default="host",
                    choices=["host", "binary-seed", "bootstrapped"])
    ap.add_argument("--isolation", default="none",
                    choices=["none", "bwrap", "unshare", "auto"])
    ap.add_argument("--dep-rpm", action="append", default=[],
                    help="the .rpm a --dep-installroot was unpacked from, "
                         "registered in the sysroot's database so rpmbuild's "
                         "BuildRequires check can see it (repeatable)")
    ap.add_argument("--dep-installroot", action="append", default=[],
                    help="dependency installroot to overlay (repeatable)")

    ap.add_argument("--rpmbuild", default="rpmbuild")
    ap.add_argument("--rpmspec", default="rpmspec")
    # Recorded in the probe report so the file says which source package it
    # describes.  The consumer collects a directory of these and has to key
    # them somehow; deriving the name from a Buck target label would make
    # the lockfile depend on a target naming scheme that the flavor macros
    # own and are free to change.
    ap.add_argument("--package-name", default=None)
    ap.add_argument("--target-cpu", default=None)
    ap.add_argument("--dist-tag", default=None)
    ap.add_argument("--fedora-release", default=None)
    ap.add_argument("--define", action="append", default=[],
                    help="raw rpm --define expression (repeatable)")
    ap.add_argument("--with", dest="with_", action="append", default=[])
    ap.add_argument("--without", action="append", default=[])
    ap.add_argument("--nocheck", action="store_true",
                    help="skip %%check; on by default for bulk rebuilds")
    ap.add_argument("--nodeps", action="store_true",
                    help="skip rpmbuild's BuildRequires check (see "
                         "build_rpmbuild_cmd)")
    ap.add_argument("--source-date-epoch", default="1700000000")
    ap.add_argument("--macros", default=None,
                    help="extra rpm macro file, loaded on top of the "
                         "normal macro path")
    ap.add_argument("--env", action="append", default=[], metavar="KEY=VALUE",
                    help="extra environment variable (repeatable)")
    args = ap.parse_args()

    required = {
        "bb": ["out_rpms", "out_installroot"],
        "br": ["out_buildrequires"],
    }[args.stage]
    for name in required:
        if not getattr(args, name):
            ap.error("--stage {} requires --{}".format(
                args.stage, name.replace("_", "-")))

    args.isolation = resolve_isolation(args.isolation)

    # Only the unsandboxed path uses the host's rpmbuild.  Under a sandbox
    # the binary comes from inside the buildroot, so checking for one on
    # the host would be checking the wrong file -- and would happily pass
    # on a machine that has no business supplying it.
    if args.isolation == "none":
        require_tool(args.rpmbuild)
        if args.stage == "br":
            require_tool(args.rpmspec)
    else:
        args.rpmbuild = "/usr/bin/rpmbuild"
        args.rpmspec = "/usr/bin/rpmspec"

    if args.work:
        work = os.path.abspath(args.work)
        shutil.rmtree(work, ignore_errors=True)
        os.makedirs(work, exist_ok=True)
    else:
        # Keyed, not random: this directory becomes %_topdir, and a
        # %_topdir that changes between runs changes the build-id of
        # every binary the package ships.  scratch_dir's docstring has
        # the chain.  An output path is the key because Buck gives each
        # target-and-configuration its own, so it is unique between
        # concurrent actions and identical between reruns of one.
        #
        # Used as spelled, not abspath'd: Buck passes it project-relative,
        # and that is the whole point -- an absolute path carries the
        # checkout location, so two developers of the same commit would
        # key differently and produce different build-ids for identical
        # sources.  Which would make the shared cache a liar.
        work = scratch_dir("buckos-distro-replay-",
                           key=args.out_installroot or args.out_buildrequires)

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

    # Shaped before anything runs inside the sandbox, not after.  This used
    # to sit below install_deps, which meant the overlay transaction ran
    # with reproducible_env's inherited *host* PATH -- and inside the
    # chroot those directories do not exist, so `rpm` was simply not found
    # and every affected replay died on exit 127 with no other explanation.
    #
    # It survived early testing because a host PATH usually contains
    # /usr/bin, which does resolve in the chroot, to the buildroot's own
    # rpm.  That is the bad kind of working: the command found was
    # whichever one the ambient environment happened to expose.
    if sysroot and args.isolation == "none":
        env.update(sysroot_env(sysroot, env))
    elif args.isolation != "none":
        # Inside the chroot the normal paths already resolve to the
        # buildroot, and an inherited PATH would name host directories
        # that either do not exist there or, worse, do.
        env["PATH"] = "/usr/bin:/usr/sbin:/bin:/sbin"
        env["HOME"] = "/builddir"

        # Same class of problem, and it is the one that actually bites.
        # Buck2 points TMPDIR at a per-action directory under buck-out,
        # which is not bound into the sandbox, so the first %install step
        # that calls mktemp dies -- redhat-rpm-config's check-buildroot
        # does, in every package that has an %install at all.  The failure
        # names a buck-out path and reads as a Buck problem rather than as
        # an environment one.
        #
        # Pointed at rpm's own %_tmppath, not at /tmp: /tmp inside the
        # sandbox is a tmpfs, and a large package's %install temporaries
        # would then be charged to memory.  This one is under the work
        # area, on real disk, bound at the same absolute path in and out.
        sandbox_tmp = os.path.abspath(os.path.join(topdir, "tmp"))
        os.makedirs(sandbox_tmp, exist_ok=True)
        for var in ("TMPDIR", "TMP", "TEMP"):
            env[var] = sandbox_tmp

    if sysroot and args.dep_rpm:
        install_deps(sysroot, args.dep_rpm, args, work, env)

    for assignment in args.env:
        key, _, value = assignment.partition("=")
        env[key] = value

    print(
        "buckos-distro: replaying {} -{} (provenance={}, isolation={})".format(
            os.path.basename(spec), args.stage, args.provenance, args.isolation
        ),
        file=sys.stderr,
    )

    if args.stage == "br":
        report = probe_buildrequires(spec, topdir, work, args, sysroot, env)
        with open(args.out_buildrequires, "w") as fh:
            json.dump(report, fh, indent=2, sort_keys=True)
        print(
            "buckos-distro: {} declares {} BuildRequires, {} of them "
            "dynamic{}".format(
                report["spec"], len(report["buildrequires"]),
                len(report["dynamic"]),
                " (unmet in this buildroot)" if report["unmet"] else "",
            ),
            file=sys.stderr,
        )
        if not args.keep_work and not args.work:
            shutil.rmtree(work, ignore_errors=True)
        return

    cmd = build_rpmbuild_cmd(spec, topdir, args, buildroot_dir)
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
