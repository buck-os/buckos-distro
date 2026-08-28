# buckos-distro architecture

This document describes the implemented build model and public Starlark interfaces.

## Package replay

The Fedora, CentOS Stream, and CentOS Hyperscale frontends treat the upstream spec file as executable build metadata. The Debian and Ubuntu frontends treat the unpacked Debian source package and `debian/rules` the same way. None translates the upstream recipe into Buck rules.

For each source package, `package()` creates these targets:

```text
:name-topdir          Unpacked source RPM with SOURCES/ and SPECS/
:name-buildrequires   `%generate_buildrequires` probe output
:name-build           Binary RPM directory, install root, and build manifest
:name-main            Projection for the binary package matching the source name
:name-<subpackage>    Projection for each additional binary package
:name                 Alias for the main binary package when it exists
```

`srpm_unpack` uses `rpm2archive` and GNU tar to preserve package payload semantics while producing an RPM topdir. `srpm_build` copies that topdir and its buildroot inputs into writable scratch space, enters the selected buildroot, and runs `rpmbuild -bb`. `rpm_subpackage` selects one binary RPM and exposes its unpacked install root as `PackageInfo`.

For Debian-family flavors, `package()` creates `:name-source`, `:name-build`, one projection per binary package, and a `:name` alias. `dsc_unpack` verifies every source member against the `.dsc` SHA-256 manifest before invoking `dpkg-source`. `deb_build` enters the selected buildroot and runs `dpkg-buildpackage -b`; `deb_subpackage` selects a named DEB and exposes its install root as `PackageInfo`.

USE flags map to RPM build conditionals. A `use_bcond` entry causes the replay to pass an explicit `--with` or `--without` option to `rpmbuild`; the source spec remains unchanged.

BuckOS is accepted by the flavor dispatcher but has no frontend. Selecting it causes `package()` to fail during loading.

## Providers

The implemented pipeline uses four providers from `defs/providers.bzl`.

`PackageInfo` describes a built or unpacked package. It carries identity, the install-root artifact, compile and link flags, native package artifacts, runtime dependency names, and SBOM metadata.

`BuildrootInfo` describes the environment used by package and image actions. It carries the root tree, provenance, target CPU, distribution tag, optional RPM macros, hermeticity, and environment variables.

`RpmArtifactInfo` accompanies Fedora package builds. It carries the binary RPM directory, optional source RPM, install root, and NEVRA.

`BootInfo` carries a kernel artifact, an optional initramfs artifact, and the kernel-version artifact used by downstream image rules.

`SourcePackageInfo` and `DebArtifactInfo` carry Debian-family source and binary artifacts. `FlavorInfo` remains reserved for a future addressable flavor abstraction.

## Dependency data flow

RPM dependencies are capabilities rather than package names. A requirement can name a package, a file, a virtual provide, a versioned capability, or a rich Boolean expression.

The solver operates outside Buck analysis because repository metadata is external input and Starlark cannot add dependency edges after actions run.

```text
Fedora primary metadata
        |
        v
tools/solve.py
        |
        +--> exact binary and source package pins
        +--> capability resolutions and explicit overrides
        +--> source build dependencies and bootstrap stages
        +--> rootfs and image runtime closures
        |
        v
flavors/fedora/lock/fedora-<release>.lock.json
        |
        v
tools/generate.py
        |
        v
flavors/fedora/generated/fedora-<release>.bzl
        |
        v
Buck targets
```

The solver resolves exact-name and unique providers directly. It defers ambiguous and Boolean requirements until the runtime closure reaches a fixed point. A provider already present in the closure satisfies the requirement. Remaining ambiguities are reported and require an explicit override.

Source and binary packages form separate graphs. Build requirements resolve to binary packages, while Buck schedules source-package builds. The generated data records which binary dependencies come from source builds and which remain pinned seed packages.

Bootstrap cycles are strongly connected components in the projected source graph. The generated plan expands included cycles into explicit stages so the Buck graph remains acyclic.

