"""Configuration and composition helpers for signed Unified Kernel Images."""

load("//defs/rules:image.bzl", "uki_image")
load("//defs/rules:signing.bzl", "efi_sign")

def configured_ima():
    """Return one validated IMA signing and appraisal configuration."""
    signing_key = read_config(
        "buckos.security",
        "ima_signing_key",
        "",
    ).strip()
    mode = read_config(
        "buckos.security",
        "ima_signing_mode",
        "all",
    ).strip()
    if mode not in ("all", "executables"):
        fail("ima_signing_mode must be all or executables")
    kernel_args = read_config(
        "buckos.security",
        "ima_kernel_args",
        "ima_appraise=enforce ima_appraise_tcb",
    ).strip()
    if signing_key and not kernel_args:
        fail("ima_kernel_args must not be empty when IMA signing is enabled")
    return struct(
        enabled = bool(signing_key),
        kernel_args = kernel_args if signing_key else "",
        mode = mode,
        signing_key = signing_key if signing_key else None,
    )

def configured_secure_boot(architecture):
    """Return the configured signer and optional architecture-specific stub."""
    signing_key = read_config(
        "buckos.security",
        "secure_boot_signing_key",
        "",
    ).strip()
    efi_stub = read_config(
        "buckos.security",
        "efi_stub_" + architecture,
        "",
    ).strip()
    if efi_stub and not signing_key:
        fail("efi_stub_{} is set without secure_boot_signing_key".format(
            architecture,
        ))
    return struct(
        enabled = bool(signing_key),
        efi_stub = efi_stub if efi_stub else None,
        signing_key = signing_key if signing_key else None,
    )

def signed_uki(
        name,
        architecture,
        buildroot,
        efi_stub,
        initramfs,
        kernel,
        kernel_args,
        layout,
        rootfs,
        signing_key,
        volume_label,
        default_target_platform = None,
        exec_compatible_with = [],
        visibility = None):
    """Assemble a UKI hermetically, then sign it through SigningKeyInfo."""
    unsigned_name = name + "-unsigned"
    uki_image(
        name = unsigned_name,
        architecture = architecture,
        buildroot = buildroot,
        efi_stub = efi_stub,
        initramfs = initramfs,
        kernel = kernel,
        kernel_args = kernel_args,
        layout = layout,
        rootfs = rootfs,
        volume_label = volume_label,
        default_target_platform = default_target_platform,
        exec_compatible_with = exec_compatible_with,
    )
    efi_sign(
        name = name,
        efi = ":" + unsigned_name,
        signing_key = signing_key,
        default_target_platform = default_target_platform,
        visibility = visibility,
    )
