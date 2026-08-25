# buckos-distro

A Buck2 repo that builds Linux distributions from their own upstream source
packages. Point it at Fedora's source RPMs and it rebuilds Fedora; point it
at Debian's source packages and it rebuilds Ubuntu; point it at BuckOS
recipes and it builds BuckOS. The distro is a **flavor**, selected by
config, and no rule knows which one is active.

```sh
./setup.sh                        # install buck2, check prerequisites
buck2 build //tests:hello         # replay a source rpm end to end
buck2 build //:flavor             # which flavor is configured
```

## The idea in one paragraph

Every distro's packaging already encodes how to build its packages —
`.spec` files, `debian/rules`. That knowledge is enormous and
battle-tested, and rewriting it as native Buck rules means maintaining a
fork of it forever. So this repo does not translate packaging; it
**replays** it. `rpmbuild -bb` runs inside a hermetic Buck action against a
buildroot Buck assembled, with the spec file untouched. Buck supplies what
the distro's build system does not: a content-addressed dependency graph,
caching, and remote execution. See [SPEC.md](SPEC.md) §1 for the rejected
alternative and what replay costs.

## Status

| flavor | source format | driver | state |
|--------|---------------|--------|-------|
| fedora | `.src.rpm` | `rpmbuild -bb` | **boots**: F43 and F44 live ISOs reach a login prompt in qemu, from solves against real repodata |
| ubuntu | `.dsc` | `dpkg-buildpackage -b` | designed, not implemented — [flavors/ubuntu/README.md](flavors/ubuntu/README.md) |
| buckos | tarball | configure/make | designed, not implemented — [flavors/buckos/README.md](flavors/buckos/README.md) |

## What actually works

`buck2 build //tests:hello` takes a checked-in `.src.rpm`, unpacks it,
replays its spec with `rpmbuild -bb`, and produces an installroot
containing a working binary. `//tests:hello-greeting` replays the *same*
source package with one `%bcond` flipped and produces a different binary —
the USE-flag mechanism, with nothing patched.

`buck2 test //tools:depgraph_test` runs 27 tests over the graph algorithms:
capability resolution, transitive closure, Tarjan SCC on a 5000-node chain,
bootstrap-cycle staging, determinism.

`tools/solve.py` resolves against real Fedora repodata — 77,664 binary and
24,019 source packages for F43 — and writes a lockfile pinning every
dependency by sha256. The only unresolved capabilities left are rich
boolean deps, which are flagged rather than guessed at.

`buck2 build //flavors/fedora:iso-live-43` produces a hybrid live ISO that
boots. Verified by booting it, not by inspecting it: Fedora 43 reaches a
login prompt under both BIOS (isolinux) and UEFI (OVMF), and Fedora 44
under UEFI, on kernels `7.1.10-100.fc43` and `6.19.10-300.fc44` — two
distros out of one build graph. Everything in the image is an upstream
binary rpm pinned by the lockfile; the source-replay path is a separate,
much smaller pipeline that the images do not consume yet.

## Multiple releases at once

Which release you build is an *axis*, not a mode the repo is switched
into. Fedora 43 and Fedora 44 are different build universes — different
compiler, different macros, different pinned dependency set — so every
release named in `[buckos.fedora] releases` gets its own targets, and they
coexist in one build graph:

```sh
buck2 build //flavors/fedora:buildroot-binary-seed-43 \
            //flavors/fedora:buildroot-binary-seed-44
```

The unsuffixed `:buildroot-host` aliases the newest release, so callers
that do not care keep working. `//platforms:fedora-43-x86_64` is a real
platform built from a `release` constraint, alongside the existing
`flavor` and `provenance` constraints, so a target can be declared
incompatible with a release rather than silently building against the
wrong one.

The point is comparison. With both lockfiles present, "what changed
between releases" is a diff rather than two builds you cannot line up —
for the three-package set above, all 220 seed packages moved and gzip went
`1.13-4.fc43` → `1.14-2.fc44`.

## The dependency chain

This is the hard part, and it is solved in three layers rather than one.
[SPEC.md](SPEC.md) §3a has the full treatment; the short version:

1. **Solve** — offline, against pinned repodata, producing a checked-in
   lockfile. `tools/solve.py`. Not a build action: the solve is reviewed as
   a diff, like `cargo update` or `reindeer vendor`.
