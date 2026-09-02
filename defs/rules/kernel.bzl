"""Producer-neutral custom-kernel rules.

The image pipeline consumes KernelInfo and does not know which build system
created it.  `linux_kernel` is the open Linux/Kbuild producer in this repo;
`kernel_artifacts` adapts declared outputs from any other producer without
making its concepts part of the distro graph.
"""

load(
    "//defs:buildroot_helpers.bzl",
    "BUILDROOT_ATTRS",
    "buildroot_cache_upload",
    "buildroot_info",
    "buildroot_local_only",
    "buildroot_sysroot_args",
)
load("//defs:providers.bzl", "BootInfo", "KernelInfo", "SigningKeyInfo")


def configured_kernel_set():
    """Configured KernelInfo labels and the explicit default selection."""
    targets = []
    seen = {}
    for value in read_config("buckos.kernel", "targets", "").split(","):
        target = value.strip()
        if not target:
            continue
        if target in seen:
            fail("[buckos.kernel] targets repeats {}".format(target))
        seen[target] = True
        targets.append(target)

    default = read_config("buckos.kernel", "default", "").strip()
    if not targets:
        if default:
            fail("[buckos.kernel] default is set but targets is empty")
        return struct(
            targets = [],
            default = None,
            default_index = None,
            additional_indices = [],
        )
    if not default and len(targets) == 1:
        default = targets[0]
    if not default:
        fail("[buckos.kernel] default is required when targets has multiple kernels")
    if default not in seen:
        fail("[buckos.kernel] default {} is not present in targets".format(default))
    default_index = [
        index for index, target in enumerate(targets) if target == default
    ][0]
    return struct(
        targets = targets,
        default = default,
        default_index = default_index,
        additional_indices = [
            index for index in range(len(targets)) if index != default_index
        ],
    )


def _kernel_default_info(image, version, modules, optional):
    outputs = [version]
    sub_targets = {
        "version": [DefaultInfo(default_output = version)],
    }
    if modules != None:
        outputs.append(modules)
        sub_targets["modules"] = [DefaultInfo(default_output = modules)]
    for name, artifact in optional.items():
        if artifact != None:
            outputs.append(artifact)
            sub_targets[name] = [DefaultInfo(default_output = artifact)]
    return DefaultInfo(
        default_output = image,
        other_outputs = outputs,
        sub_targets = sub_targets,
    )


def _kernel_artifacts_impl(ctx: AnalysisContext) -> list[Provider]:
    if bool(ctx.attrs.release) == (ctx.attrs.release_file != None):
        fail("kernel_artifacts requires exactly one of release or release_file")

    version = ctx.attrs.release_file
    if ctx.attrs.release:
        version = ctx.actions.write(
            ctx.attrs.name + ".version",
            ctx.attrs.release,
        )

    modules = None
    if ctx.attrs.modules != None:
        modules = ctx.actions.declare_output("modules", dir = True)
        command = cmd_args(
            ctx.attrs._normalize_modules[RunInfo],
            "--modules",
            ctx.attrs.modules,
            "--version-file",
            version,
            "--layout",
            ctx.attrs.modules_layout,
            "--out",
            modules.as_output(),
        )
        # re-contract: buildroot-independent -- normalization reads only the
        # declared module tree/archive and release artifact, and writes a
        # canonical rootfs-shaped tree without invoking target binaries.
        ctx.actions.run(
            command,
            category = "kernel_modules_normalize",
            identifier = ctx.attrs.name,
            local_only = False,
            allow_cache_upload = True,
        )

    optional = {
        "config": ctx.attrs.config,
        "efi-stub": ctx.attrs.efi_stub,
        "ima-certificate": ctx.attrs.ima_certificate,
        "module-symvers": ctx.attrs.module_symvers,
        "system-map": ctx.attrs.system_map,
        "vmlinux": ctx.attrs.vmlinux,
    }
    info = KernelInfo(
        image = ctx.attrs.image,
        version = version,
        architecture = ctx.attrs.architecture,
        modules = modules,
        config = ctx.attrs.config,
        vmlinux = ctx.attrs.vmlinux,
        system_map = ctx.attrs.system_map,
        module_symvers = ctx.attrs.module_symvers,
        efi_stub = ctx.attrs.efi_stub,
        ima_certificate = ctx.attrs.ima_certificate,
    )
    return [
        _kernel_default_info(ctx.attrs.image, version, modules, optional),
        info,
        BootInfo(vmlinuz = ctx.attrs.image, initramfs = None, kver = version),
    ]


