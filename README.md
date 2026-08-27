# buckos-distro

`buckos-distro` is a Buck2 repository for replaying upstream Linux package builds and assembling bootable distribution images.

Fedora 43 and Fedora 44 have checked-in package graphs, binary-seeded buildroots, source RPM replay targets, root filesystem targets, and hybrid live ISO targets. Ubuntu 26.04 has a pinned Debian source-package replay path and binary-seeded buildroot. BuckOS remains a declared flavor without a build frontend.

## Quick start

The build runs on Linux. A Fedora build needs Python 3, GNU tar, `rpm2archive`, and either Bubblewrap or util-linux `unshare`. An Ubuntu build needs Python 3, GNU tar, `dpkg-source`, `dpkg-buildpackage`, `dpkg-deb`, and the same isolation choice. The unshare path also needs `newuidmap`, `newgidmap`, and subordinate UID and GID ranges for the build user.

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
| Fedora | Implemented | RPMs, root filesystems, live ISOs |
| [Ubuntu](flavors/ubuntu/README.md) | Source replay | DEBs and install roots |
| [BuckOS](flavors/buckos/README.md) | Stub | None |

The Fedora lockfiles currently replay `gzip`, `xz`, and `zlib-ng` from source. The live image package sets are pinned upstream binary RPMs. The source-replay pipeline and image package sets are separate inputs.

## Build model

An upstream source package remains authoritative. The Fedora frontend unpacks the source RPM, assembles a buildroot from pinned packages, and runs `rpmbuild -bb` without translating the spec file into Starlark. The Ubuntu frontend applies the same model to a `.dsc` source set and runs `dpkg-buildpackage -b` inside a buildroot assembled from SHA-256-pinned DEBs.

Dependency resolution happens before Buck analysis:

1. `tools/solve.py` reads Fedora repository metadata, resolves capabilities, computes runtime closures, and emits a JSON lockfile.
2. `tools/generate.py` converts the lockfile into Starlark data.
3. Buck loads that generated data as an ordinary dependency graph.

Ubuntu uses the corresponding `tools/ubuntu_lock.py` and `tools/ubuntu_generate.py` pair. The checked-in Ubuntu graph currently replays GNU hello as the end-to-end source-build fixture.

The lockfile records exact package locations, SHA-256 digests, repository origins, source recipes, bootstrap stages, overrides, and image sets. Buck verifies each downloaded package against its recorded digest.

`tools/relock.py` refreshes an existing release from Fedora's release and update repositories. It reuses the build list, overrides, and image roots already recorded in the lockfile.

```sh
buck2 run //tools:relock -- --release 43 --dry-run
```

Remove `--dry-run` to fetch metadata, solve the release, and regenerate `flavors/fedora/generated/`. Add `--probe` to run `%generate_buildrequires` before the final solve.

## Releases and targets

`[buckos.fedora] releases` defines every Fedora release loaded into the graph. The final entry is the default unless `[buckos.fedora] release` selects another listed release.

Each release receives suffixed targets such as:

```text
//flavors/fedora:buildroot-binary-seed-43
//flavors/fedora:rootfs-live-43
//flavors/fedora:kernel-live-43
//flavors/fedora:initramfs-live-43
//flavors/fedora:squashfs-live-43
//flavors/fedora:iso-live-43
```

The default release also receives unsuffixed targets. Release-specific target platforms, such as `//platforms:fedora-43-x86_64`, carry the release as a constraint value.

Ubuntu 26.04 similarly provides `//flavors/ubuntu:buildroot-binary-seed-26.04`, `//flavors/ubuntu:hello-26.04`, and unsuffixed aliases for the default release.

## Buildroots

Fedora and Ubuntu support two buildroot provenances:

- `binary-seed` assembles the build environment from pinned Fedora RPMs or Ubuntu DEBs. It is the default and is eligible for remote execution and shared-cache upload.
- `host` uses the host root filesystem and installed distro toolchain. It is non-hermetic, local-only, and excluded from shared-cache upload.

