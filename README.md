# buckos-distro

`buckos-distro` is a Buck2 repository for replaying upstream Linux package builds and assembling bootable distribution images.

This project is licensed under the GNU General Public License, version 2 only (`GPL-2.0-only`). See [LICENSE](LICENSE). Upstream packages, generated distribution metadata, and other third-party materials retain their respective licenses.

Fedora 44 and 45, CentOS Stream 9 and 10, CentOS Hyperscale 9 and 10, Debian 13, and Ubuntu 26.04 have checked-in package graphs for both x86_64 and AArch64. Every one has binary-seeded buildroots, source replay targets, bootable root filesystems, and live ISO targets. BuckOS remains a declared flavor without a build frontend.

## Quick start

The build runs on Linux. RPM-family builds need Python 3, GNU tar, `rpm2archive`, and either Bubblewrap or util-linux `unshare`. Debian-family builds need Python 3, GNU tar, `dpkg-source`, `dpkg-buildpackage`, `dpkg-deb`, and the same isolation choice. Bubblewrap requires `newuidmap`, `newgidmap`, and subordinate UID and GID ranges for the build user, and refuses to start without them. The unshare path warns and continues with a single mapped ID, which cannot preserve package file ownership.

`setup.sh` installs the open-source Buck2 binary under `$HOME/.local/bin` when no working `buck2` is already available. It also creates the ignored `prelude/` mount point and writes `.buckconfig.local` when that file does not exist. It does not install system packages.

```sh
export PATH="$HOME/.local/bin:$PATH"
./setup.sh
buck2 build //:flavor
buck2 test //tools/...
buck2 build //tests:hello //tests:hello-greeting
```

On a host without access to GitHub releases, install a local Buck2 binary through the same setup path:

```sh
BUCK2_SOURCE=/path/to/buck2 ./setup.sh
```

`//:flavor` writes the selected flavor name. The two `hello` targets replay the same checked-in source RPM; `hello-greeting` enables the package's `greeting` `%bcond`.

## Current scope

| Flavor | Status | Primary outputs |
|---|---|---|
| Fedora 44, 45 | Implemented | x86_64/AArch64 RPMs, root filesystems, live ISOs |
| [CentOS Stream](flavors/centos/README.md) 9, 10 | Implemented | x86_64/AArch64 RPMs, root filesystems, live ISOs |
| [CentOS Hyperscale](flavors/centos-hyperscale/README.md) 9, 10 | Implemented | x86_64/AArch64 RPMs, root filesystems, live ISOs |
| [Debian](flavors/debian/README.md) 13 | Implemented | x86_64/AArch64 DEBs, root filesystems, live ISOs |
| [Ubuntu](flavors/ubuntu/README.md) 26.04 | Implemented | x86_64/AArch64 DEBs, root filesystems, live ISOs |
| [BuckOS](flavors/buckos/README.md) | Stub | None |

Every checked-in lock solves with zero static problems. The x86_64 RPM-family graphs stand as follows:

| Release | Source packages | Live image | Source-resolved | Staged targets | Probed |
| ------- | --------------- | ---------- | ----------------- | -------------- | ------ |
| Fedora 44 | 125 | 186 | 181 | 339 | 114 |
| Fedora 45 | 129 | 193 | 187 | 351 | 119 |
| CentOS Stream 9 | 133 | 200 | 195 | 360 | none |
| CentOS Stream 10 | 121 | 190 | 185 | 303 | 101 |
| CentOS Hyperscale 9 | 132 | 200 | 195 | 357 | none |
| CentOS Hyperscale 10 | 121 | 191 | 186 | 303 | none |

Solving, probing, and building are separate milestones, and every figure above is a solve result. `Source-resolved` counts live payload packages the source policy has a producer for, not packages that have been built: a lock with no probe report has unresolved dynamic `BuildRequires`, so a source build from it would fail at `%prep` on the first package whose buildroot is incomplete. The `Probed` column counts source packages with checked-in dynamic `BuildRequires` reports; it does not claim that every reported requirement was satisfied or that every package builds. A graph is converged only when a probe report exists and static solve problems, unprobed dynamic requirements, and dynamic-unmet records are all zero. The three counters alone do not establish it: a lock with no probe report reports all three as zero because there is nothing to count, which is why `rpm_relock.py` forces a probe pass whenever the lock names no report. Outside Fedora, CentOS Stream 10 x86_64 is currently the only RPM-family lock carrying one. The Fedora x86_64 locks have zero static problems and zero unprobed requirements, but five dynamic-unmet records each.

A solve reads static `BuildRequires` out of repodata, and repodata does not carry everything the spec asks for: `tar`'s real list is eleven packages, of which repodata names one. The rest come from `tools/probe.py` running `rpmbuild -br` against the spec. A release with a clean static solve and no probe data can still fail at `%prep` with `Failed build dependencies`, while a fully probed release can still report unmet dynamic requirements.

The Fedora source policies explicitly keep `kernel`, its three live subpackages, and `libxcrypt` pinned because their source builds require a build host with `CONFIG_CRYPTO_USER`. These source recipes are absent from the generated graph; local `prebuilt` configuration can only suppress an existing recipe and cannot add them back.

Fedora 45 has one additional source-policy exception. `tar` 1.35 declares its own three-argument `acl_get_file_at` immediately after including `<sys/acl.h>`, with no configure probe guarding it, and `acl` 2.4.0 declares that name with four arguments. Fedora never hits this because it does not rebuild `tar` in a released branch. Fedora 44 resolves it with a version variant: `acl` is built twice and only `tar` is routed to the older copy. Fedora 45 ships 2.4.0 in both its source and binary trees, so there is no older copy to route to. Until upstream `tar` carries the fix, 45 takes the pinned binary.

