"""Buildroot helpers -- the remote-execution contract for replay actions.

Mirrors buckos-build's defs/toolchain_helpers.bzl (toolchain_local_only,
toolchain_path_args) because the RE hazards are identical and were learned
the hard way there.

Two rules govern every action in this repo:

  1. A non-hermetic buildroot must never run on RE and must never upload
     to the shared cache.  RE workers are sterile -- they have no rpm
     macros, no redhat-rpm-config, no host /usr -- so an action that
     reads the host filesystem either fails there or, worse, succeeds
     with different inputs and poisons the cache for everyone.

  2. When an action references a *subpath* of a tree artifact, the WHOLE
     tree must be carried as a hidden input.  On RE only an action's
     declared inputs are materialized, so projecting
     buildroot.project("usr/bin") materializes exactly that directory and
     leaves usr/lib64/libc.so.6 absent -- the tools then fail to load.
     buckos-build documents this same trap in three separate places.
"""

load("//defs:providers.bzl", "BuildrootInfo", "PackageInfo")

# Every rule that consumes a buildroot mixes these in.  Two slots:
#
#   buildroot   explicit override.  Two things need it.  Cycle staging:
#               gcc-stage1 builds against the seed while gcc-stage3 builds
#               against the stage2 result (SPEC.md section 3a).  And the
#               release axis: fedora's package macros pin each release's
#               targets to that release's buildroot, so gzip-43 cannot end
#               up compiled against Fedora 44 (defs/releases.bzl).
#   _buildroot  the flavor's default, selected in the root cell so it reads
#               the same configuration as the package graph.
#
# Both are toolchain_dep, not dep: host_buildroot and seeded_buildroot are
# toolchain rules, and buck2 rejects a plain dep on one.
BUILDROOT_ATTRS = {
    "buildroot": attrs.option(
        attrs.toolchain_dep(providers = [BuildrootInfo]),
        default = None,
    ),
    "_buildroot": attrs.toolchain_dep(
        default = "//:buildroot",
        providers = [BuildrootInfo],
    ),
}

def buildroot_info(ctx):
    """The BuildrootInfo this action builds against.

    An explicit `buildroot` wins over the flavor default so a staged
    bootstrap target can pin an earlier stage.
    """
    if ctx.attrs.buildroot != None:
        return ctx.attrs.buildroot[BuildrootInfo]
    return ctx.attrs._buildroot[BuildrootInfo]

def buildroot_local_only(ctx):
    """True when this action must run locally rather than on RE.

    A buildroot with hermetic = False reads the host's rpm installation
    (provenance "host"), which no RE worker has.  Directly analogous to
    buckos-build's toolchain_local_only().
    """
    return not buildroot_info(ctx).hermetic

def buildroot_cache_upload(ctx):
    """True when this action's output may be uploaded to the shared cache.

    Non-hermetic output depends on host state, so it is machine-specific
    and must never be served to another machine -- the same reasoning
    host_tools_exec applies (local_only = True, allow_cache_upload =
    False).
    """
    return buildroot_info(ctx).hermetic

def buildroot_args(ctx):
    """Flags describing the buildroot, with RE-safe hidden inputs.

    The root tree is passed whole -- never as a projection -- so every
    file in it materializes on an RE worker.
    """
    info = buildroot_info(ctx)
    args = [
        cmd_args("--provenance", info.provenance),
        cmd_args("--isolation", _isolation_for(info)),
    ]
    if info.root:
        args.append(cmd_args("--buildroot-tree", info.root, hidden = info.root))
    if info.target_cpu:
        args.append(cmd_args("--target-cpu", info.target_cpu))
    if info.dist_tag:
        args.append(cmd_args("--dist-tag", info.dist_tag))
    if info.macros:
        args.append(cmd_args("--macros", info.macros, hidden = info.macros))
    return args

def buildroot_sysroot_args(ctx):
    """Just the flags that say "enter this tree, this way".

    buildroot_args() also describes the *target* being built for --
    --target-cpu, --dist-tag, --macros -- which only a build driver cares
    about.  Rootfs installation is not building anything; it needs the
    buildroot solely as a place to find an rpm that speaks the target
    release's database format.  Passing the build flags to it would mean
    teaching that tool arguments it has no use for.

    Isolation still comes from _isolation_for(), not from a second opinion:
    whether the sandbox is real is one policy, and a rule that decided it
    separately could sandbox the replay and not the rootfs.
    """
    info = buildroot_info(ctx)
    args = [cmd_args("--isolation", _isolation_for(info))]
    if info.root:
        args.append(cmd_args("--buildroot-tree", info.root, hidden = info.root))
    if info.target_cpu:
        args.append(cmd_args("--target-cpu", info.target_cpu))
    return args

def _isolation_for(info):
    """Pick the sandbox level a buildroot's provenance implies.

    "host" cannot be sandboxed -- the whole point is that it uses the
    host's installation -- so it runs with isolation "none" and is
    pinned to local_only by buildroot_local_only().  A seeded buildroot
    is a self-contained tree, so it can be bind-mounted as / under
    bubblewrap and get real hermeticity plus network isolation.
    """
    if info.provenance == "host":
        return "none"

    # "auto", not "bwrap": the mechanism is a property of the machine, not
    # of the build, and bubblewrap is not installed everywhere.  The replay
    # picks bwrap or an unprivileged userns+chroot, both genuinely
    # hermetic, and hard-errors if it can find neither rather than
    # quietly reverting to the host toolchain.
    return "auto"

def buildroot_env(ctx):
    """Extra env the buildroot requires, as --env KEY=VALUE flags."""
    info = buildroot_info(ctx)
    return [
        cmd_args("--env", "{}={}".format(k, v))
        for k, v in sorted(info.env.items())
    ]

def dep_installroot_args(deps):
    """Flags overlaying each dependency's installroot into the buildroot.

    Each dep must provide PackageInfo; its prefix is an installroot
    unpacked from exactly one binary rpm (an rpm_subpackage or a
    prebuilt_rpm), never a whole source package's output -- see SPEC.md
    section 3a.  Whole trees, carried as hidden inputs for RE.
    """
    args = []
    for dep in deps:
        prefix = dep[PackageInfo].prefix
        args.append(cmd_args("--dep-installroot", prefix, hidden = prefix))
    return args

def dep_rpm_args(deps):
    """Flags registering each dependency in the sysroot's rpm database.

    The companion to dep_installroot_args, and needed because the two
    halves of "this dependency is available" live in different places: the
    installroot supplies the files, the .rpm supplies the database entry
    that rpmbuild's BuildRequires check actually consults.  Supply only the
    first and a build fails on a package whose files are demonstrably in
    the tree.

    A plain dep rather than one carrying PackageInfo: what is wanted here
    is the rpm file, which is what built_rpm and an http_file both produce
    as their default output, and requiring a provider would rule out the
    downloads that make up most of a buildroot.

    Hidden, like the installroots, so the file materialises on an RE
    worker rather than being named on a command line that then cannot
    find it.
    """
    args = []
    for dep in deps:
        outputs = dep[DefaultInfo].default_outputs
        if not outputs:
            fail("{} produces no rpm to register".format(dep.label))
        args.append(cmd_args("--dep-rpm", outputs[0], hidden = outputs[0]))
    return args
