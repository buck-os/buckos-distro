"""RPM source-package replay rules.

The pipeline, per SPEC.md section 4:

    srpm_unpack          .src.rpm            -> rpmbuild topdir (SOURCES/, SPECS/)
    srpm_buildrequires   topdir + buildroot  -> the spec's real dependency set
    srpm_build           topdir + buildroot  -> binary rpms + installroot
    rpm_subpackage       one rpm out of an srpm_build -> its own installroot
    prebuilt_rpm         a fetched binary rpm -> installroot (the seed)

srpm_build is the one expensive action.  rpm_subpackage is a cheap
projection off it, so N subpackages of one source package share a single
compile -- Buck memoizes it (SPEC.md section 3a).

Remote execution
----------------
Every action follows buckos-build's convention:

    allow_cache_upload = buildroot_cache_upload(ctx)
    local_only         = buildroot_local_only(ctx)

so a non-hermetic (host-provenance) buildroot pins the action to the local
machine and keeps its output out of the shared cache.  Tree artifacts are
always passed whole with hidden inputs, never as bare projections.
"""

load(
    "//defs:buildroot_helpers.bzl",
    "BUILDROOT_ATTRS",
    "buildroot_args",
    "buildroot_cache_upload",
    "buildroot_env",
    "buildroot_info",
    "buildroot_local_only",
    "dep_installroot_args",
)
load("//defs:providers.bzl", "PackageInfo", "RpmArtifactInfo")

# ── Unpack ───────────────────────────────────────────────────────────

def _srpm_unpack_impl(ctx: AnalysisContext) -> list[Provider]:
    out = ctx.actions.declare_output("topdir", dir = True)

    cmd = cmd_args(ctx.attrs._unpack[RunInfo])
    cmd.add("--srpm", ctx.attrs.srpm)
    cmd.add("--out", out.as_output())
    if ctx.attrs.expect_spec:
        cmd.add("--expect-spec", ctx.attrs.expect_spec)

    # re-contract: buildroot-independent -- rpm2cpio on a pinned artifact
    # is deterministic and reads no host state, so this is cacheable and
    # RE-safe regardless of which buildroot the flavor selects.
    ctx.actions.run(
        cmd,
        category = "srpm_unpack",
        identifier = ctx.attrs.name,
        allow_cache_upload = True,
    )
    return [DefaultInfo(default_output = out)]

srpm_unpack = rule(
    impl = _srpm_unpack_impl,
    attrs = {
        "expect_spec": attrs.option(attrs.string(), default = None),
        "srpm": attrs.source(),
        "_unpack": attrs.default_only(
            attrs.exec_dep(default = "//tools:srpm_unpack"),
        ),
    },
)

# ── Dependency probe ─────────────────────────────────────────────────

def _srpm_buildrequires_impl(ctx: AnalysisContext) -> list[Provider]:
    """Ask a spec what it actually needs, by running its generator.

    Specs packaged with rust-packaging, go-rpm-macros or
    pyproject-rpm-macros compute BuildRequires from a lockfile in a
    %generate_buildrequires shell block, so repodata does not know them
    and neither does anything that only parses the spec.  `rpmbuild -br`
    runs the block and reports the result (SPEC.md section 3a).

    This target is not in the build graph.  It cannot be: Buck resolves
    dependencies during analysis, and an edge discovered by running an
    action exists only after analysis is over -- there is no way to add
    it, and a rule that tried would be a rule that cannot run remotely.
    So the probe is a lock-time tool.  `relock --probe` builds these,
    reads the JSON, and writes the answer into the lockfile, where the
    generator turns it into ordinary declared build_deps like any other.

    The buildroot matters to the answer: the generator is a shell script
    and needs its own interpreter present -- cargo, or the python
    pyproject bootstrap -- which is why this takes the same buildroot and
    static build_deps the real build would get.
    """
    out = ctx.actions.declare_output("buildrequires.json")
    topdir = ctx.attrs.topdir[DefaultInfo].default_outputs[0]

    cmd = cmd_args(ctx.attrs._replay[RunInfo])
    cmd.add("--stage", "br")
    cmd.add(cmd_args("--topdir", topdir, hidden = topdir))
    cmd.add("--out-buildrequires", out.as_output())
    cmd.add("--package-name", ctx.attrs.package_name)

    cmd.add(buildroot_args(ctx))
    cmd.add(buildroot_env(ctx))
    cmd.add(dep_installroot_args(ctx.attrs.build_deps))

    if ctx.attrs.fedora_release:
        cmd.add("--fedora-release", ctx.attrs.fedora_release)
    for expr in ctx.attrs.defines:
        cmd.add("--define", expr)
    for name in ctx.attrs.with_bconds:
        cmd.add("--with", name)
    for name in ctx.attrs.without_bconds:
        cmd.add("--without", name)
    cmd.add("--source-date-epoch", ctx.attrs.source_date_epoch)

    # re-contract: buildroot-gated -- %generate_buildrequires is arbitrary
    # shell from the spec, so under host provenance it reads the host's
    # /usr and its answer is about this machine rather than about the
    # flavor.  Same gate as srpm_build, for the same reason.
    ctx.actions.run(
        cmd,
        category = "srpm_buildrequires",
        identifier = ctx.attrs.name,
        allow_cache_upload = buildroot_cache_upload(ctx),
        local_only = buildroot_local_only(ctx),
    )
    return [DefaultInfo(default_output = out)]

