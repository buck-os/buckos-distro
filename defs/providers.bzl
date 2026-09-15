"""
Typed providers for buckos-distro.

The provider layer is the whole abstraction: nothing above it knows which
upstream distro a package came from.

  PackageInfo     — returned by EVERY flavor's build rule.  The universal
                    contract consumed by rootfs/image/transform rules.
  BuildrootInfo   — a populated build environment.  Per-flavor provenance
                    (SPEC.md section 3) lives here.
  RootfsInfo      — a producer-neutral Linux filesystem archive plus a
                    versioned metadata manifest.  Image composers should
                    consume this instead of guessing at DefaultInfo.
  FlavorInfo      — binds a distro's source format, build driver, and
                    buildroot into one addressable thing.
  SourcePackageInfo — an unpacked upstream source package, normalized into
                    whatever layout the flavor's build driver expects.
  RpmArtifactInfo / DebArtifactInfo — native binary packages, for flavors
                    that publish a real repo alongside the installroot.
  RpmFileInfo      — one RPM, optionally signed as a derived release artifact.
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

# ── Root filesystem artifacts ───────────────────────────────────────
#
# This is intentionally a filesystem contract, not a container-runtime or
# live-media contract. A downstream image builder can import the tar directly,
# while a non-Buck consumer can use the JSON sidecar without knowing anything
# about Starlark providers.

RootfsInfo = provider(fields = [
    "archive",              # artifact: complete rootfs rooted at ./
    "manifest",             # artifact: buckos.rootfs.v1 JSON sidecar
    "format",               # str: currently "tar"
    "compression",          # str: currently "none"
    "layout",               # str: currently "complete-rootfs"
    "architecture",         # str: x86_64 | aarch64 | unknown
    "flavor",               # str: distro identity, e.g. fedora
    "release",              # str: distro release, e.g. 44
    "package_manager",      # str: rpm | dpkg | unknown
    "package_provenance",   # str: source-preferred | upstream-binary | unknown
    "role",                 # str: base | live | buildroot-seed | custom
    "buildroot_provenance", # str: binary-seed | bootstrapped | host | unknown
    "transforms",           # list[str]: ordered post-assembly transformations
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
    "architecture",     # str | None: x86_64 or aarch64 for kernel artifacts
    "efi_stub",          # artifact | None: systemd EFI stub for UKI assembly
    # An *artifact* holding the version string, not a str.  Which kernel an
    # image contains is discovered by reading the tarball, which happens
    # when the action runs -- long after analysis, where a string attribute
    # would have to be filled in.  Making it a file is what keeps the
    # kernel version out of the BUCK files, where it would rot on every
    # kernel update and be wrong in a way nothing checks.
    "kver",             # artifact: the kernel version, no trailing newline
    # Additive command-line arguments of the kernel artifact itself.
    # Image policy remains on iso_image.kernel_args; the image rule combines
    # the two without making a downstream kernel family part of its API.
    "boot_args",        # list[str]
])

# A typed EFI executable prevents the ISO rule from treating an arbitrary file
# as a Secure Boot entry point.  Unsigned producers return signed = False;
# efi_sign creates signed images; efi_image adapts a caller-attested one.
EfiImageInfo = provider(fields = [
    "image",                # artifact: PE/COFF EFI executable
    "architecture",         # str: x86_64 or aarch64
    "signed",               # bool
    "signing_certificate",  # artifact | None
])

# A producer-neutral custom-kernel contract.  Image rules deliberately know
# nothing about how these artifacts were built: an upstream Linux build, a
# different repository, and a site-specific build system all cross the same
# boundary.  Optional development artifacts are carried here so downstream
# module/BPF rules can grow without changing the boot-image contract.
#
# `modules` is a rootfs-shaped tree containing
# usr/lib/modules/<release>/... .  Requiring one canonical layout here keeps
# distro image rules free of producer-specific paths.
KernelInfo = provider(fields = [
    "image",            # artifact: bootable bzImage/Image-style kernel
    "version",          # artifact: uname -r value, no trailing newline
    "architecture",     # str: x86_64 or aarch64
    "modules",          # artifact | None: rootfs-shaped module tree
    "config",           # artifact | None: final kernel .config
    "vmlinux",          # artifact | None: uncompressed kernel ELF
    "system_map",       # artifact | None: System.map
    "module_symvers",   # artifact | None: Module.symvers
    "efi_stub",         # artifact | None: systemd EFI stub for UKI assembly
    "ima_certificate",  # artifact | None: public cert trusted for IMA
    "boot_args",        # list[str]: additive kernel command-line arguments
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
    "operations",       # list[str]: supported signer contract operations
    "cacheable",        # bool: signed outputs may enter a shared action cache
    "local_only",       # bool: signer must execute on the local machine
])

# ── Native binary package artifacts ──────────────────────────────────

# The single-file boundary used by image assembly, signing, and publication.
# RpmArtifactInfo represents all outputs of one source build; RpmFileInfo is
# deliberately narrower so a signer cannot accidentally receive an output
# directory containing packages that were not selected for release.
RpmFileInfo = provider(fields = [
    "rpm",                 # artifact: exactly one binary .rpm
    "package_name",        # str: binary package identity
    "signed",              # bool
    "signing_key_id",      # str | None: identity that signed this artifact
    "verification_key",    # artifact | None: public RPM verification key
])

# RPM package signatures normally use an OpenPGP identity, which is a distinct
# trust domain from the X.509 identities carried by SigningKeyInfo for IMA and
# PE/COFF. Keeping the providers separate prevents an image key from being
# silently reused as a repository/package key.
RpmSigningKeyInfo = provider(fields = [
    "public_key",          # artifact: public RPM verification key
    "key_id",              # str: stable operator-facing identity
    "cacheable",           # bool: signed outputs may enter a shared cache
    "local_only",          # bool: signer must execute on the local machine
])

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
