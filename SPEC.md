# buckos-distro — a Buck2 distro builder

`buckos-distro` builds Linux distributions from their own upstream source
packages.  Fedora is built from Fedora SRPMs, Ubuntu from Debian source
packages, and BuckOS from its native from-source recipes — all through one
dependency graph, one cache, and one set of image-assembly rules.

BuckOS is not the host of this repo; it is one flavor among several.

## 1. The core idea: replay, don't transpile

An upstream source package already contains a complete, tested build recipe.
Fedora's `zlib-1.3.1-2.fc41.src.rpm` holds a `zlib.spec` that Fedora's own
build farm runs every day.  There are two ways to consume it.

**Transpile** — parse the `.spec` and emit native Buck rules
(`autotools_package(...)`).  Rejected.  It makes this repo the owner of rpm
macro semantics: `%configure`/`%cmake`/`%meson` expand through
`redhat-rpm-config`, `%if %{with foo}` gates whole subtrees, `%{?_isa}`
suffixes deps, and macros can call shell (`%(...)`) and Lua (`%{lua:...}`).
That is an interpreter, not a grammar, and it drifts every Fedora release.
Worse, it fails the way generated code always fails: the output is lossy, you
hand-fix it, and the next rebase cannot be regenerated without clobbering the
fixups.  You end up having forked several thousand specs.  It also generalizes
to nothing — Debian's recipe is `debian/rules`, an arbitrary Makefile driving
`dh` sequences, so a second distro means a second unrelated transpiler.

**Replay** — run the upstream build tool (`rpmbuild -bb`,
`dpkg-buildpackage -b`) inside a hermetic Buck action.  Adopted.  The spec
stays authoritative and is never rewritten; Buck supplies the buildroot from
dependency targets and captures the result.  A Fedora rebase is a version and
hash bump.  `rpmbuild` is the canonical macro interpreter, so we invoke it
instead of reimplementing it.

Replay also preserves everything Buck2 was actually adopted for: the
dependency graph, content-addressed inputs, parallelism, remote execution, and
the entire distro-agnostic layer *after* the build — the strip/stamp/IMA
transform chain and rootfs/ostree/ISO assembly.  None of that layer cares how
a package was compiled.

### What replay costs

Stated plainly, because these are real and permanent:

- **Coarse actions.** A replayed package is one action.  Buck cannot cache
  individual object files inside it, so a one-line change rebuilds the whole
  package.  Incrementality is per-package, not per-translation-unit.
- **Flag control is macro-level.** buckos-build injects hardening flags
  per-compile via `extra_cflags`.  Under replay you override
  `%__global_cflags` and friends, which the spec may still opt out of.
- **USE flags narrow.** See §5.

The pressure valve is per-package **graduation**: rewrite one package as a
native recipe when you need deep control.  Graduation is hand-authored, never
machine-generated — that is what keeps a transpiler from existing as a
maintained component.

## 2. The flavor abstraction

Every upstream distro fills the same four slots:

| flavor | source package | declared build deps | build driver | output |
|--------|----------------|---------------------|--------------|--------|
| fedora | `.src.rpm` | `BuildRequires` | `rpmbuild -bb` | `.rpm` |
| ubuntu | `.dsc` + `debian.tar.xz` | `Build-Depends` | `dpkg-buildpackage -b` | `.deb` |
| buckos | upstream tarball | Buck labels | `configure && make` | install prefix tree |

The third row matters: buckos-build's existing `package()` macro already fits
this shape unmodified.  The abstraction is validated by a flavor that predates
it rather than designed speculatively around one.

A flavor is a `FlavorInfo` provider binding five things:

```
FlavorInfo(
    name           = "fedora",
    source_rule    = srpm_source,       # fetch + unpack a source package
    build_rule     = srpm_build,        # the replay driver
    buildroot      = "//flavors/fedora:buildroot",
    dep_resolver   = fedora_dep_to_label,
    artifact_kind  = "rpm",
)
```

Nothing above the flavor layer knows which distro it is looking at.  Rules
consume `PackageInfo`, which every flavor's build rule returns.

## 3. Buildroot provenance is per-flavor

This is the single most load-bearing decision in the repo, and the thing that
will bite hardest if it is made global.

