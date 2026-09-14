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

The archive remains DefaultInfo's default output for compatibility with
existing image rules.  RootfsInfo and the `[archive]` / `[manifest]`
subtargets provide the explicit boundary for new consumers.  A consumer
that needs a directory must unpack the archive without discarding numeric
ownership, ACLs, or xattrs.

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
    "buildroot_info",
    "buildroot_local_only",
    "buildroot_sysroot_args",
)
load("//defs:providers.bzl", "PackageInfo", "RootfsInfo")

_ROOTFS_SCHEMA = "buckos.rootfs.v1"

def rootfs_artifact(dep):
    """Return a rootfs archive, preferring the typed contract when present.

    The DefaultInfo fallback keeps hand-written fixtures and older external
    producers source-compatible while they migrate to RootfsInfo.
    """
    if RootfsInfo in dep:
        return dep[RootfsInfo].archive
    outputs = dep[DefaultInfo].default_outputs
    if not outputs:
        fail("{} produces no rootfs archive".format(dep.label))
    return outputs[0]

def rootfs_metadata(dep):
    """Metadata inherited by a rule that transforms a typed rootfs."""
    if RootfsInfo in dep:
        info = dep[RootfsInfo]
        return struct(
            architecture = info.architecture,
            flavor = info.flavor,
            release = info.release,
            package_manager = info.package_manager,
            package_provenance = info.package_provenance,
            role = info.role,
            buildroot_provenance = info.buildroot_provenance,
            transforms = info.transforms,
        )
    return None

def rootfs_result(ctx, archive, metadata, added_transforms = []):
    """Publish the neutral rootfs contract without changing the default output."""
    transforms = metadata.transforms + added_transforms
    manifest = ctx.actions.write(
        ctx.attrs.name + ".rootfs.json",
        json.encode({
            "archive": {
                "compression": "none",
                "format": "tar",
                "layout": "complete-rootfs",
                "media_type": "application/x-tar",
                "root": "./",
            },
            "build": {
                "buildroot_provenance": metadata.buildroot_provenance,
                "package_provenance": metadata.package_provenance,
                "transforms": transforms,
            },
            "filesystem": {
                "acls": True,
                "numeric_ownership": True,
                "path_semantics": "posix",
                "xattrs": True,
            },
            "role": metadata.role,
            "schema": _ROOTFS_SCHEMA,
            "target": {
                "architecture": metadata.architecture,
                "os": {
                    "id": metadata.flavor,
                    "version_id": metadata.release,
                },
                "package_manager": metadata.package_manager,
            },
        }) + "\n",
    )
    return [
        DefaultInfo(
            default_output = archive,
            other_outputs = [manifest],
            sub_targets = {
                "archive": [DefaultInfo(default_output = archive)],
                "manifest": [DefaultInfo(default_output = manifest)],
            },
        ),
        RootfsInfo(
            archive = archive,
            manifest = manifest,
            format = "tar",
            compression = "none",
            layout = "complete-rootfs",
            architecture = metadata.architecture,
            flavor = metadata.flavor,
            release = metadata.release,
            package_manager = metadata.package_manager,
            package_provenance = metadata.package_provenance,
            role = metadata.role,
            buildroot_provenance = metadata.buildroot_provenance,
            transforms = transforms,
        ),
    ]

def transformed_rootfs_result(ctx, archive, source, added_transforms):
    """Preserve RootfsInfo, or only DefaultInfo for an untyped legacy input."""
    metadata = rootfs_metadata(source)
    if metadata == None:
        return [DefaultInfo(default_output = archive)]
    return rootfs_result(
        ctx,
        archive,
        metadata,
        added_transforms = added_transforms,
    )

def _declared_metadata(ctx, package_manager):
    info = buildroot_info(ctx)
    return struct(
        architecture = ctx.attrs.architecture,
        flavor = ctx.attrs.flavor,
        release = ctx.attrs.release,
        package_manager = package_manager,
        package_provenance = ctx.attrs.package_provenance,
        role = ctx.attrs.role,
        buildroot_provenance = info.provenance,
        transforms = [],
    )

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

    return rootfs_result(ctx, out, _declared_metadata(ctx, "rpm"))

rootfs = rule(
    impl = _rootfs_impl,
    attrs = {
        # Off by default, and that is the interesting choice.  The replay
        # must pass --nodeps because its buildroot has no database to check
        # against; here rpm builds the database as it goes, so its
        # dependency check is the one end-to-end verification that the set
        # tools/solve.py computed is actually closed and installable.
        "nodeps": attrs.bool(default = False),
        "architecture": attrs.string(default = "unknown"),
        "flavor": attrs.string(default = "unknown"),
        "package_provenance": attrs.enum(
            ["source-preferred", "upstream-binary", "unknown"],
            default = "unknown",
        ),
        "packages": attrs.list(attrs.dep(providers = [PackageInfo]), default = []),
        "release": attrs.string(default = "unknown"),
        "rpms": attrs.list(attrs.dep(), default = []),
        "role": attrs.enum(
            ["base", "live", "buildroot-seed", "custom"],
            default = "custom",
        ),
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
    return rootfs_result(ctx, out, _declared_metadata(ctx, "dpkg"))

deb_rootfs = rule(
    impl = _deb_rootfs_impl,
    attrs = {
        "architecture": attrs.string(default = "unknown"),
        "debs": attrs.list(attrs.dep(), default = []),
        "flavor": attrs.string(default = "unknown"),
        "package_provenance": attrs.enum(
            ["source-preferred", "upstream-binary", "unknown"],
            default = "unknown",
        ),
        "release": attrs.string(default = "unknown"),
        "role": attrs.enum(
            ["base", "live", "buildroot-seed", "custom"],
            default = "custom",
        ),
        "source_date_epoch": attrs.string(default = "1700000000"),
        "_deb_install": attrs.default_only(
            attrs.exec_dep(default = "//tools:deb_rootfs_install"),
        ),
    } | BUILDROOT_ATTRS,
)
