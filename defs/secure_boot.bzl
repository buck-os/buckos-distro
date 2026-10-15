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

def configured_dm_verity():
    """Return validated, reproducible dm-verity image policy."""
    enabled_value = read_config(
        "buckos.security",
        "dm_verity",
        "false",
    ).strip().lower()
    if enabled_value not in ("true", "false"):
        fail("dm_verity must be true or false")
    algorithm = read_config(
        "buckos.security",
        "dm_verity_algorithm",
        "sha256",
    ).strip()
    if algorithm not in ("sha256", "sha512"):
        fail("dm_verity_algorithm must be sha256 or sha512")
    corruption_policy = read_config(
        "buckos.security",
        "dm_verity_corruption_policy",
        "eio",
    ).strip()
    if corruption_policy not in ("eio", "panic", "restart"):
        fail("dm_verity_corruption_policy must be eio, panic, or restart")
    data_block_size = int(read_config(
        "buckos.security",
        "dm_verity_data_block_size",
        "4096",
    ))
    hash_block_size = int(read_config(
        "buckos.security",
        "dm_verity_hash_block_size",
        "4096",
    ))
    for name, size in (
        ("dm_verity_data_block_size", data_block_size),
        ("dm_verity_hash_block_size", hash_block_size),
    ):
        if size < 512 or size > 65536 or size & (size - 1):
            fail("{} must be a power of two from 512 through 65536".format(name))
    return struct(
        algorithm = algorithm,
        corruption_policy = corruption_policy,
        data_block_size = data_block_size,
        enabled = enabled_value == "true",
        hash_block_size = hash_block_size,
        salt = read_config(
            "buckos.security",
            "dm_verity_salt",
            "auto",
        ).strip(),
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
        verity = None,
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
        verity = verity,
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
