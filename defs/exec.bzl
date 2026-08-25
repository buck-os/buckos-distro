"""Execution platform registration.

A trimmed version of buckos-build's tc/defs.bzl.  The important part is
identical: remote execution and remote caching are *config-driven*, so the
same target graph runs locally on a laptop and on RE in CI without editing
any rule.

Actions opt out individually.  A replay against a non-hermetic buildroot
sets local_only = True and allow_cache_upload = False (see
defs/buildroot_helpers.bzl), which overrides what is enabled here.  This
platform decides what is *possible*; the buildroot's provenance decides
what is *permitted*.
"""

load("@prelude//platforms:defs.bzl", "host_configuration")

def _distro_execution_platforms_impl(ctx: AnalysisContext) -> list[Provider]:
    constraints = dict()
    constraints.update(ctx.attrs.cpu_configuration[ConfigurationInfo].constraints)
    constraints.update(ctx.attrs.os_configuration[ConfigurationInfo].constraints)

    cfg = ConfigurationInfo(constraints = constraints, values = {})

    name = ctx.label.raw_target()
    platform = ExecutionPlatformInfo(
        label = name,
        configuration = cfg,
        executor_config = CommandExecutorConfig(
            local_enabled = True,
            remote_enabled = ctx.attrs.remote_execution_enabled,
            remote_cache_enabled = True if ctx.attrs.remote_cache_enabled else None,
            allow_cache_uploads = ctx.attrs.remote_cache_enabled,
            # Hybrid so an action marked local_only still runs, rather
            # than failing for want of a local-capable platform.
            use_limited_hybrid = ctx.attrs.remote_execution_enabled,
            use_windows_path_separators = False,
            # Set unconditionally, not under `if remote_enabled`.  buck2
            # rejects remote_enabled = True with these unset -- which is how
            # the flag came to be untested: with them absent, flipping
            # `remote_execution = true` failed in analysis, before any
            # action ran, so "config-driven RE" was a claim the config
            # could not actually satisfy.  Setting them always costs
            # nothing when RE is off and makes the flag mean something
            # when it is on.
            remote_execution_properties = ctx.attrs.remote_execution_properties,
            remote_execution_use_case = ctx.attrs.remote_execution_use_case,
        ),
    )

    return [
        DefaultInfo(),
        platform,
        PlatformInfo(label = str(name), configuration = cfg),
        ExecutionPlatformRegistrationInfo(platforms = [platform]),
    ]

distro_execution_platforms = rule(
    impl = _distro_execution_platforms_impl,
    attrs = {
        "cpu_configuration": attrs.dep(
            providers = [ConfigurationInfo],
            default = host_configuration.cpu,
        ),
        "os_configuration": attrs.dep(
            providers = [ConfigurationInfo],
            default = host_configuration.os,
        ),
        "remote_cache_enabled": attrs.bool(default = False),
        "remote_execution_enabled": attrs.bool(default = False),
        # What the scheduler matches a worker against.  The default is the
        # only property that is true of every backend; anything more
        # specific (a container image, a instruction-set label, a pool
        # name) belongs to a particular RE deployment, so it is an
        # attribute rather than a constant here.
        "remote_execution_properties": attrs.dict(
            attrs.string(),
            attrs.string(),
            default = {"platform.OSFamily": "linux"},
        ),
        "remote_execution_use_case": attrs.string(default = "buck2-default"),
    },
)
