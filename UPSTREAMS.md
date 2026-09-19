# Remote upstream overlays

Remote upstreams are optional, named binary-package overlays. They do not
change any BuckOS flavor or base image by default. Each overlay has its own
lockfile and records the complete package-manager transaction needed for a
derived rootfs.

The complete transaction matters: a PPA or RPM repository can replace a base
library as well as add a requested package. The overlay lock also records an
additions/replacements summary and the semantic SHA-256 of its base lock. Buck
refuses to evaluate the generated overlay after that base lock changes until
the overlay is resolved again.

Overlay repositories are used only while resolving. Builds download the exact
URLs in the generated data and verify every DEB or RPM by SHA-256. PPA and
generic APT repositories require an explicit `signed-by` keyring; `trusted=yes`
is rejected. The keyring's digest, not its local path, is recorded for review.

## Ubuntu PPA example

The package name below is only an example. It does not add Podman to the
checked-in Ubuntu image.

```sh
mkdir -p flavors/ubuntu/upstream-locks

buck2 run //tools:upstream_lock -- \
  --base-lock flavors/ubuntu/lock/ubuntu-26.04-x86_64.lock.json.gz \
  --name containers \
  --ppa OWNER/ARCHIVE=/usr/share/keyrings/containers-ppa.gpg \
  --package podman \
  --output flavors/ubuntu/upstream-locks/containers-26.04-x86_64.lock.json.gz

buck2 run //tools:upstream_generate -- \
  flavors/ubuntu/upstream-locks/containers-26.04-x86_64.lock.json.gz \
  --output flavors/ubuntu/generated/overlay-containers-26.04-x86_64.bzl
```

A non-Launchpad repository uses its complete APT source line:

```sh
--apt-repository \
  'deb [signed-by=/usr/share/keyrings/vendor.gpg] https://packages.vendor.example/ubuntu resolute main'
```

The key must already be obtained and verified out of band. The tool does not
download keys or trust a key identifier implicitly.

## EPEL or another RPM repository

Repository URLs point at the architecture-specific directory containing
`repodata/`. For example:

```sh
buck2 run //tools:upstream_lock -- \
  --base-lock flavors/centos/lock/centos-10-x86_64.lock.json.gz \
  --name tools \
  --rpm-repository epel=https://dl.fedoraproject.org/pub/epel/10/Everything/x86_64 \
  --package PACKAGE \
  --output flavors/centos/upstream-locks/tools-10-x86_64.lock.json.gz

buck2 run //tools:upstream_generate -- \
  flavors/centos/upstream-locks/tools-10-x86_64.lock.json.gz \
  --output flavors/centos/generated/overlay-tools-10-x86_64.bzl
```

Current CentOS locks already include EPEL. Repeating the same `epel` name and
canonical URL selects it without duplicating it. A different URL under an
existing name is rejected. Other public RPM repositories use another unique
name in the same `NAME=URL` form.

Use `--repo-cache` to choose where RPM metadata is stored. A later `--offline`
refresh uses the exact digest-named metadata recorded by the prior overlay
lock.

## Expose the derived rootfs

Generated files contain data only. Opt in by loading one from the flavor's
`BUCK` file and declaring the derived rootfs:

```python
load("//defs:architectures.bzl", "execution_compatible_with", "target_platform")
load("//defs:upstream_overlay.bzl", "upstream_overlay_rootfs")
load("//flavors/ubuntu/generated:overlay-containers-26.04-x86_64.bzl", "OVERLAY")

upstream_overlay_rootfs(
    name = "containers",
    flavor = "ubuntu",
    base_data = DATA_BY_RELEASE_ARCH["26.04"]["x86_64"],
    overlay = OVERLAY,
    suffix = "-26.04-x86_64",
    default_target_platform = target_platform("ubuntu", "26.04", "x86_64"),
    exec_compatible_with = execution_compatible_with("x86_64"),
)
```

This creates `//flavors/ubuntu:rootfs-containers-26.04-x86_64`. It is a
kernel-free reusable rootfs with `upstream-binary` provenance. The normal
`rootfs-base`, `rootfs-live`, and ISO targets are untouched.

Overlay resolution is currently binary-only. It preserves package-manager
dependency semantics and digest verification but does not replay PPA or EPEL
source packages.
