#!/usr/bin/env python3
"""Refresh a release's lockfile against upstream's current repodata.

This is the update loop: fetch each repo's primary.xml, re-solve, and
regenerate the .bzl data. Run it on whatever cadence you want fixes at --
daily, weekly, on a security advisory -- and review the resulting lockfile
diff, which is the whole point of the exercise. Nothing here runs inside
the build graph; see the comment in tools/BUCK on why the solve is a
human-run generate step rather than a build action.

    buck2 run //tools:relock -- --release 44 --arch x86_64

What makes the refresh meaningful is `updates`. Fedora publishes a release
twice: the frozen GA compose under releases/, which never changes, and
every post-GA rebuild under updates/. Solving against releases/ alone
produces a lockfile that is perfectly reproducible and permanently
unpatched. Both are layered here, newest build wins, and each pinned
package records which tree it came from -- because an updated rpm has the
same repo-relative path under updates/ that its original has under
releases/, so the base URL cannot be recovered later.

The solve's own inputs -- the build list, the overrides, the image sets --
are read back out of the existing lockfile rather than passed again, so a
refresh changes package versions and nothing else. A release with no
lockfile yet is not something this can bootstrap: choosing that first set
of overrides is iterative human work.

Expect new ambiguities over time. An override settles a capability that
several packages provide, and updates/ can introduce a new provider of a
capability that had exactly one -- an older Fedora python compat
interpreter started providing python(abi), for instance. That surfaces
here as an unresolved-capability report naming both candidates, and is
fixed by adding an --override to the lockfile's solve block and re-running.
"""

import argparse
import hashlib
import json
import os
import sys
import urllib.request
import xml.etree.ElementTree as ET

import generate
import solve

UPSTREAM = "https://dl.fedoraproject.org/pub/fedora/linux"
REPOMD_NS = "http://linux.duke.edu/metadata/repo"

# Where each repo lives under the upstream root, and what kind of packages
# it holds. Order matters: later repos are layered over earlier ones.
#
# The paths are upstream's and are not as regular as they look. updates/
# has no `os` component where releases/ does, and the source tree is
# `source/tree` in both -- not `SRPMS`, which 404s. These are transcribed
# from what the server actually serves, so resist tidying them.
FEDORA_REPOS = [
    ("binary-releases", "binary", "releases/{release}/Everything/{arch}/os"),
    ("binary-updates", "binary", "updates/{release}/Everything/{arch}"),
    ("source-releases", "source", "releases/{release}/Everything/source/tree"),
    ("source-updates", "source", "updates/{release}/Everything/source/tree"),
]

# A release that has branched from rawhide but has not reached GA is served
# from development/ instead, and has no updates/ tree at all -- there have
# been no post-GA pushes because there has been no GA.
#
# The repo *names* are deliberately the ones above rather than
# development-flavoured spellings. Every pin records the repo it came from,
# and at GA the same packages simply move from development/ to releases/;
# naming them for the tree they happen to sit in today would churn every
# pin's `repo` field on a day when nothing about the packages changed.
FEDORA_BRANCHED_REPOS = [
    ("binary-releases", "binary",
     "development/{release}/Everything/{arch}/os"),
    ("source-releases", "source",
     "development/{release}/Everything/source/tree"),
]

# Repos that legitimately do not exist yet. A release has no updates/ tree
# until its first post-GA push, so a 404 there is news, not an error.
OPTIONAL = {"binary-updates", "source-updates"}


def repo_root():
    """Where the checked-in lockfiles live.

    Neither the working directory nor __file__ is reliable on its own:
    `buck2 run` leaves the cwd wherever it was invoked and hands the script
    a path inside a packaged binary, while running the file directly leaves
    the cwd anywhere at all. Walking up for the marker that defines the
    repo works for both.
    """
    seen = []
    for start in (os.getcwd(), os.path.dirname(os.path.abspath(__file__))):
        directory = start
        while True:
            if os.path.exists(os.path.join(directory, ".buckroot")):
                return directory
            parent = os.path.dirname(directory)
            if parent == directory:
                seen.append(start)
                break
            directory = parent
    sys.exit("cannot find the repo root (no .buckroot above {}); pass "
             "--lock-dir".format(" or ".join(seen)))