CentOS Stream and CentOS Hyperscale use the same live routing as Fedora. Normal unsuffixed live targets consume locally built RPMs wherever the source policy has a producer and retain pinned upstream RPMs only for explicit source-policy exceptions. Their `-prebuilt` siblings consume pinned upstream binaries for the entire payload.

## Build model

An upstream source package remains authoritative. The Fedora, CentOS Stream, and CentOS Hyperscale frontends unpack the source RPM, assemble a buildroot from pinned packages, and run `rpmbuild -bb` without translating the spec file into Starlark. The Debian and Ubuntu frontends apply the same model to a `.dsc` source set and run `dpkg-buildpackage -b` inside a buildroot assembled from SHA-256-pinned DEBs.

Dependency resolution happens before Buck analysis:

1. `tools/solve.py` reads RPM repository metadata, resolves capabilities, computes runtime closures, and emits a JSON lockfile.
2. `tools/generate.py` converts the lockfile into Starlark data.
3. Buck loads that generated data as an ordinary dependency graph.

Debian and Ubuntu use the corresponding `tools/deb_lock.py` and `tools/deb_generate.py` pair. Debian 13 carries 86 source recipes and Ubuntu 26.04 carries 114.

The lockfile records exact package locations, SHA-256 digests, repository origins, source recipes, bootstrap stages, overrides, and image sets. Buck verifies each downloaded package against its recorded digest.

`tools/relock.py` refreshes an existing release from Fedora's release and update repositories. It reuses the build list, overrides, and image roots already recorded in the lockfile.

```sh
buck2 run //tools:relock -- --release 44 --arch x86_64 --dry-run
```

Remove `--dry-run` to fetch metadata, solve the release, and regenerate `flavors/fedora/generated/`. Add `--probe` to run `%generate_buildrequires` before the final solve.

## Releases and targets

`[buckos.fedora] releases` defines every Fedora release loaded into the graph. The final entry is the default unless `[buckos.fedora] release` selects another listed release.

Each release and architecture receives canonical targets such as:

```text
//flavors/fedora:buildroot-binary-seed-44-x86_64
//flavors/fedora:rootfs-live-44-aarch64
//flavors/fedora:kernel-live-44-aarch64
//flavors/fedora:initramfs-live-44-aarch64
//flavors/fedora:squashfs-live-44-aarch64
//flavors/fedora:iso-live-44-aarch64
```

Every bootable image set is built two ways from the same package list. The
unsuffixed name is the source build: each package comes from the recipe
that produces it wherever one exists. A `-prebuilt` sibling takes the
whole set from pinned upstream binaries instead:

```text
//flavors/fedora:iso-live-44-x86_64             181 of 186 packages built here
//flavors/fedora:iso-live-prebuilt-44-x86_64    186 of 186 pinned upstream
```

Both exist because they are most useful side by side: the same image built
both ways is the direct evidence that replaying a distro's sources
reproduces the distro, and the pinned image is what to boot when a source
build is broken somewhere unrelated to what is being tested. The prebuilt
variant consults no recipe at all, including for packages the host could
build, so a half-source image cannot be mistaken for the pinned one it is
being compared against.

`rootfs-seed-<release>-<architecture>` is a third thing and not an image: it installs the
`@buildsys-build` closure, which is the environment packages are built in
rather than a set anyone would ship.

Release-only and unsuffixed compatibility targets remain x86_64. Release-and-architecture target platforms, such as `//platforms:fedora-44-aarch64`, carry the flavor, release, and CPU as constraints.

Fedora 45 has branched from rawhide but has not reached GA, which changes where it is served from rather than how it is built. Upstream publishes a branched release under `development/<release>/` and gives it no `updates/` tree, because there have been no post-GA pushes. `tools/relock.py --branched 45` selects that layout:

```sh
tools/relock.py --release 45 --branched 45
```

The repo names it records are still `binary-releases` and `source-releases`. At GA the same packages move from `development/` to `releases/`, and naming the pins for the directory they happen to sit in today would churn every pin's `repo` field on a day when nothing about the packages changed.

A pre-GA release is also the case `[buckos.fedora] release` exists for. The default otherwise follows the newest entry, which would point every unsuffixed target at a release whose packages still move daily, so the checked-in configuration lists 45 and pins the default to 44.

All implemented flavors provide the same architecture-suffixed buildroot, package, rootfs, boot, and image targets. Each flavor also has release-only and unsuffixed x86_64 compatibility targets.

On an AArch64 host, build an AArch64 target directly. On x86_64, register a persistent QEMU binfmt handler and opt the local execution platform into running ARM binaries:

```sh
buck2 build -c buckos.aarch64_emulation=true \
  //flavors/fedora:iso-live-44-aarch64
```

The flag only declares a scheduler capability. Every action that executes target binaries also checks `/proc/sys/fs/binfmt_misc/qemu-aarch64` and fails before entering the buildroot if the handler is absent. Host-provenance cross builds are rejected.

## Buildroots

Fedora, CentOS Stream, CentOS Hyperscale, Debian, and Ubuntu support two buildroot provenances:

- `binary-seed` assembles the build environment from pinned Fedora RPMs or Debian-family DEBs. It is the default and is eligible for remote execution and shared-cache upload.
- `host` uses the host root filesystem and installed distro toolchain. It is non-hermetic, local-only, and excluded from shared-cache upload.

The binary seed cuts bootstrap cycles that Buck cannot represent directly. The solver records staged source builds for cycles that are included in the source-replay set.

A package builds in a base plus its own overlay, not in the union of everyone's build dependencies. The base is the flavor's implicit build group -- `@buildsys-build` -- closed over its runtime `Requires`, which is fixed per release rather than derived from the build set; each package then installs what its own `BuildRequires` closed over on top of it. That distinction is not cosmetic. A union is not a buildroot, it is every tool any package asked for handed to all of them, and autoconf-era build systems feature-detect: `libmnl` declares `gcc`, `gnupg2` and `make`, the union handed it 311 packages including `doxygen`, and it emitted man pages its `%files` does not list. Under base-plus-overlay it builds in 166 packages. The mirror-image failure -- a *missing* tool, `libcap` finding no Go and silently omitting a program -- is the same fault seen from the other side: the buildroot did not match what the spec expects.

