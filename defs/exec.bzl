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

def _properties(raw, field):
    properties = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        key, separator, value = item.partition("=")
        if not separator or not key.strip() or not value.strip():
            fail("{} must be a comma-separated key=value list, got {!r}".format(field, raw))
        key = key.strip()
        if key in properties:
            fail("{} contains duplicate key {!r}".format(field, key))
        properties[key] = value.strip()
    if not properties:
        fail("{} must name at least one remote execution property".format(field))
    return properties

def _distro_execution_platform_impl(ctx: AnalysisContext) -> list[Provider]:
    if ctx.attrs.target_platform != None:
        constraints = dict(ctx.attrs.target_platform[PlatformInfo].configuration.constraints)
    else:
        constraints = dict()
        constraints.update(ctx.attrs.cpu_configuration[ConfigurationInfo].constraints)
        constraints.update(ctx.attrs.os_configuration[ConfigurationInfo].constraints)

    host_cpu = ""
    for value in constraints.values():
        label = str(value.label)
        if label.endswith(":x86_64"):
            host_cpu = "x86_64"
        elif label.endswith(":arm64") or label.endswith(":aarch64"):
            host_cpu = "aarch64"

    native_capability = ctx.attrs.x86_64_execution_capability
    if host_cpu == "aarch64":
        native_capability = ctx.attrs.aarch64_execution_capability
    native_value = native_capability[ConstraintValueInfo]
    constraints[native_value.setting.label] = native_value

    capability = native_capability
    if ctx.attrs.target_cpu == "x86_64":
        capability = ctx.attrs.x86_64_execution_capability
    elif ctx.attrs.target_cpu == "aarch64":
        capability = ctx.attrs.aarch64_execution_capability
    value = capability[ConstraintValueInfo]
    constraints[value.setting.label] = value
    if ctx.attrs.aarch64_emulation_enabled and host_cpu != "aarch64":
        emulated_value = ctx.attrs.aarch64_execution_capability[ConstraintValueInfo]
        constraints[emulated_value.setting.label] = emulated_value

    cfg = ConfigurationInfo(constraints = constraints, values = {})
    platform = ExecutionPlatformInfo(
        label = ctx.label.raw_target(),
        configuration = cfg,
        executor_config = CommandExecutorConfig(
            local_enabled = ctx.attrs.local_enabled,
            remote_enabled = ctx.attrs.remote_enabled,
            remote_cache_enabled = True if ctx.attrs.remote_cache_enabled else None,
            allow_cache_uploads = ctx.attrs.remote_cache_enabled,
            use_limited_hybrid = False,
            use_windows_path_separators = False,
            remote_execution_properties = ctx.attrs.remote_execution_properties,
            remote_execution_use_case = ctx.attrs.remote_execution_use_case,
        ),
    )

    return [
        DefaultInfo(),
        platform,
        PlatformInfo(label = str(ctx.label.raw_target()), configuration = cfg),
    ]

_distro_execution_platform = rule(
    impl = _distro_execution_platform_impl,
    attrs = {
        "cpu_configuration": attrs.dep(
            providers = [ConfigurationInfo],
            default = host_configuration.cpu,
        ),
        "os_configuration": attrs.dep(
            providers = [ConfigurationInfo],
            default = host_configuration.os,
        ),
        "aarch64_emulation_enabled": attrs.bool(default = False),
        "aarch64_execution_capability": attrs.dep(providers = [ConstraintValueInfo]),
        "local_enabled": attrs.bool(default = False),
        "remote_cache_enabled": attrs.bool(default = False),
        "remote_enabled": attrs.bool(default = False),
        "remote_execution_properties": attrs.dict(attrs.string(), attrs.string()),
        "remote_execution_use_case": attrs.string(default = "buck2-default"),
        "target_cpu": attrs.option(attrs.string(), default = None),
        "target_platform": attrs.option(attrs.dep(providers = [PlatformInfo]), default = None),
        "x86_64_execution_capability": attrs.dep(providers = [ConstraintValueInfo]),
    },
)

def _execution_platform_registry_impl(ctx: AnalysisContext) -> list[Provider]:
    return [
        DefaultInfo(),
        ExecutionPlatformRegistrationInfo(
            platforms = [dep[ExecutionPlatformInfo] for dep in ctx.attrs.platforms],
        ),
    ]