def lockfile_name(flavor, release, arch):
    """Canonical RPM-family lockfile name."""
    return "{}-{}-{}.lock.json".format(flavor, release, arch)


def lockfile_releases(lock_dir, arch="x86_64", flavor="fedora"):
    """Every release that has been solved at least once.

    Shared with tools/probe.py, which needs the same default and must not
    disagree about it: a probe run that covered a different set of
    releases than the refresh did would write results for one lockfile
    and not the other, silently.
    """
    return sorted(
        name[len(flavor + "-"):-len("-{}.lock.json".format(arch))]
        for name in os.listdir(lock_dir)
        if name.startswith(flavor + "-")
        and name.endswith("-{}.lock.json".format(arch))
    )


def fetch(url):
    with urllib.request.urlopen(url, timeout=120) as resp:
        return resp.read()


def primary_from_repomd(xml_bytes):
    """The primary.xml's href and sha256, as repomd.xml states them."""
    root = ET.fromstring(xml_bytes)
    for data in root.findall("{{{}}}data".format(REPOMD_NS)):
        if data.get("type") != "primary":
            continue
        location = data.find("{{{}}}location".format(REPOMD_NS))
        checksum = data.find("{{{}}}checksum".format(REPOMD_NS))
        if location is None or checksum is None:
            break
        if checksum.get("type") != "sha256":
            sys.exit("repomd.xml gives primary.xml a {} checksum, expected "
                     "sha256".format(checksum.get("type")))
        return location.get("href"), checksum.text
    sys.exit("repomd.xml declares no primary.xml")


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def local_primary(dest_dir):
    """Whatever primary.xml a previous sync left in this repo's directory."""
    if not os.path.isdir(dest_dir):
        return None
    found = sorted(n for n in os.listdir(dest_dir)
                   if n.endswith("-primary.xml.zst"))
    return os.path.join(dest_dir, found[0]) if found else None


def sync_repo(fetch_base, dest_dir, name):
    """Bring one repo's primary.xml local. Returns its path, or None.

    Verified against the sha256 repomd.xml declares, not merely downloaded:
    a truncated read produces a short package list, and a short package list
    solves cleanly to a smaller closure rather than failing.
    """
    try:
        repomd = fetch(fetch_base + "/repodata/repomd.xml")
    except Exception as exc:  # noqa: BLE001 -- urllib raises several kinds
        if name in OPTIONAL:
            print("  {}: not published ({}), skipping".format(name, exc),
                  file=sys.stderr)
            return None
        sys.exit("{}: cannot read repomd.xml from {}: {}".format(
            name, fetch_base, exc))

    href, want = primary_from_repomd(repomd)
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, os.path.basename(href))

    if os.path.exists(path) and sha256_file(path) == want:
        print("  {}: unchanged".format(name), file=sys.stderr)
        return path

    print("  {}: fetching {}".format(name, os.path.basename(href)),
          file=sys.stderr)
    blob = fetch("{}/{}".format(fetch_base, href))
    got = hashlib.sha256(blob).hexdigest()
    if got != want:
        sys.exit("{}: primary.xml sha256 mismatch (repomd says {}, got {})"
                 .format(name, want, got))
    with open(path, "wb") as fh:
        fh.write(blob)

    # Upstream names these by digest, so a refresh leaves the previous one
    # behind. Harmless but confusing: two primary.xml files in a directory
    # and nothing saying which the lockfile was solved from.
    for stale in os.listdir(dest_dir):
        if stale.endswith("-primary.xml.zst") and stale != os.path.basename(path):
            os.remove(os.path.join(dest_dir, stale))
    return path


def recorded_probe(lock, lock_dir):
    """The probe file a lockfile says it was solved with, if any.

    Carried forward rather than re-derived: a release that has been probed
    once must keep being solved against those results, or the next refresh
    quietly drops every dynamically-generated BuildRequires and produces a
    lockfile whose buildroots are smaller for no reviewable reason.

    Missing on disk is an error, not a fallback, for the same reason. The
    probe file is a committed artifact beside the lockfile; if it is gone,
    the fix is to restore it or re-probe, not to solve without it.
    """
    name = lock["solve"].get("probe")
    if not name:
        return None
    path = os.path.join(lock_dir, os.path.basename(name))
    if not os.path.exists(path):
        sys.exit("{} was solved with {}, which is not in {}. Restore it, or "
                 "re-run with --probe to regenerate it.".format(
                     lock.get("release"), name, lock_dir))
    return path