The overlay is installed, not merely unpacked, because rpmbuild resolves `BuildRequires` against the rpmdb rather than the filesystem. It uses `--replacepkgs --replacefiles --oldpackage`, since a bootstrap stage rebuilding something the base already carries at the same NEVRA is both a reinstall and a file conflict against the copy the overlay just wrote -- and a version variant deliberately supersedes the base with something *older*, which rpm otherwise refuses.

### Multilib

A spec that asks for a 32-bit dependency gets one. `gcc` requires `(glibc32 or glibc-devel(x86-32))` on every 64-bit arch, and the solver resolves it without an override: the discarded i686 builds are indexed under an arch-qualified name -- `glibc-devel.i686`, spelled the way rpm and every Fedora bug report spell it -- and their capabilities are registered wherever the collapsed universe has no answer at all.

That restriction is the whole safety argument: it cannot introduce an ambiguity, because it only ever fills an empty slot. Registering every i686 `Provides` was measured first -- 9,230 builds offer 59,078 capabilities, 30,790 already answered, and 21,556 of those become ambiguities the exact-name rule cannot settle, `/bin/awk` between `gawk` and `gawk.i686`. It also lands on rpm's own answer without hardcoding rpm's spelling of it: the capabilities with no collapsed provider are exactly the ones rpm marks unambiguously 32-bit, while the contested ones are arch-neutral names where the 64-bit build is right anyway.

Arch-specificity therefore lives in the capability, not in a per-arch preference. `glibc-devel.i686` requires unmarked `libm.so.6`, which only the 32-bit build provides, and plain `kernel-headers`, which is arch-neutral and answered by whatever the base already has. Preferring i686 for both is wrong: it pulls in `kernel-headers.i686`, and rpm refuses the transaction outright when a newer `kernel-headers.x86_64` is installed, since the two are one package with one name. gcc's 32-bit slice is six packages.

### Version variants

One source package can be built twice, at two versions, when the distro genuinely needs both:

```ini
--source-variant acl-compat=acl@2.3.2-6.fc44:tar
```

Fedora 44 needs exactly that. `acl` 2.4.0 added a versioned symbol `rsync` requires, and `rsync` is a build dependency of the kernel; the same release's header change broke `tar` 1.35, whose source declares its own three-argument `acl_get_file_at` where 2.4.0 declares four. Fedora resolves this by not rebuilding `tar`; its shipped binary predates the header, which a repo that builds everything from source cannot do. So it builds both, and only `tar` is routed to the older one.

This is modelled on the staging machinery rather than beside it. A stage is already "this source package, built more than once, with consumers routed to the right one"; a variant differs only in that the copies differ by version rather than by position in a cycle. A variant is an ordinary recipe with its own srpm, kept out of the binary-to-recipe map so it reroutes only the consumers that named it, and its routed edges are visible to the cycle planner -- without that the planner stages nothing and Buck rejects the target graph at analysis over a cycle the solver said did not exist.

## Image pipeline

The image pipeline has separate targets for work with different invalidation costs:

- `rootfs` runs the target package manager transaction with its database and maintainer scripts, then returns a tar archive.
- `kernel_image` extracts the selected kernel and version from the rootfs archive.
- `initramfs` runs the image's own dracut, Debian live-boot, or Ubuntu casper generator against the rootfs.
- `squashfs` compresses the rootfs and can write SELinux labels from the image's policy.
- `iso_image` creates the RPM, Debian live, or Ubuntu casper media layout around the kernel, initramfs, and squashfs.

The rootfs is a tar archive because package ownership and valid RPM filenames cannot always be represented safely as a Buck directory artifact. Image actions unpack it inside their isolated work areas.

The live squashfs is used directly as the root filesystem. x86_64 ISOs contain BIOS and UEFI boot entries. AArch64 ISOs contain the removable-media `BOOTAA64.EFI` UEFI path. Images are not signed for Secure Boot.

Build Fedora 44, CentOS Stream 9 with EPEL Next, CentOS Stream 10, or CentOS Hyperscale live media with:

```sh
buck2 build //flavors/fedora:iso-live-44-x86_64
buck2 build -c buckos.aarch64_emulation=true \
  //flavors/fedora:iso-live-44-aarch64
buck2 build //flavors/centos:iso-live-10-x86_64
buck2 build //flavors/centos-hyperscale:iso-live-10-x86_64
buck2 build //flavors/debian:iso-live-13-x86_64
buck2 build //flavors/ubuntu:iso-live-26.04-x86_64
```

## Boot validation

Each `//tests:boot-*` target performs two QEMU boots. It first boots the exact architecture-qualified production ISO through the requested firmware, reports that artifact's SHA-256, and requires the serial getty's `login:` prompt. The prompt is the common late normal-boot milestone because every image selects a serial kernel console and carries systemd plus util-linux. It then boots the matching instrumented verification ISO and checks flavor, release, architecture, systemd as PID 1, zero failed units, zero SELinux AVC denials, and enforcing mode for RPM-family images. For CentOS Hyperscale that clean result includes the compatibility module the live rootfs ships, described in SPEC.md, without which its systemd raises denials against the base policy. x86_64 is tested through both BIOS and UEFI; AArch64 is tested through UEFI.

```sh
buck2 test //tests:boot-fedora-44-x86_64-bios
buck2 test //tests:boot-fedora-44-x86_64-uefi
buck2 test -c buckos.aarch64_emulation=true \
  //tests:boot-fedora-44-aarch64-uefi
buck2 test -c buckos.aarch64_emulation=true \
  //tests:boot-fedora-prebuilt-44-aarch64-uefi
```

