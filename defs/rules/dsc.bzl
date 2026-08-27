"""Debian-family source package replay rules."""

load(
    "//defs:buildroot_helpers.bzl",
    "BUILDROOT_ATTRS",
    "buildroot_cache_upload",
    "buildroot_env",
    "buildroot_local_only",
    "buildroot_sysroot_args",
    "dep_installroot_args",
)
load("//defs:providers.bzl", "DebArtifactInfo", "PackageInfo", "SourcePackageInfo")


def _dsc_unpack_impl(ctx: AnalysisContext) -> list[Provider]:
    out = ctx.actions.declare_output("source", dir = True)
    cmd = cmd_args(ctx.attrs._unpack[RunInfo])
    cmd.add("--dsc", ctx.attrs.dsc)
    for source in ctx.attrs.source_files:
        cmd.add("--file", source)
    cmd.add("--out", out.as_output())

    ctx.actions.run(
        cmd,
        category = "dsc_unpack",
        identifier = ctx.attrs.name,
        # re-contract: buildroot-independent -- unpacking a SHA-256-pinned
        # Debian source set does not consume the selected buildroot.
        allow_cache_upload = True,
    )
    return [
        DefaultInfo(default_output = out),
        SourcePackageInfo(
            topdir = out,
            recipe = "debian/rules",
            name = ctx.attrs.package_name,
            version = ctx.attrs.version,
            release = ctx.attrs.release,
            flavor = ctx.attrs.flavor,
        ),
    ]


dsc_unpack = rule(
    impl = _dsc_unpack_impl,
    attrs = {
        "dsc": attrs.source(),
        "flavor": attrs.string(default = "debian"),
        "package_name": attrs.string(),
        "release": attrs.string(default = ""),
        "source_files": attrs.list(attrs.source()),
        "version": attrs.string(default = ""),
        "_unpack": attrs.default_only(attrs.exec_dep(default = "//tools:dsc_unpack")),
    },
)


def _deb_build_impl(ctx: AnalysisContext) -> list[Provider]:
    debs = ctx.actions.declare_output("debs", dir = True)
    installroot = ctx.actions.declare_output("installroot", dir = True)
    manifest = ctx.actions.declare_output("manifest.json")
    source = ctx.attrs.source[SourcePackageInfo]

    cmd = cmd_args(ctx.attrs._replay[RunInfo])
    cmd.add(cmd_args("--source", source.topdir, hidden = source.topdir))
    cmd.add("--out-debs", debs.as_output())
    cmd.add("--out-installroot", installroot.as_output())
    cmd.add("--out-manifest", manifest.as_output())
    cmd.add(buildroot_sysroot_args(ctx))
    cmd.add(buildroot_env(ctx))
    cmd.add(dep_installroot_args(ctx.attrs.build_deps))
    if ctx.attrs.dpkg_buildpackage:
        cmd.add("--dpkg-buildpackage", ctx.attrs.dpkg_buildpackage)
    for profile in ctx.attrs.build_profiles:
        cmd.add("--build-profile", profile)
    if ctx.attrs.nocheck:
        cmd.add("--nocheck")
    cmd.add("--source-date-epoch", ctx.attrs.source_date_epoch)

    ctx.actions.run(
        cmd,
        category = "deb_build",
        identifier = ctx.attrs.name,
        allow_cache_upload = buildroot_cache_upload(ctx),
        local_only = buildroot_local_only(ctx),
    )

    return [
        DefaultInfo(
            default_output = installroot,
            sub_targets = {
                "debs": [DefaultInfo(default_output = debs)],
                "installroot": [DefaultInfo(default_output = installroot)],
                "manifest": [DefaultInfo(default_output = manifest)],
            },
        ),
        DebArtifactInfo(
            debs = [debs],
            dsc = ctx.attrs.dsc,
            installroot = installroot,
        ),
        PackageInfo(
            name = source.name,
            version = source.version,
            release = source.release,
            flavor = source.flavor,
            prefix = installroot,
            libraries = ctx.attrs.libraries,
            cflags = [],
            ldflags = [],
            artifacts = [debs],
            requires = ctx.attrs.requires,
            license = ctx.attrs.license,
            src_uri = ctx.attrs.src_uri,
            src_sha256 = ctx.attrs.src_sha256,
            homepage = ctx.attrs.homepage,
            supplier = ctx.attrs.supplier,
            description = ctx.attrs.description,
            cpe = ctx.attrs.cpe,
        ),
    ]


deb_build = rule(
    impl = _deb_build_impl,
    attrs = {
        "build_deps": attrs.list(attrs.dep(providers = [PackageInfo]), default = []),
        "build_profiles": attrs.list(attrs.string(), default = []),
        "cpe": attrs.option(attrs.string(), default = None),
        "description": attrs.string(default = ""),
        "dpkg_buildpackage": attrs.option(attrs.string(), default = None),
        "dsc": attrs.source(),
        "homepage": attrs.option(attrs.string(), default = None),
        "libraries": attrs.list(attrs.string(), default = []),
        "license": attrs.string(default = ""),
        "nocheck": attrs.bool(default = True),
        "requires": attrs.list(attrs.string(), default = []),
        "source": attrs.dep(providers = [SourcePackageInfo]),
        "source_date_epoch": attrs.string(default = "1700000000"),
        "src_sha256": attrs.string(default = ""),
        "src_uri": attrs.string(default = ""),
        "supplier": attrs.string(default = "Organization: Debian"),
        "_replay": attrs.default_only(attrs.exec_dep(default = "//tools:dpkgbuild_replay")),
    } | BUILDROOT_ATTRS,
)


def _deb_subpackage_impl(ctx: AnalysisContext) -> list[Provider]:
    out = ctx.actions.declare_output("installroot", dir = True)
    debs = ctx.attrs.source[DebArtifactInfo].debs[0]
    cmd = cmd_args(ctx.attrs._extract[RunInfo])
    cmd.add(cmd_args("--deb-dir", debs, hidden = debs))
    cmd.add("--select", ctx.attrs.package_name)
    cmd.add("--out", out.as_output())

    ctx.actions.run(
        cmd,
        category = "deb_subpackage",
        identifier = ctx.attrs.name,
        # re-contract: buildroot-independent -- this only projects one
        # binary package from an already-built, content-addressed artifact.
        allow_cache_upload = True,
    )

    parent = ctx.attrs.source[PackageInfo]
    return [
        DefaultInfo(default_output = out),
        PackageInfo(
            name = ctx.attrs.package_name,
            version = parent.version,
            release = parent.release,
            flavor = parent.flavor,
            prefix = out,
            libraries = ctx.attrs.libraries,
            cflags = [],
            ldflags = [],
            artifacts = None,
            requires = ctx.attrs.requires,
            license = parent.license,
            src_uri = parent.src_uri,
            src_sha256 = parent.src_sha256,
            homepage = parent.homepage,
            supplier = parent.supplier,
            description = ctx.attrs.description or parent.description,
            cpe = parent.cpe,
        ),
    ]


deb_subpackage = rule(
    impl = _deb_subpackage_impl,
    attrs = {
        "description": attrs.string(default = ""),
        "libraries": attrs.list(attrs.string(), default = []),
        "package_name": attrs.string(),
        "requires": attrs.list(attrs.string(), default = []),
        "source": attrs.dep(providers = [DebArtifactInfo, PackageInfo]),
        "_extract": attrs.default_only(attrs.exec_dep(default = "//tools:deb_extract")),
    },
)