_execution_platform_registry = rule(
    impl = _execution_platform_registry_impl,
    attrs = {
        "platforms": attrs.list(attrs.dep(providers = [ExecutionPlatformInfo])),
    },
)

def _combined_execution_platforms_impl(ctx: AnalysisContext) -> list[Provider]:
    """Combine registries without changing any platform they contain."""
    platforms = []
    platform_labels = {}
    exec_marker_constraint = None
    fallback = None
    fallback_registry = None
    for registry in ctx.attrs.registries:
        registration = registry[ExecutionPlatformRegistrationInfo]
        for platform in registration.platforms:
            label = str(platform.label)
            if label in platform_labels:
                fail("execution platform {} is registered more than once".format(label))
            platform_labels[label] = True
            platforms.append(platform)

        marker = getattr(registration, "exec_marker_constraint", None)
        if marker != None:
            if exec_marker_constraint != None and exec_marker_constraint != marker:
                fail("execution platform registries have different marker constraints")
            exec_marker_constraint = marker

        registry_fallback = getattr(registration, "fallback", None)
        if registry_fallback != None:
            if fallback_registry != None:
                fail("execution platform registries {} and {} both define fallbacks".format(
                    fallback_registry,
                    registry.label,
                ))
            fallback = registry_fallback
            fallback_registry = registry.label

    kwargs = {}
    if exec_marker_constraint != None:
        kwargs["exec_marker_constraint"] = exec_marker_constraint
    if fallback != None:
        kwargs["fallback"] = fallback
    return [
        DefaultInfo(),
        ExecutionPlatformRegistrationInfo(
            platforms = platforms,
            **kwargs
        ),
    ]

combined_execution_platforms = rule(
    impl = _combined_execution_platforms_impl,
    attrs = {
        "registries": attrs.list(
            attrs.dep(providers = [ExecutionPlatformRegistrationInfo]),
        ),
    },
    is_configuration_rule = True,
)

def distro_execution_platforms(
        name,
        aarch64_emulation_enabled,
        aarch64_execution_capability,
        aarch64_platform,
        remote_cache_enabled,
        remote_execution_enabled,
        remote_aarch64_properties,
        remote_aarch64_use_case,
        remote_x86_64_properties,
        remote_x86_64_use_case,
        x86_64_execution_capability,
        x86_64_platform,
        additional_registries = [],
        visibility = None):
    common = {
        "aarch64_emulation_enabled": aarch64_emulation_enabled,
        "aarch64_execution_capability": aarch64_execution_capability,
        "remote_cache_enabled": remote_cache_enabled,
        "x86_64_execution_capability": x86_64_execution_capability,
    }
    local = name + "-local"
    _distro_execution_platform(
        name = local,
        local_enabled = True,
        remote_execution_properties = {"platform.OSFamily": "linux"},
        **common
    )
    platforms = [":" + local]
    if remote_execution_enabled:
        remote_x86 = name + "-remote-x86_64"
        _distro_execution_platform(
            name = remote_x86,
            remote_enabled = True,
            target_cpu = "x86_64",
            target_platform = x86_64_platform,
            remote_execution_properties = _properties(remote_x86_64_properties, "remote_x86_64_properties"),
            remote_execution_use_case = remote_x86_64_use_case,
            **common
        )
        remote_arm = name + "-remote-aarch64"
        _distro_execution_platform(
            name = remote_arm,
            remote_enabled = True,
            target_cpu = "aarch64",
            target_platform = aarch64_platform,
            remote_execution_properties = _properties(remote_aarch64_properties, "remote_aarch64_properties"),
            remote_execution_use_case = remote_aarch64_use_case,
            **common
        )
        platforms = [":" + remote_x86, ":" + remote_arm] + platforms
    if additional_registries:
        native_registry = name + "-native"
        _execution_platform_registry(
            name = native_registry,
            platforms = platforms,
        )
        combined_execution_platforms(
            name = name,
            registries = [":" + native_registry] + additional_registries,
            visibility = visibility,
        )
    else:
        _execution_platform_registry(
            name = name,
            platforms = platforms,
            visibility = visibility,
        )
