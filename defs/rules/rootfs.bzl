"""Root filesystem assembly.

Where buildroot.bzl produces a place to *run a compiler*, this produces a
place to *boot*.  The difference is not size, it is the two things a
buildroot deliberately does without and an image cannot: a real rpm
database, and scriptlets having run.  tools/rootfs_install.py's docstring
has the full argument; the short version is that an image you cannot
`dnf update` is a demo, and an image whose `%post` never ran has no
systemd presets, no ldconfig cache and no modules.dep.

So this rule does not unpack anything.  It hands a pinned set of rpms to
rpm itself, inside the buildroot chroot, as one transaction, and lets rpm
decide install order and run the scriptlets (SPEC.md section 1: we never
reimplement rpm).

The output is a tar archive, not a directory, and that is forced rather
than chosen.  Two independent reasons, either of which is sufficient.

Ownership: a directory artifact full of files owned by ids inside this
user's subordinate range is one Buck can hash and cannot delete, and
chowning them away is throwing out the thing that makes it a rootfs.
tools/rootfs_install.py's docstring has the long form.

Filenames: systemd-udev ships
`/usr/lib/systemd/system/system-systemd\\x2dcryptsetup.slice` -- systemd
escapes `-` as `\\x2d` in slice unit names, and that backslash is a real
byte in a real filename.  Buck2's path types cannot represent it; a tree
containing one fails with "Error relativizing ... is not relative to
project root", and the failure is sticky, because it happens while the
daemon is walking the directory rather than while the action runs.  No
permission trick helps with this one.  Inside a tar the name is data.

It carries no provider of its own, so buckos-build's image rules -- which
take `attrs.dep()` and read `DefaultInfo.default_outputs[0]` -- still
accept it directly.  They will need an unpack step, since those rules
expect a directory; that unpack has to happen inside a namespace for the
same reason the tar does.

Two sources of packages, because a distro image is always both:

  packages  targets built by this repo, contributing PackageInfo.artifacts
            -- whole RPMS/ directories, since one spec makes many
            subpackages.
  rpms      pinned upstream binaries, for everything not yet replayed.
            Honest bootstrap debt, exactly like the buildroot seed, and
            visible in the target's own attribute list rather than hidden
            inside a script.
"""

load(
    "//defs:buildroot_helpers.bzl",
    "BUILDROOT_ATTRS",
    "buildroot_cache_upload",
    "buildroot_local_only",
    "buildroot_sysroot_args",
)
load("//defs:providers.bzl", "PackageInfo")

def _rootfs_impl(ctx: AnalysisContext) -> list[Provider]:
    out = ctx.actions.declare_output(ctx.attrs.name + ".tar")

    cmd = cmd_args(ctx.attrs._install[RunInfo])
    cmd.add("--out", out.as_output())

    # Whole trees, never projections: on RE only declared inputs
    # materialize, and half an RPMS/ directory is a transaction rpm will
    # reject for missing dependencies -- or worse, one it accepts, leaving
    # an image quietly short a package.
    for dep in ctx.attrs.packages:
        for artifact in dep[PackageInfo].artifacts or []:
            cmd.add(cmd_args("--rpm", artifact, hidden = artifact))

    for dep in ctx.attrs.rpms:
        for artifact in dep[DefaultInfo].default_outputs:
            cmd.add(cmd_args("--rpm", artifact))

    for source in ctx.attrs.selinux_modules:
        cmd.add("--selinux-module", source)

    cmd.add(buildroot_sysroot_args(ctx))

    if ctx.attrs.nodeps:
        cmd.add("--nodeps")
    cmd.add("--source-date-epoch", ctx.attrs.source_date_epoch)

    ctx.actions.run(
        cmd,
        category = "rootfs_install",
        identifier = ctx.attrs.name,
        # The rpmdb records an install time per package, so this archive is
        # reproducible in content but not bit-for-bit.  That is a caching
        # nuisance, not a correctness problem: two runs produce equivalent
        # images, and the cache is keyed on inputs rather than output
        # hashes.  Uploading is still governed by provenance, because a
        # host-provenance rootfs was assembled by the host's rpm and is
        # genuinely machine-specific.
        allow_cache_upload = buildroot_cache_upload(ctx),
        local_only = buildroot_local_only(ctx),
    )

    return [DefaultInfo(default_output = out)]

rootfs = rule(
    impl = _rootfs_impl,
    attrs = {
        # Off by default, and that is the interesting choice.  The replay
        # must pass --nodeps because its buildroot has no database to check
        # against; here rpm builds the database as it goes, so its
        # dependency check is the one end-to-end verification that the set
        # tools/solve.py computed is actually closed and installable.
        "nodeps": attrs.bool(default = False),
        "packages": attrs.list(attrs.dep(providers = [PackageInfo]), default = []),
        "rpms": attrs.list(attrs.dep(), default = []),
        "selinux_modules": attrs.list(attrs.source(), default = []),
        "source_date_epoch": attrs.string(default = "1700000000"),
        "_install": attrs.default_only(
            attrs.exec_dep(default = "//tools:rootfs_install"),
        ),
    } | BUILDROOT_ATTRS,
)

def _deb_rootfs_impl(ctx: AnalysisContext) -> list[Provider]:
    out = ctx.actions.declare_output(ctx.attrs.name + ".tar")
    cmd = cmd_args(ctx.attrs._deb_install[RunInfo], "--out", out.as_output())
    for dep in ctx.attrs.debs:
        for artifact in dep[DefaultInfo].default_outputs:
            cmd.add("--deb", artifact)
    cmd.add(buildroot_sysroot_args(ctx))
    cmd.add("--source-date-epoch", ctx.attrs.source_date_epoch)
    ctx.actions.run(
        cmd,
        category = "deb_rootfs_install",
        identifier = ctx.attrs.name,
        allow_cache_upload = buildroot_cache_upload(ctx),
        local_only = buildroot_local_only(ctx),
    )
    return [DefaultInfo(default_output = out)]

deb_rootfs = rule(
    impl = _deb_rootfs_impl,
    attrs = {
        "debs": attrs.list(attrs.dep(), default = []),
        "source_date_epoch": attrs.string(default = "1700000000"),
        "_deb_install": attrs.default_only(
            attrs.exec_dep(default = "//tools:deb_rootfs_install"),
        ),
    } | BUILDROOT_ATTRS,
)