QEMU and firmware are host test prerequisites. Defaults cover common distro paths. Override them when needed:

```ini
[buckos]
  qemu_x86_64 = /usr/bin/qemu-system-x86_64
  qemu_aarch64 = /usr/bin/qemu-system-aarch64
  ovmf_code = /usr/share/OVMF/OVMF_CODE_4M.fd
  ovmf_vars = /usr/share/OVMF/OVMF_VARS_4M.fd
  aarch64_uefi = /usr/share/qemu-efi-aarch64/QEMU_EFI.fd
```

## Configuration

The checked-in configuration builds Fedora releases 44 and 45 with the binary-seeded buildroot. Machine-specific overrides belong in `.buckconfig.local`, which is ignored by Git.

Select a different configured release:

```ini
[buckos.fedora]
  release = 44
```

Use the host buildroot for local development:

```ini
[buckos.fedora]
  buildroot = host
```

CentOS Stream, CentOS Hyperscale, Debian, and Ubuntu use the same release and provenance settings under `[buckos.centos]`, `[buckos.centos-hyperscale]`, `[buckos.debian]`, and `[buckos.ubuntu]`; the checked-in releases are `9,10`, `9,10`, `13`, and `26.04`. CentOS Stream release 9 layers EPEL and EPEL Next, while unsuffixed CentOS Stream targets remain on release 10. CentOS Hyperscale 9 uses that EPEL Next base; Hyperscale 10 uses EPEL without EPEL Next. Release 10 is the default for both CentOS flavors.

### Checking the host

A source build reaches outside the sandbox. The sandbox pins every byte of the *filesystem* a package builds in, but a spec can still call a tool that talks to the running kernel, and no amount of pinning changes which kernel that is.

```sh
buck2 run //tools:hostcheck
```

It probes each capability by doing the thing rather than by reading a version or `/proc/config.gz`, names the packages affected by each result, and prints valid `%bcond` overrides for host capability gaps:

```
MISS netlink-crypto   kernel crypto user API (CONFIG_CRYPTO_USER)   errno 93 (Protocol not supported)
ok   af-alg           kernel crypto sockets                        available
ok   user-namespaces  unprivileged user namespaces with a subid range  available

netlink-crypto: libkcapi's sha512hmac and fipshmac open a NETLINK_CRYPTO socket
to look up an algorithm. gmp and nettle guard their use with bconds.
  buildable with a feature disabled: gmp, nettle

[buckos.fedora]
  without = gmp:fips, nettle:fipshmac
```

A `%bcond` keeps the package building from source and drops only the guarded feature. Hostcheck reports a missing capability as fatal when no such fallback exists.

It exits non-zero only for a capability with no `%bcond` fallback, such as user namespaces, so it is usable as a CI gate without rejecting a host that can use an explicit feature override.

Source-policy exceptions are already represented in the checked-in lock and generated graph. Hostcheck does not attempt to reverse them. The generic `prebuilt` setting remains available for diagnostics when a source recipe exists, but it only routes that existing recipe to its pinned upstream binary:

```ini
[buckos.fedora]
  prebuilt = bash
```

It cannot restore the Fedora `kernel` or `libxcrypt` recipes because those recipes are absent by source-policy exception.

Turn off a spec's `%bcond` for one source package, when the build host cannot support it:

```ini
[buckos.fedora]
  without = gmp:fips, nettle:fipshmac
```

The case this exists for is a kernel built without `CONFIG_CRYPTO_USER`. libkcapi's `fipshmac` opens a `NETLINK_CRYPTO` socket to ask the kernel about an algorithm, gets `EPROTONOSUPPORT`, and dies in `%install`:

```
Allocation of hmac(sha256) cipher failed (ret=-93)
```

Nothing to do with the sandbox: the same binary fails identically run straight on the host, and the `AF_ALG` socket it actually hashes with binds fine. A stock Fedora kernel enables `CONFIG_CRYPTO_USER`, so the guarded `gmp` and `nettle` features remain enabled by default.

`kernel` and `libxcrypt` have no equivalent `%bcond`. Their pinned treatment is part of the Fedora source policy rather than hostcheck output.

Rewrite Fedora's recorded repository prefix to a mirror with the same directory layout:

```ini
[buckos.fedora]
  mirror_base = https://archives.fedoraproject.org/pub/archive/fedora/linux
```

For CentOS Stream and CentOS Hyperscale, `mirror_base` names the common root above the release and SIG directories:

```ini
[buckos.centos]
  mirror_base = https://mirror.example.invalid/centos
```

That mirror contains `9-stream/`, `10-stream/`, and `SIGs/`. EPEL repositories retain their recorded Fedora Project bases; use `package_url_template` to redirect every pinned RPM into one content-addressed store.

A static content-addressed HTTP store can provide pinned packages through `package_url_template`. The template must contain `{sha256}` and may contain `{sha256_12}`, `{filename}`, `{stem}`, `{ext}`, and `{release}`. Filename and release components are escaped for use in URL paths.

```ini
[buckos.fedora]
  package_url_template = https://cache.example.invalid/fedora/{release}/{stem}-{sha256_12}{ext}?digest={sha256}
```

A content-addressed read-through service can be configured with `blob_base`. The service receives the SHA-256 digest and filename in the path, plus the Fedora release and repository-relative location as query parameters.

```ini
[buckos.fedora]
  blob_base = https://cache.example.invalid/rpm
```

`package_url_template` cannot be combined with `mirror_base` or `blob_base`. These settings change where bytes are fetched but do not change the full SHA-256 digest enforced by Buck2.

CentOS Stream 9 and CentOS Hyperscale 9 compile pinned squashfs-tools 4.6.1 source because their packaged versions cannot add the per-file SELinux xattrs required by an enforcing live image. The source archive is digest-verified like package downloads; an equivalent mirror can be selected with `[buckos] squashfs_tools_source_url`.

