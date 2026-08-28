#!/usr/bin/env python3
"""Resolve an RPM-family build set into a pinned lockfile.

Runs offline against repository metadata only -- never downloads source
packages.  See SPEC.md section 3a for why this is a separate out-of-band
step rather than something Buck evaluates.

Inputs are the binary and source primary.xml files RPM repositories publish:

    binary primary.xml   Provides, Requires, and <rpm:sourcerpm> for every
                         binary package.  Gives the capability map and the
                         binary -> source mapping.
    source primary.xml   the src.rpm entries.  For a source package the
                         <rpm:requires> ARE its BuildRequires, plus
                         <location> and <checksum> for lazy fetching.

Usage:
    solve.py --binary-primary primary.xml.gz \
             --source-primary source-primary.xml.gz \
             --build zlib --build curl \
             --release 41 --dist-tag .fc41 \
             --out flavors/fedora/lock/f41.lock.json

For a buildroot-only flavor bootstrap, source metadata is not required:

    solve.py --flavor centos --seed-only \
             --binary-primary baseos-primary.xml.gz \
             --binary-base https://mirror.stream.centos.org/10-stream/BaseOS/x86_64/os \
             --release 10 --dist-tag .el10 \
             --out flavors/centos/lock/centos-10.lock.json
"""

import argparse
import gzip
import json
import lzma
import os
import subprocess
import sys
import urllib.parse
import xml.etree.ElementTree as ET

from rpmvercmp import package_is_newer

from depgraph import (
    AmbiguousProvider,
    UnresolvedCapability,
    bootstrap_depth,
    is_rich_dep,
    plan_build_order,
    resolve_capability,
    runtime_closure,
    strip_capability_version,
    validate_overrides,
)

RPM_NS = "http://linux.duke.edu/metadata/rpm"
COMMON_NS = "http://linux.duke.edu/metadata/common"

# Lockfile format version, written here and read by tools/generate.py --
# which imports it rather than repeating it, so a bump cannot land on one
# side of the pair alone.  A checked-in lockfile and the .bzl generated
# from it are reviewed together and have to be produced by tools that
# agree; a generator reading a newer lockfile with an older understanding
# emits plausible, wrong data instead of failing.
#
#   1: initial.
#   2: dynamic_buildrequires is a record, not a list of macro names, and
#      solve.probe names the probe file the solve consumed.
LOCK_SCHEMA = 2

# tools/probe.py's format version, checked separately because the two
# files are produced by different tools on different cadences.
PROBE_SCHEMA = 1

# rpm's internal feature capabilities. They are satisfied by rpm itself and
# have no providing package, so they must be filtered or every solve fails.
# Verified against a real SRPM header: `rpm -qp --requires` emits
# rpmlib(CompressedFileNames) and rpmlib(FileDigests) unconditionally.
PSEUDO_CAPABILITY_PREFIXES = ("rpmlib(", "config(", "rpmlib")

# Hosts a lockfile may name.  The lockfile is committed and published, so
# the base URL it records has to be one anybody can reach.
#
# Solving against an internal mirror is entirely reasonable -- it is faster
# and it is often the only route with egress -- so the mistake this catches
# is not "used a mirror", it is "wrote the mirror's address down". The two
# are easy to conflate because passing the mirror URL is exactly what makes
# the solve work, and the resulting lockfile looks correct: it is only wrong
# in a way that shows up as a leaked hostname in a public repo, and as a
# clone that tries to fetch from a host that does not exist for it.
#
# An allowlist rather than a denylist of internal patterns, because the set
# of public Fedora mirrors is short and knowable while the set of things
# that are not is neither.
PUBLIC_BASE_HOSTS = (
    "dl.fedoraproject.org",
    "download.fedoraproject.org",
    "archives.fedoraproject.org",
    "kojipkgs.fedoraproject.org",
    "mirror.stream.centos.org",
)


def check_public_base(flag, url):
    """Refuse to record a base URL a fresh clone could not fetch from."""
    if not url:
        return url
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        sys.exit(
            "{} must be an absolute http(s) URL, got {!r}".format(flag, url)
        )
    # hostname rather than netloc, so `dl.fedoraproject.org@internal.host`
    # is read as the internal host it actually resolves to rather than as
    # the public name sitting in front of the `@`.
    #
    # An explicit port is refused even on a public name: real mirrors serve
    # on 80 and 443, so `dl.fedoraproject.org:8080` is a port-forward
    # wearing the right hostname -- the one shape that passes a
    # host-only check while still being local.
    port_is_default = parts.port is None or parts.port in (80, 443)
    if parts.hostname not in PUBLIC_BASE_HOSTS or not port_is_default:
        sys.exit(
            "{}={} names {!r}, which is not an approved public RPM mirror.\n"
            "The lockfile is committed, so this URL gets published and has "
            "to be one any clone can reach.\n"
            "Solve against whatever mirror you like, but pass the canonical "
            "upstream URL here -- the sha256 pins make the two "
            "interchangeable. Point the *build* at another source instead, "
            "with the selected flavor's mirror_base, package_url_template, or "
            "blob_base in .buckconfig.local.\n"
            "Public hosts: {}".format(
                flag, url, parts.hostname, ", ".join(PUBLIC_BASE_HOSTS)
            )
        )
    return url.rstrip("/")


# Layout words Fedora's paths use to distinguish one repo from another.
# Only for naming: a repo's name is a review convenience, and getting it
# wrong costs a confusing lockfile rather than a wrong download.
_LAYOUT_WORDS = ("releases", "updates", "development", "rawhide", "archive")


def derive_repo_name(kind, base, taken):
    """A short, stable name for a repo, from its URL.

    Names exist so the lockfile can say `"repo": "updates"` on each of
    several hundred entries instead of repeating a 90-character URL. They
    are derived rather than required because the obvious name is already in
    the URL, and a flag nobody remembers to pass is a flag that ends up
    holding a default that means nothing.
    """
    words = [w for w in urllib.parse.urlsplit(base).path.split("/") if w]
    layout = next((w for w in words if w in _LAYOUT_WORDS), None)
    stem = "{}-{}".format(kind, layout) if layout else kind

    if stem not in taken:
        return stem
    # Two repos of the same kind under the same layout is unusual but not
    # invalid; suffix rather than collide, since the name is a key.
    for suffix in range(2, 100):
        candidate = "{}{}".format(stem, suffix)
        if candidate not in taken:
            return candidate
    raise AssertionError("cannot name repo for " + base)