2. **Generate** — lockfile to Buck targets.
3. **Build** — a DAG by construction, because the generate step already
   resolved every capability to a concrete target.

Genuine bootstrap cycles (gcc↔glibc) are found with Tarjan SCC and broken
by explicit staging — `gcc-stage1` → `glibc-stage2` → `gcc-stage3` — not by
quietly demoting a package to the prebuilt seed. The size of the seed set
is the repo's honest bootstrap debt, and `tools/depgraph.py` reports it as
`bootstrap_depth`.

## From packages to a bootable image

Five rules stand between a solved package set and an ISO, and they are
separate rules because of what each one costs:

| rule | does | cost |
|------|------|------|
| `rootfs` | real `rpm --install`, with a database and scriptlets | minutes |
| `kernel_image` | reads the rootfs tar index, copies `vmlinuz` out | instant |
| `initramfs` | runs the *image's own* dracut inside the image | minutes |
| `squashfs` | compresses the rootfs | minutes, the expensive half |
| `iso_image` | arranges the result, stamps a bootloader on it | seconds |

Fusing any adjacent pair would be less code and worse. `kernel_image` is
split from `initramfs` because everything downstream needs the kernel
version, and asking for it should not re-run dracut. `squashfs` is split
from `iso_image` because a change to the kernel command line or the volume
label should not recompress a root filesystem.

`squashfs` and `iso_image` run inside an **`image-tools` buildroot** — a
`seeded_buildroot` solved from its own package list, exactly like the one
packages are compiled in. `mksquashfs`, `xorriso`, `grub2-mkimage` and
`mtools` are build inputs like any rpm. An ISO built by whatever `xorriso`
the build machine happened to have is not reproducible, and on a machine
with none it is not buildable at all. It also makes both rules hermetic,
which is what makes them RE-eligible and cacheable.

Two layout decisions are worth stating, because the obvious alternative is
what most people write first:

- **The squashfs has no top-level `LiveOS/`, so it *is* the root
  filesystem.** Fedora's own images nest an ext4 `rootfs.img` inside the
  squashfs; dracut's `dmsquash-live` supports both and falls back to using
  the squashfs directly when that directory is absent. The ext4 variant
  needs a filesystem image sized in advance and a loop mount, and the only
  unprivileged way to build one is `mkfs.ext4 -d`, which silently truncates
  when the size guess is low. Using the squashfs directly has no size to
  guess.
- **`EFI/BOOT` is written twice**, into the ISO9660 tree and again inside
  `images/efiboot.img`. Firmware booting optical media reads the FAT image
  named by the El Torito alternate entry, so `BOOTX64.EFI` and `grub.cfg`
  have to be in there; firmware booting the same file written raw to a USB
  stick reads the ISO9660 tree instead. Ship one and not the other and the
  image boots in exactly one of the two ways someone will try.

The grub binary is assembled by the *target's* `grub2-mkimage` from the
target's modules, with the module list filtered against what that release
actually ships — a module named and absent is a hard error, and which
modules exist moves between releases. It is unsigned, so Secure Boot is
not supported.

`root=live:CDLABEL=` is derived from the volume label by the rule rather
than taken as a second attribute. When those two disagree the initramfs
waits for a device that never appears and says nothing about why.

## Layout

```
defs/
  providers.bzl          PackageInfo, BuildrootInfo, FlavorInfo, BootInfo
  flavor.bzl             package(), the one macro every recipe calls
  releases.bzl           release as a config-driven axis, not a mode
  buildroot_helpers.bzl  the remote-execution contract
  exec.bzl               execution platform registration
  rules/
    srpm.bzl             srpm_unpack, srpm_build, rpm_subpackage, prebuilt_rpm
    buildroot.bzl        host_buildroot, seeded_buildroot
    rootfs.bzl           rootfs — a real rpm transaction, output as a tarball
    boot.bzl             kernel_image, initramfs
    image.bzl            squashfs, iso_image
flavors/<name>/          per-flavor buildroots, seed sets, lockfiles, recipes
tools/                   the drivers, the solver, and the probe
platforms/               target platforms, flavor and provenance constraints
toolchains/              prelude toolchains, and toolchains//:buildroot
```

