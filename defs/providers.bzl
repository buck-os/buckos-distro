"""
Typed providers for buckos-distro.

The provider layer is the whole abstraction: nothing above it knows which
upstream distro a package came from.

  PackageInfo     — returned by EVERY flavor's build rule.  The universal
                    contract consumed by rootfs/image/transform rules.
  BuildrootInfo   — a populated build environment.  Per-flavor provenance
                    (SPEC.md section 3) lives here.
  FlavorInfo      — binds a distro's source format, build driver, and
                    buildroot into one addressable thing.
  SourcePackageInfo — an unpacked upstream source package, normalized into
                    whatever layout the flavor's build driver expects.
  RpmArtifactInfo / DebArtifactInfo — native binary packages, for flavors
                    that publish a real repo alongside the installroot.
"""

# ── The universal package contract ───────────────────────────────────
#
# Deliberately the same shape as buckos-build's PackageInfo so its native
# recipes and rootfs/image rules port over without edits.

PackageInfo = provider(fields = [
    # Identity
    "name",             # str
    "version",          # str: upstream version ("1.3.1")
    "release",          # str | None: distro release ("2.fc41", "1ubuntu3")
    "flavor",           # str: "fedora" | "ubuntu" | "buckos"

    # Build outputs
    "prefix",           # artifact: the install prefix tree (an installroot)
    "libraries",        # list[str]: library names for -l flags

    # Extra flags this package requires consumers to use
    "cflags",           # list[str]
    "ldflags",          # list[str]

    # Native binary packages, when the flavor produces them.  None for
    # from-source flavors like buckos.
    "artifacts",        # list[artifact] | None: .rpm / .deb files

    # Runtime dependency names as the upstream distro declares them.
    # Kept as strings, not labels: rootfs assembly resolves them through
    # the flavor's dep_resolver so a Requires: on a virtual provide
    # ("webserver", "/bin/sh") does not have to be a Buck target.
    "requires",         # list[str]

    # SBOM metadata
    "license",          # str: SPDX expression
    "src_uri",          # str: upstream source-package URL
    "src_sha256",       # str
    "homepage",         # str | None
    "supplier",         # str
    "description",      # str
    "cpe",              # str | None
])

# ── Build environment ────────────────────────────────────────────────

BuildrootInfo = provider(fields = [
    "root",             # artifact: populated tree (usr/, etc/, ...)
    "provenance",       # str: "binary-seed" | "bootstrapped" | "host"
    "target_cpu",       # str: x86_64 | aarch64
    "dist_tag",         # str: ".fc41" | "" — rpm %{dist}
    "macros",           # artifact | None: extra rpm macros for the replay
    "hermetic",         # bool: False => consuming actions must be local_only
    "env",              # dict[str, str]: extra env for the build action
])

# No accessor for `hermetic` lives here on purpose.  The two consumers of
# that field -- buildroot_local_only() and buildroot_cache_upload() in
# defs/buildroot_helpers.bzl -- are the whole remote-execution policy, and
# a third reader is how they drift apart.  tools/re_contract_test.py fails
# the build if anything outside that file reads it.

# ── Flavor definition ────────────────────────────────────────────────

FlavorInfo = provider(fields = [
    "name",             # str: "fedora"
    "artifact_kind",    # str: "rpm" | "deb" | "tree"
    "buildroot",        # dep providing BuildrootInfo
    "dist_tag",         # str
    # Human-readable description of where source packages come from,
    # surfaced by `buck2 audit` and the flavor listing target.
    "source_hint",      # str
])

# ── Source packages ──────────────────────────────────────────────────

SourcePackageInfo = provider(fields = [
    "topdir",           # artifact: rpm-style topdir (SOURCES/, SPECS/) or
                        #           debian-style unpacked source tree
    "recipe",           # str: path within topdir to the build recipe,
                        #      e.g. "SPECS/zlib.spec" or "debian/rules"
    "name",             # str
    "version",          # str
    "release",          # str | None
    "flavor",           # str
])

# ── Native binary package artifacts ──────────────────────────────────

RpmArtifactInfo = provider(fields = [
    "rpms",             # list[artifact]: binary .rpm files (incl. subpackages)
    "srpm",             # artifact | None: the source rpm it came from
    "installroot",      # artifact: unpacked BUILDROOT tree
    "nevra",            # str: name-epoch:version-release.arch
])

DebArtifactInfo = provider(fields = [
    "debs",             # list[artifact]
    "dsc",              # artifact | None
    "installroot",      # artifact
])