The binary seed cuts bootstrap cycles that Buck cannot represent directly. The solver records staged source builds for cycles that are included in the source-replay set.

Package dependencies contribute install-root trees. Each replay copies the seed and dependency trees into writable scratch space before invoking the target release's RPM tools.

## Image pipeline

The Fedora image pipeline has separate targets for work with different invalidation costs:

- `rootfs` runs an RPM transaction with a package database and scriptlets, then returns a tar archive.
- `kernel_image` extracts the selected kernel and version from the rootfs archive.
- `initramfs` runs the image's own dracut against the rootfs.
- `squashfs` compresses the rootfs and can write SELinux labels from the image's policy.
- `iso_image` creates BIOS and UEFI boot layouts around the kernel, initramfs, and squashfs.

The rootfs is a tar archive because package ownership and valid RPM filenames cannot always be represented safely as a Buck directory artifact. Image actions unpack it inside their isolated work areas.

The live squashfs is used directly as the root filesystem. The ISO contains BIOS and UEFI boot entries, derives `root=live:CDLABEL=` from its volume label, and is not signed for Secure Boot.

Build a Fedora 44 live ISO with:

```sh
buck2 build //flavors/fedora:iso-live-44
```

## Configuration

The checked-in configuration builds Fedora releases 43 and 44 with the binary-seeded buildroot. Machine-specific overrides belong in `.buckconfig.local`, which is ignored by Git.

Select a different configured release:

```ini
[buckos.fedora]
  release = 43
```

Use the host buildroot for local development:

```ini
[buckos.fedora]
  buildroot = host
```

Ubuntu uses the same release and provenance settings under `[buckos.ubuntu]`; the checked-in release is `26.04`.

Rewrite Fedora's recorded repository prefix to a mirror with the same directory layout:

```ini
[buckos.fedora]
  mirror_base = https://archives.fedoraproject.org/pub/archive/fedora/linux
```

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

Enable remote cache lookups or remote execution through the execution platform:

```ini
[buckos]
  remote_cache = true
  remote_execution = true
```

A remote backend still requires a matching `[buck2_re_client]` configuration. Remote workers need Linux user namespaces, an accepted isolation tool, subordinate IDs, RPM namespace helpers, and enough scratch space for unpacked package trees.

## Repository layout

```text
defs/                    Providers, flavor dispatch, release handling, and Buck rules
flavors/fedora/          Fedora target generation, lockfiles, and generated package data
flavors/ubuntu/          Ubuntu lockfile, generated package data, and replay targets
flavors/buckos/          BuckOS implementation-status documentation
platforms/               Target constraints and execution-platform registration
tests/                   Checked-in source RPM replay fixtures
toolchains/              Prelude toolchain registrations
tools/                   Solver, generators, action drivers, and tests
```

See [SPEC.md](SPEC.md) for the implemented interfaces and data flow.

## Constraints

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

- **The FHS skeleton is still fabricated.** `tools/buildroot_assemble.py`
  creates `/dev`, `/proc`, `/sys`, `/tmp` and friends, because several
  `brp-*` scripts and `%__os_install_post` steps fail on a missing one and
  no package owns them. Each fabrication is listed explicitly in that file
  rather than inferred. The `/usr/sbin -> bin` compat link used to be on
  this list and no longer is — `filesystem`'s `%pretrans` now makes it for
  real, see below.
