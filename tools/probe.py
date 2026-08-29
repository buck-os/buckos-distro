#!/usr/bin/env python3
"""Run the dynamic-BuildRequires probes and collect their answers.

Most RPM specs list their BuildRequires and mean it.  Anything
packaged with rust-packaging, go-rpm-macros or pyproject-rpm-macros does
not: it computes them from a lockfile in a %generate_buildrequires shell
block, and repodata carries only the handful of static entries that let
that block run.  Solving from repodata alone therefore produces a
buildroot that is missing most of what the package needs, and the gap
does not surface until %build fails somewhere unrecognisable.

The only way to learn the rest is to run the block.  `rpmbuild -br` does
that and reports what came out, which is what //flavors/<flavor>:probe-<pkg>
wraps.  This drives one probe per source package and merges the results
into a single file that solve.py can read.

    buck2 run //tools:probe -- --release 44 --arch x86_64

Why this is a separate step and not part of the build
-----------------------------------------------------
Buck resolves dependencies during analysis.  An edge discovered by
*running* an action exists only after analysis is over, so there is no
way to add it -- and a rule that tried would be a rule that cannot be
scheduled remotely, since the RE worker would have to be told its inputs
before they were known.  So the discovery happens here, at lock time, and
its result is written into the lockfile where it becomes an ordinary
declared dependency like any other.

That makes the update loop two solves deep:

    solve  ->  generate  ->  probe  ->  solve  ->  generate

The first solve produces a buildroot good enough to run the generators;
the second one knows what they asked for.  mock does the same thing at
build time, installing the static set and then re-running; the difference
is only that a lockfile has to remember the answer.

The RPM-family relock tools run that whole loop with `--probe`. This tool
is separate so the probe can also be run on its own, which is what you
want when a single package's generator starts asking for something new.
"""

import argparse
import json
import os
import subprocess
import sys

import relock
# For PROBE_SCHEMA only: the reader defines the version, so a file written
# here can never claim a version the solver does not accept.
import solve

# Probing means building the package's %prep and running arbitrary shell
# out of its spec.  Under host provenance that shell reads the host's
# /usr, so the answer describes this machine rather than the flavor --
# and the host's rpmbuild may not even be rpm's (a site wrapper that
# refuses to run is the friendly version of that failure).
DEFAULT_CONFIG = ["--config-file", "tools/probe.buckconfig"]


def probe_targets(lock):
    """One target per source package on the build list.

    Named from the lockfile rather than discovered with `buck2 targets`,
    so a probe run covers exactly the set that was solved.  A package
    that has been added to --build but not yet generated fails here as a
    missing target, which is the correct complaint.
    """
    return [
        "//flavors/{}:probe-{}-{}-{}".format(
            lock["flavor"], src, lock["release"], lock["target_cpu"])
        for src in sorted(lock["solve"]["build"])
    ]