kernel_artifacts = rule(
    impl = _kernel_artifacts_impl,
    attrs = {
        "architecture": attrs.enum(["x86_64", "aarch64"]),
        "config": attrs.option(attrs.source(), default = None),
        "efi_stub": attrs.option(attrs.source(), default = None),
        "image": attrs.source(),
        "ima_certificate": attrs.option(attrs.source(), default = None),
        "module_symvers": attrs.option(attrs.source(), default = None),
        "modules": attrs.option(attrs.source(), default = None),
        # rootfs: input already contains [usr/]lib/modules/<release>.
        # version: input root is the contents of that release directory.
        # auto: accept either and fail if no unambiguous layout is found.
        "modules_layout": attrs.enum(
            ["auto", "rootfs", "version"],
            default = "auto",
        ),
        "release": attrs.string(default = ""),
        "release_file": attrs.option(attrs.source(), default = None),
        "system_map": attrs.option(attrs.source(), default = None),
        "vmlinux": attrs.option(attrs.source(), default = None),
        "_normalize_modules": attrs.default_only(
            attrs.exec_dep(default = "//tools:kernel_modules_normalize"),
        ),
    },
)


def _linux_kernel_impl(ctx: AnalysisContext) -> list[Provider]:
    if ctx.attrs.buildroot == None:
        fail(
            "linux_kernel requires an explicit buildroot; select the " +
            "distro/release-specific kernel build environment in the producer target",
        )
    buildroot_architecture = buildroot_info(ctx).target_cpu
    if buildroot_architecture and buildroot_architecture != ctx.attrs.architecture:
        fail("linux_kernel {} is {}, but its buildroot is {}".format(
            ctx.attrs.name,
            ctx.attrs.architecture,
            buildroot_architecture,
        ))
    image = ctx.actions.declare_output("kernel-image")
    version = ctx.actions.declare_output("kernel-version")
    modules = ctx.actions.declare_output("modules", dir = True)
    config = ctx.actions.declare_output("kernel.config")
    vmlinux = ctx.actions.declare_output("vmlinux")
    system_map = ctx.actions.declare_output("System.map")
    module_symvers = ctx.actions.declare_output("Module.symvers")

    command = cmd_args(
        ctx.attrs._build[RunInfo],
        "--source",
        ctx.attrs.source,
        "--config",
        ctx.attrs.config,
        "--architecture",
        ctx.attrs.architecture,
        "--out-image",
        image.as_output(),
        "--out-version",
        version.as_output(),
        "--out-modules",
        modules.as_output(),
        "--out-config",
        config.as_output(),
        "--out-vmlinux",
        vmlinux.as_output(),
        "--out-system-map",
        system_map.as_output(),
        "--out-module-symvers",
        module_symvers.as_output(),
        "--source-date-epoch",
        ctx.attrs.source_date_epoch,
        "--jobs",
        str(ctx.attrs.jobs),
        "--make",
        ctx.attrs.make,
    )
    command.add(buildroot_sysroot_args(ctx))
    if ctx.attrs.expected_release:
        command.add("--expected-release", ctx.attrs.expected_release)
    if ctx.attrs.image_path:
        command.add("--image-path", ctx.attrs.image_path)
    if ctx.attrs.localversion:
        command.add("--localversion", ctx.attrs.localversion)
    for value in ctx.attrs.make_args:
        command.add("--make-arg", value)
    if ctx.attrs.ima_signing_key != None:
        command.add(
            "--ima-certificate",
            ctx.attrs.ima_signing_key[SigningKeyInfo].certificate,
        )

    ctx.actions.run(
        command,
        category = "linux_kernel",
        identifier = ctx.attrs.name,
        # Kbuild runs entirely in the declared buildroot and /build scratch
        # tree. A hermetic buildroot is RE-eligible and its complete output
        # bundle can enter the shared cache; host provenance remains local.
        local_only = buildroot_local_only(ctx),
        allow_cache_upload = buildroot_cache_upload(ctx),
    )

    ima_certificate = None
    if ctx.attrs.ima_signing_key != None:
        ima_certificate = ctx.attrs.ima_signing_key[SigningKeyInfo].certificate
    optional = {
        "config": config,
        "efi-stub": ctx.attrs.efi_stub,
        "ima-certificate": ima_certificate,
        "module-symvers": module_symvers,
        "system-map": system_map,
        "vmlinux": vmlinux,
    }
    info = KernelInfo(
        image = image,
        version = version,
        architecture = ctx.attrs.architecture,
        modules = modules,
        config = config,
        vmlinux = vmlinux,
        system_map = system_map,
        module_symvers = module_symvers,
        efi_stub = ctx.attrs.efi_stub,
        ima_certificate = ima_certificate,
    )
    return [
        _kernel_default_info(image, version, modules, optional),
        info,
        BootInfo(vmlinuz = image, initramfs = None, kver = version),
    ]