- **Genuinely ambiguous capabilities need a human.** Real repodata has
  capabilities with many providers — `glibc-langpack` has 211,
  `system-release` 34 — and the solver refuses to guess. `--override
  cap=package` settles each one, and resolving a batch tends to expose the
  next layer beneath it, so arriving at a clean solve is iterative. The
  overrides are an input to the solve and belong in review alongside the
  lockfile.

  Most ambiguity is not genuine, though, and is no longer reported as
  such. An ambiguous capability is deferred to the fixed point exactly as
  `(A or B)` is, and if the closure already contains one of its providers
  the requirement is simply satisfied. That is a fact about the set rather
  than a policy about which package is nicer — nothing a reviewer would
  have decided differently. It is most of them: solving the live image's
  126 source packages reports 500 ambiguities resolved eagerly and **69**
  deferred, because `/usr/bin/basename` between `coreutils` and
  `coreutils-single` is not a real question in a buildroot that has had
  `coreutils` in it since `@buildsys-build`.

  What survives is 51 distinct decisions, and they are real ones —
  `text-www-browser` between elinks, lynx and w3m; `crate(regex-syntax)`
  between the current and the 0.6 compat package; `libfofi.so.4()(64bit)`
  between `xpdf` and `xpdf-libs`. Each is reported once with its
  candidates and the packages that asked, rather than once per asker.
- **Rich/boolean dependencies are parsed, but `or` still needs a human.**
  `tools/depgraph.py` implements rpm's boolean grammar — `and`, `or`,
  `with`, `without`, `if`/`else`, `unless`/`else`, nested to any depth —
  and evaluates it against the buildroot closure, iterating to a fixed
  point. Splitting is paren-depth aware, which is not decoration:
  capability names carry parentheses of their own (`crate(anyhow/default)`,
  `python3.14dist(ldap3)`).

  It used to be three hand-written shape matchers, one per expression
  Fedora had so far been observed to emit. That held while three packages
  were built from source and broke at 126: a full solve of the live
  image's sources surfaced 44 expressions none of them could read, 27 of
  them redhat-rpm-config's
  `((rpm-build >= … with (rpm-build < … or rpm-build >= …)) if rpm-build)`,
  which is a BuildRequires of a large fraction of the distro. A fourth
  matcher would have stopped at the fifth shape. The full solve now reads
  all 44.

  Two things still need a person. An `or` with no branch present is
  reported rather than chosen, because rpm picks a branch by policy and
  inventing a policy is how a solver quietly installs a different distro
  than the one anyone reviewed; `--override '(a or b)=a'` settles it. And
  `unless` is order-dependent by nature — "required unless B appears"
  cannot be answered while B might still appear — so it is settled only
  once nothing else can grow, which is a decision the code makes
  explicitly rather than a property of the graph.
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
- **Actions are coarse, and permanently so — rpm forbids the fix.** One
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
  — it is coarse actions or rpms that refuse to install.

  The usual framing of the cost is also wrong for this repo, and worth
  correcting: "a one-line change recompiles the package" describes editing
  upstream source, which a replay builder never does — the sources are
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

  SPEC.md §1 names the escape hatch — per-package **graduation**, rewriting
  one package as a native recipe when the control is worth the
  maintenance. It is not implemented: `package()` dispatches only to the
  fedora replay, and every other flavor fails with "frontend is not
  implemented yet".
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
- **~~Images are unlabeled~~ — SELinux is enforcing.**
  `setxattr("security.selinux")` returns `EPERM` inside a nested user
  namespace, which is where every stage here runs, so nothing in the build
  can label a file by asking the kernel. That much is unchanged. What
  changed is that it no longer has to ask: `mksquashfs` takes per-file
  xattrs in a pseudo-file (`path x name=value`) and writes them straight
  into the image's xattr table, and working out *what* each label should
  be is a pure policy lookup needing no privilege at all. The image ships
  its own `selinux-policy-targeted` and its own `matchpathcon`, so the
  contexts come from the distro being built rather than the build host —
  the same argument `initramfs_build.py` makes for using the image's own
  dracut. 22,912 paths resolve in about a second and dedup to 187 distinct
  contexts in the image.

  Measured on the built ISO, not assumed: `enforcing=1`, policy loaded in
  78 ms, PID 1 running as `system_u:system_r:init_t`, **zero AVC denials**,
  login prompt reached. `tools/squashfs_build.py` has the mechanism;
  `squashfs(selinux_relabel = True)` turns it on, and it is off by default
  because an image with no policy has no contexts to look up.

  Two paths out of 22,912 are skipped rather than labelled — systemd's
  `system-systemd\x2dcryptsetup.slice` and its veritysetup sibling. The
  pseudo-file grammar splits on spaces and gives the backslash meaning of
  its own, so emitting them would label some *other* path, and
  mislabelling is worse than leaving a unit file unlabelled.

  What follows is the reasoning as it stood before, kept because the
  constraint it describes is real and still shapes the design:

