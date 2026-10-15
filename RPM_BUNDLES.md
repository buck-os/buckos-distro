# Vendor SDK payloads without a container or chroot

`rpm_payload_bundle` extracts explicitly selected binary RPMs into a directory
and deterministic tar archive. It runs no package installation, scriptlets,
target binaries, container, or chroot. `rpm_sdk` additionally declares ordinary
Buck `prebuilt_cxx_library` targets for selected static archives and headers.

This supports a DOCA/DPDK/RDMA SDK imported from NVIDIA's repositories.
The resulting application binary and payload archive can be used by external
packaging tools to distribute standalone applications.

Extraction needs Python 3, `rpm`, `rpm2archive`, and GNU tar on the execution
worker. Linking needs the consumer's C++ toolchain and its static libraries.

## Select and pin RPMs

First list the packages actually present in the desired repository:

```sh
./buck2 run //tools:rpm_bundle_lock -- \
  --repository https://linux.mellanox.com/public/repo/doca/3.3.0-bf4-dev-drop6/oraclelinux9/arm64-dpu/ \
  --architecture aarch64 --list
```

Choose the development/static packages and dependencies needed by your code.
Run the same command with `--package NAME` once per package and
`--output vendor/doca/packages.bzl` instead of `--list`:

```sh
./buck2 run //tools:rpm_bundle_lock -- \
  --repository https://linux.mellanox.com/public/repo/doca/3.3.0-bf4-dev-drop6/oraclelinux9/arm64-dpu/ \
  --architecture aarch64 \
  --package NAME_FROM_LIST --package ANOTHER_NAME_FROM_LIST \
  --output vendor/doca/packages.bzl
```

The tool verifies primary metadata against the SHA-256 in `repomd.xml`, selects
the newest requested version for the CPU, and writes `PACKAGES` with exact
HTTPS URLs and SHA-256 pins, plus a `packages.lock.json.gz` review sidecar containing
versions and declared RPM requirements. Buck verifies every downloaded RPM.
Review and commit both files. This is explicit selection: it does not resolve
RPM runtime requirements or the static link closure. `--primary PATH` uses
previously obtained primary metadata without fetching it; its provenance must
be checked when obtaining that file.

New SDK lockfiles follow `[buckos.lockfiles] compression`, which defaults to
`gzip`. Existing locks retain their encoding. Readers accept plain `.lock.json`
and compressed `.lock.json.gz`; duplicate encodings for one SDK are rejected.
Use `//tools:lockfile_convert` to convert an existing SDK lockfile.

An opt-in import for this repository is provided under
[`sdks/doca-bf4-dev-drop6`](sdks/doca-bf4-dev-drop6/README.md). It has verified
pins and archive paths for 16 RPMs containing 84 static archives. This does
not establish a complete static link closure or BF4 application compatibility.
Repositories missing rdma-core need separately pinned, compatible packages.
A second repository can be pinned to another `.bzl` file and its `PACKAGES`
list combined explicitly.

## Build the payload and inspect its paths

Create `vendor/doca/BUCK`:

```python
load("//defs:rpm_sdk.bzl", "rpm_sdk")
load(":packages.bzl", "PACKAGES")

rpm_sdk(
    name = "doca",
    packages = PACKAGES,
    architecture = "aarch64",
    visibility = ["PUBLIC"],
)
```

```sh
./buck2 build '//vendor/doca:doca[manifest]' --show-output
./buck2 build '//vendor/doca:doca[tree]' --show-output
./buck2 build //vendor/doca:doca --show-output
```

The default output is `payload.tar`. The manifest contains package identities,
package digests, all extracted paths, symlink targets, file digests, and a list
of static archives. RPM architecture must match the bundle or be `noarch`.
Absolute payload symlinks become relative, conflicting files fail, and missing
explicit exports fail. Shared identical files and directories are accepted.
The archive normalizes ownership and timestamps. Extraction grants owner read
access, so this is a userspace development/package payload rather than an RPM
installation with original ownership and permission semantics.

## Expose static libraries to C++ consumers

Add `libraries` to the `rpm_sdk` call using paths from the manifest:

```python
    libraries = {
        "common": {
            "archive": "PATH_FROM_MANIFEST/libdoca_common.a",
            "headers": ["HEADER_DIRECTORY_FROM_MANIFEST"],
            "deps": [":doca-required-library"],
            "exported_linker_flags": ["-pthread"],
        },
        "required-library": {
            "archive": "PATH_FROM_MANIFEST/librequired.a",
            "headers": [],
        },
    },
```

This declares `:doca-common` and `:doca-required-library`. Static archives and
header directories are copied into independent declared outputs with symlinks
dereferenced, so downstream actions can materialize them on remote workers.
`preferred_linkage = "static"` selects the vendor archive. It does not make
the entire consumer executable static. The consumer needs a suitable target
toolchain and libc archives, and a full static link option such as `-static`.
Ordinary `deps`, `exported_linker_flags`, `exported_preprocessor_flags`, and
`link_whole` are supported per library. Thin archives are rejected because
they can reference object files outside the bundle.

Use vendor `.pc` files, including `Libs.private` and `Requires.private`, to
declare the full static closure. DPDK registration objects may need
`link_whole`. A static verbs application needs the provider registration/link
scheme supported by its specific rdma-core release. Querying a `.pc` file is
not an automatic Buck dependency import: its `/opt` paths and system `-l`
options need mapping to declared artifacts and the target toolchain.

## What remains specific to DOCA and the deployment

EL9 and a destination both reporting glibc 2.34 does not prove compatibility.
Check ELF machine, symbol versions, C++ ABI, transitive link dependencies and
the actual executable on the destination. A vendor glibc static archive is
not automatically usable with a musl toolchain.

Prebuilt NVIDIA daemons remain dynamically linked. Including them and `.so`
files in the tar does not make them runnable after relocation: verify their
ELF interpreter, shared dependency closure, plugin search paths, configuration
paths and data files. A per-program wrapper using an explicit loader/library
path or tested `$ORIGIN` runpaths may be necessary. Package scriptlets are not
executed here; setup they perform must be handled explicitly. The manifest
therefore reports runtime compatibility as unverified.

Kernel drivers, firmware, device permissions, hugepages and BF4 feature
support remain deployment prerequisites. Static linking packages userspace
libraries; it does not bundle a compatible kernel or prove hardware support.

## Local verification

```sh
./buck2 test //tools:rpm_bundle_test //tools:re_contract_test //tests:rpm-sdk-consumer
./buck2 build //tests:rpm-payload-bundle --show-output
```

The fixture consumer links a real RPM-provided archive into an executable with
`-static` and runs it directly. This verifies the SDK mechanism on x86_64,
without claiming DOCA or AArch64 compatibility. The fixture source is
`tests/fixtures/sdk.spec`. Its RPM was built with `_buildhost` set to
`buckos-distro-fixture`, a fixed `SOURCE_DATE_EPOCH`, and debuginfo disabled.