def collect_repos(args):
    """Pair up the repeatable repo flags into one ordered table.

    Positional pairing -- the Nth base and name go with the Nth primary --
    so the lengths have to agree. Checked rather than zipped, because
    zip() would silently drop a base URL if one were missed and the failure
    would surface much later as a package with no download URL.
    """
    repos = []
    for kind, primaries, bases, names in (
        ("binary", args.binary_primary, args.binary_base, args.binary_repo),
        ("source", args.source_primary, args.source_base, args.source_repo),
    ):
        if bases and len(bases) != len(primaries):
            sys.exit(
                "--{k}-base given {b} time(s) but --{k}-primary given {p}; "
                "the Nth base pairs with the Nth primary, so pass one each "
                "or none at all".format(k=kind, b=len(bases), p=len(primaries))
            )
        if names and len(names) != len(primaries):
            sys.exit(
                "--{k}-repo given {n} time(s) but --{k}-primary given {p}; "
                "the Nth name pairs with the Nth primary".format(
                    k=kind, n=len(names), p=len(primaries)
                )
            )
        taken = {r["name"] for r in repos}
        for i, primary in enumerate(primaries):
            base = check_public_base(
                "--{}-base".format(kind), bases[i] if bases else ""
            )
            name = names[i] if names else derive_repo_name(kind, base, taken)
            taken.add(name)
            repos.append({
                "name": name,
                "kind": kind,
                "base": base,
                "primary": os.path.basename(primary),
                "path": primary,
            })
    return repos