Dynamic build requirements are collected by running `rpmbuild -br` in `:name-buildrequires` targets. `tools/probe.py` writes the reports consumed by a subsequent solve. Probe results are checked in with the lockfile.

Debian-family dependency resolution is deliberately smaller: `tools/deb_lock.py` runs APT inside the target release, resolves the source Build-Depends plus the essential build base against an empty dpkg status database, and records every source and binary artifact by URL and SHA-256. `tools/deb_generate.py` converts that lock into pure Starlark data.

## Buildroot provenance

Fedora, CentOS Stream, CentOS Hyperscale, Debian, and Ubuntu define a buildroot per configured release and provenance.

`binary-seed` assembles a tree from the pinned Fedora `@buildsys-build` closure or a Debian-family APT build closure. The tree contains the target release's compiler, package implementation, libraries, and build utilities. Actions using it run with Bubblewrap or unshare isolation and are eligible for remote execution and cache upload.

`host` exposes the host filesystem as the buildroot. Actions using it run without the hermetic sandbox, set `local_only`, and disable shared-cache upload.

`//:buildroot` aliases the configured flavor and provenance target. Rules consume `BuildrootInfo` through that toolchain unless they receive an explicit buildroot.

Tree artifacts are passed as complete hidden inputs when a command also references projected paths. This ensures remote workers materialize the libraries and data required by tools inside the tree.

## Fedora release graph

`[buckos.fedora] releases` is a comma-separated list. Each release receives suffixed download, buildroot, package, rootfs, boot, and image targets. The selected default release also receives unsuffixed copies.

The checked-in configuration defines Fedora 43, Fedora 44, and Fedora 45. Each has its own repository table, package pins, buildroot seed, source recipes, probe data, bootstrap plan, and image package sets. All three solve their live image from source with no unresolved capabilities.

A release that has branched from rawhide without reaching GA is served from `development/<release>/` and has no `updates/` tree. `tools/relock.py --branched` selects that repository table. The repo names it records are the GA ones, so the pins do not churn when upstream moves the same packages into `releases/`. Fedora 45 is in that state; the configuration pins the default release to 44 rather than letting it follow the newest entry.

Package downloads use one URL and one SHA-256 digest per target. The URL comes from the package's recorded repository base, an optional `mirror_base` prefix rewrite, an optional static `package_url_template`, or an optional read-through `blob_base`. The static template requires the full digest and may include its 12-character prefix plus escaped filename, stem, extension, and release components. It cannot be combined with either existing redirect setting. The digest remains authoritative for every source.

`tools/relock.py` refreshes releases and updates metadata, calls the solver, and regenerates the Starlark data. A release must already have a lockfile because the initial package set and override policy require review.

## CentOS Stream release graph

`[buckos.centos] releases` uses the same release expansion and RPM-family rules as Fedora. Release 9 layers CentOS Stream BaseOS, AppStream, and CRB with EPEL and EPEL Next. Its buildroot includes the EPEL RPM macros, and its live image installs the EPEL and EPEL Next release packages without forcing an unrelated EPEL Next workload package. Release 10 retains its BaseOS, AppStream, and CRB graph and remains the default.

Both CentOS releases pin the build-system package group, live root filesystem, and image toolchain. Their source replay targets build the checked-in SRPM fixture with the target release's compiler and `.el9` or `.el10` macros. Both define hybrid live ISO targets; the release 10 image has been boot-verified through BIOS and UEFI with SELinux enforcing.

## CentOS Hyperscale release graph

`[buckos.centos-hyperscale] releases` is independent of the CentOS Stream release graph so both variants can coexist at releases 9 and 10. Hyperscale layers the SIG's `main` repository and `centos-release-hyperscale` from CentOS Extras on the corresponding CentOS Stream BaseOS, AppStream, and CRB repositories. Release 9 also uses EPEL and EPEL Next; its release package requires both. Release 10 uses EPEL, and its release package requires only `epel-release`.

