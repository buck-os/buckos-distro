# buckos flavor

BuckOS is not the host of this repo — it is one flavor among several, on
exactly the same footing as fedora and ubuntu. This directory is the
bridge.

**Status: designed, not implemented.** `package(flavor = "buckos")` fails
with a pointer to this file.

## The four slots

| slot            | value                                              |
|-----------------|----------------------------------------------------|
| source package  | upstream release tarball, fetched by sha256        |
| declared deps   | Buck target labels — already a real graph          |
| build driver    | `configure && make`, cmake, or meson               |
| output          | an install prefix tree; no native package format   |

Note what is *missing* from that column: a source-package format and a
declared-dependency language. BuckOS recipes are already written in
Starlark against Buck labels, so the flavor skips the solve step
entirely. There is no repodata to parse, no capability to resolve, and no
lockfile to generate — the dependency graph is written by hand and is a
Buck graph by construction.

That makes buckos the *easiest* flavor to integrate and the one that
demonstrates the abstraction is real: if the flavor seam only fits
distros that ship `.src.rpm`-shaped things, it is not a seam.

## Why the bridge is thin

`PackageInfo` in `defs/providers.bzl` was given deliberately the same
field set as buckos-build's, plus four additive fields (`release`,
`flavor`, `artifacts`, `requires`). buckos-build's rules populate the
shared fields already; the bridge fills in the rest:

```
release   = ""            # BuckOS has no distro release field
flavor    = "buckos"
artifacts = None          # no .rpm / .deb produced
requires  = []            # deps are labels, not name strings
```

`artifacts = None` and `requires = []` are the honest values, not
placeholders: a from-source flavor has no native packages to publish and
resolves its runtime deps through the target graph rather than through
capability strings.

## Integration shape

buckos-build is consumed as a **cell**, not vendored:

```ini
# .buckconfig.local
[cells]
  buckos_build = ../buckos-build
```

Kept out of the checked-in `.buckconfig` on purpose: it is a path to
another working copy, so it is machine-local by nature, and a
checked-in cell pointing at a directory that may not exist would break
`buck2 targets //...` for everyone else.

The frontend therefore lives in this directory rather than in
`defs/flavor.bzl`, and recipes under `flavors/buckos/` load it directly:

```python
load("//flavors/buckos:defs.bzl", "buckos_package")
```

A `load()` is resolved when the enclosing file is parsed, so putting the
`buckos_build//` load inside `defs/flavor.bzl` would make *every* BUCK
file in the repo fail to parse on a machine without the cell configured.
Confining it here means only `flavors/buckos/...` fails, and only when
targeted.

## The one genuine mismatch

Buildroot provenance. The fedora and ubuntu flavors get a buildroot by
unpacking pinned binary packages (`binary-seed`); buckos gets one by
bootstrapping a cross-compiler from a seed toolchain
(`bootstrapped`). `BuildrootInfo` already models both — that is why
`provenance` is a three-valued string rather than a `hermetic` boolean —
but the two are populated by entirely different machinery.

The bridge should return buckos-build's seed toolchain wrapped as a
`BuildrootInfo` with `provenance = "bootstrapped"` and `hermetic` copied
from `BuildToolchainInfo.allows_host_path` (inverted). That keeps the
remote-execution contract in `defs/buildroot_helpers.bzl` correct without
it needing to know that a BuckOS buildroot is a toolchain rather than a
tree — which is the point of routing everything through
`toolchains//:buildroot`.
