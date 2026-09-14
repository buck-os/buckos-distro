# Root filesystem interchange contract

`buckos-distro` publishes complete Linux root filesystems independently of any
container runtime or downstream image builder. The contract has two parts:

- `RootfsInfo`, for Buck2 rules that depend on a rootfs target;
- a `buckos.rootfs.v1` JSON manifest, for consumers that should not import this
  repository's Starlark provider.

The rootfs archive remains `DefaultInfo`'s default output. Every typed rootfs
target also exposes `[archive]` and `[manifest]` subtargets, so existing labels
keep working while new integrations can name the contract explicitly:

```sh
buck2 build //flavors/fedora:rootfs-base-44-x86_64[archive]
buck2 build //flavors/fedora:rootfs-base-44-x86_64[manifest]
```

## Archive contract

The archive is an uncompressed POSIX tar rooted at `./`. It represents a
complete root filesystem rather than an incremental container layer. Producers
record numeric ownership and preserve ACL and xattr metadata in PAX headers.
Consumers must not flatten ownership, ACLs, file capabilities, or other xattrs
while extracting or converting it.

The archive deliberately has no container-runtime-, VM-, or live-media-specific
metadata. A converter may use it as a single complete container layer, import
it into an image-layer implementation, populate a disk filesystem, or pass it
to this repository's SquashFS pipeline.

## Buck2 provider

Load `RootfsInfo` from `//defs:providers.bzl`. Its fields are:

| Field | Meaning |
| --- | --- |
| `archive` | Complete rootfs tar artifact |
| `manifest` | `buckos.rootfs.v1` JSON artifact |
| `format` / `compression` / `layout` | Currently `tar` / `none` / `complete-rootfs` |
| `flavor` / `release` / `architecture` | Target distribution identity |
| `package_manager` | `rpm`, `dpkg`, or `unknown` |
| `package_provenance` | `source-preferred`, `upstream-binary`, or `unknown` |
| `role` | `base`, `live`, `buildroot-seed`, or `custom` |
| `buildroot_provenance` | Build environment provenance |
| `transforms` | Ordered transformations applied after package installation |

`source-preferred` is intentionally not called `source-only`: source-policy
exceptions may retain pinned upstream binaries. A `prebuilt` target reports
`upstream-binary` because its entire package set comes from pinned distro
artifacts.

Rules transforming a rootfs should preserve its identity fields, append a
stable name to `transforms`, and emit a new manifest for the resulting archive.
The custom-kernel and boot-verification overlay rules implement this behavior.

Consumers may temporarily accept `DefaultInfo` for older or hand-written
rootfs producers. Transformations preserve that legacy shape rather than
inventing metadata they cannot verify. Such inputs should not be published as
a named distribution base without an explicit adapter.

## JSON manifest

The sidecar is deterministic JSON with this shape:

```json
{
  "archive": {
    "compression": "none",
    "format": "tar",
    "layout": "complete-rootfs",
    "media_type": "application/x-tar",
    "root": "./"
  },
  "build": {
    "buildroot_provenance": "binary-seed",
    "package_provenance": "source-preferred",
    "transforms": []
  },
  "filesystem": {
    "acls": true,
    "numeric_ownership": true,
    "path_semantics": "posix",
    "xattrs": true
  },
  "role": "base",
  "schema": "buckos.rootfs.v1",
  "target": {
    "architecture": "x86_64",
    "os": {
      "id": "fedora",
      "version_id": "44"
    },
    "package_manager": "rpm"
  }
}
```

The manifest describes interpretation and provenance. Content identity remains
the digest of the archive artifact itself, as computed by Buck's CAS or by the
system transporting it. Keeping transport identity separate also lets the same
rootfs contract move through a CAS, an OCI converter, or another publisher.

## Downstream adapter boundary

A downstream integration should consume `RootfsInfo.archive` as a complete
root layer. It may use a generic tarball-import feature or a small rule that
converts the archive into the image builder's native layer provider. The
adapter should validate `format == "tar"`, `compression == "none"`, `layout ==
"complete-rootfs"`, and the requested OS and architecture before importing it.

Keeping that adapter out of `buckos-distro` prevents this base-OS producer from
depending on downstream provider internals while leaving the archive directly
usable by container tooling, disk-image builders, and other consumers.
