"""Rules for firmware-level ISO boot validation."""


def _rootfs_overlay_impl(ctx: AnalysisContext) -> list[Provider]:
    out = ctx.actions.declare_output(ctx.attrs.name + ".tar")
    rootfs = ctx.attrs.rootfs[DefaultInfo].default_outputs[0]
    cmd = cmd_args(
        ctx.attrs._overlay[RunInfo],
        "--rootfs",
        rootfs,
        "--out",
        out.as_output(),
        "--source-date-epoch",
        ctx.attrs.source_date_epoch,
    )
    for destination, source in sorted(ctx.attrs.files.items()):
        mode = ctx.attrs.modes.get(destination, "0644")
        cmd.add("--file", cmd_args(destination, mode, source, delimiter = ":"))
    # re-contract: buildroot-independent. This copies a tar archive and
    # appends declared source bytes with fixed metadata using Python only.
    ctx.actions.run(
        cmd,
        category = "rootfs_overlay",
        identifier = ctx.attrs.name,
        allow_cache_upload = True,
    )
    return [DefaultInfo(default_output = out)]


rootfs_overlay = rule(
    impl = _rootfs_overlay_impl,
    attrs = {
        "files": attrs.dict(attrs.string(), attrs.source()),
        "modes": attrs.dict(attrs.string(), attrs.string(), default = {}),
        "rootfs": attrs.dep(),
        "source_date_epoch": attrs.string(default = "1700000000"),
        "_overlay": attrs.default_only(
            attrs.exec_dep(default = "//tools:rootfs_overlay"),
        ),
    },
)


def _iso_boot_test_impl(ctx: AnalysisContext) -> list[Provider]:
    production_iso = ctx.attrs.production_iso[DefaultInfo].default_outputs[0]
    verification_iso = ctx.attrs.verification_iso[DefaultInfo].default_outputs[0]
    cmd = cmd_args(
        ctx.attrs._runner[RunInfo],
        "--production-iso",
        production_iso,
        "--verification-iso",
        verification_iso,
        "--production-milestone",
        ctx.attrs.production_milestone,
        "--arch",
        ctx.attrs.architecture,
        "--firmware",
        ctx.attrs.firmware,
        "--expected-flavor",
        ctx.attrs.expected_flavor,
        "--expected-version",
        ctx.attrs.expected_version,
        "--timeout",
        str(ctx.attrs.timeout_secs),
        "--qemu",
        ctx.attrs.qemu,
    )
    if ctx.attrs.firmware_path:
        cmd.add("--firmware-path", ctx.attrs.firmware_path)
    if ctx.attrs.firmware_vars:
        cmd.add("--firmware-vars", ctx.attrs.firmware_vars)
    if ctx.attrs.expect_selinux:
        cmd.add("--expect-selinux")

    return [
        DefaultInfo(),
        ExternalRunnerTestInfo(
            command = [cmd],
            labels = ctx.attrs.labels,
            type = "custom",
        ),
    ]


_iso_boot_test = rule(
    impl = _iso_boot_test_impl,
    attrs = {
        "architecture": attrs.enum(["x86_64", "aarch64"]),
        "expected_flavor": attrs.string(),
        "expected_version": attrs.string(),
        "firmware": attrs.enum(["bios", "uefi"]),
        "firmware_path": attrs.string(default = ""),
        "firmware_vars": attrs.string(default = ""),
        "labels": attrs.list(attrs.string(), default = []),
        "expect_selinux": attrs.bool(default = False),
        "production_iso": attrs.dep(),
        "production_milestone": attrs.string(default = "login:"),
        "qemu": attrs.string(),
        "timeout_secs": attrs.int(default = 600),
        "verification_iso": attrs.dep(),
        "_runner": attrs.default_only(
            attrs.exec_dep(default = "//tools:iso_boot_test"),
        ),
    },
)


def iso_boot_test(name, architecture, firmware, **kwargs):
    qemu_default = "qemu-system-aarch64" if architecture == "aarch64" else "qemu-system-x86_64"
    firmware_path = ""
    firmware_vars = ""
    if firmware == "uefi":
        if architecture == "aarch64":
            firmware_path = read_config("buckos", "aarch64_uefi", "")
        else:
            firmware_path = read_config("buckos", "ovmf_code", "")
            firmware_vars = read_config("buckos", "ovmf_vars", "")
    _iso_boot_test(
        name = name,
        architecture = architecture,
        firmware = firmware,
        firmware_path = firmware_path,
        firmware_vars = firmware_vars,
        qemu = read_config("buckos", "qemu_" + architecture, qemu_default),
        **kwargs
    )