srpm_buildrequires = rule(
    impl = _srpm_buildrequires_impl,
    attrs = {
        # The *static* BuildRequires, resolved as usual.  The generator
        # runs inside them, so a missing one is the difference between
        # learning the dynamic set and learning that cargo is not
        # installed.
        "build_deps": attrs.list(attrs.dep(providers = [PackageInfo]), default = []),
        "defines": attrs.list(attrs.string(), default = []),
        "fedora_release": attrs.option(attrs.string(), default = None),
        "package_name": attrs.string(),
        "source_date_epoch": attrs.string(default = "1700000000"),
        "topdir": attrs.dep(),
        "with_bconds": attrs.list(attrs.string(), default = []),
        "without_bconds": attrs.list(attrs.string(), default = []),
        "_replay": attrs.default_only(
            attrs.exec_dep(default = "//tools:rpmbuild_replay"),
        ),
    } | BUILDROOT_ATTRS,
)

# ── Replay ───────────────────────────────────────────────────────────

def _srpm_build_impl(ctx: AnalysisContext) -> list[Provider]:
    rpms_dir = ctx.actions.declare_output("rpms", dir = True)
    installroot = ctx.actions.declare_output("installroot", dir = True)
    manifest = ctx.actions.declare_output("manifest.json")

    topdir = ctx.attrs.topdir[DefaultInfo].default_outputs[0]

    cmd = cmd_args(ctx.attrs._replay[RunInfo])
    # Pass the topdir whole; the replay copies it to a writable work area
    # because Buck artifacts are read-only and rpmbuild writes into BUILD/.
    cmd.add(cmd_args("--topdir", topdir, hidden = topdir))
    # No --work: the driver derives its own scratch area, keyed on the
    # output path below so it is stable across reruns -- it becomes
    # %_topdir, and a %_topdir that moves moves every build-id with it.
    # A path chosen here would resolve against the action's cwd (the
    # project root), so it would write into the source tree.
    cmd.add("--out-rpms", rpms_dir.as_output())
    cmd.add("--out-installroot", installroot.as_output())
    cmd.add("--out-manifest", manifest.as_output())

    cmd.add(buildroot_args(ctx))
    cmd.add(buildroot_env(ctx))

    # BuildRequires: each dep is one binary package's installroot.
    cmd.add(dep_installroot_args(ctx.attrs.build_deps))

    if ctx.attrs.rpmbuild:
        cmd.add("--rpmbuild", ctx.attrs.rpmbuild)
    if ctx.attrs.fedora_release:
        cmd.add("--fedora-release", ctx.attrs.fedora_release)
    for expr in ctx.attrs.defines:
        cmd.add("--define", expr)
    # USE flags map onto rpm's own bcond mechanism (SPEC.md section 5).
    for name in ctx.attrs.with_bconds:
        cmd.add("--with", name)
    for name in ctx.attrs.without_bconds:
        cmd.add("--without", name)
    if ctx.attrs.nocheck:
        cmd.add("--nocheck")
    cmd.add("--source-date-epoch", ctx.attrs.source_date_epoch)

    ctx.actions.run(
        cmd,
        category = "srpm_build",
        identifier = ctx.attrs.name,
        allow_cache_upload = buildroot_cache_upload(ctx),
        local_only = buildroot_local_only(ctx),
    )

    info = buildroot_info(ctx)
    return [
        DefaultInfo(
            default_output = installroot,
            sub_targets = {
                "installroot": [DefaultInfo(default_output = installroot)],
                "manifest": [DefaultInfo(default_output = manifest)],
                "rpms": [DefaultInfo(default_output = rpms_dir)],
            },
        ),
        RpmArtifactInfo(
            rpms = [rpms_dir],
            srpm = None,
            installroot = installroot,
            nevra = "{}-{}-{}.{}".format(
                ctx.attrs.package_name,
                ctx.attrs.version,
                ctx.attrs.release,
                info.target_cpu,
            ),
        ),
        PackageInfo(
            name = ctx.attrs.package_name,
            version = ctx.attrs.version,
            release = ctx.attrs.release,
            flavor = ctx.attrs.flavor,
            prefix = installroot,
            libraries = ctx.attrs.libraries,
            cflags = [],
            ldflags = [],
            artifacts = [rpms_dir],
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

srpm_build = rule(
    impl = _srpm_build_impl,
    attrs = {
        # Resolved BuildRequires. Each must be an rpm_subpackage or a
        # prebuilt_rpm -- one binary package, never a whole srpm output.
        "build_deps": attrs.list(attrs.dep(providers = [PackageInfo]), default = []),
        "cpe": attrs.option(attrs.string(), default = None),
        "defines": attrs.list(attrs.string(), default = []),
        "description": attrs.string(default = ""),
        "fedora_release": attrs.option(attrs.string(), default = None),
        "flavor": attrs.string(default = "fedora"),
        "homepage": attrs.option(attrs.string(), default = None),
        "libraries": attrs.list(attrs.string(), default = []),
        "license": attrs.string(default = ""),
        "nocheck": attrs.bool(default = True),
        "package_name": attrs.string(),
        "release": attrs.string(default = ""),
        "requires": attrs.list(attrs.string(), default = []),
        "rpmbuild": attrs.option(attrs.string(), default = None),
        # Pinned so replayed builds are reproducible across machines;
        # without it rpm bakes wall-clock mtimes into every payload.
        "source_date_epoch": attrs.string(default = "1700000000"),
        "src_sha256": attrs.string(default = ""),
        "src_uri": attrs.string(default = ""),
        "supplier": attrs.string(default = "Organization: buckos-distro"),
        "topdir": attrs.dep(),
        "version": attrs.string(default = ""),
        "with_bconds": attrs.list(attrs.string(), default = []),
        "without_bconds": attrs.list(attrs.string(), default = []),
        "_replay": attrs.default_only(
            attrs.exec_dep(default = "//tools:rpmbuild_replay"),
        ),
    } | BUILDROOT_ATTRS,
)

# ── Subpackage projection ────────────────────────────────────────────

def _rpm_subpackage_impl(ctx: AnalysisContext) -> list[Provider]:
    """Unpack exactly one rpm from an srpm_build's output.

    Cheap: the expensive compile lives in the srpm_build dep, which Buck
    memoizes across every subpackage that references it.
    """
    out = ctx.actions.declare_output("installroot", dir = True)
    rpms_dir = ctx.attrs.srpm[RpmArtifactInfo].rpms[0]

    cmd = cmd_args(ctx.attrs._extract[RunInfo])
    # Whole directory as a hidden input so every rpm materializes on RE,
    # then select by name inside the action.
    cmd.add(cmd_args("--rpm-dir", rpms_dir, hidden = rpms_dir))
    cmd.add("--select", ctx.attrs.rpm)
    cmd.add("--out", out.as_output())

    ctx.actions.run(
        cmd,
        category = "rpm_subpackage",
        identifier = ctx.attrs.name,
        # re-contract: buildroot-independent -- unpacking an rpm is
        # deterministic and reads no host state, so it is cacheable even
        # when the build that produced the rpm was local-only.
        allow_cache_upload = True,
    )

    parent = ctx.attrs.srpm[PackageInfo]
    return [
        DefaultInfo(default_output = out),
        PackageInfo(
            name = ctx.attrs.rpm,
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

rpm_subpackage = rule(
    impl = _rpm_subpackage_impl,
    attrs = {
        "description": attrs.string(default = ""),
        "libraries": attrs.list(attrs.string(), default = []),
        "requires": attrs.list(attrs.string(), default = []),
        # Binary package name to select, e.g. "glibc-devel".
        "rpm": attrs.string(),
        "srpm": attrs.dep(providers = [PackageInfo, RpmArtifactInfo]),
        "_extract": attrs.default_only(
            attrs.exec_dep(default = "//tools:rpm_extract"),
        ),
    },
)

def _built_rpm_impl(ctx: AnalysisContext) -> list[Provider]:
    """The .rpm file for one binary package out of an srpm_build.

    rpm_subpackage's sibling, and the difference is what the consumer
    needs.  A *build* dependency wants the unpacked tree, to be overlaid
    into a buildroot; a *rootfs* wants the rpm file, because it runs a
    real rpm transaction and an unpacked tree cannot be installed, only
    copied over.

    This is what lets an image contain a package this repo compiled rather
    than one it downloaded.  Without it a source-built package could be
    depended on by another build and by nothing else -- which is why the
    replay pipeline and the image pipeline were, until this rule, two
    halves that never met.
    """
    out = ctx.actions.declare_output(ctx.attrs.rpm + ".rpm")
    rpms_dir = ctx.attrs.srpm[RpmArtifactInfo].rpms[0]

    cmd = cmd_args(ctx.attrs._extract[RunInfo])
    # Whole directory as a hidden input so every rpm materializes on RE,
    # then select by name inside the action.
    cmd.add(cmd_args("--rpm-dir", rpms_dir, hidden = rpms_dir))
    cmd.add("--select", ctx.attrs.rpm)
    cmd.add("--out-rpm", out.as_output())

    ctx.actions.run(
        cmd,
        category = "built_rpm",
        identifier = ctx.attrs.name,
        # re-contract: buildroot-independent -- picking one file out of a
        # directory by name reads no host state, so it is cacheable even
        # when the build that produced the directory was local-only.
        allow_cache_upload = True,
    )

    return [DefaultInfo(default_output = out)]

built_rpm = rule(
    impl = _built_rpm_impl,
    attrs = {
        # Binary package name to select, e.g. "zlib-ng-compat".
        "rpm": attrs.string(),
        "srpm": attrs.dep(providers = [PackageInfo, RpmArtifactInfo]),
        "_extract": attrs.default_only(
            attrs.exec_dep(default = "//tools:rpm_extract"),
        ),
    },
)

# ── Seed packages ────────────────────────────────────────────────────

def _prebuilt_rpm_impl(ctx: AnalysisContext) -> list[Provider]:
    """Unpack a fetched binary rpm. This is where the graph is cut."""
    out = ctx.actions.declare_output("installroot", dir = True)

    cmd = cmd_args(ctx.attrs._extract[RunInfo])
    cmd.add("--rpm", ctx.attrs.rpm)
    cmd.add("--out", out.as_output())

    ctx.actions.run(
        cmd,
        category = "prebuilt_rpm",
        identifier = ctx.attrs.name,
        # re-contract: buildroot-independent -- the seed rpm is pinned by
        # sha256 and unpacking it reads no host state, so this is cacheable
        # and RE-safe whatever the flavor's provenance is.
        allow_cache_upload = True,
    )

    return [
        DefaultInfo(default_output = out),
        PackageInfo(
            name = ctx.attrs.package_name,
            version = ctx.attrs.version,
            release = ctx.attrs.release,
            flavor = ctx.attrs.flavor,
            prefix = out,
            libraries = ctx.attrs.libraries,
            cflags = [],
            ldflags = [],
            artifacts = None,
            requires = ctx.attrs.requires,
            license = ctx.attrs.license,
            src_uri = ctx.attrs.src_uri,
            src_sha256 = ctx.attrs.src_sha256,
            homepage = None,
            supplier = ctx.attrs.supplier,
            description = ctx.attrs.description,
            cpe = None,
        ),
    ]

prebuilt_rpm = rule(
    impl = _prebuilt_rpm_impl,
    attrs = {
        "description": attrs.string(default = ""),
        "flavor": attrs.string(default = "fedora"),
        "libraries": attrs.list(attrs.string(), default = []),
        "license": attrs.string(default = ""),
        "package_name": attrs.string(),
        "release": attrs.string(default = ""),
        "requires": attrs.list(attrs.string(), default = []),
        "rpm": attrs.source(),
        "src_sha256": attrs.string(default = ""),
        "src_uri": attrs.string(default = ""),
        "supplier": attrs.string(default = "Organization: Fedora Project"),
        "version": attrs.string(default = ""),
        "_extract": attrs.default_only(
            attrs.exec_dep(default = "//tools:rpm_extract"),
        ),
    },
)