Fedora specs assume a Fedora-shaped buildroot: `redhat-rpm-config`,
`/usr/lib/rpm/redhat/macros`, a glibc laid out Fedora's way, `%{_libdir}` ==
`/usr/lib64`.  Pointing those specs at a toolchain bootstrapped from source
by buckos-build is where all the friction concentrates.  Trying to force one
global buildroot policy onto both flavors fails in both directions.

So each flavor declares its own, and both terminate — just differently:

- **fedora** seeds from real Fedora binaries at the bottom.  A small set of
  `@buildsys-build` RPMs (gcc, glibc-devel, make, coreutils, bash, rpm-build,
  redhat-rpm-config) is fetched by content hash and unpacked into a buildroot
  tree.  From there, Fedora builds Fedora with Fedora's own toolchain, which
  is the only configuration Fedora's specs are tested against.
- **buckos** bootstraps from its seed toolchain through the existing
  stage1/2/3 chain.  No binary seed beyond the seed compiler.
- **ubuntu** seeds from a `debootstrap`-style base, same principle as fedora.

`BuildrootInfo` is the contract:

```
BuildrootInfo(
    root         = artifact,   # populated tree: usr/, etc/, ...
    provenance   = str,        # "binary-seed" | "bootstrapped" | "host"
    target_cpu   = str,        # x86_64
    dist_tag     = str,        # .fc41
    macros       = artifact,   # extra rpm macros injected into the replay
    hermetic     = bool,       # False => action must run local_only
)
```

`provenance = "host"` is a deliberate bootstrap escape hatch mirroring
buckos-build's `allows_host_path`: it uses the host's installed rpm toolchain,
is non-hermetic, forces `local_only`, and exists so the machinery can be
developed and tested before a full binary seed is mirrored.  It is not a
production path and rules that consume it refuse remote execution.

## 3a. The dependency chain — the actual hard problem

Replaying one spec is easy.  Getting the graph right is the whole job.
Three properties of rpm dependencies drive the design.

**Dependencies are capabilities, not package names.**  A spec says
`BuildRequires: pkgconfig(libssl) >= 1.1.1`, or `/usr/bin/python3`, or
`perl(ExtUtils::MakeMaker)`, or `gcc-c++%{?_isa}`, or a rich boolean
`(pkgconfig(foo) or foo-devel)`.  Mapping a capability to the package that
provides it is a SAT solve over repository metadata — libsolv's job.
Starlark cannot do this at analysis time: it has no I/O, and the answer
depends on a repodata snapshot, not on anything in the tree.

**Source packages and binary packages are different graphs.**  One SRPM
produces many binary RPMs: `glibc.src.rpm` yields `glibc`, `glibc-devel`,
`glibc-common`, `glibc-langpack-*`.  `BuildRequires` are declared against
*binary* packages, but the thing you build is a *source* package.  The graph
is bipartite, which does not fit Buck's default one-target-one-output shape.

**The chain contains real cycles.**  `gcc` BuildRequires `glibc-devel`;
`glibc` BuildRequires `gcc`.  Fedora breaks this by having the previous
release's binaries already installed in the buildroot.  A Buck2 graph is a
DAG and cannot express the cycle at all.  So the chain *must* be cut, and
where it is cut is exactly the binary seed of section 3.  That makes the seed
structural, not a convenience.

### Three layers: solve, generate, build

The solve cannot live in `.bzl`, so it moves out of band — the same shape as
cargo/reindeer, `go.mod`, or `yarn.lock`.

| layer | runs | output |
|-------|------|--------|
| **solve** | offline, against pinned repodata | lockfile: capabilities resolved to exact NEVRAs |
| **generate** | offline, checked in | Buck targets derived from the lockfile |
| **build** | `buck2 build` | a DAG by construction |

The lockfile (`flavors/fedora/lock/f<release>.lock.json`) records, per source
package: the binary subpackages it produces, its `BuildRequires` resolved to
pinned NEVRAs, and whether each resolved dep is **built** from source in this
tree or taken from the **seed**.  Everything is pinned by sha256, so the
build is reproducible and offline-capable.

