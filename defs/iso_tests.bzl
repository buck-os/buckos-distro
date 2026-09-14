"""Build and boot validation images for every supported release."""

load(
    "//defs:architectures.bzl",
    "execution_compatible_with",
    "target_platform",
)
load("//defs/rules:boot_test.bzl", "iso_boot_test", "rootfs_overlay")
load("//defs/rules:image.bzl", "iso_image", "squashfs")
load("//defs/rules:kernel.bzl", "configured_kernel_set")
load("//defs/rules:signing.bzl", "ima_manifest")
load("//defs:secure_boot.bzl", "configured_ima", "configured_secure_boot", "signed_uki")


def live_iso_boot_tests(
        flavor,
        release,
        architecture,
        layout = "rpm",
        expect_selinux = False,
        image_variant = None):
    """Define one verification ISO and paired production/verification boots."""
    variant_suffix = "-" + image_variant if image_variant else ""
    suffix = "{}{}-{}-{}".format(
        flavor,
        variant_suffix,
        release.replace(".", "_"),
        architecture,
    )
    release_arch_suffix = "-{}-{}".format(release, architecture)
    image_suffix = variant_suffix + release_arch_suffix
    flavor_package = "//flavors/{}:".format(flavor)
    platform = target_platform(flavor, release, architecture)
    exec_constraints = execution_compatible_with(architecture)
    rootfs_name = "rootfs-verify-" + suffix
    squashfs_name = "squashfs-verify-" + suffix
    iso_name = "iso-verify-" + suffix
    production_iso = flavor_package + "iso-live" + image_suffix
    # Same routing as the production image in defs/rpm_family.bzl, and it
    # has to stay the same: the verification image is the production one
    # plus an overlay, so a squashfs written by a different tool would make
    # the boot pair prove something about two different builds.
    old_squashfs = release == "9" and flavor in ("centos", "centos-hyperscale")
    squashfs_tools = (
        flavor_package + "buildroot-squashfs-tools" + release_arch_suffix
        if old_squashfs
        else flavor_package + "buildroot-image-tools" + release_arch_suffix
    )

    kernel_set = configured_kernel_set()
    ima = configured_ima()
    if ima.enabled and not kernel_set.targets:
        fail("IMA verification requires a configured KernelInfo target")
    secure_boot = configured_secure_boot(architecture)
    if secure_boot.enabled and kernel_set.additional_indices:
        fail("Secure Boot UKIs currently support one kernel per ISO")
    production_rootfs = flavor_package + "rootfs-live" + image_suffix
    if kernel_set.targets:
        production_rootfs = flavor_package + "rootfs-kernel-live" + image_suffix

    rootfs_overlay(
        name = rootfs_name,
        rootfs = production_rootfs,
        files = {
            "/etc/systemd/system/buckos-verify.service": "fixtures/buckos-verify.service",
            "/usr/local/bin/buckos-boot-verify": "fixtures/boot-verify.sh",
        },
        modes = {
            "/etc/systemd/system/buckos-verify.service": "0644",
            "/usr/local/bin/buckos-boot-verify": "0755",
        },
        default_target_platform = platform,
    )
    manifest_target = None
    if ima.enabled:
        manifest_name = "ima-manifest-verify-" + suffix
        ima_manifest(
            name = manifest_name,
            rootfs = ":" + rootfs_name,
            signing_key = ima.signing_key,
            mode = ima.mode,
            default_target_platform = platform,
        )
        manifest_target = ":" + manifest_name
    squashfs(
        name = squashfs_name,
        buildroot = squashfs_tools,
        mksquashfs_source = "//tools:squashfs-tools-4.6.1-source" if old_squashfs else None,
        ima_manifest = manifest_target,
        rootfs = ":" + rootfs_name,
        selinux_relabel = expect_selinux,
        default_target_platform = platform,
        exec_compatible_with = exec_constraints,
    )
    volume_label = "VERIFY-{}-{}-{}".format(flavor[:8], release, architecture)
    kernel_args = "console=tty0 {} {} systemd.unit=buckos-verify.service".format(
        "console=ttyAMA0,115200" if architecture == "aarch64" else "console=ttyS0,115200",
        ima.kernel_args,
    )
    secure_boot_image = None
    if secure_boot.enabled:
        # UKI assembly needs the target's objcopy/objdump, so use the same
        # package-build buildroot as the production UKI rather than assuming
        # those tools happen to be in the ISO assembly buildroot.
        default_provenance = "binary-seed" if flavor in ("debian", "ubuntu") else "host"
        provenance = read_config(
            "buckos." + flavor,
            "buildroot",
            default_provenance,
        )
        secure_boot_name = "secure-boot-verify-" + suffix
        signed_uki(
            name = secure_boot_name,
            architecture = architecture,
            buildroot = flavor_package + "buildroot-{}{}".format(
                provenance,
                release_arch_suffix,
            ),
            efi_stub = secure_boot.efi_stub,
            initramfs = flavor_package + "initramfs-live" + image_suffix,
            kernel = flavor_package + "kernel-live" + image_suffix,
            kernel_args = kernel_args,
            layout = layout,
            rootfs = ":" + rootfs_name,
            signing_key = secure_boot.signing_key,
            volume_label = volume_label,
            default_target_platform = platform,
            exec_compatible_with = exec_constraints,
        )
        secure_boot_image = ":" + secure_boot_name

    iso_image(
        name = iso_name,
        buildroot = flavor_package + "buildroot-image-tools" + release_arch_suffix,
        kernel = flavor_package + "kernel-live" + image_suffix,
        initramfs = flavor_package + "initramfs-live" + image_suffix,
        additional_initramfs = [
            flavor_package + "initramfs-live{}-custom-{}".format(image_suffix, index)
            for index in kernel_set.additional_indices
        ],
        additional_kernels = [
            flavor_package + "kernel-live{}-custom-{}".format(image_suffix, index)
            for index in kernel_set.additional_indices
        ],
        squashfs = ":" + squashfs_name,
        secure_boot_image = secure_boot_image,
        volume_label = volume_label,
        kernel_args = kernel_args,
        boot_mode = "hybrid" if architecture == "x86_64" else "uefi",
        layout = layout,
        target_cpu = architecture,
        default_target_platform = platform,
        exec_compatible_with = exec_constraints,
    )

    firmwares = ["uefi"] if architecture == "aarch64" else ["bios", "uefi"]
    for firmware in firmwares:
        iso_boot_test(
            name = "boot-{}-{}".format(suffix, firmware),
            production_iso = production_iso,
            production_milestone = "login:",
            verification_iso = ":" + iso_name,
            architecture = architecture,
            firmware = firmware,
            expected_flavor = flavor,
            expected_version = release,
            expect_selinux = expect_selinux,
            expect_ima = ima.enabled,
            expect_secure_boot = secure_boot.enabled and firmware == "uefi",
            labels = ["vm", "slow", "integration", "heavy", architecture, firmware],
            default_target_platform = platform,
        )


def all_live_iso_boot_tests():
    for release in ("44", "45"):
        for architecture in ("x86_64", "aarch64"):
            live_iso_boot_tests("fedora", release, architecture, expect_selinux = True)
            live_iso_boot_tests(
                "fedora",
                release,
                architecture,
                expect_selinux = True,
                image_variant = "prebuilt",
            )

    for flavor in ("centos", "centos-hyperscale"):
        for release in ("9", "10"):
            for architecture in ("x86_64", "aarch64"):
                live_iso_boot_tests(flavor, release, architecture, expect_selinux = True)
                live_iso_boot_tests(
                    flavor,
                    release,
                    architecture,
                    expect_selinux = True,
                    image_variant = "prebuilt",
                )

    for architecture in ("x86_64", "aarch64"):
        live_iso_boot_tests("debian", "13", architecture, layout = "debian")
        live_iso_boot_tests(
            "debian",
            "13",
            architecture,
            layout = "debian",
            image_variant = "prebuilt",
        )
        live_iso_boot_tests("ubuntu", "26.04", architecture, layout = "ubuntu")
        live_iso_boot_tests(
            "ubuntu",
            "26.04",
            architecture,
            layout = "ubuntu",
            image_variant = "prebuilt",
        )