def solve_argv(lock, repos, out, probe=None):
    """Rebuild solve's argv from the lockfile's own recorded inputs.

    As a list, never through a shell: the overrides contain `(`, `)` and `=`
    and passing them back through command substitution mangles them into
    package names that do not exist.
    """
    recorded = lock["solve"]
    argv = [
        "--release", str(lock["release"]),
        "--dist-tag", lock["dist_tag"],
        "--target-cpu", lock.get("target_cpu", "x86_64"),
        "--stages", str(recorded["stages"]),
        "--out", out,
    ]
    if probe:
        argv += ["--probe", probe]
    for repo in repos:
        argv += ["--{}-repo".format(repo["kind"]), repo["name"],
                 "--{}-base".format(repo["kind"]), repo["base"],
                 "--{}-primary".format(repo["kind"]), repo["path"]]
    # Replayed like the rest, and not optional: solve refuses a run with no
    # --build, --seed-only or --seed-package, so a flavor that pins a
    # buildroot without building anything from source -- CentOS today --
    # cannot be refreshed at all if these are dropped.  It fails loudly
    # rather than quietly, which is the only reason this was survivable.
    if recorded.get("seed_only"):
        argv.append("--seed-only")
    builds = recorded.get("explicit_build")
    if builds is None:
        builds = [] if recorded.get("source_image_sets") else recorded.get("build", [])
    for item in builds:
        argv += ["--build", item]
    for flag, key in (("--override", "overrides"),
                      ("--image", "images"),
                      ("--image-override", "image_overrides"),
                      ("--seed-package", "seed_packages"),
                      ("--source-variant", "source_variants"),
                      ("--source-image", "source_image_sets"),
                      ("--prebuilt-source", "prebuilt_sources")):
        for item in recorded.get(key, []):
            argv += [flag, item]
    return argv


def repo_list(release, args, offline=False):
    """The repos to solve from, synced unless told otherwise.

    `offline` is separate from args.offline because the two mean different
    things. The flag is a user saying "do not touch the network"; the
    argument is a caller saying "this release was synced a moment ago" --
    which the probe pass's second solve is, and re-fetching for it would
    let the answer change between two solves that are supposed to differ
    only by the probe results.
    """
    repos = []
    table = (FEDORA_BRANCHED_REPOS if str(release) in args.branched
             else FEDORA_REPOS)
    for name, kind, template in table:
        tail = template.format(release=release, arch=args.arch)
        # The canonical URL is what gets recorded; the mirror, if any, is
        # only where the bytes come from now. Identity is the sha256, so a
        # mirror is substitutable and does not belong in a committed file.
        base = "{}/{}".format(UPSTREAM, tail)
        fetch_base = ("{}/{}".format(args.mirror.rstrip("/"), tail)
                      if args.mirror else base)
        # Named for the repo, so the directory a primary.xml sits in matches
        # the `repo` field every pin that came from it carries.
        dest = os.path.join(args.lock_dir, "repodata", str(release), name)

        if args.dry_run:
            print("  {}: would sync {}".format(name, fetch_base),
                  file=sys.stderr)
            continue
        if offline or args.offline:
            path = local_primary(dest)
            if path is None:
                if name not in OPTIONAL:
                    sys.exit("{}: no primary.xml under {} and not fetching"
                             .format(name, dest))
                print("  {}: absent, skipping".format(name), file=sys.stderr)
            else:
                print("  {}: {}".format(name, os.path.basename(path)),
                      file=sys.stderr)
        else:
            path = sync_repo(fetch_base, dest, name)
        if path:
            repos.append({"name": name, "kind": kind, "base": base,
                          "path": path})
    return repos


def read_lock(release, args):
    lock_path = os.path.join(
        args.lock_dir,
        lockfile_name("fedora", release, args.arch),
    )
    if not os.path.exists(lock_path):
        sys.exit("no lockfile at {}: a release has to be solved by hand once "
                 "before it can be refreshed, because its overrides and image "
                 "sets are not derivable from repodata".format(lock_path))
    with open(lock_path) as fh:
        return lock_path, json.load(fh)