The payoff of making the cut explicit: the lockfile states exactly how much of
the distro is built from source versus seeded from binaries.  Moving a package
from `seed` to `built` deepens the bootstrap, and the ratio is a number you can
watch go up over time instead of a vague aspiration.  `bootstrap_depth` in the
lockfile summary reports it.

### Subpackages as projections

One expensive build, N cheap unpacks:

```
srpm_package(name = "glibc", ...)                   # one rpmbuild action
rpm_subpackage(name = "glibc-devel", srpm = ":glibc", rpm = "glibc-devel")
rpm_subpackage(name = "glibc-common", srpm = ":glibc", rpm = "glibc-common")
```

`rpm_subpackage` selects one `.rpm` from the srpm build's outputs and unpacks
it into its own installroot, returning `PackageInfo`.  Buck memoizes the
`srpm_package` action across every consumer, so the compile happens once
regardless of how many subpackages are depended on.

This is also why a dependency edge must point at a *subpackage*, never at a
source package: depending on `glibc` when you need `glibc-devel` would drag
the entire binary output of the SRPM into the buildroot.

### Buildroot composition

For a given package the buildroot is assembled as:

```
flavor seed base (@buildsys-build, from BuildrootInfo.root)
  + for each resolved BuildRequires:
      seed dep    -> prebuilt_rpm    (fetched binary, unpacked)
      built dep   -> rpm_subpackage  (built here, unpacked)
```

Composition is a copy into a writable work area, never a mutation of the
`BuildrootInfo.root` artifact — that artifact is a Buck input, shared across
every package, and writing into it would corrupt concurrent actions.

## 4. Rule pipeline

Per package, mirroring buckos-build's `package()` chain so intermediates stay
independently buildable:

```
:name-srpm       http_file        fetched .src.rpm (content-hashed)
:name-src        srpm_unpack      SOURCES/ + SPECS/ laid out as an rpm topdir
:name-buildroot  buildroot_merge  flavor buildroot + BuildRequires closure
:name-build      srpm_build       rpmbuild -bb  ->  RPMS/*.rpm + installroot
:name-stripped   strip_package    (distro-agnostic transforms)
:name-stamped    stamp_package
:name            alias
```

`srpm_build` returns `PackageInfo` with `prefix` pointing at the unpacked
installroot, so downstream rootfs/ISO rules are identical across flavors.  It
additionally returns `RpmArtifactInfo` carrying the binary `.rpm`s, for
flavors that want to publish a real repo.

## 5. USE flags under replay

Replay narrows USE flags, but less than it first appears, because rpm has
first-class conditional-build support.  A USE flag maps onto rpm's own
mechanism:

```
use_bcond = {"ssl": "openssl", "http2": "nghttp2"}
   ->   rpmbuild --with openssl --without nghttp2
```

This is declarative and edits nothing.  Packages whose specs declare
`%bcond_with` get USE flags for free; packages that do not get none.  For the
handful where that is unacceptable, graduate the package to a native recipe.

## 6. Layout

```
defs/providers.bzl     PackageInfo, BuildrootInfo, FlavorInfo, RpmArtifactInfo
defs/flavor.bzl        flavor() + the distro-neutral package() front door
defs/rules/srpm.bzl    Fedora replay (unpack, buildroot merge, rpmbuild)
defs/rules/dsc.bzl     Debian replay
defs/rules/buildroot.bzl  buildroot assembly from binary RPMs
defs/rules/transforms.bzl strip/stamp — flavor-independent
flavors/fedora/        fedora toolchain, buildroot seed set, packages
flavors/ubuntu/        ubuntu equivalent
flavors/buckos/        bridge to buckos-build's native recipes
tools/*.py             action helpers (no logic in .bzl beyond graph wiring)
platforms/             target platforms + the distro_compat constraint
```

`platforms/BUCK` carries a `distro_compat` constraint (`fedora`, `ubuntu`,
`buckos-native`) so a target can declare which flavors it is valid under —
buckos-build already had this constraint stubbed out, unused.

## 7. Non-goals

- Reimplementing rpm or dpkg.  We shell out to them.
- Bit-for-bit reproducing Fedora's official builds.  Same specs and same
  buildroot make that plausible, but Koji-specific metadata (build IDs,
  timestamps) is not chased.
- A spec-to-Buck transpiler.  Explicitly rejected in §1.
