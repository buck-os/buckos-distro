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
| fedora | `.src.rpm` | `rpmbuild -bb` | **working end to end** on a fixture; solves against real F43 and F44 repodata |
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

## Layout

```
defs/
  providers.bzl          PackageInfo, BuildrootInfo, FlavorInfo — the abstraction
  flavor.bzl             package(), the one macro every recipe calls
  releases.bzl           release as a config-driven axis, not a mode
  buildroot_helpers.bzl  the remote-execution contract
  exec.bzl               execution platform registration
  rules/
    srpm.bzl             srpm_unpack, srpm_build, rpm_subpackage, prebuilt_rpm
    buildroot.bzl        host_buildroot, seeded_buildroot
flavors/<name>/          per-flavor buildroots, seed sets, lockfiles, recipes
tools/                   the drivers, and the solver
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
  buildroot = host           # or binary-seed
```

Only non-EOL releases work: once a release goes EOL Fedora moves it to
`archives.fedoraproject.org` and every repodata path 404s, so `releases` is
a list that needs revisiting once a year rather than a permanent setting.

`buildroot = host` uses the host's rpm installation. It is the development
escape hatch: not hermetic, local-only, no cache upload.
`buildroot = binary-seed` unpacks pinned rpms into a self-contained tree
that becomes `/` inside the isolation namespace, and is the production path.
`tools/_isolation.py` uses bubblewrap where it is installed and an
unprivileged user namespace via util-linux `unshare` otherwise; the two are
equivalent in hermeticity, so neither is a prerequisite.

## Limitations

Stated plainly, because each one is load-bearing:

- **The buildroot has no rpmdb, so `rpmbuild` runs with `--nodeps`.**
  Payloads are unpacked with `rpm2archive | tar`, and rpm's
  `BuildRequires` check queries the database rather than the filesystem —
  without `--nodeps` every declared dependency fails with the tool sitting
  right there in the tree. Dropping the check does not drop the
  dependencies: buildroot membership is decided by `tools/solve.py`, pinned
  by sha256, and reviewed as a diff. It costs one thing worth naming — rpm
  no longer catches a buildroot the solver got wrong, so a missing
  `BuildRequires` surfaces as a compile error rather than a clear
  dependency error.
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
- **Dynamic `BuildRequires` are not handled.** Specs using
  `%generate_buildrequires` (most Rust, Go, and modern Python packages)
  declare dependencies that do not exist in static repodata. `solve.py`
  detects and flags them; resolving them needs an `rpmbuild -br` probe
  pass, which is not written. This is a real gap, not an edge case.
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
  and xattrs (file capabilities, SELinux labels) as metadata. Downstream
  image rules must unpack it inside their own namespace.
- **No scriptlets *in a buildroot*.** Trees are unpacked with `rpm2archive | tar` — GNU
  tar's `--delay-directory-restore`, not `cpio`, because rpm payloads ship
  read-only directories with files beneath them and cpio applies a
  directory's mode as soon as it creates it. Nothing runs `%post`, so a
  package whose install-time scriptlet matters cannot be satisfied this
  way. This is deliberate and does not extend to images: `rootfs` runs
  them, because that is where they matter.
- **`--isolation none` is best-effort.** Under host provenance the
  dependency sysroot is exported via `PATH`, `PKG_CONFIG_PATH`, and
  friends; a spec that hardcodes `/usr/lib64` still reads the host's copy.
  This is why a seeded buildroot is the production path.

## Non-goals

Reimplementing rpm or dpkg. Reproducing Koji or sbuild bit-for-bit.
Transpiling `.spec` files into Buck rules. See SPEC.md §7.