`toolchains//:buildroot` is the only integration point — the socket every
package build reaches its environment through, mirroring how buckos-build
exposes `toolchains//:buckos`. Swapping flavors or swapping a flavor's
buildroot provenance is a config change that touches no rule.

## Remote execution

RE compatibility is a constraint, not a later concern, and the patterns are
lifted from buckos-build where they were learned the hard way:

- **A non-hermetic buildroot never runs remotely and never uploads to the
  shared cache.** `buildroot_local_only()` and `buildroot_cache_upload()`
  derive both from `BuildrootInfo.hermetic`, so a `host`-provenance build is
  pinned local automatically. An RE worker has no rpm macros and no host
  `/usr`; an action that reads them either fails there or, worse, succeeds
  with different inputs and poisons the cache for everyone.
- **Tree artifacts are passed whole, as `hidden` inputs.** RE materializes
  only an action's declared inputs, so projecting a subpath of a buildroot
  gets you that subdirectory and nothing else — the tools then fail to load
  their own libraries.
- **The seed is never mutated.** Dependency installroots are composed into
  a fresh tree, because the seed is a Buck input shared by every package in
  the build.

`tools/re_contract_test.py` enforces all three over the rule sources, because
each one fails by omission: an author writes `ctx.actions.run(...)` without
thinking about RE and nothing goes red until someone else downloads a
machine-specific artifact.

**Status: RE-shaped, not RE-tested.** No RE backend has ever run this graph.
Both switches default off and `.buckconfig` ships no `[buck2_re_client]`
section, so there is nothing to dispatch to:

```ini
[buckos]
  remote_execution = true    # in .buckconfig.local
  remote_cache = true
```

Two things are known to stand between that and a green remote build. Neither
is a Buck problem:

- **`rootfs` cannot run on an arbitrary worker.** It needs unprivileged user
  namespaces, setuid `newuidmap`/`newgidmap`, and a `/etc/subuid` +
  `/etc/subgid` range for the executing user. A worker without them fails at
  `unshare`, and no amount of action annotation changes that. The replay
  actions have no such requirement.
- **Cold fetches are slower than buck2's HTTP timeout.** `http_head` gives up
  after 10s; a 21MB rpm pulled through a cold read-through cache took 36s
  here. Fetches are retried and eventually win, but a cold clone will show a
  wave of HTTP warnings first.

## Configuration

```ini
[buckos]
  flavor = fedora            # -c buckos.flavor=ubuntu to override

[buckos.fedora]
  releases = 43,44           # every release to define targets for
  # release = 43             # which one the unsuffixed targets alias;
                             # defaults to the newest in `releases`
  buildroot = binary-seed    # or host, for local development
```

Pinned rpms are fetched straight from Fedora's own mirrors, so a fresh
clone builds with nothing else running. Each `http_file` gets exactly one
URL — this prelude's `http_file` asserts `len(urls) == 1`, so a fallback
chain is not on offer — which means the URL has to be right rather than
merely likely. It is built from a table of repos recorded in the lockfile
at solve time, plus a `repo` key on every pin:

```
"repos": [
  {"name": "binary-releases", "kind": "binary",
   "base": "https://dl.fedoraproject.org/.../releases/43/Everything/x86_64/os"},
  {"name": "binary-updates",  "kind": "binary",
   "base": "https://dl.fedoraproject.org/.../updates/43/Everything/x86_64"}
]
```