def run_probes(buck2, targets, config, cwd):
    """Build every probe and return {target: output path}.

    One invocation, not one per package: the probes share a buildroot and
    most of their inputs, and buck2 schedules them against each other far
    better than a loop out here could.

    --keep-going, and a failure that is reported rather than fatal.  The
    probe pass is bootstrapping: a package's probe can need another
    package *built*, and that build can be exactly what is blocked by the
    dependency this pass exists to discover.  libcap-ng is the case --
    its %ifarch BuildRequires are invisible to repodata, so its buildroot
    is short of clang and libbpf-devel, so it cannot build, so anything
    whose probe wants it built fails too.

    Sinking the whole batch for that would make the pass useless at the
    scale it is for: over a hundred packages, something is always in this
    state, and the answers from the rest are what break the deadlock on
    the next solve.  So partial results are the normal case, and the
    caller is told what is missing rather than left with nothing.
    """
    if not targets:
        return {}
    argv = ([buck2, "build"] + list(config)
            + ["--keep-going", "--show-json-output"] + targets)
    print("$ {}".format(" ".join(argv)), file=sys.stderr)
    proc = subprocess.run(argv, cwd=cwd, stdout=subprocess.PIPE, text=True)

    outputs = {}
    # buck2 prints its progress on stderr and the JSON map on stdout, but
    # only the last line of stdout is that map.  With --keep-going the map
    # carries the targets that succeeded and a null for those that did not.
    for line in reversed(proc.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            outputs = {k: v for k, v in json.loads(line).items() if v}
            break

    # Compared on the label after the cell, because the two sides spell it
    # differently: this passes a cell-relative label and buck2 answers with
    # a cell-qualified one. A plain `in`
    # never matches, which reported every probe as failed while happily
    # returning fifteen results.
    def _label(target):
        return target.split("//", 1)[-1]

    got = {_label(key) for key in outputs}
    missing = [t for t in targets if _label(t) not in got]
    if missing:
        print(
            "buckos-distro: {} of {} probes did not build; solving with the "
            "rest and leaving these for the next pass:\n  {}".format(
                len(missing), len(targets), "\n  ".join(sorted(missing))
            ),
            file=sys.stderr,
        )
    if not outputs:
        sys.exit(
            "probe build failed ({}) and produced nothing to solve "
            "with".format(proc.returncode)
        )
    return outputs


def collect(outputs, cwd):
    """Merge the per-package reports, keyed by the package each declares."""
    packages = {}
    for target, path in sorted(outputs.items()):
        with open(os.path.join(cwd, path)) as fh:
            report = json.load(fh)
        name = report.get("package")
        if not name:
            sys.exit("{} produced a report with no package name".format(target))
        packages[name] = {
            k: report[k]
            for k in ("buildrequires", "dynamic", "static", "generated",
                      "unmet", "spec")
            if k in report
        }
    return packages


def resolve_buck2(root, override=None):
    """Which buck2 to shell out to.

    The repo's own `buck2` first. A checkout that pins its buck2 does so
    because the one on PATH is a different version or, at some sites, a
    wrapper that refuses to run at all -- and a probe answered by the wrong
    buck2 is a probe answered against the wrong graph.
    """
    if override:
        return override
    local = os.path.join(root, "buck2")
    return local if os.path.exists(local) else "buck2"


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--release", action="append", default=[], metavar="N",
                    help="release to probe (repeatable; default: every "
                         "release with a lockfile)")
    ap.add_argument("--arch", default="x86_64", choices=("x86_64", "aarch64"))
    ap.add_argument("--flavor", default="fedora",
                    choices=sorted(solve.IMPLICIT_GROUPS),
                    help="RPM flavor to probe (default: fedora)")
    ap.add_argument("--lock-dir", default=None)
    ap.add_argument("--buck2", default=None,
                    help="buck2 to invoke (default: ./buck2 in the repo, or "
                         "whatever is on PATH)")
    ap.add_argument("-c", "--config", action="append", default=[],
                    metavar="SECTION.KEY=VALUE",
                    help="extra buck2 -c flag (repeatable); replaces the "
                         "default buildroot selection if given")
    args = ap.parse_args(argv)

    root = relock.repo_root()
    lock_dir = args.lock_dir or os.path.join(
        root, "flavors", args.flavor, "lock")

    config = list(DEFAULT_CONFIG)
    for flag in args.config:
        config += ["-c", flag]

    releases = args.release or relock.lockfile_releases(
        lock_dir, args.arch, args.flavor)
    for release in releases:
        lock_path = os.path.join(
            lock_dir,
            relock.lockfile_name(args.flavor, release, args.arch),
        )
        write_probe_file(
            lock_path, root, config,
            buck2=resolve_buck2(root, args.buck2),
        )


def probe_path(lock_path):
    """Where a release's probe results live.

    Beside the lockfile and named for it, because it is the same kind of
    thing: a recorded answer from upstream that gets reviewed as a diff.
    Separate from it rather than merged in because the two are produced by
    different machinery -- repodata is fetched, this is executed -- and a
    reviewer reading a lockfile diff should be able to tell which.
    """
    suffix = ".lock.json"
    if not lock_path.endswith(suffix):
        raise ValueError("lockfile must end in {}: {}".format(
            suffix, lock_path))
    return lock_path[:-len(suffix)] + ".probe.json"