Enable remote cache lookups or remote execution through the execution platform:

```ini
[buckos]
  remote_cache = true
  remote_execution = true
  remote_x86_64_properties = platform.OSFamily=linux,platform.arch=x86_64
  remote_aarch64_properties = platform.OSFamily=linux,platform.arch=aarch64
  remote_x86_64_use_case = buck2-default
  remote_aarch64_use_case = buck2-default
```

A remote backend still requires a matching `[buck2_re_client]` configuration. The property keys and values are backend-defined; the repository contains no service-specific defaults. Remote workers need Linux user namespaces, an accepted isolation tool, subordinate IDs, package-manager namespace helpers, and enough scratch space for unpacked package trees. Architecture constraints select the matching x86_64 or AArch64 platform.

See [REMOTE_EXECUTION.md](REMOTE_EXECUTION.md) for the implementation-ready NativeLink reference topology, worker preflight contract, cache policy, deployment layout, bring-up sequence, and acceptance gates.

## Repository layout

```text
defs/                    Providers, flavor dispatch, release handling, and Buck rules
flavors/fedora/          Fedora target generation, lockfiles, and generated package data
flavors/centos/          CentOS Stream lockfile, generated package data, and replay targets
flavors/centos-hyperscale/ CentOS Hyperscale lockfile, generated data, and replay targets
flavors/debian/          Debian lockfile, generated package data, and replay targets
flavors/ubuntu/          Ubuntu lockfile, generated package data, and replay targets
flavors/buckos/          BuckOS implementation-status documentation
platforms/               Target constraints and execution-platform registration
REMOTE_EXECUTION.md       NativeLink reference deployment and acceptance plan
infra/remote-execution/   NativeLink configuration, SDME provisioning, and validation tools
tests/                   Checked-in source RPM replay fixtures
toolchains/              Prelude toolchain registrations
tools/                   Solver, generators, action drivers, and tests
```

See [SPEC.md](SPEC.md) for the implemented interfaces and data flow.

## Constraints

A release with no `updates/` tree yet -- a just-branched one -- is not an
error; the repo is reported absent and skipped.

`buildroot = binary-seed` is the default. It unpacks pinned rpms into a
self-contained tree that becomes `/` inside the isolation namespace, so the
toolchain is the one that release shipped rather than the one the build
machine happens to have. That is a compatibility argument before it is a
purity one: a package compiled against the host's glibc can reference a
symbol version the target's glibc lacks, and a host rpm of another vintage
writes an rpmdb the image cannot read -- both of which fail at *runtime*, in
the image, long after a green build. It is also what makes the graph
remote-executable, since a seeded buildroot is hermetic.

`buildroot = host` uses the host's rpm installation. It is the development
escape hatch: not hermetic, local-only, no cache upload.

`tools/_isolation.py` uses bubblewrap where it is installed and an
unprivileged user namespace via util-linux `unshare` otherwise. Bubblewrap is
the production path and the two are not interchangeable. Bubblewrap gives a
build a minimal `/dev` and a read-only `/proc`, while the unshare path rebinds
the host's, so a spec that probes for a device node or writes a procfs tunable
can get a different answer from each. Bubblewrap also refuses to start without
subordinate ID ranges where unshare degrades to a single mapped ID.

## Limitations

Stated plainly, because each one is load-bearing:

- **The FHS skeleton is still fabricated.** `tools/buildroot_assemble.py`
  creates `/dev`, `/proc`, `/sys`, `/tmp` and friends, because several
  `brp-*` scripts and `%__os_install_post` steps fail on a missing one.
  Each fabrication is listed explicitly in that file rather than inferred.

  `filesystem` does own those four, and they are `--excludepath`'d out of
  the transaction rather than left to it: the sandbox bind-mounts them
  inside the tree so rpm and the scriptlets have a working system to run
  in, and rpm cannot chown a live mount -- `cpio: chown failed - Device or
  resource busy`. Nothing is lost. They hold no package content, and their
  modes are the sandbox's business rather than the image's; this tree is a
  chroot to build in, not a filesystem to boot.

  The `/usr/sbin -> bin` compat link is made for real by `filesystem`'s
  `%pretrans`, and is still created up front as well, because payloads are
  unpacked *before* that transaction and anything reading the tree in
  between would see the gap.

  Up front means *only where the path is free*. A release that predates the
  sbin merge ships `/usr/sbin` as a real directory full of real binaries,
  and replacing it with a symlink would discard them, so an occupied path is
  left alone. Enterprise Linux buildroots are that case: they have a real
  `/usr/sbin`, not a link, and a tool that lives only there is unreachable
  through `/usr/bin`. Anything resolving a binary by name has to try both.
- **Genuinely ambiguous capabilities need a human.** Real repodata has
  capabilities with many providers -- `glibc-langpack` has 211,
  `system-release` 34 -- and the solver refuses to guess. `--override
  cap=package` settles each one, and resolving a batch tends to expose the
  next layer beneath it, so arriving at a clean solve is iterative. The
  overrides are an input to the solve and belong in review alongside the
  lockfile.

  Most ambiguity is not genuine, and is not reported as such. An
  ambiguous capability is deferred to the fixed point exactly as
  `(A or B)` is, and if the closure already contains one of its providers
  the requirement is simply satisfied. That is a fact about the set rather
  than a policy about which package is nicer. It is most of them: solving
  the live image's source packages reports 500 ambiguities resolved
  eagerly and **69**
  deferred, because `/usr/bin/basename` between `coreutils` and
  `coreutils-single` is not a real question in a buildroot that has had
  `coreutils` in it since `@buildsys-build`.

  What survives is **20** distinct decisions, and they are real ones --
  `text-www-browser` between elinks, lynx and w3m; `libfofi.so.4()(64bit)`
  between `xpdf` and `xpdf-libs`; `java-devel` between the JDKs. Each is
  reported once with its candidates and the packages that asked, rather
  than once per asker.