def relock(release, args):
    lock_path, lock = read_lock(release, args)

    print("fedora {}:".format(release), file=sys.stderr)
    repos = repo_list(release, args)

    if args.dry_run or args.fetch_only:
        return

    solve.main(solve_argv(lock, repos, lock_path,
                          recorded_probe(lock, args.lock_dir)))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--release", action="append", default=[], metavar="N",
                    help="release to refresh (repeatable; default: every "
                         "release with a lockfile)")
    ap.add_argument("--arch", default="x86_64")
    ap.add_argument("--branched", action="append", default=[], metavar="N",
                    help="release that has branched but not reached GA, so "
                         "it is served from development/ and has no updates "
                         "tree (repeatable). Stated rather than detected: "
                         "which releases exist and where is a fact about the "
                         "world on a given day, not something to infer from "
                         "a 404")
    ap.add_argument("--lock-dir", default=None)
    ap.add_argument("--mirror", default=os.environ.get("BUCKOS_FEDORA_MIRROR"),
                    metavar="URL",
                    help="fetch repodata from this mirror of upstream's "
                         "layout instead of dl.fedoraproject.org; the "
                         "canonical URL is still what gets recorded")
    ap.add_argument("--offline", action="store_true",
                    help="re-solve from the repodata already on disk instead "
                         "of fetching; for reproducing a solve, or on a host "
                         "with no route to upstream")
    ap.add_argument("--fetch-only", action="store_true",
                    help="sync repodata but do not re-solve")
    ap.add_argument("--no-generate", action="store_true",
                    help="re-solve but do not regenerate the .bzl data")
    ap.add_argument("--probe", action="store_true",
                    help="after solving, run %%generate_buildrequires for "
                         "every package and solve again knowing what it "
                         "asked for (slow: this builds each package's "
                         "buildroot and %%prep)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    root = repo_root()
    if args.lock_dir is None:
        args.lock_dir = os.path.join(root, "flavors", "fedora", "lock")

    releases = args.release or lockfile_releases(
        args.lock_dir, args.arch, flavor="fedora")
    if not releases:
        sys.exit("no lockfiles in {}".format(args.lock_dir))

    for release in releases:
        relock(release, args)

    if args.dry_run or args.fetch_only or args.no_generate:
        return

    regenerate(releases, args, root)
    if args.probe:
        probe_pass(releases, args, root)


def regenerate(releases, args, root):
    generate.main(
        [os.path.join(args.lock_dir, lockfile_name("fedora", r, args.arch))
         for r in releases]
        + ["--out-dir", os.path.join(root, "flavors", "fedora", "generated")]
    )


def probe_pass(releases, args, root):
    """The second half of the loop: probe, re-solve, regenerate.

    Two solves deep because the probe needs a buildroot to run in and the
    buildroot comes from a solve. The first one is built from repodata
    alone, which is enough to run a %generate_buildrequires block -- that
    is what its handful of static BuildRequires are for -- and the second
    one knows what the block asked for. mock does the same thing at build
    time; the difference is only that a lockfile has to remember.

    Not run by default: it builds every package's buildroot and %prep, so
    it costs a real build rather than a repodata fetch, and its answer only
    changes when a spec's generator does.
    """
    # Imported here rather than at the top because probe.py imports this
    # module for repo_root and lockfile_releases, and the two must agree
    # about them. A local import is the honest way to say that the
    # dependency runs one way at module scope and the other way on demand.
    import probe as probe_mod

    for release in releases:
        lock_path, lock = read_lock(release, args)
        probe_mod.write_probe_file(lock_path, root)
        # offline: the repodata was synced minutes ago by the first solve,
        # and a second fetch could pick up an upstream push, which would
        # make these two lockfiles differ by more than the probe results --
        # the one thing this pass is meant to isolate.
        solve.main(solve_argv(
            lock, repo_list(release, args, offline=True), lock_path,
            probe_mod.probe_path(lock_path),
        ))
    regenerate(releases, args, root)


if __name__ == "__main__":
    main()
