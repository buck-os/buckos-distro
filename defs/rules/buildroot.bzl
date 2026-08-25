"""Buildroot assembly.

A buildroot is the build environment a flavor's packages compile against.
Its *provenance* is per-flavor and is the structural decision that cuts
the dependency graph's cycles -- see SPEC.md sections 3 and 3a.

Two rules:

  host_buildroot     uses the host's installed rpm toolchain.  Not
                     hermetic, never runs on RE, never uploads to the
                     shared cache.  A development escape hatch, exactly
                     analogous to buckos-build's bootstrap host toolchain.

  seeded_buildroot   unpacks a pinned set of binary rpms into a tree.
                     Self-contained, so it can be bind-mounted as / under
                     bubblewrap and run on RE.
"""

load("//defs:providers.bzl", "BuildrootInfo")

# ── Host buildroot (development escape hatch) ────────────────────────

def _host_buildroot_impl(ctx: AnalysisContext) -> list[Provider]:
    # No populated tree at all: with provenance "host" the replay reads the
    # host's own /usr, so root stays None and buildroot_args emits no
    # --buildroot-tree.  The rule still produces an output so the graph
    # shape matches seeded_buildroot and consumers need no special-casing.
    marker = ctx.actions.write(
        "host-buildroot.txt",
        "provenance=host\nnot hermetic; local_only enforced by consumers\n",
    )

    return [
        DefaultInfo(default_output = marker),
        BuildrootInfo(
            root = None,
            provenance = "host",
            target_cpu = ctx.attrs.target_cpu,
            dist_tag = ctx.attrs.dist_tag,
            macros = None,
            # The whole point of this rule is reading host state, so it can
            # never be hermetic.  Consumers turn this into local_only = True
            # and allow_cache_upload = False.
            hermetic = False,
            env = ctx.attrs.env,
        ),
    ]

host_buildroot = rule(
    impl = _host_buildroot_impl,
    # A toolchain rule, so it can sit behind toolchains//:buildroot and be
    # swapped per flavor from .buckconfig -- the same socket buckos-build
    # exposes as toolchains//:buckos.
    is_toolchain_rule = True,
    attrs = {
        "dist_tag": attrs.string(default = ""),
        "env": attrs.dict(attrs.string(), attrs.string(), default = {}),
        "target_cpu": attrs.string(default = "x86_64"),
    },
)

# ── Seeded buildroot (production) ────────────────────────────────────

def _seeded_buildroot_impl(ctx: AnalysisContext) -> list[Provider]:
    out = ctx.actions.declare_output("buildroot", dir = True)

    cmd = cmd_args(ctx.attrs._assemble[RunInfo])
    cmd.add("--out", out.as_output())

    # Each seed rpm is a declared artifact, so it materializes on RE.
    for rpm in ctx.attrs.seed_rpms:
        for artifact in rpm[DefaultInfo].default_outputs:
            cmd.add(cmd_args("--rpm", artifact))

    if ctx.attrs.macros:
        cmd.add(cmd_args("--macros", ctx.attrs.macros))

    # Writing the tree's rpmdb means running the tree's own rpm inside it,
    # so this action needs a sandbox exactly as a replay does.  "auto" and
    # not "bwrap": the mechanism is a property of the machine, and the
    # assembler hard-errors rather than silently producing a database
    # written by whatever rpm the host happens to have.  Kept in step with
    # defs/buildroot_helpers.bzl's _isolation_for() for a seeded root.
    cmd.add("--isolation", ctx.attrs.isolation)
    cmd.add("--source-date-epoch", ctx.attrs.source_date_epoch)

    ctx.actions.run(
        cmd,
        category = "buildroot_assemble",
        identifier = ctx.attrs.name,
        # re-contract: buildroot-independent -- this action *builds* the
        # buildroot rather than running against one.  Its inputs are rpms
        # pinned by sha256 and it reads no host state, so it is safe to
        # cache and to run remotely whatever the flavor's provenance is.
        allow_cache_upload = True,
    )

    return [
        DefaultInfo(default_output = out),
        BuildrootInfo(
            root = out,
            provenance = "binary-seed",
            target_cpu = ctx.attrs.target_cpu,
            dist_tag = ctx.attrs.dist_tag,
            macros = ctx.attrs.macros,
            hermetic = True,
            env = ctx.attrs.env,
        ),
    ]

seeded_buildroot = rule(
    impl = _seeded_buildroot_impl,
    is_toolchain_rule = True,
    attrs = {
        "dist_tag": attrs.string(default = ""),
        "env": attrs.dict(attrs.string(), attrs.string(), default = {}),
        "isolation": attrs.enum(
            ["auto", "bwrap", "unshare", "none"],
            default = "auto",
        ),
        "macros": attrs.option(attrs.source(), default = None),
        # The @buildsys-build closure, pinned by sha256 upstream via
        # http_file.  This set is where the dependency graph is cut.
        "seed_rpms": attrs.list(attrs.dep(), default = []),
        # Pins the install times and transaction id rpm writes into the
        # database.  Without it this tree's hash changes every build, and
        # since every package compiles against it, so does the whole
        # downstream cache.
        "source_date_epoch": attrs.string(default = "1700000000"),
        "target_cpu": attrs.string(default = "x86_64"),
        "_assemble": attrs.default_only(
            attrs.exec_dep(default = "//tools:buildroot_assemble"),
        ),
    },
)