- **A version constraint picks the provider.** Fedora keeps several majors
  of a Rust crate side by side -- `rust-base64-devel` is the current one,
  `rust-base64_0.21-devel` and friends are compat packages -- and every one
  of them provides `crate(base64)`. The range is the only thing that tells
  them apart, and it is stated in the requirement:

  ```
  (crate(base64) >= 0.21 with crate(base64) < 0.23)
  ```

  Repodata puts that in attributes -- `flags="LT" ver="0.23"` -- rather than
  in the capability name, so a parser reading only `@name` turns every
  constrained dependency into an unconstrained one. That is what this did,
  and the cost was 120 hand-written `--override crate(...)=...` entries
  saying "pick the current major", plus one package that needed the
  opposite and failed several minutes into `%build`:

  ```
  error: failed to select a version for the requirement `base64 = ">=0.21, <0.23"`
  candidate versions found which didn't match: 0.23.1
  ```

  Constraints now reach the resolver, from repodata and from a probe
  alike, and providers that cannot satisfy them are dropped before the
  ambiguity is even considered. Where several satisfy, the newest wins --
  what rpm, dnf and cargo all do, and not the judgement call an
  unconstrained ambiguity is. All 120 crate overrides went away and
  exactly one package's buildroot changed: `rust-rpm-sequoia`, which now
  gets the 54 compat crates its constraints actually name.

  Three details are rpm's rules rather than obvious ones, and each was a
  wrong answer first. Comparison happens at the precision the *constraint*
  states, so `Requires: automake = 1.18.1` is satisfied by 1.18.1-2.fc43.
  A requirement stating no epoch is read as epoch 0 while the provider
  keeps its own, so `emacs-filesystem >= 30.2` is satisfied by 1:30.0 --
  which is what an epoch is for. And one package can provide a capability
  at more than one version: `texlive-kpathsea` carries both its NEVR and
  the upstream svn revision.
- **Rich/boolean dependencies are parsed; a genuine `or` still needs a human.**
  `tools/depgraph.py` implements rpm's boolean grammar -- `and`, `or`,
  `with`, `without`, `if`/`else`, `unless`/`else`, nested to any depth --
  and evaluates it against the buildroot closure, iterating to a fixed
  point. Splitting is paren-depth aware, which is not decoration:
  capability names carry parentheses of their own (`crate(anyhow/default)`,
  `python3.14dist(ldap3)`).

  Shape matching does not scale here, which is why the grammar is
  implemented rather than pattern-matched. A full solve of the live
  image's sources surfaces 44 distinct expressions, 27 of them
  redhat-rpm-config's
  `((rpm-build >= … with (rpm-build < … or rpm-build >= …)) if rpm-build)`,
  which is a BuildRequires of a large fraction of the distro. The solve
  reads all 44.

  Two things still need a person. An `or` with no branch present is
  reported rather than chosen, because rpm picks a branch by policy and
  inventing a policy is how a solver quietly installs a different distro
  than the one anyone reviewed; `--override '(a or b)=a'` settles it. And
  `unless` is order-dependent by nature -- "required unless B appears"
  cannot be answered while B might still appear -- so it is settled only
  once nothing else can grow, which is a decision the code makes
  explicitly rather than a property of the graph.
- **Dynamic `BuildRequires` cost a second solve.** Specs using
  `%generate_buildrequires` (most Rust, Go, and modern Python packages)
  compute dependencies that do not exist in static repodata, so solving
  from repodata alone produces a buildroot missing most of what the
  package needs -- and the gap does not surface until `%build` fails
  somewhere unrecognisable. `tools/probe.py` resolves them the only way
  they can be resolved, by running the block: `rpmbuild -br` per source
  package, merged into a checked-in `<release>.probe.json` that `solve.py`
  reads back.

  This cannot be a build rule, and the reason is structural. Buck resolves
  dependencies during analysis; an edge discovered by *running* an action
  exists only after analysis is over. A rule that tried would also be one
  that could never be scheduled remotely, since the worker has to be told
  its inputs before they are known. So it happens at lock time and lands
  in the lockfile as an ordinary declared dependency. What remains is the
  cost: the update loop is two solves deep -- solve, probe, solve again --
  and the probe needs a working buildroot for the very package whose
  buildroot it is computing.
- **Actions are coarse, and permanently so -- rpm forbids the fix.** One
  `srpm_build` action is `%prep` through `%install`, so Buck's caching
  works between packages and not within one.

  The obvious repair is to make each `rpmbuild` stage its own action, and
  rpm has exactly the flag for it: `--short-circuit` skips straight to
  `-bc`, `-bi` or `-bb`. It cannot be used here, and not for a Buck
  reason. Quoting rpm 6.0.2's own manual, the version these images are
  built with:

  > Useful for local testing only. Packages built this way will be marked
  > with an unsatisfiable dependency to prevent their accidental use.

  rpm deliberately poisons short-circuited output, because a package
  assembled from stages that never ran together is not a package it is
  willing to vouch for. So the choice is not "coarse actions or fine ones"
  -- it is coarse actions or rpms that refuse to install.

  The usual framing of the cost is also wrong for this repo, and worth
  correcting: "a one-line change recompiles the package" describes editing
  upstream source, which a replay builder never does -- the sources are
  pinned tarballs. What actually invalidates is a buildroot change or a
  `%bcond` flip, and both are *correct* invalidations rather than caching
  failures: a package compiled against a different toolchain really is a
  different artifact. The cost that bites is fan-out. Move one seed rpm
  and everything built against it rebuilds, which is why the seed set's
  size is tracked as `bootstrap_depth` rather than left implicit.

  That fan-out cannot be narrowed either, for the reason in the remote
  execution section above: the buildroot reaches each action as one whole
  tree, because RE materializes only an action's declared inputs and a
  projection would ship a subdirectory whose libraries are missing. Whole
  tree means one hash, and one hash means any change to it invalidates
  every consumer.

  SPEC.md §1 names the escape hatch -- per-package **graduation**, rewriting
  one package as a native recipe when the control is worth the
  maintenance. It is not implemented: `package()` dispatches to the RPM and
  Debian-family replays, and BuckOS fails during loading because it has no
  frontend.
