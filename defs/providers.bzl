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
    "flavor",           # str: a value declared by defs/flavor.bzl

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
    "name",             # str: a value declared by defs/flavor.bzl
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
                        #           Debian source-tree archive
    "recipe",           # str: path within topdir to the build recipe,
                        #      e.g. "SPECS/zlib.spec" or "debian/rules"
    "name",             # str
    "version",          # str
    "release",          # str | None
    "flavor",           # str
])

# ── Boot artifacts ───────────────────────────────────────────────────
#
# What a bootloader needs from an image, split out from the image itself.
# A rootfs is a tarball for reasons defs/rules/rootfs.bzl explains; a
# bootloader needs plain files it can read.

BootInfo = provider(fields = [
    "vmlinuz",          # artifact: the kernel, lifted out of the rootfs tar
    "initramfs",        # artifact | None: built separately, see boot.bzl
    # An *artifact* holding the version string, not a str.  Which kernel an
    # image contains is discovered by reading the tarball, which happens
    # when the action runs -- long after analysis, where a string attribute
    # would have to be filled in.  Making it a file is what keeps the
    # kernel version out of the BUCK files, where it would rot on every
    # kernel update and be wrong in a way nothing checks.
    "kver",             # artifact: the kernel version, no trailing newline
])

# ── Signing identities ───────────────────────────────────────────────
#
# A consumer invokes the target's RunInfo rather than reading private key
# bytes.  A development target may wrap a checked-in test key; a production
# target can instead call an HSM/KMS-backed signer.  Only the public certificate
# is part of this provider and safe for downstream image/verification rules.

SigningKeyInfo = provider(fields = [
    "certificate",      # artifact: public X.509 certificate
    "key_id",           # str: stable operator-facing identity
    "cacheable",        # bool: signed outputs may enter a shared action cache
    "local_only",       # bool: signer must execute on the local machine
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