- **Why the kernel will not do it for you.**
  `setxattr("security.selinux")` returns `EPERM` inside a nested user
  namespace, which is where every stage here runs — even under
  `unshare -Ur`, on a file where setting a `user.*` xattr succeeds. This is
  not a blanket rule about `security.*`, and the difference is worth
  knowing: `security.capability` *is* settable from a user namespace, via
  the kernel's v3 format that records a rootid, which is why the images
  here do carry working file capabilities on `arping`, `clockdiff`,
  `newuidmap` and `newgidmap`. The kernel extends that courtesy to
  capabilities and withholds it from labels. So `rpm-plugin-selinux` sets
  no context during the rootfs transaction, and there is nothing on disk
  for `mksquashfs` to copy — measured on a live rootfs, zero labels across
  the whole tree. That is why the labels have to be *written into the
  image* rather than set on files: the fix above is offline image editing,
  not privilege.

  buckos-build reaches the same conclusion from the other side, using
  `debugfs`'s `ea_set` to inject `security.ima` into ext4 from `.sig`
  sidecars, and records that it has "no unprivileged equivalent to
  `debugfs` for those filesystems" otherwise — its squashfs path falls
  back to `evmctl ima_setxattr`, which goes through the kernel and so
  silently no-ops unprivileged. `mksquashfs -pf` looks like the missing
  equivalent for that case too.
- **No scriptlets in a buildroot, and `--justdb` is why.** Trees are
  unpacked with `rpm2archive | tar` — GNU tar's
  `--delay-directory-restore`, not `cpio`, because rpm payloads ship
  read-only directories with files beneath them and cpio applies a
  directory's mode as soon as it creates it. The database is then written
  by `rpm --justdb --install`, which updates the database and declines to
  run install scriptlets for files it is not installing. So no `%pre` or
  `%post` executes, and `--noscripts` is passed to say so rather than to
  cause it.

  Dropping `--noscripts` was tried and reverted, and the reason is worth
  recording because the mistake was in the measurement rather than the
  idea. `/usr/sbin -> bin` and the systemd sysusers entries in
  `/etc/passwd` were both observed after the change and attributed to it —
  without ever building the same tree *with* `--noscripts` to compare.
  The control says they are there either way: they come from package
  payloads, not scriptlets.

  What settles it is `golang-bin`, whose `%post` runs
  `update-alternatives --install /usr/bin/go …`. Run by hand inside the
  finished buildroot it exits 0 and `go version` works. Run as part of the
  `--justdb` transaction it has no effect: `/etc/alternatives` stays
  empty, and `/usr/bin/go` — which ships as a symlink into it — dangles.

  That is a real limit, not a detail. **A package whose build needs a tool
  registered through `alternatives` cannot be built in this buildroot.**
  `libcap` is the case: it autodetects Go with `go version`, gets nothing,
  silently omits its `captree` program, and then fails in `%files` on a
  file nothing said it was skipping. Every step is a warning or a success
  until the last one. Fixing it needs a real (non-`--justdb`) transaction,
  which is exactly what the ownership constraint above rules out — so it
  is a genuine gap rather than an oversight.

  Triggers are off (`--notriggers`) as well, and would be moot regardless.

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

- Reimplementing RPM or DPKG semantics.
- Translating spec files into native Buck rules.
- Reproducing Fedora's Koji artifacts bit for bit.