- **USE flags reach only as far as the packaging exposes them.** For rpm
  that is `%bcond`, which is generous; for Debian it is much narrower.
- **A rootfs needs subordinate id ranges, and its artifact is a tarball.**
  `rootfs` is the other half of the buildroot: it runs `rpm --install` for
  real, with a database and scriptlets, inside the target release's own rpm
  (a current Fedora keeps its rpmdb in sqlite; an older host rpm can write bdb and
  the image could not read its own database). That transaction chowns files
  to `mail`, `tss`, `systemd-network` and others, and
  `unshare --map-root-user` maps exactly one id -- `filesystem`'s
  `/var/spool/mail` fails with `EINVAL` a few files into the first package.
  So the builder needs an entry in `/etc/subuid` and `/etc/subgid` plus
  `newuidmap`/`newgidmap`; `tools/_isolation.py` installs the wider map via
  a handshake with the namespaced child. Without it the build still runs,
  with a warning, and fails on the first package that ships a non-root
  file. The consequence is the artifact shape: the installed tree contains
  files the unprivileged Buck daemon does not own and cannot delete, so the
  output is a tar archive, created inside the namespace, carrying ownership
  and xattrs (file capabilities) as metadata. Downstream
  image rules must unpack it inside their own namespace.
- **SELinux is enforcing, and the labels are written into the image.**
  `setxattr("security.selinux")` returns `EPERM` inside a nested user
  namespace, which is where every stage here runs, so nothing in the build
  can label a file by asking the kernel. It does not have to ask:
  `mksquashfs` takes per-file xattrs in a pseudo-file
  (`path x name=value`) and writes them straight into the image's xattr
  table, and working out *what* each label should be is a pure policy
  lookup needing no privilege at all. The image ships
  its own `selinux-policy-targeted` and its own `matchpathcon`, so the
  contexts come from the distro being built rather than the build host --
  the same argument `initramfs_build.py` makes for using the image's own
  dracut. 22,912 paths resolve in about a second and dedup to 187 distinct
  contexts in the image.

  Measured on the built ISO, not assumed: `enforcing=1`, policy loaded in
  78 ms, PID 1 running as `system_u:system_r:init_t`, **zero AVC denials**,
  login prompt reached. `tools/squashfs_build.py` has the mechanism;
  `squashfs(selinux_relabel = True)` turns it on, and it is off by default
  because an image with no policy has no contexts to look up.

  Two paths out of 22,912 are skipped rather than labelled -- systemd's
  `system-systemd\x2dcryptsetup.slice` and its veritysetup sibling. The
  pseudo-file grammar splits on spaces and gives the backslash meaning of
  its own, so emitting them would label some *other* path, and
  mislabelling is worse than leaving a unit file unlabelled.

  The `EPERM` is not a blanket rule about `security.*`, and the difference
  is worth knowing. It holds even under `unshare -Ur`, on a file where
  setting a `user.*` xattr succeeds, while `security.capability` *is*
  settable from a user namespace, via
  the kernel's v3 format that records a rootid, which is why the images
  here do carry working file capabilities on `arping`, `clockdiff`,
  `newuidmap` and `newgidmap`. The kernel extends that courtesy to
  capabilities and withholds it from labels. So `rpm-plugin-selinux` sets
  no context during the rootfs transaction, and there is nothing on disk
  for `mksquashfs` to copy -- measured on a live rootfs, zero labels across
  the whole tree. That is why the labels are *written into the
  image* rather than set on files: it is offline image editing, not
  privilege.

  buckos-build reaches the same conclusion from the other side, using
  `debugfs`'s `ea_set` to inject `security.ima` into ext4 from `.sig`
  sidecars, and records that it has "no unprivileged equivalent to
  `debugfs` for those filesystems" otherwise -- its squashfs path falls
  back to `evmctl ima_setxattr`, which goes through the kernel and so
  silently no-ops unprivileged. `mksquashfs -pf` looks like the missing
  equivalent for that case too.
- **Scriptlets run, and a real transaction is why.** Trees are unpacked
  with `rpm2archive | tar` -- GNU tar's `--delay-directory-restore`, not
  `cpio`, because rpm payloads ship read-only directories with files
  beneath them and cpio applies a directory's mode as soon as it creates
  it. That unpack is a bootstrap step: it puts an rpm on disk to run the
  real transaction with. `rpm --install` then runs inside the tree, writes
  the database, and executes `%pre`/`%post`.

  A real install chowns files into the subordinate id range, and Buck,
  which does not own those ids, then cannot delete or re-materialize its
  own output. Making directories writable in a `finally` handles that, and
  also covers the case a `--justdb` tree never had: a transaction that
  fails partway leaves the tree unwritable too.

  `--justdb --noscripts` is not sufficient, and `golang-bin` is why. Its
  `%post` runs `update-alternatives --install /usr/bin/go …`; without
  scriptlets `/etc/alternatives` stays empty and `/usr/bin/go`, which ships
  as a symlink into it, dangles. `libcap` then autodetects Go with `go
  version`, gets nothing, silently omits its `captree` program, and fails
  in `%files` on a file nothing said it was skipping. Every step is a
  warning or a success until the last one.

  `/usr/sbin -> bin` and the systemd sysusers entries in `/etc/passwd` are
  not evidence for running scriptlets: a tree built *with* `--noscripts`
  has them too. The reasons differ, and neither is a scriptlet. The passwd
  entries ship in `setup`'s payload; the compat link is fabricated by the
  buildroot assembly itself, for the gap described above. `golang-bin` is
  the case that distinguishes the two.

  Triggers are off (`--notriggers`) for the shared base, where a single
  transaction installs everything at once and firing order would be rpm's
  internal ordering rather than anything this repo decides. The per-package
  overlay turns them on and needs to, because it installs into a tree that
  already exists -- glibc's file trigger rebuilds `/etc/ld.so.cache`, and
  without it `bpftool` cannot load a library sitting on disk.

