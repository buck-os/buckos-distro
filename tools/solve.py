#!/usr/bin/env python3
"""Resolve a Fedora build set into a pinned lockfile.

Runs offline against repository metadata only -- never downloads source
packages.  See SPEC.md section 3a for why this is a separate out-of-band
step rather than something Buck evaluates.

Inputs are the two primary.xml files a Fedora mirror publishes:

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
"""

import argparse
import gzip
import json
import lzma
import os
import subprocess
import sys
import xml.etree.ElementTree as ET

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

# rpm's internal feature capabilities. They are satisfied by rpm itself and
# have no providing package, so they must be filtered or every solve fails.
# Verified against a real SRPM header: `rpm -qp --requires` emits
# rpmlib(CompressedFileNames) and rpmlib(FileDigests) unconditionally.
PSEUDO_CAPABILITY_PREFIXES = ("rpmlib(", "config(", "rpmlib")


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


def parse_primary(path):
    """Stream a primary.xml into package records.

    Uses iterparse and clears elements as it goes: Fedora's binary
    primary.xml is ~1 GB uncompressed and will not fit comfortably in
    memory as a tree.
    """
    packages = []
    with _open_maybe_gz(path) as fh:
        for event, elem in ET.iterparse(fh, events=("end",)):
            if not elem.tag.endswith("}package"):
                continue
            rec = _parse_package_elem(elem)
            if rec:
                packages.append(rec)
            elem.clear()
    return packages


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


def build_universe(binary_pkgs, source_pkgs):
    """Index the repodata into the maps depgraph needs."""
    provides = {}
    requires = {}
    source_of = {}
    subpackages = {}
    binary_index = {}

    for pkg in binary_pkgs:
        name = pkg["name"]
        binary_index[name] = pkg
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

    source_index = {p["name"]: p for p in source_pkgs}

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


def build_requires_of(source_pkg_record, implicit=BUILDSYS_BUILD):
    """Extract BuildRequires from a source package's repodata record.

    For a src.rpm, rpm:requires IS the BuildRequires list -- verified
    against a real SRPM header. Pseudo-capabilities are filtered.

    The implicit @buildsys-build group is prepended, because every Fedora
    spec is written assuming it is already installed.  Passing
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
    them.  Packages flagged here need an `rpmbuild -br` probe pass before
    their buildroot is complete; the lockfile records the flag so the gap
    is visible rather than silent.
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


def solve(universe, build_set, overrides=None, strict=False):
    """Resolve every build package's BuildRequires into pinned deps."""
    overrides = overrides or {}
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

        dyn = detect_dynamic_buildrequires(record)
        if dyn:
            dynamic[src] = dyn

        direct = []
        for cap in build_requires_of(record):
            base = strip_capability_version(cap)
            if is_rich_dep(base):
                problems.append(("rich", base, src))
                continue
            try:
                provider = resolve_capability(
                    base, universe["provides"], src, overrides
                )
            except (AmbiguousProvider, UnresolvedCapability) as exc:
                problems.append(("unresolved", str(exc), src))
                continue
            direct.append(provider)
            resolutions.setdefault(src, {})[base] = provider

        # The buildroot must contain the runtime closure of each build dep,
        # or the compiler will not actually find the libraries.
        closure, closure_problems = runtime_closure(
            direct, universe["requires"], universe["provides"], overrides
        )
        problems.extend(closure_problems)
        build_deps[src] = closure

    if strict and problems:
        for kind, detail, who in problems:
            print("solve error [{}] {}: {}".format(kind, who, detail), file=sys.stderr)
        sys.exit("solve failed with {} unresolved item(s)".format(len(problems)))

    return build_deps, resolutions, problems, dynamic


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
        missing = [r for r in roots if r not in universe["binary_index"]]
        for r in missing:
            problems.append(
                ("missing-binary",
                 "no binary package named {!r} in repodata".format(r),
                 "image:" + name)
            )
        closure, closure_problems = runtime_closure(
            [r for r in roots if r not in missing],
            universe["requires"], universe["provides"], scoped,
        )
        # Retagged with the image set rather than the requiring package, so
        # a problem reported here is attributable to the set that asked for
        # it -- the build closure and an image can disagree about the same
        # capability, and "unresolved in image:live" is actionable in a way
        # that a bare package name is not.
        problems.extend(
            (kind, detail, "image:{} ({})".format(name, who))
            for kind, detail, who in closure_problems
        )
        sets[name] = sorted(closure)
    return sets, problems