def previous_packages(lock_path):
    """What the last probe run recorded, or nothing if there was none.

    Tolerant of a file that does not parse or predates the schema: this is
    a cache of answers, and a corrupt one should cost a re-probe rather
    than the run.
    """
    path = probe_path(lock_path)
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as fh:
            recorded = json.load(fh)
    except ValueError:
        print("buckos-distro: {} does not parse; starting from an empty "
              "probe set".format(path), file=sys.stderr)
        return {}
    if recorded.get("schema") != solve.PROBE_SCHEMA:
        print("buckos-distro: {} is schema {}, not {}; starting from an "
              "empty probe set".format(
                  path, recorded.get("schema"), solve.PROBE_SCHEMA),
              file=sys.stderr)
        return {}
    return recorded.get("packages", {})


def write_probe_file(lock_path, root, config=None, buck2=None):
    if not os.path.exists(lock_path):
        sys.exit("no lockfile at {}".format(lock_path))
    with open(lock_path) as fh:
        lock = json.load(fh)
    flavor = lock["flavor"]
    release = lock["release"]

    config = DEFAULT_CONFIG if config is None else config
    print("{} {}: probing {} source package(s)".format(
        flavor, release, len(lock["solve"]["build"])), file=sys.stderr)
    outputs = run_probes(resolve_buck2(root, buck2),
                         probe_targets(lock), config, root)

    # Layered over whatever is already recorded, not written in place of it.
    #
    # A probe that fails is the normal case -- run_probes says so at length
    # -- but this file is the solver's only source for BuildRequires that
    # repodata cannot see, so dropping a package from it silently downgrades
    # the next solve to the weaker answer.  gcc is the case: its probe
    # failed one run, its entry vanished, and the solve fell back to
    # arch-neutral source repodata, which carries neither its
    # %ifarch-guarded multilib BuildRequires nor llvm and lld.  The lockfile
    # still said 0 unresolved; rpmbuild refused the build three steps later
    # with a dependency the solver had known about the run before.
    #
    # A fresh answer always wins, so re-probing still updates.  What this
    # prevents is a *missing* answer counting as a new one.
    packages = dict(previous_packages(lock_path))
    fresh = collect(outputs, root)
    kept = sorted(set(packages) - set(fresh))
    packages.update(fresh)
    if kept:
        print("  keeping {} earlier result(s) for packages that did not "
              "probe this run: {}".format(len(kept), ", ".join(kept)),
              file=sys.stderr)

    out = probe_path(lock_path)
    with open(out, "w") as fh:
        json.dump(
            {
                "schema": solve.PROBE_SCHEMA,
                "flavor": flavor,
                "release": lock["release"],
                # What the probes were run against.  A probe answer is only
                # as good as the buildroot that produced it -- a generator
                # can and does branch on the version of its own toolchain --
                # so the file says which one that was.
                "buildroot": " ".join(config),
                "packages": packages,
            },
            fh,
            indent=2,
            sort_keys=True,
        )
        fh.write("\n")

    generated = sorted(n for n, p in packages.items() if p.get("generated"))
    unmet = sorted(n for n, p in packages.items() if p.get("unmet"))
    print("  wrote {} ({} with a generator{})".format(
        os.path.relpath(out, root), len(generated),
        ", {} still unmet".format(len(unmet)) if unmet else "",
    ), file=sys.stderr)
    for name in unmet:
        # Worth naming rather than counting: an unmet probe means the
        # generator asked for something the buildroot does not have, so
        # the *next* solve is the one that fixes it -- and if the name
        # persists across two probe runs, it is not going to.
        print("  unmet: {} -> {}".format(
            name, ", ".join(packages[name]["dynamic"]) or "(nothing new)"),
            file=sys.stderr)


if __name__ == "__main__":
    main()