- **The Debian-family buildroot has no trigger to turn on, so it runs
  `ldconfig` itself.** Composing a tree from payloads runs neither maintainer
  scripts nor dpkg triggers, and there is no per-package overlay transaction to
  enable them in. So `/etc/ld.so.cache` is absent, `ctypes.util.find_library`
  shells out to `ldconfig -p` and gets nothing, and `xkeyboard-config`'s
  generator cannot load `libxkbcommon` from a file sitting in the tree. The
  assembly therefore runs the buildroot's own `ldconfig` once, in the sandbox,
  after composition.

  In the sandbox rather than on the host, and the difference is not stylistic.
  Both Debian-family flavors carry AArch64 locks, and the host's `ldconfig -r`
  against a foreign-architecture tree **exits 0 while writing a cache that holds
  no libraries**. The failure mode of the obvious fix is a cache that exists and
  answers nothing, which is worse than the absence it replaces. Reached through
  the binfmt handler, the tree's own `ldconfig` produces a populated one.

  Once at assembly is necessary and not sufficient, because there are two
  trees. The package replay composes its own: it overlays each dependency's
  installroot on top of the seed *after* that seed's cache was built, so the
  cache it inherits is valid for what it was built from and stale for exactly
  the libraries the package was given. Measured in a failing `xkeyboard-config`
  sysroot: cache present at 5607 bytes, `libxkbcommon.so.0` present, and zero
  `xkbcommon` entries in the cache. A populated cache that omits the library
  sitting beside it is harder to diagnose than an empty one, because the file
  is there and looks right. The replay therefore runs `ldconfig` again after
  the overlay, which takes that sysroot to 10323 bytes and two entries.

  This is the same need the RPM per-package overlay meets by turning triggers
  on, one paragraph above. The Debian family has no trigger to enable, so the
  overlay does it directly.

- **Backslashes in payload paths become directories, in buildroots only.**
  buck2 reserves the backslash as a path separator and cannot address a
  file whose name contains one. systemd escapes a dash in a unit name as
  `\x2d`, so `systemd-udev` ships
  `usr/lib/systemd/system/system-systemd\x2dcryptsetup.slice`, and F44 adds
  a two-backslash one. Any tree holding them is unbuildable as a directory
  output. `tools/_rpm.py` therefore splits such names at the backslash as
  `tar` writes them, giving buck2 the tree it already believes it is
  looking at -- one file becomes a directory and a file. It round-trips:
  joining the components back with a backslash reconstructs the original,
  which dropping the file or deleting the byte would not.

  Safe only because of where it applies. Every caller unpacks into a tree
  that runs `mksquashfs` or `rpmbuild` and never boots, where a systemd
  unit is inert either way. A shipped image does not come through that
  path at all -- `rootfs` runs a real transaction and hands back a tarball,
  and a tar member has no such restriction -- so the image keeps every file
  rpm puts in it, backslashes included.

  Worth knowing if you ever hit it directly: doing this *after* extraction
  instead of during it appears to work and is wrong. The file exists under
  `buck-out` between `tar` creating it and the rename, buck2 notices it
  there, and the build that observed it **succeeds** while the next one
  fails on a path no longer on disk. That reads exactly like stale daemon
  state, and clearing `buck-out/v2/cache/{materializer_state,incremental_state}`
  after a `buck2 kill` does make it go away -- until the next build
  re-poisons it.
- **A configure probe with a wall-clock timeout makes build output depend
  on machine load.** gnulib decides whether the system `getcwd` copes with
  paths past `PATH_MAX` by compiling a test that walks a deep directory
  chain, and gives it `alarm(5)`. Under emulation on a busy host it does not
  finish. Every failing run measured here died on `SIGALRM`, exit 142, six of
  six, and none reached a verdict at all. `configure` records `no` for a probe
  it killed, gnulib's replacement is compiled in, and `find` gains 1536 bytes
  and swaps `getcwd` for `lstat`, `readlink` and `rewinddir`.

  So a loaded build farm ships different code from an idle one, silently, with
  nothing failing anywhere. `tools/dpkgbuild_replay.py` pins that one cache
  variable, and the pinned value is the probe's own answer rather than a
  chosen one: the archive's `find` calls the system `getcwd` on both amd64 and
  arm64, so leaving it unpinned is the deviation.

  **The class is wider than the fix.** Ten packages in the Debian-family set
  carry alarm-guarded probes, roughly fourteen cache variables, with budgets
  down to one and two seconds. Only the one above is pinned, because a pin is
  only justified where the probe is known not to have completed *and* the true
  answer is known independently. The rest are latent.

  They are also hard to observe. `config.log` is last-writer-wins, so scanning
  it after the fact cannot see a verdict that flapped between runs; detecting
  that needs the verdicts captured per build rather than read afterwards.

- **`--isolation none` is best-effort.** Under host provenance the
  dependency sysroot is exported via `PATH`, `PKG_CONFIG_PATH`, and
  friends; a spec that hardcodes `/usr/lib64` still reads the host's copy.
  This is why a seeded buildroot is the production path.

## Non-goals

- Reimplementing RPM or DPKG semantics.
- Translating spec files into native Buck rules.
- Reproducing Fedora's Koji artifacts bit for bit.