linux_kernel = rule(
    impl = _linux_kernel_impl,
    attrs = {
        "architecture": attrs.enum(["x86_64", "aarch64"]),
        "config": attrs.source(),
        "efi_stub": attrs.option(attrs.source(), default = None),
        "expected_release": attrs.string(default = ""),
        "image_path": attrs.string(default = ""),
        "ima_signing_key": attrs.option(
            attrs.dep(providers = [SigningKeyInfo]),
            default = None,
        ),
        "jobs": attrs.int(default = 0),
        "localversion": attrs.string(default = ""),
        "make": attrs.string(default = "/usr/bin/make"),
        "make_args": attrs.list(attrs.string(), default = []),
        "source": attrs.source(),
        "source_date_epoch": attrs.string(default = "1700000000"),
        "_build": attrs.default_only(
            attrs.exec_dep(default = "//tools:linux_kernel_build"),
        ),
    } | BUILDROOT_ATTRS,
)


def _kernel_rootfs_impl(ctx: AnalysisContext) -> list[Provider]:
    rootfs = ctx.attrs.rootfs[DefaultInfo].default_outputs[0]
    out = ctx.actions.declare_output(ctx.attrs.name + ".tar")
    command = cmd_args(
        ctx.attrs._compose[RunInfo],
        "--rootfs",
        rootfs,
        "--out",
        out.as_output(),
        "--source-date-epoch",
        ctx.attrs.source_date_epoch,
    )
    for dep in ctx.attrs.kernels:
        kernel = dep[KernelInfo]
        if kernel.architecture != ctx.attrs.architecture:
            fail("kernel {} is {}, but image {} is {}".format(
                dep.label,
                kernel.architecture,
                ctx.attrs.name,
                ctx.attrs.architecture,
            ))
        if ctx.attrs.ima_signing_key != None and kernel.ima_certificate == None:
            fail("kernel {} does not declare the IMA certificate it trusts".format(
                dep.label,
            ))
        command.add(
            "--entry",
            kernel.image,
            kernel.version,
            kernel.modules if kernel.modules != None else "-",
            kernel.config if kernel.config != None else "-",
            kernel.system_map if kernel.system_map != None else "-",
            kernel.ima_certificate if kernel.ima_certificate != None else "-",
        )
    if ctx.attrs.ima_signing_key != None:
        command.add(
            "--expected-ima-certificate",
            ctx.attrs.ima_signing_key[SigningKeyInfo].certificate,
        )

    # re-contract: buildroot-independent -- composing a rootfs copies one
    # declared tar archive and appends only declared kernel artifacts with
    # normalized metadata. No distro or host tool is consulted.
    ctx.actions.run(
        command,
        category = "kernel_rootfs",
        identifier = ctx.attrs.name,
        local_only = False,
        allow_cache_upload = True,
    )
    return [DefaultInfo(default_output = out)]


kernel_rootfs = rule(
    impl = _kernel_rootfs_impl,
    attrs = {
        "architecture": attrs.enum(["x86_64", "aarch64"]),
        "ima_signing_key": attrs.option(
            attrs.dep(providers = [SigningKeyInfo]),
            default = None,
        ),
        "kernels": attrs.list(attrs.dep(providers = [KernelInfo])),
        "rootfs": attrs.dep(),
        "source_date_epoch": attrs.string(default = "1700000000"),
        "_compose": attrs.default_only(
            attrs.exec_dep(default = "//tools:kernel_rootfs"),
        ),
    },
)
