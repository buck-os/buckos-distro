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
    out = ctx.actions.declare_output("source.tar")
    cmd = cmd_args(ctx.attrs._unpack[RunInfo])
    cmd.add("--dsc", ctx.attrs.dsc)
    for source in ctx.attrs.source_files:
        cmd.add("--file", source)
    cmd.add("--out", out.as_output())
    cmd.add("--source-name", ctx.attrs.package_name)
    cmd.add("--source-version", ctx.attrs.version_full)
    cmd.add("--source-date-epoch", ctx.attrs.source_date_epoch)

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
        "source_date_epoch": attrs.string(default = "1700000000"),
        "version": attrs.string(default = ""),
        "version_full": attrs.string(),
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
    for key, value in sorted(ctx.attrs.build_env.items()):
        cmd.add("--env", "{}={}".format(key, value))
    cmd.add(dep_installroot_args(ctx.attrs.build_deps))
    for dep in ctx.attrs.build_deps:
        for artifact in dep[PackageInfo].artifacts or []:
            cmd.add(cmd_args("--dep-deb", artifact, hidden = artifact))
    if ctx.attrs.dpkg_buildpackage:
        cmd.add("--dpkg-buildpackage", ctx.attrs.dpkg_buildpackage)
    cmd.add("--build-type", ctx.attrs.build_type)
    for option in ctx.attrs.build_options:
        cmd.add("--build-option", option)
    for profile in ctx.attrs.build_profiles:
        cmd.add("--build-profile", profile)
    for package_name in ctx.attrs.install_packages:
        cmd.add("--install-package", package_name)
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
        "build_env": attrs.dict(attrs.string(), attrs.string(), default = {}),
        "build_options": attrs.list(attrs.string(), default = []),
        "build_profiles": attrs.list(attrs.string(), default = []),
        "build_type": attrs.enum(["binary", "arch", "indep"], default = "binary"),
        "cpe": attrs.option(attrs.string(), default = None),
        "description": attrs.string(default = ""),
        "dpkg_buildpackage": attrs.option(attrs.string(), default = None),
        "dsc": attrs.source(),
        "homepage": attrs.option(attrs.string(), default = None),
        "install_packages": attrs.list(attrs.string()),
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
    out_deb = ctx.actions.declare_output(ctx.attrs.package_name + ".deb")
    debs = ctx.attrs.source[DebArtifactInfo].debs[0]
    cmd = cmd_args(ctx.attrs._extract[RunInfo])
    cmd.add(cmd_args("--deb-dir", debs, hidden = debs))
    cmd.add("--select", ctx.attrs.package_name)
    if ctx.attrs.architecture:
        cmd.add("--architecture", ctx.attrs.architecture)
    if ctx.attrs.source_name:
        cmd.add("--source-name", ctx.attrs.source_name)
    if ctx.attrs.source_version:
        cmd.add("--source-version", ctx.attrs.source_version)
    cmd.add("--out", out.as_output())
    cmd.add("--out-deb", out_deb.as_output())

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
        DefaultInfo(
            default_output = out,
            sub_targets = {
                "deb": [DefaultInfo(default_output = out_deb)],
            },
        ),
        PackageInfo(
            name = ctx.attrs.package_name,
            version = parent.version,
            release = parent.release,
            flavor = parent.flavor,
            prefix = out,
            libraries = ctx.attrs.libraries,
            cflags = [],
            ldflags = [],
            artifacts = [out_deb],
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
        "architecture": attrs.string(default = ""),
        "description": attrs.string(default = ""),
        "libraries": attrs.list(attrs.string(), default = []),
        "package_name": attrs.string(),
        "requires": attrs.list(attrs.string(), default = []),
        "source": attrs.dep(providers = [DebArtifactInfo, PackageInfo]),
        "source_name": attrs.string(default = ""),
        "source_version": attrs.string(default = ""),
        "_extract": attrs.default_only(attrs.exec_dep(default = "//tools:deb_extract")),
    },
)


def _built_deb_impl(ctx: AnalysisContext) -> list[Provider]:
    out = ctx.actions.declare_output(ctx.attrs.package_name + ".deb")
    debs = ctx.attrs.source[DebArtifactInfo].debs[0]
    cmd = cmd_args(ctx.attrs._extract[RunInfo])
    cmd.add(cmd_args("--deb-dir", debs, hidden = debs))
    cmd.add("--select", ctx.attrs.package_name)
    cmd.add("--architecture", ctx.attrs.architecture)
    cmd.add("--source-name", ctx.attrs.source_name)
    cmd.add("--source-version", ctx.attrs.source_version)
    cmd.add("--out-deb", out.as_output())

    ctx.actions.run(
        cmd,
        category = "built_deb",
        identifier = ctx.attrs.name,
        # re-contract: buildroot-independent -- selecting one verified DEB
        # from an already-built directory reads no host state.
        allow_cache_upload = True,
    )
    return [DefaultInfo(default_output = out)]


built_deb = rule(
    impl = _built_deb_impl,
    attrs = {
        "architecture": attrs.string(),
        "package_name": attrs.string(),
        "source": attrs.dep(providers = [DebArtifactInfo, PackageInfo]),
        "source_name": attrs.string(),
        "source_version": attrs.string(),
        "_extract": attrs.default_only(attrs.exec_dep(default = "//tools:deb_extract")),
    },
)


def _prebuilt_deb_impl(ctx: AnalysisContext) -> list[Provider]:
    out = ctx.actions.declare_output("installroot", dir = True)
    cmd = cmd_args(ctx.attrs._extract[RunInfo])
    cmd.add("--deb", ctx.attrs.deb)
    cmd.add("--select", ctx.attrs.package_name)
    cmd.add("--architecture", ctx.attrs.architecture)
    cmd.add("--source-name", ctx.attrs.source_name)
    cmd.add("--source-version", ctx.attrs.source_version)
    cmd.add("--out", out.as_output())

    ctx.actions.run(
        cmd,
        category = "prebuilt_deb",
        identifier = ctx.attrs.name,
        # re-contract: buildroot-independent -- the DEB is pinned by SHA-256
        # and unpacking it reads no host state.
        allow_cache_upload = True,
    )
    return [
        DefaultInfo(default_output = out),
        PackageInfo(
            name = ctx.attrs.package_name,
            version = ctx.attrs.version,
            release = "",
            flavor = ctx.attrs.flavor,
            prefix = out,
            libraries = [],
            cflags = [],
            ldflags = [],
            artifacts = [ctx.attrs.deb],
            requires = [],
            license = "",
            src_uri = "",
            src_sha256 = "",
            homepage = None,
            supplier = ctx.attrs.supplier,
            description = "",
            cpe = None,
        ),
    ]


prebuilt_deb = rule(
    impl = _prebuilt_deb_impl,
    attrs = {
        "architecture": attrs.string(),
        "deb": attrs.source(),
        "flavor": attrs.string(),
        "package_name": attrs.string(),
        "source_name": attrs.string(),
        "source_version": attrs.string(),
        "supplier": attrs.string(),
        "version": attrs.string(),
        "_extract": attrs.default_only(attrs.exec_dep(default = "//tools:deb_extract")),
    },
)