Hyperscale inherits the CentOS build-system package group, adds the release's EPEL RPM macros to the binary seed, and uses `.hs.el9` or `.hs.el10` for source replay. Its image closures install the Hyperscale release package, select newer Hyperscale replacements by RPM version, and explicitly resolve the split `systemd-sysusers` provider used by Hyperscale systemd. EPEL 10's rich release dependency is pinned to `centos-stream-release` by an explicit solver override.

## Debian-family release graphs

`[buckos.debian] releases` and `[buckos.ubuntu] releases` use the same suffix/default expansion as Fedora. The checked-in Debian 13 (`trixie`) and Ubuntu 26.04 (`resolute`) data pin the GNU hello source set and their complete binary buildroot closures. Package downloads support the same content-addressed `package_url_template` placeholders as Fedora.

## Root filesystem and media pipeline

`rootfs` gives pinned RPMs to the target release's RPM implementation as one transaction. RPM writes the database, checks dependencies, and runs scriptlets after the payload trees have been staged. Triggers remain disabled because staging all payloads before one transaction does not preserve ordinary inter-package trigger timing.

The rootfs output is a tar archive. The archive preserves ownership, capabilities, and RPM filenames without requiring Buck to own or represent every unpacked path.

`kernel_image` reads the archive index and extracts the selected kernel and version without entering a buildroot.

`initramfs` unpacks the rootfs into isolated scratch space and runs the image's own dracut with a non-host-only configuration.

`squashfs` unpacks the rootfs and runs the target image toolchain's `mksquashfs`. Fedora, CentOS Stream, and CentOS Hyperscale live images enable SELinux relabeling, which derives contexts from the image's own policy and writes them through the squashfs pseudo-file interface.

`iso_image` creates the ISO9660 tree, BIOS boot files, UEFI files, El Torito entries, and optional isohybrid metadata. The volume label is also used to derive the live-root kernel argument. Secure Boot signing is not implemented.

## Remote execution contract

The execution platform reads `buckos.remote_execution` and `buckos.remote_cache`. Both default to false.

Rules that consume a buildroot derive `local_only` and cache-upload permission from `BuildrootInfo.hermetic`. Buildroot-independent actions declare that property directly. `tools/re_contract_test.py` checks these requirements in the rule source.

The repository does not provide service addresses or credentials for a remote backend. A configured worker must support the Linux isolation and scratch-space requirements used by the same actions locally.

## Source layout

```text
defs/providers.bzl          Shared providers
defs/flavor.bzl             Package frontend and flavor dispatch
defs/rpm_family.bzl         Shared RPM-family target generation
defs/releases.bzl           Configured release expansion
defs/buildroot_helpers.bzl  Buildroot access and execution policy
defs/exec.bzl               Execution-platform registration
defs/rules/srpm.bzl         Source RPM unpack, replay, and projection rules
defs/rules/dsc.bzl          Debian source unpack, replay, and projection rules
defs/rules/buildroot.bzl    Host and binary-seeded buildroots
defs/rules/rootfs.bzl       RPM transaction and rootfs archive
defs/rules/boot.bzl         Kernel and initramfs rules
defs/rules/image.bzl        Squashfs and ISO rules
flavors/fedora/             Fedora configuration, lockfiles, and generated data
flavors/centos/             CentOS Stream configuration, lockfile, and generated data
flavors/centos-hyperscale/  CentOS Hyperscale configuration, lockfile, and generated data
flavors/debian/             Debian configuration, lockfile, and generated data
flavors/ubuntu/             Ubuntu configuration, lockfile, and generated data
tools/                      Solver, generators, action drivers, and tests
```

## Non-goals

- Reimplement RPM's spec, macro, dependency, transaction, or scriptlet semantics.
- Translate spec files into native Buck build recipes.
- Make host-provenance outputs hermetic or remotely cacheable.
- Reproduce Fedora's Koji artifacts bit for bit.
