# ubuntu flavor

**Status: designed, not implemented.** `package(flavor = "ubuntu")` fails
with a pointer to this file.

The four slots from [SPEC.md](../../SPEC.md) section 2, filled in for
Debian/Ubuntu:

| slot            | fedora            | ubuntu                          |
|-----------------|-------------------|---------------------------------|
| source package  | `.src.rpm`        | `.dsc` + `.orig.tar.*` + `.debian.tar.*` |
| declared deps   | `BuildRequires`   | `Build-Depends`, `Build-Depends-Indep` |
| build driver    | `rpmbuild -bb`    | `dpkg-buildpackage -b`          |
| output          | `.rpm`            | `.deb`                          |

## What ports unchanged

Everything structural. The replay decision, the three-layer
solve/generate/build split, cycle staging, buildroot provenance, and the
remote-execution contract in `defs/buildroot_helpers.bzl` are all
flavor-neutral — they are stated in terms of "source package", "declared
build deps", and "installroot", not in terms of rpm.

`PackageInfo` needs no new fields. `release` already holds
`1ubuntu3`-shaped values and `artifacts` already holds a list of native
binary packages.

## What has to be written

1. **`tools/dsc_unpack.py`** — the analogue of `srpm_unpack.py`. A `.dsc`
   is a manifest, not an archive: unpacking means fetching the files it
   lists, verifying their checksums, extracting the orig tarball, and
   applying `debian.tar.*` on top. `dpkg-source -x` does all of it, so
   this is thinner than the rpm side, not thicker.

2. **`tools/dpkg_replay.py`** — the analogue of `rpmbuild_replay.py`.
   Same shape: copy the source tree to a writable area, compose the
   buildroot, run the driver, collect the outputs, derive the installroot
   by unpacking the produced `.deb`s with `dpkg-deb -x`.

3. **`tools/solve.py` needs a Debian backend.** This is the only place
   with real new work, because the metadata model differs in ways that
   matter:

   - **Sources and Packages are separate indices.** Fedora's `primary.xml`
     carries both binary and source packages with a `rpm:sourcerpm` field
     linking them; Debian splits them into `Sources` and `Packages`, joined
     through the `Source:` field. The bipartite graph is the same shape,
     the join is just spelled differently.
   - **Virtual packages are `Provides:` without versions** (mostly), so
     capability resolution has less to go on than rpm's rich
     `pkgconfig(...)` / `libfoo.so.6()(64bit)` provides. Expect the
     `AmbiguousProvider` path in `depgraph.py` to fire more often and the
     override map to be correspondingly larger.
   - **`Build-Depends` has architecture and profile qualifiers**
     (`foo [!armhf] <!nocheck>`). These must be evaluated against the
     target, not passed through. `depgraph.is_rich_dep` covers rpm's
     boolean deps; Debian's qualifiers need their own parser.
   - **`Build-Depends-Indep` only applies when building `Architecture:
     all` packages**, so it is conditional on which binary packages are
     being produced.

4. **A seed set.** Debian's analogue of `@buildsys-build` is the
   `build-essential` closure plus `debhelper`. Same role: it is where the
   dependency graph is cut, and its size is the bootstrap debt.

## What does not port

`use_bcond`. Debian has no `%bcond`, so there is no equivalent of mapping
a USE flag onto a flag the packaging already understands. The options are
`DEB_BUILD_OPTIONS` (a fixed vocabulary — `nocheck`, `nodoc`, `noopt` —
not extensible per package), `DEB_BUILD_PROFILES` (closer, but only
package-declared profiles), or patching `debian/rules`.

Patching `debian/rules` is transpiling by another name and is a non-goal
(SPEC.md section 7). So the honest answer is that the ubuntu flavor
supports a **smaller** set of USE flags than the fedora flavor, limited to
what the packaging already exposes as a build profile. That asymmetry
should be visible in the recipes rather than papered over.