A table rather than one binary and one source base, because a closure
legitimately spans both trees once updates are layered in — see [Package
updates](#package-updates).

The bases are recorded rather than reconstructed because a package's
`location` in repodata is relative to its repo and says nothing about which
repo that was, and the repodata is gitignored — the solve is the last point
that knows. They are always canonical upstream URLs, even when a mirror
served the solve: the sha256 is the package's identity and buck2 enforces
it, so any mirror of the same digest is interchangeable and none can
corrupt a build. `tools/solve.py` refuses to write a base that is not a
public Fedora host, since the lockfile is committed and that URL gets
published with it.

Fetches can be redirected without touching the pins:

```ini
[buckos.fedora]
  # A plain mirror of upstream's layout: rewrites the recorded base's prefix.
  mirror_base = https://archives.fedoraproject.org/pub/archive/fedora/linux
```

That knob is what a URL pin needs, because a pin rots on upstream's
schedule rather than this repo's: at EOL a release moves to
`archives.fedoraproject.org` and the recorded base stops resolving even
though every pin in the lockfile is still perfectly good. Repointing the
prefix fixes that without a re-solve.

Re-solving is the part that genuinely needs a non-EOL release, because the
repodata the solver reads is only published for current ones — so
`releases` is a list to revisit once a year rather than a permanent
setting.

### Package updates

Fedora publishes a release twice. `releases/43/` is the frozen GA compose
and never changes; every rebuild after it — errata, CVE fixes, plain bug
fixes — lands under `updates/43/`. Solving against `releases/` alone gives
a lockfile that is perfectly reproducible and permanently unpatched, so
both trees are layered, newest build wins:

```console
$ buck2 run //tools:relock -- --release 43
fedora 43:
  binary-releases: unchanged
  binary-updates: fetching 4a1f…-primary.xml.zst
  source-releases: unchanged
  source-updates: fetching c7b2…-primary.xml.zst
universe: 81817 binary, 24858 source packages (23730 binary / 5341 source superseded by a later repo)
  unresolved            : 0
  image live           : 187 packages
  from binary-releases : 591 pins
  from binary-updates  : 618 pins
```

One command fetches each repo's `primary.xml`, re-solves, and regenerates
the `.bzl` data. Run it on whatever cadence you want fixes at and review
the lockfile diff — that diff *is* the update, and it is the artifact worth
reading. The last two lines are the ones to check: they say how much of
what actually gets installed came from the updates tree, and a zero there
is what a mispointed repo looks like from the outside — a solve that
succeeds and changes nothing.

The refresh reuses the build list, overrides and image sets recorded in the
existing lockfile, so it changes package versions and nothing else. It
cannot bootstrap a release that has no lockfile yet: arriving at that first
set of overrides is iterative human work.

Useful flags: `--offline` re-solves from repodata already on disk, and
`--dry-run` prints the URLs it would sync without touching anything.

Three details are load-bearing:

- **Version comparison is rpm's, not string comparison.** Lexicographically
  `1.10` < `1.9` and `1.0` < `1.0~rc1`, and both are backwards. Getting this
  wrong does not fail loudly — it pins an older build, which looks like a
  perfectly normal lockfile and quietly means the security update everyone
  believes is applied is not. `tools/rpmvercmp.py` is a transcription of
  rpm's `lib/rpmvercmp.c`, tested against rpm's own `tests/rpmvercmp.at`
  corpus rather than against cases invented here.
- **Repo order settles ties only.** The winner is whichever build has the
  higher EVR, so passing the repos the wrong way round cannot silently
  downgrade a package.
- **The base URL is per package, not per release.** An updated rpm has the
  same repo-relative `location` under `updates/` that its original has
  under `releases/`, so a single base would be wrong for whichever half it
  does not describe — quietly, as a 404 on exactly the packages that
  received a fix. Hence the `repos` table and the `repo` key on every pin.

`updates/` also has a genuinely different path shape from `releases/`:
`updates/43/Everything/x86_64` with no `os` component, and
`updates/43/Everything/source/tree` rather than `SRPMS`. Both are upstream
inconsistencies rather than typos; they are transcribed in
`FEDORA_REPOS` in `tools/relock.py`.

Expect new ambiguities over time. An override settles a capability that
several packages provide, and `updates/` can introduce a new provider of
one that previously had exactly one — Fedora 43's `python3.9` compat
interpreter started providing `python(abi)`, which is why that override is
in the 43 lockfile. It surfaces as an unresolved-capability report naming
both candidates, and is fixed by adding an `--override` to the lockfile's
`solve` block and re-running.

A release with no `updates/` tree yet — a just-branched one — is not an
error; the repo is reported absent and skipped.

`buildroot = binary-seed` is the default. It unpacks pinned rpms into a
self-contained tree that becomes `/` inside the isolation namespace, so the
toolchain is the one that release shipped rather than the one the build
machine happens to have. That is a compatibility argument before it is a
purity one: a package compiled against the host's glibc can reference a
symbol version the target's glibc lacks, and a host rpm of another vintage
writes an rpmdb the image cannot read — both of which fail at *runtime*, in
the image, long after a green build. It is also what makes the graph
remote-executable, since a seeded buildroot is hermetic.

`buildroot = host` uses the host's rpm installation. It is the development
escape hatch: not hermetic, local-only, no cache upload.

`tools/_isolation.py` uses bubblewrap where it is installed and an
unprivileged user namespace via util-linux `unshare` otherwise; the two are
equivalent in hermeticity, so neither is a prerequisite.

## Limitations

Stated plainly, because each one is load-bearing:

- **Scriptlets do not run, so a few tree invariants are fabricated.**
  `tools/buildroot_assemble.py` creates the FHS skeleton and the
  `/usr/sbin -> bin` compat link, which on a real system is made by
  `filesystem`'s `%pretrans`. Fedora 43 completed the sbin merge, so
  without it `brp-ldconfig`'s hardcoded `/sbin/ldconfig` dangles one link
  short of a perfectly good `/usr/bin/ldconfig`. Each such fabrication is
  listed explicitly in that file rather than inferred.
- **Ambiguous capabilities need a human.** Real repodata has capabilities
  with many providers — `glibc-langpack` has 211, `system-release` 34 —
  and the solver refuses to guess. `--override cap=package` settles each
  one, and resolving a batch tends to expose the next layer beneath it, so
  arriving at a clean solve is iterative. The overrides are an input to the
  solve and belong in review alongside the lockfile.
- **Rich/boolean dependencies are only partly resolved.** The solver
  evaluates the simple `(A if B)` shape against the buildroot closure,
  iterating to a fixed point, because that is how Fedora attaches
  build-time macros to a buildroot — `cmake` carries
  `Requires: (cmake-rpm-macros = … if rpm-build)`, and every other
  `*-rpm-macros` package hangs off its tool the same way. Deferring those
  is not a nicety: the macro file never lands, `%cmake` stays unexpanded,
  and the spec's `%build` runs as literal shell text. Compound expressions
  (`or`, `and`, `with`, `unless`, `else`) are still flagged `kind: rich`
  and left alone, because a partial reading of a boolean expression is
  worse than an honest refusal to read it — with one exception. When both
  halves of a `with` name the *same* capability the expression is a version
  range, not a choice, and it is collapsed to that capability; rpm's `with`
  means "one package satisfying both", so there is nothing to guess. This
  was written only after `rpm --install` rejected the F43 seed over
  `(python3.14dist(gitdb) < 5~~ with python3.14dist(gitdb) >= 4.0.1)`.
  Both the F43 and F44 solves now report zero unresolved capabilities.
- **Dynamic `BuildRequires` cost a second solve.** Specs using
  `%generate_buildrequires` (most Rust, Go, and modern Python packages)
  compute dependencies that do not exist in static repodata, so solving
  from repodata alone produces a buildroot missing most of what the
  package needs — and the gap does not surface until `%build` fails
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
  cost: the update loop is two solves deep — solve, probe, solve again —
  and the probe needs a working buildroot for the very package whose
  buildroot it is computing.
- **Actions are coarse.** One `srpm_package` action is the whole
  `%build`, so a one-line change recompiles the package. Buck's caching
  works between packages, not within one. See SPEC.md §1.
- **USE flags reach only as far as the packaging exposes them.** For rpm
  that is `%bcond`, which is generous; for Debian it is much narrower.
- **A rootfs needs subordinate id ranges, and its artifact is a tarball.**
  `rootfs` is the other half of the buildroot: it runs `rpm --install` for
  real, with a database and scriptlets, inside the target release's own rpm
  (Fedora 43 keeps its rpmdb in sqlite; a host rpm 4.16 would write bdb and
  the image could not read its own database). That transaction chowns files
  to `mail`, `tss`, `systemd-network` and others, and
  `unshare --map-root-user` maps exactly one id — `filesystem`'s
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
- **Images are unlabeled, so a live ISO boots with `selinux=0`.**
  `setxattr("security.selinux")` returns `EPERM` inside a nested user
  namespace, which is where every stage here runs — even under
  `unshare -Ur`, on a file where setting a `user.*` xattr succeeds. This is
  not a blanket rule about `security.*`, and the difference is worth
  knowing: `security.capability` *is* settable from a user namespace, via
  the kernel's v3 format that records a rootid, which is why the images
  here do carry working file capabilities on `arping`, `clockdiff`,
  `newuidmap` and `newgidmap`. The kernel extends that courtesy to
  capabilities and withholds it from labels. So `rpm-plugin-selinux` sets
  no context and `mksquashfs` has none to carry — measured on a live
  rootfs, zero labels across the whole tree. The image still ships `selinux-policy-targeted`, so a kernel
  with SELinux enabled loads that policy and enforces it against a
  filesystem where nothing has a label. The result is not a degraded boot
  but no boot at all: systemd cannot label `/run/systemd/units`, fails to
  allocate its manager object, and freezes as PID 1 before starting a
  single unit. `enforcing=0` would get past that, but it leaves the policy
  loaded with every file `unlabeled_t` — a system that reports itself
  confined and is not. `selinux=0` states the truth instead.

  The way out is not privilege but *offline image editing*: write the xattr
  into the filesystem's bytes rather than asking the kernel to set it.
  buckos-build does exactly this for IMA, using `debugfs`'s `ea_set` to
  inject `security.ima` from `.sig` sidecars — and its own docstring records
  the limit, that this works for ext4 and has "no unprivileged equivalent to
  `debugfs` for those filesystems" otherwise. squashfs is one of those
  otherwises: `mksquashfs` 4.6.1 can only read xattrs from the source tree,
  and its `-xattrs-add` applies a single value to every file, which is not
  what a per-file context table is. buckos-build's own squashfs path reaches
  for `evmctl ima_setxattr`, which goes through the kernel and therefore
  silently no-ops unprivileged. So this is a genuine gap in both repos, not a
  shortcut taken in this one.
- **No scriptlets *in a buildroot*.** Trees are unpacked with `rpm2archive | tar` — GNU
  tar's `--delay-directory-restore`, not `cpio`, because rpm payloads ship
  read-only directories with files beneath them and cpio applies a
  directory's mode as soon as it creates it. Nothing runs `%post`, so a
  package whose install-time scriptlet matters cannot be satisfied this
  way. This is deliberate and does not extend to images: `rootfs` runs
  them, because that is where they matter.
- **Backslashes in payload paths become directories, in buildroots only.**
  buck2 reserves the backslash as a path separator and cannot address a
  file whose name contains one. systemd escapes a dash in a unit name as
  `\x2d`, so `systemd-udev` ships
  `usr/lib/systemd/system/system-systemd\x2dcryptsetup.slice`, and F44 adds
  a two-backslash one. Any tree holding them is unbuildable as a directory
  output. `tools/_rpm.py` therefore splits such names at the backslash as
  `tar` writes them, giving buck2 the tree it already believes it is
  looking at — one file becomes a directory and a file. It round-trips:
  joining the components back with a backslash reconstructs the original,
  which dropping the file or deleting the byte would not.

  Safe only because of where it applies. Every caller unpacks into a tree
  that runs `mksquashfs` or `rpmbuild` and never boots, where a systemd
  unit is inert either way. A shipped image does not come through that
  path at all — `rootfs` runs a real transaction and hands back a tarball,
  and a tar member has no such restriction — so the image keeps every file
  rpm puts in it, backslashes included.

  Worth knowing if you ever hit it directly: doing this *after* extraction
  instead of during it appears to work and is wrong. The file exists under
  `buck-out` between `tar` creating it and the rename, buck2 notices it
  there, and the build that observed it **succeeds** while the next one
  fails on a path no longer on disk. That reads exactly like stale daemon
  state, and clearing `buck-out/v2/cache/{materializer_state,incremental_state}`
  after a `buck2 kill` does make it go away — until the next build
  re-poisons it.
- **`--isolation none` is best-effort.** Under host provenance the
  dependency sysroot is exported via `PATH`, `PKG_CONFIG_PATH`, and
  friends; a spec that hardcodes `/usr/lib64` still reads the host's copy.
  This is why a seeded buildroot is the production path.

## Non-goals

Reimplementing rpm or dpkg. Reproducing Koji or sbuild bit-for-bit.
Transpiling `.spec` files into Buck rules. See SPEC.md §7.