class _ZstdStream:
    """primary.xml.zst streamed through the zstd CLI.

    Current Fedora publishes repodata as zstd, which Python 3.12 cannot
    decompress: compression.zstd arrives in 3.14 and the zstandard package
    is not a dependency worth taking for one call site.  Streaming keeps
    the ~1 GB of decompressed xml out of memory, and the exit status is
    checked on close so a truncated mirror read fails loudly instead of
    silently yielding a short package list.
    """

    def __init__(self, path):
        self._proc = subprocess.Popen(
            ["zstd", "-dc", path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.read = self._proc.stdout.read

    def close(self):
        self._proc.stdout.close()
        stderr = self._proc.stderr.read()
        self._proc.stderr.close()
        if self._proc.wait() != 0:
            raise RuntimeError(
                "zstd -dc failed: " + stderr.decode(errors="replace").strip()
            )

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False


def _open_maybe_gz(path):
    if path.endswith(".zst"):
        return _ZstdStream(path)
    if path.endswith(".xz"):
        return lzma.open(path, "rb")
    if path.endswith(".gz"):
        return gzip.open(path, "rb")
    return open(path, "rb")


def is_pseudo_capability(cap):
    return cap.startswith(PSEUDO_CAPABILITY_PREFIXES)


def parse_primary(path, repo=None):
    """Stream a primary.xml into package records.

    Uses iterparse and clears elements as it goes: Fedora's binary
    primary.xml is ~1 GB uncompressed and will not fit comfortably in
    memory as a tree.

    Each record is tagged with the repo it came from, because `location` is
    relative to its repo and nothing else in the record says which one that
    was. Once several repos are merged, that tag is the only way back to a
    URL -- see merge_packages.
    """
    packages = []
    with _open_maybe_gz(path) as fh:
        for event, elem in ET.iterparse(fh, events=("end",)):
            if not elem.tag.endswith("}package"):
                continue
            rec = _parse_package_elem(elem)
            if rec:
                rec["repo"] = repo
                packages.append(rec)
            elem.clear()
    return packages


def merge_packages(groups):
    """Collapse per-repo package lists into one newest-wins package list.

    `groups` is an ordered list of (repo_name, packages). Later repos are
    the ones layered on top -- `updates` over `releases` -- but order only
    settles exact ties: the winner is whichever build has the higher EVR,
    so passing the repos in the wrong order cannot silently downgrade a
    package.

    Returns (packages, replacements). `packages` is sorted by (name, arch)
    and `replacements` by the same key, because both are consumed into a
    committed lockfile and a dict preserves *insertion* order, not sorted
    order -- leaving them in the order they were built would make the file
    depend on the order packages happen to appear in primary.xml, which is
    Fedora's to change every time it regenerates repodata.

    `replacements` records what won over what, so the solve can report the
    update count rather than leaving the reader to diff two lockfiles to
    find out whether anything moved.
    """
    index = {}
    # What the earliest repo carrying each key had, so a replacement is
    # reported against the base rather than against whatever intermediate
    # build happened to be seen most recently.
    origin = {}

    for _repo, packages in groups:
        # Resolve within the repo first. A repo carrying two builds of one
        # package is unusual but legal, and folding them straight into the
        # global index would make even the *number* of replacements depend
        # on document order: builds seen as 1.0, 1.1, 1.2 report two
        # replacements, the same three seen as 1.2, 1.0, 1.1 report none.
        group_best = {}
        for pkg in packages:
            # Keyed by (name, arch), not name: i686 and x86_64 builds of the
            # same package coexist in one repo and are not candidates to
            # replace each other. Collapsing them by name would make the
            # winner depend on document order and could pin a 32-bit rpm
            # into an x86_64 closure.
            key = (pkg["name"], pkg["arch"])
            incumbent = group_best.get(key)
            if incumbent is None or package_is_newer(pkg, incumbent):
                group_best[key] = pkg

        for key, pkg in group_best.items():
            incumbent = index.get(key)
            if incumbent is None:
                index[key] = pkg
                origin[key] = pkg
            elif package_is_newer(pkg, incumbent):
                index[key] = pkg

    replacements = [
        {
            "name": key[0],
            "arch": key[1],
            "from": _evr_string(origin[key]),
            "from_repo": origin[key].get("repo"),
            "to": _evr_string(pkg),
            "to_repo": pkg.get("repo"),
        }
        for key, pkg in sorted(index.items())
        if pkg is not origin[key]
    ]
    return [index[key] for key in sorted(index)], replacements


def _evr_string(pkg):
    """The epoch:version-release form used in the lockfile and in reports."""
    epoch = pkg.get("epoch")
    prefix = "{}:".format(epoch) if epoch and epoch != "0" else ""
    return "{}{}-{}".format(prefix, pkg.get("version"), pkg.get("release"))


def _text(elem, tag, ns=COMMON_NS):
    found = elem.find("{{{}}}{}".format(ns, tag))
    return found.text if found is not None else None


def _parse_package_elem(elem):
    name = _text(elem, "name")
    if not name:
        return None
    arch = _text(elem, "arch") or ""

    version_el = elem.find("{{{}}}version".format(COMMON_NS))
    epoch = version_el.get("epoch") if version_el is not None else None
    version = version_el.get("ver") if version_el is not None else None
    release = version_el.get("rel") if version_el is not None else None

    location_el = elem.find("{{{}}}location".format(COMMON_NS))
    location = location_el.get("href") if location_el is not None else None

    checksum_el = elem.find("{{{}}}checksum".format(COMMON_NS))
    checksum = checksum_el.text if checksum_el is not None else None
    checksum_type = checksum_el.get("type") if checksum_el is not None else None

    fmt = elem.find("{{{}}}format".format(COMMON_NS))
    provides, requires, sourcerpm = [], [], None
    if fmt is not None:
        sourcerpm = _text(fmt, "sourcerpm", ns=RPM_NS)
        for kind, sink in (("provides", provides), ("requires", requires)):
            container = fmt.find("{{{}}}{}".format(RPM_NS, kind))
            if container is None:
                continue
            for entry in container.findall("{{{}}}entry".format(RPM_NS)):
                cap = entry.get("name")
                if not cap:
                    continue
                sink.append(cap)

        # A package also provides every file it ships, and specs lean on
        # that constantly -- `BuildRequires: /usr/bin/perl` is idiomatic,
        # not exotic.  These are <file> children of <format>, in the common
        # namespace rather than the rpm one, so they are easy to miss;
        # skipping them leaves the most ordinary kind of dependency looking
        # unsatisfiable.  primary.xml carries only rpm's "primary files"
        # subset (bin dirs, /etc), which is exactly the subset dependencies
        # are allowed to name, so this suffices without filelists.xml.
        for file_el in fmt.findall("{{{}}}file".format(COMMON_NS)):
            if file_el.text:
                provides.append(file_el.text)

    return {
        "name": name,
        "arch": arch,
        "epoch": epoch,
        "version": version,
        "release": release,
        "location": location,
        "checksum": checksum,
        "checksum_type": checksum_type,
        "provides": provides,
        "requires": requires,
        "sourcerpm": sourcerpm,
    }


def source_name_from_sourcerpm(sourcerpm):
    """glibc-2.39-1.fc41.src.rpm -> glibc

    Strips .src.rpm then the trailing -version-release. Two rsplits,
    because a source name may itself contain hyphens
    (e.g. python-setuptools-69.0-1.fc41.src.rpm -> python-setuptools).
    """
    if not sourcerpm:
        return None
    base = sourcerpm
    for suffix in (".src.rpm", ".nosrc.rpm"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    parts = base.rsplit("-", 2)
    return parts[0] if len(parts) == 3 else base


def _arch_rank(arch, target_cpu):
    """How much this arch is wanted, lower being better.

    An x86_64 repo also carries the i686 multilib builds -- 9,078 of them
    in Fedora 43 -- and an i686 rpm is named exactly what its 64-bit
    counterpart is. So `index[name] = pkg` over the raw package list picks
    whichever arch the document happened to mention last, which is a
    coin toss that lands on i686 for any package whose entries are ordered
    the other way. Ranking makes the choice explicit instead.

    Foreign arches are ranked last rather than dropped: a package that
    exists *only* as i686 is still the sole answer to a Requires on it, and
    dropping it would turn a resolvable capability into a solve failure.
    """
    if arch == target_cpu:
        return 0
    if arch in ("noarch", "src"):
        return 1
    return 2


def build_universe(binary_pkgs, source_pkgs, target_cpu="x86_64"):
    """Index the repodata into the maps depgraph needs.

    Collapses the arch dimension: callers work in package names, so of the
    several builds that can share one name exactly one has to win. Arch
    preference decides first and version only breaks ties within an arch,
    because a newer i686 build is still the wrong answer for an x86_64
    closure.
    """
    provides = {}
    requires = {}
    source_of = {}
    subpackages = {}

    def better(candidate, incumbent):
        rank_new = _arch_rank(candidate["arch"], target_cpu)
        rank_old = _arch_rank(incumbent["arch"], target_cpu)
        if rank_new != rank_old:
            return rank_new < rank_old
        return package_is_newer(candidate, incumbent)

    # Pick the winners before reading anything off them. Building the
    # capability maps in the same pass would mean a package that loses later
    # has already contributed its Provides, and for a losing i686 build
    # those are the 32-bit capabilities: `libc.so.6` without the `(64bit)`
    # marker would end up mapped to the name of the 64-bit package, which
    # does not provide it. The capability resolves, the buildroot is wrong.
    binary_index = {}
    for pkg in binary_pkgs:
        incumbent = binary_index.get(pkg["name"])
        if incumbent is None or better(pkg, incumbent):
            binary_index[pkg["name"]] = pkg

    for name, pkg in binary_index.items():
        requires[name] = [
            c for c in pkg["requires"] if not is_pseudo_capability(c)
        ]
        for cap in pkg["provides"]:
            if is_pseudo_capability(cap):
                continue
            provides.setdefault(cap, []).append(name)
        # A package always provides its own name, even if repodata is odd.
        provides.setdefault(name, []).append(name)

        src = source_name_from_sourcerpm(pkg["sourcerpm"])
        if src:
            source_of[name] = src
            subpackages.setdefault(src, []).append(name)

    source_index = {}
    for pkg in source_pkgs:
        incumbent = source_index.get(pkg["name"])
        if incumbent is None or better(pkg, incumbent):
            source_index[pkg["name"]] = pkg

    # Deduplicate and sort for deterministic output.
    provides = {k: sorted(set(v)) for k, v in provides.items()}
    subpackages = {k: sorted(set(v)) for k, v in subpackages.items()}

    return {
        "provides": provides,
        "requires": requires,
        "source_of": source_of,
        "subpackages": subpackages,
        "binary_index": binary_index,
        "source_index": source_index,
    }


# Fedora's @buildsys-build group: what koji puts in every buildroot before
# a spec's own BuildRequires are even considered.
#
# This is not an optimisation, it is a correctness requirement.  A spec
# never writes `BuildRequires: bash` or `BuildRequires: rpm-build`, because
# rpm's own documentation says the build system provides them -- so they
# appear in no repodata, and a solver that reads only declared
# BuildRequires produces a buildroot with a compiler but no rpmbuild, no
# redhat-rpm-config, and no gzip to unpack the sources with.  The failure
# is late and confusing: the tree looks plausibly complete and dies inside
# rpmbuild.
#
# Kept as an explicit list rather than read from comps.xml because it is a
# bootstrap-debt number that should be visible in review when it changes,
# and because comps is another piece of repodata to pin.
BUILDSYS_BUILD = (
    "bash",
    "binutils",
    "bzip2",
    "coreutils",
    "cpio",
    "diffutils",
    "fedora-release-common",
    "findutils",
    "gawk",
    "gcc",
    "gcc-c++",
    "grep",
    "gzip",
    "info",
    "make",
    "patch",
    "redhat-rpm-config",
    "rpm-build",
    "sed",
    "shadow-utils",
    "tar",
    "unzip",
    "util-linux",
    "which",
    "xz",
)

CENTOS_BUILDSYS_BUILD = (
    "bash",
    "binutils",
    "bzip2",
    "centos-stream-release",
    "coreutils",
    "cpio",
    "diffutils",
    "findutils",
    "gawk",
    "gcc",
    "gcc-c++",
    "grep",
    "gzip",
    "info",
    "make",
    "patch",
    "redhat-rpm-config",
    "rpm-build",
    "sed",
    "shadow-utils",
    "tar",
    "unzip",
    "util-linux",
    "which",
    "xz",
)

IMPLICIT_GROUPS = {
    "centos": CENTOS_BUILDSYS_BUILD,
    "centos-hyperscale": CENTOS_BUILDSYS_BUILD,
    "fedora": BUILDSYS_BUILD,
}


def build_requires_of(source_pkg_record, implicit=BUILDSYS_BUILD):
    """Extract BuildRequires from a source package's repodata record.

    For a src.rpm, rpm:requires IS the BuildRequires list -- verified
    against a real SRPM header. Pseudo-capabilities are filtered.

    The flavor's implicit build-system group is prepended, because RPM specs
    are written assuming it is already installed. Passing
    implicit=() gives the declared set alone, which is what a report on
    "what does this package actually ask for" wants.
    """
    declared = [
        c
        for c in source_pkg_record.get("requires", [])
        if not is_pseudo_capability(c)
    ]
    # Implicit first so a declared, versioned BuildRequires on the same
    # capability wins the resolution and the pin reflects the tighter one.
    return list(implicit) + declared


def detect_dynamic_buildrequires(source_pkg_record):
    """Heuristic: does this package use %generate_buildrequires?

    Dynamic BuildRequires are computed by running a script during the
    build, so they are absent from static repodata (SPEC.md section 3a).
    A package that declares one of these generators almost certainly has
    them.

    Only a guess, and only used where there is nothing better: the real
    answer comes from running the generator, which tools/probe.py does.
    This is what the lockfile records for a package that has not been
    probed, so the gap is visible rather than silent.
    """
    markers = (
        "rust-packaging",
        "go-rpm-macros",
        "pyproject-rpm-macros",
        "cargo-rpm-macros",
    )
    # Declared only: the implicit group never carries these markers,
    # and including it would just be noise.
    brs = set(build_requires_of(source_pkg_record, implicit=()))
    return sorted(m for m in markers if m in brs)


def probed_buildrequires(report, implicit=BUILDSYS_BUILD):
    """BuildRequires as `rpmbuild -br` reported them for one package.

    Same shape as build_requires_of, and deliberately so: this is the
    same question answered by a better instrument.  repodata carries what
    the spec says; a probe carries what the spec *does*, which for
    anything using rust-packaging or pyproject-rpm-macros is most of the
    list.

    The flavor's implicit group is still prepended. A probe runs inside a
    buildroot that already has @buildsys-build, so the generator never
    mentions it, and dropping it here would quietly remove make and gcc
    from every probed package's buildroot.
    """
    declared = [
        c for c in report.get("buildrequires", [])
        if not is_pseudo_capability(c)
    ]
    return list(implicit) + declared


def load_probe(path, build_set):
    """Read a probe file, keeping only the packages being solved.

    Filtered against build_set because a probe file outlives the build
    list that produced it: dropping a package from --build leaves its
    report behind, and a stale report would go on contributing
    BuildRequires for something no longer built.  The reverse -- a package
    on the build list with no report -- is normal and handled by falling
    back to repodata, since that is exactly the state of a release whose
    first solve has not been probed yet.
    """
    if not path:
        return {}
    with open(path) as fh:
        data = json.load(fh)
    if data.get("schema") != PROBE_SCHEMA:
        sys.exit("{}: probe schema {} not understood (this solver reads {})"
                 .format(path, data.get("schema"), PROBE_SCHEMA))
    packages = data.get("packages", {})
    known = {k: v for k, v in packages.items() if k in build_set}
    stale = sorted(set(packages) - set(known))
    print("probe: {} of {} package(s) from {}{}".format(
        len(known), len(packages), os.path.basename(path),
        " (ignoring {})".format(", ".join(stale)) if stale else "",
    ), file=sys.stderr)
    return known


def solve(universe, build_set, overrides=None, strict=False, probe=None,
          implicit=BUILDSYS_BUILD):
    """Resolve every build package's BuildRequires into pinned deps.

    `probe` is {source: report} from tools/probe.py, and where it has an
    entry it wins over repodata -- see probed_buildrequires.
    """
    overrides = overrides or {}
    probe = probe or {}
    build_deps = {}
    resolutions = {}
    dynamic = {}

    # Checked once, before anything consumes them, so a wrong flag is
    # reported as a wrong flag rather than as whatever it happens to break.
    problems = validate_overrides(overrides, universe["provides"])

    for src in sorted(build_set):
        record = universe["source_index"].get(src)
        if record is None:
            problems.append(
                ("missing-source", "no source package named {!r} in repodata".format(src), src)
            )
            build_deps[src] = set()
            continue

        report = probe.get(src)
        if report is None:
            requires = build_requires_of(record, implicit=implicit)
            dynamic[src] = {
                "source": "repodata",
                "capabilities": [],
                "suspected": detect_dynamic_buildrequires(record),
                # Same keys in both branches: a record whose shape depends
                # on which branch produced it makes every reader carry a
                # default, and one of them eventually gets it wrong.
                "unmet": False,
            }
        else:
            requires = probed_buildrequires(report, implicit=implicit)
            dynamic[src] = {
                "source": "probe",
                "capabilities": sorted(report.get("dynamic", [])),
                "suspected": [],
                # The generator asked for something the buildroot it ran in
                # did not have.  Recorded, deliberately not a problem: on
                # the first probe of a package that has any dynamic
                # BuildRequires at all this is the expected state, and
                # resolving them here is what fixes it.  It only means
                # something if it survives a re-probe, which is a
                # comparison between two runs and so not solve's to make.
                "unmet": bool(report.get("unmet")),
            }

        direct = []
        # BuildRequires that are boolean expressions.  Deferred to the
        # closure rather than refused here, which is what this loop used to
        # do: `(A if B)` asks whether B is in the buildroot, and the
        # buildroot is precisely what runtime_closure is about to compute,
        # so there is no answer available at this point in the loop.
        #
        # Refusing them was invisible while three packages were built from
        # source and loud at 126: redhat-rpm-config's conditional alone
        # accounted for 27 of 44 unreadable expressions, and it is a
        # BuildRequires of a large fraction of Fedora.
        conditional = []
        for cap in requires:
            base = strip_capability_version(cap)
            if is_rich_dep(base):
                conditional.append((base, src))
                continue
            try:
                provider = resolve_capability(
                    base, universe["provides"], src, overrides
                )
            except AmbiguousProvider:
                # Handed to the closure for the same reason the boolean
                # expressions are: "which of these provides it" is often
                # answered by the buildroot already containing one, and
                # that is not known until the closure settles.
                conditional.append((base, src))
                continue
            except UnresolvedCapability as exc:
                problems.append(("unresolved", str(exc), src))
                continue
            direct.append(provider)
            resolutions.setdefault(src, {})[base] = provider

        # The buildroot must contain the runtime closure of each build dep,
        # or the compiler will not actually find the libraries.
        closure, closure_problems = runtime_closure(
            direct, universe["requires"], universe["provides"], overrides,
            extra=conditional,
        )
        problems.extend(closure_problems)
        build_deps[src] = closure

    if strict and problems:
        for kind, detail, who in problems:
            print("solve error [{}] {}: {}".format(kind, who, detail), file=sys.stderr)
        sys.exit("solve failed with {} unresolved item(s)".format(len(problems)))

    return build_deps, resolutions, problems, dynamic


def solve_package_set(universe, roots, overrides=None, scope="package-set"):
    """Close one set of binary package names over runtime Requires."""
    overrides = overrides or {}
    problems = []
    missing = [root for root in roots if root not in universe["binary_index"]]
    for root in missing:
        problems.append(
            ("missing-binary",
             "no binary package named {!r} in repodata".format(root),
             scope)
        )
    closure, closure_problems = runtime_closure(
        [root for root in roots if root not in missing],
        universe["requires"], universe["provides"], overrides,
    )
    problems.extend(
        (kind, detail, "{} ({})".format(scope, who))
        for kind, detail, who in closure_problems
    )
    return sorted(closure), problems


def solve_image_sets(universe, image_roots, overrides=None,
                     image_overrides=None):
    """Close each named set of binary packages over its runtime Requires.

    A second, independent closure over the same universe, and the axis this
    repo was missing.  Everything above answers "what has to be installed
    before gcc can run"; this answers "what has to be installed before the
    machine can boot", and the two sets barely overlap -- the build closure
    has gcc and no kernel, an image has a kernel and no gcc.

    Deliberately the same `runtime_closure` the buildroot uses, not a
    parallel implementation.  A `Requires` means one thing, and a second
    reading of it would drift: the fixed-point evaluation of `(A if B)` is
    what pulls in `systemd-udev` off `kernel-core`, exactly as it pulls
    `cmake-rpm-macros` off `cmake` in a buildroot.

    Roots are *binary* package names, unlike --build, which takes source
    names.  An image is a set of installed packages, and the source they
    came from is not a thing rpm can install.

    Overrides layer, and they have to.  The same ambiguity can have
    different right answers in a buildroot and in an image, and
    `/usr/bin/systemd-sysusers` is the case that proves it: a buildroot
    wants systemd-standalone-sysusers, which is that one binary and nothing
    else, while an image wants the systemd that is already installed.
    Force the buildroot's answer on an image and rpm refuses the whole
    transaction over a file conflict on that single path -- correctly, since
    both packages own it.  So --image-override wins over --override for the
    set it names, and the global answer is left alone for everything else.
    """
    overrides = overrides or {}
    image_overrides = image_overrides or {}
    sets = {}
    problems = []
    for name in sorted(image_roots):
        roots = image_roots[name]
        scoped = dict(overrides, **image_overrides.get(name, {}))
        problems.extend(validate_overrides(
            image_overrides.get(name, {}), universe["provides"],
            scope="image:" + name,
        ))
        closure, set_problems = solve_package_set(
            universe, roots, scoped, scope="image:" + name,
        )
        problems.extend(set_problems)
        sets[name] = closure
    return sets, problems


def _count_pins_by_repo(lock):
    """How many pinned entries each repo accounts for.

    The question after layering updates/ over releases/ is not how many
    packages the merge moved -- most of the universe is never pinned -- but
    how many of the packages this lockfile actually installs came from the
    updates repo. Zero there means the repo was fetched, merged, and
    reached nothing, which is what a mispointed URL looks like from the
    outside: a solve that succeeds and changes nothing.
    """
    counts = {}

    def bump(entry):
        if entry:
            key = entry.get("repo") or "unattributed"
            counts[key] = counts.get(key, 0) + 1

    for entry in lock["buildroot_seed"]:
        bump(entry)
    for members in lock["image_sets"].values():
        for entry in members:
            bump(entry)
    for pkg in lock["packages"].values():
        bump(pkg["source"])
        for field in ("deps_built", "deps_seed"):
            for entry in pkg[field]:
                bump(entry)
    return dict(sorted(counts.items()))


def emit_lockfile(universe, build_set, build_deps, resolutions, problems,
                  dynamic, plan, depth, image_sets, seed_packages,
                  replacements, args):
    """Produce the lockfile. Every entry is pinned by checksum."""

    def pin_binary(name):
        pkg = universe["binary_index"].get(name)
        if not pkg:
            return {"name": name, "unresolved": True}
        return {
            "name": name,
            "evr": "{}{}-{}".format(
                "{}:".format(pkg["epoch"]) if pkg["epoch"] and pkg["epoch"] != "0" else "",
                pkg["version"],
                pkg["release"],
            ),
            "arch": pkg["arch"],
            "location": pkg["location"],
            "sha256": pkg["checksum"] if pkg["checksum_type"] == "sha256" else None,
            "source": universe["source_of"].get(name),
            # Which repo's base URL `location` hangs off. Per package, not
            # per release: once updates/ is layered over releases/ a closure
            # legitimately spans both, and a single base would be wrong for
            # whichever half it does not describe -- silently, as a 404 on
            # exactly the packages that received a fix.
            "repo": pkg.get("repo"),
        }

    packages = {}
    for src in sorted(build_set):
        record = universe["source_index"].get(src, {})
        deps = sorted(build_deps.get(src, ()))
        built, seeded = [], []
        for dep in deps:
            dep_src = universe["source_of"].get(dep)
            (built if dep_src in build_set else seeded).append(dep)

        packages[src] = {
            "source": {
                "name": src,
                "evr": "{}-{}".format(record.get("version"), record.get("release")),
                "location": record.get("location"),
                "sha256": record.get("checksum")
                if record.get("checksum_type") == "sha256"
                else None,
                "repo": record.get("repo"),
            },
            "subpackages": universe["subpackages"].get(src, []),
            "build_requires_resolved": resolutions.get(src, {}),
            # The cut: which deps are built here vs taken from the seed.
            "deps_built": [pin_binary(d) for d in built],
            "deps_seed": [pin_binary(d) for d in seeded],
            # Where this package's BuildRequires came from, and what the
            # generator added if one ran.  A record rather than a list
            # because "probed, nothing dynamic" and "never probed, and the
            # spec looks like it has a generator" are opposite states that a
            # bare empty list cannot tell apart -- and the second one means
            # deps_built below is incomplete.  See tools/probe.py.
            "dynamic_buildrequires": dynamic.get(src, {
                "source": "repodata", "capabilities": [], "suspected": [],
                "unmet": False,
            }),
        }

    # The stage-1 buildroot: every build dependency of every package,
    # pinned as a *prebuilt* binary rpm -- deliberately including the ones
    # this repo goes on to rebuild.
    #
    # deps_built / deps_seed answer "where does this package's dependency
    # come from", which is a different question from "what has to already
    # exist before anything can be built at all".  Conflating the two is
    # how a buildroot ends up without libz.so.1 while the rpmbuild inside
    # it is linked against it: zlib-ng is on the build list, so it was
    # excluded from the seed, so nothing could be built -- including
    # zlib-ng.  Fedora's own bootstrap has exactly this shape; stage 1
    # compiles against the previous release's binaries.
    seed_closure = sorted(
        {d for deps in build_deps.values() for d in deps} | set(seed_packages)
    )

    lock = {
        "schema": LOCK_SCHEMA,
        "flavor": args.flavor,
        "release": args.release,
        "dist_tag": args.dist_tag,
        "target_cpu": args.target_cpu,
        # Everything needed to reproduce this solve.
        # One entry per repo the solve read, in the order they were layered.
        # `base` is the URL that repo's `location` paths hang off, so an
        # entry's "repo" is what turns its pin into a download.  Public by
        # construction -- see check_public_base.
        #
        # `path` is dropped rather than carried: it is wherever the repodata
        # happened to sit on the machine that ran the solve, which is a
        # local accident and, in a committed file, a disclosed home
        # directory. The basename is the reproducibility-relevant part.
        "repos": [
            {k: v for k, v in repo.items() if k != "path"}
            for repo in args.repos
        ],
        # The solve's own inputs, recorded because they are not derivable
        # from the output.  Arriving at a clean solve is iterative -- each
        # batch of --override exposes the next layer of ambiguity beneath
        # it -- so the flag list is a reviewed artifact in its own right,
        # and a lockfile that does not carry it cannot be regenerated by
        # anyone but the person who happened to still have the shell
        # history.
        "solve": {
            "build": sorted(build_set),
            "overrides": sorted(args.override),
            "implicit_group": list(args.implicit_group),
            "seed_only": args.seed_only,
            "seed_packages": sorted(args.seed_package),
            "stages": args.stages,
            "images": sorted(args.image),
            "image_overrides": sorted(args.image_override),
            # Basename only, on the same reasoning as `repos` above: the
            # directory it sat in is a local accident, and in a committed
            # file a disclosed home directory.  Present so a refresh keeps
            # reading the probe results instead of quietly falling back to
            # repodata and shrinking every buildroot.
            "probe": os.path.basename(args.probe) if args.probe else None,
        },
        "summary": dict(
            depth,
            cycles=len(plan["cycles"]),
            staged_targets=len(plan["staged"]),
            problems=len(problems),
            # Split because they measure opposite things: the first is what
            # the probe found, the second is what has not been probed and
            # looks like it should be.  A refresh wants the second at zero.
            dynamic_buildrequires=sum(
                1 for d in dynamic.values() if d["capabilities"]),
            dynamic_unprobed=sum(
                1 for d in dynamic.values() if d["suspected"]),
            # Third state: probed, and the generator wanted something the
            # buildroot it ran in did not have.  Expected on a first probe
            # and cleared by the re-probe this solve enables; a count that
            # does not fall is the signal that something is genuinely
            # unresolvable rather than merely not yet resolved.
            dynamic_unmet=sum(
                1 for d in dynamic.values() if d.get("unmet")),
            image_sets={k: len(v) for k, v in sorted(image_sets.items())},
            # Universe-wide, so much larger than anything pinned here: it
            # measures the input repos, and is the number that goes to zero
            # if an updates repo is mispointed or was never passed.
            superseded=len(replacements),
        ),
        "buildroot_seed": [pin_binary(d) for d in seed_closure],
        # Runtime closures, one per --image.  Kept apart from
        # buildroot_seed because they answer a different question and are
        # consumed by a different rule; an image that happened to equal the
        # build closure would still be a coincidence, not a shared list.
        "image_sets": {
            name: [pin_binary(p) for p in members]
            for name, members in sorted(image_sets.items())
        },
        "build_order": plan["order"],
        "cycles": plan["cycles"],
        "staged": plan["staged"],
        "packages": packages,
        "problems": [
            {"kind": k, "detail": d, "package": p} for k, d, p in problems
        ],
    }
    lock["summary"]["pins_by_repo"] = _count_pins_by_repo(lock)
    return lock


def parse_override(item, flag):
    """Split capability=package without breaking `=` inside a rich dep."""
    if "=" not in item:
        sys.exit("{} expects capability=package, got {!r}".format(flag, item))
    cap, pkg = item.rsplit("=", 1)
    return cap.strip(), pkg.strip()


def main(argv=None):
    ap = argparse.ArgumentParser()
    # Repos are repeatable and layered in the order given, so a release can
    # be solved as releases/ plus updates/ rather than as a frozen GA
    # snapshot.  Order settles exact ties only: the winner is whichever
    # build has the higher EVR, so listing them the wrong way round cannot
    # silently downgrade a package.
    #
    # Each repo carries its own base URL because that is what turns a pin
    # into a download, and `location` in repodata is relative to its repo
    # while saying nothing about which repo that was.  The repodata itself
    # is gitignored, so the solve is the last point that knows.
    #
    # Always the canonical upstream URL, even when a mirror or a
    # content-addressed cache is what actually serves the bytes: resolution
    # is the build's concern and configurable there, identity belongs in the
    # lockfile, and the sha256 is what makes any mirror interchangeable.
    ap.add_argument("--binary-primary", action="append", default=[],
                    required=True, metavar="PATH",
                    help="binary primary.xml (repeatable, layered in order)")
    ap.add_argument("--source-primary", action="append", default=[],
                    metavar="PATH",
                    help="source primary.xml (repeatable, layered in order)")
    ap.add_argument("--binary-base", action="append", default=[],
                    metavar="URL",
                    help="upstream URL the Nth --binary-primary's `location` "
                         "paths are relative to")
    ap.add_argument("--source-base", action="append", default=[],
                    metavar="URL",
                    help="upstream URL the Nth --source-primary's `location` "
                         "paths are relative to")
    ap.add_argument("--binary-repo", action="append", default=[],
                    metavar="NAME",
                    help="name for the Nth binary repo, used to attribute "
                         "each pinned package (default: derived from its URL)")
    ap.add_argument("--source-repo", action="append", default=[],
                    metavar="NAME",
                    help="name for the Nth source repo "
                         "(default: derived from its URL)")
    ap.add_argument("--build", action="append", default=[],
                    help="source package to build from source (repeatable)")
    ap.add_argument("--build-list", default=None,
                    help="file with one source package name per line")
    ap.add_argument("--flavor", choices=sorted(IMPLICIT_GROUPS),
                    default="fedora")
    ap.add_argument("--seed-only", action="store_true",
                    help="pin the flavor's implicit build group without "
                         "requiring source repodata")
    ap.add_argument("--seed-package", action="append", default=[],
                    help="additional binary package root for the buildroot "
                         "seed (repeatable)")
    ap.add_argument("--override", action="append", default=[],
                    help="capability=package to break an ambiguity (repeatable)")
    ap.add_argument("--image", action="append", default=[], metavar="NAME=PKGS",
                    help="named image set: comma-separated *binary* package "
                         "names, closed over their runtime Requires "
                         "(repeatable)")
    ap.add_argument("--image-override", action="append", default=[],
                    metavar="NAME:CAP=PKG",
                    help="capability=package for one image set only, layered "
                         "over --override (repeatable)")
    ap.add_argument("--release", default=None)
    ap.add_argument("--dist-tag", default="")
    ap.add_argument("--target-cpu", default="x86_64")
    ap.add_argument("--stages", type=int, default=3)
    ap.add_argument("--strict", action="store_true",
                    help="fail on any unresolved capability")
    # Optional by necessity, not by preference: the probe results are
    # produced by building the packages, and the packages cannot be built
    # until a lockfile exists.  The first solve of a release has no probe
    # file and cannot; the second one should.  See tools/probe.py.
    ap.add_argument("--probe", default=None, metavar="PATH",
                    help="probe results from tools/probe.py; where present "
                         "they replace repodata's BuildRequires")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    args.implicit_group = IMPLICIT_GROUPS[args.flavor]

    # Before the solve, not after: a solve is minutes of work and a
    # mispaired repo list is worth reporting before spending them.
    args.repos = collect_repos(args)

    build_set = set(args.build)
    if args.build_list:
        with open(args.build_list) as fh:
            build_set |= {
                line.strip() for line in fh
                if line.strip() and not line.startswith("#")
            }
    if not build_set and not args.seed_only and not args.seed_package:
        sys.exit("nothing to solve: pass --build, --build-list, --seed-only, "
                 "or --seed-package")
    if build_set and not args.source_primary:
        sys.exit("source builds require at least one --source-primary")

    overrides = {}
    for item in args.override:
        cap, pkg = parse_override(item, "--override")
        overrides[cap] = pkg

    image_roots = {}
    for item in args.image:
        if "=" not in item:
            sys.exit("--image expects name=pkg[,pkg...], got {!r}".format(item))
        name, members = item.split("=", 1)
        roots = [m.strip() for m in members.split(",") if m.strip()]
        if not roots:
            sys.exit("--image {} lists no packages".format(name))
        # Union rather than replace, so the same set can be built up over
        # several flags without the last one silently winning.
        image_roots.setdefault(name.strip(), [])
        image_roots[name.strip()] += roots

    image_overrides = {}
    for item in args.image_override:
        # Split the set name off first: a capability can contain a colon
        # (an epoch, say), a set name cannot, so the leftmost colon is the
        # only unambiguous place to cut.
        if ":" not in item:
            sys.exit("--image-override expects name:capability=package, got "
                     "{!r}".format(item))
        name, rest = item.split(":", 1)
        cap, pkg = parse_override(rest, "--image-override")
        name = name.strip()
        if name not in image_roots:
            sys.exit("--image-override names image set {!r}, which no --image "
                     "defines (have: {})".format(
                         name, ", ".join(sorted(image_roots)) or "none"))
        image_overrides.setdefault(name, {})[cap] = pkg

    groups = {"binary": [], "source": []}
    for repo in args.repos:
        print("parsing {} repodata ({})...".format(repo["name"], repo["primary"]),
              file=sys.stderr)
        packages = parse_primary(repo["path"], repo=repo["name"])
        print("  {} packages".format(len(packages)), file=sys.stderr)
        groups[repo["kind"]].append((repo["name"], packages))

    binary_pkgs, binary_updates = merge_packages(groups["binary"])
    source_pkgs, source_updates = merge_packages(groups["source"])
    replacements = binary_updates + source_updates
    print(
        "universe: {} binary, {} source packages "
        "({} binary / {} source superseded by a later repo)".format(
            len(binary_pkgs), len(source_pkgs),
            len(binary_updates), len(source_updates),
        ),
        file=sys.stderr,
    )

    universe = build_universe(binary_pkgs, source_pkgs,
                              target_cpu=args.target_cpu)
    build_deps, resolutions, problems, dynamic = solve(
        universe, build_set, overrides, strict=args.strict,
        probe=load_probe(args.probe, build_set),
        implicit=args.implicit_group,
    )
    plan = plan_build_order(
        build_deps, universe["source_of"], build_set, stages=args.stages
    )
    depth = bootstrap_depth(build_deps, universe["source_of"], build_set)

    image_sets, image_problems = solve_image_sets(
        universe, image_roots, overrides, image_overrides
    )
    problems.extend(image_problems)
    if args.strict and image_problems:
        for kind, detail, who in image_problems:
            print("solve error [{}] {}: {}".format(kind, who, detail), file=sys.stderr)
        sys.exit("solve failed with {} unresolved image item(s)".format(
            len(image_problems)))

    seed_roots = set(args.seed_package)
    if args.seed_only:
        seed_roots.update(args.implicit_group)
    seed_packages, seed_problems = solve_package_set(
        universe, sorted(seed_roots), overrides, scope="buildroot",
    )
    problems.extend(seed_problems)
    if args.strict and seed_problems:
        for kind, detail, who in seed_problems:
            print("solve error [{}] {}: {}".format(
                kind, who, detail), file=sys.stderr)
        sys.exit("solve failed with {} unresolved buildroot item(s)".format(
            len(seed_problems)))

    lock = emit_lockfile(
        universe, build_set, build_deps, resolutions, problems,
        dynamic, plan, depth, image_sets, seed_packages, replacements, args,
    )

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump(lock, fh, indent=2, sort_keys=True)
        fh.write("\n")

    s = lock["summary"]
    print(
        "wrote {}\n"
        "  source packages built : {}\n"
        "  build deps from source: {} ({:.1%})\n"
        "  build deps from seed  : {}\n"
        "  cycles                : {} ({} staged targets)\n"
        "  dynamic BuildRequires : {} probed, {} unprobed\n"
        "  unresolved            : {}".format(
            args.out,
            s["source_packages_built"],
            s["built_from_source"],
            s["fraction_built"],
            s["from_seed"],
            s["cycles"],
            s["staged_targets"],
            s["dynamic_buildrequires"],
            s["dynamic_unprobed"],
            s["problems"],
        ),
        file=sys.stderr,
    )
    if s["dynamic_unmet"]:
        print("  unmet at probe time: {} (expected on a first probe; re-run "
              "the probe against this lockfile and it should clear)".format(
                  ", ".join(sorted(
                      src for src, d in lock["packages"].items()
                      if d["dynamic_buildrequires"].get("unmet")))),
              file=sys.stderr)
    if s["dynamic_unprobed"]:
        # Named, because this is the one summary line that means the
        # lockfile is knowingly incomplete: these packages compute their
        # BuildRequires at build time and nothing has run the computation,
        # so their buildroots are missing whatever it would have asked for.
        # Points at relock rather than at probe.py directly, because
        # relock --probe *is* the loop: it probes, re-solves against the
        # answers and regenerates, in one command.  Naming the low-level
        # tool told the reader to do by hand what a flag already does, and
        # to remember the re-solve afterwards or ship a lockfile with the
        # probe results left out of it.
        print("  unprobed: {}\n"
              "  re-run with --probe to resolve these "
              "(`buck2 run //tools:relock -- --probe --release {}`)"
              .format(
                  ", ".join(sorted(
                      src for src, d in lock["packages"].items()
                      if d["dynamic_buildrequires"]["suspected"])),
                  args.release or "N",
              ), file=sys.stderr)
    for name, count in sorted(s["image_sets"].items()):
        print("  image {:<15}: {} packages".format(name, count),
              file=sys.stderr)
    # Per repo rather than in total, because the total is already known --
    # what is worth seeing is whether the updates repo reached anything.
    for name, count in sorted(s["pins_by_repo"].items()):
        print("  from {:<16}: {} pins".format(name, count), file=sys.stderr)


if __name__ == "__main__":
    main()