def emit_lockfile(universe, build_set, build_deps, resolutions, problems,
                  dynamic, plan, depth, image_sets, args):
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
            },
            "subpackages": universe["subpackages"].get(src, []),
            "build_requires_resolved": resolutions.get(src, {}),
            # The cut: which deps are built here vs taken from the seed.
            "deps_built": [pin_binary(d) for d in built],
            "deps_seed": [pin_binary(d) for d in seeded],
            "dynamic_buildrequires": dynamic.get(src, []),
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
    seed_closure = sorted({d for deps in build_deps.values() for d in deps})

    return {
        "schema": 1,
        "flavor": "fedora",
        "release": args.release,
        "dist_tag": args.dist_tag,
        "target_cpu": args.target_cpu,
        # Everything needed to reproduce this solve.
        "repodata": {
            "binary_primary": os.path.basename(args.binary_primary),
            "source_primary": os.path.basename(args.source_primary),
        },
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
            "implicit_group": list(BUILDSYS_BUILD),
            "stages": args.stages,
            "images": sorted(args.image),
            "image_overrides": sorted(args.image_override),
        },
        "summary": dict(
            depth,
            cycles=len(plan["cycles"]),
            staged_targets=len(plan["staged"]),
            problems=len(problems),
            dynamic_buildrequires=len(dynamic),
            image_sets={k: len(v) for k, v in sorted(image_sets.items())},
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary-primary", required=True)
    ap.add_argument("--source-primary", required=True)
    ap.add_argument("--build", action="append", default=[],
                    help="source package to build from source (repeatable)")
    ap.add_argument("--build-list", default=None,
                    help="file with one source package name per line")
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
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    build_set = set(args.build)
    if args.build_list:
        with open(args.build_list) as fh:
            build_set |= {
                line.strip() for line in fh
                if line.strip() and not line.startswith("#")
            }
    if not build_set:
        sys.exit("nothing to build: pass --build or --build-list")

    overrides = {}
    for item in args.override:
        if "=" not in item:
            sys.exit("--override expects capability=package, got {!r}".format(item))
        cap, pkg = item.split("=", 1)
        overrides[cap.strip()] = pkg.strip()

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
        if ":" not in item or "=" not in item.split(":", 1)[1]:
            sys.exit("--image-override expects name:capability=package, got "
                     "{!r}".format(item))
        name, rest = item.split(":", 1)
        cap, pkg = rest.split("=", 1)
        name = name.strip()
        if name not in image_roots:
            sys.exit("--image-override names image set {!r}, which no --image "
                     "defines (have: {})".format(
                         name, ", ".join(sorted(image_roots)) or "none"))
        image_overrides.setdefault(name, {})[cap.strip()] = pkg.strip()

    print("parsing binary repodata...", file=sys.stderr)
    binary_pkgs = parse_primary(args.binary_primary)
    print("parsing source repodata...", file=sys.stderr)
    source_pkgs = parse_primary(args.source_primary)
    print(
        "universe: {} binary, {} source packages".format(
            len(binary_pkgs), len(source_pkgs)
        ),
        file=sys.stderr,
    )

    universe = build_universe(binary_pkgs, source_pkgs)
    build_deps, resolutions, problems, dynamic = solve(
        universe, build_set, overrides, strict=args.strict
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

    lock = emit_lockfile(
        universe, build_set, build_deps, resolutions, problems,
        dynamic, plan, depth, image_sets, args,
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
        "  dynamic BuildRequires : {}\n"
        "  unresolved            : {}".format(
            args.out,
            s["source_packages_built"],
            s["built_from_source"],
            s["fraction_built"],
            s["from_seed"],
            s["cycles"],
            s["staged_targets"],
            s["dynamic_buildrequires"],
            s["problems"],
        ),
        file=sys.stderr,
    )
    for name, count in sorted(s["image_sets"].items()):
        print("  image {:<15}: {} packages".format(name, count),
              file=sys.stderr)


if __name__ == "__main__":
    main()
