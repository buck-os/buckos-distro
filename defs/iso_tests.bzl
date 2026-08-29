"""Build and boot validation images for every supported release."""

load(
    "//defs:architectures.bzl",
    "execution_compatible_with",
    "target_platform",
)
load("//defs/rules:boot_test.bzl", "iso_boot_test", "rootfs_overlay")
load("//defs/rules:image.bzl", "iso_image", "squashfs")


def live_iso_boot_tests(
        flavor,
        release,
        architecture,
        layout = "rpm",
        expect_selinux = False,
        image_variant = None):
    """Define one instrumented ISO and each applicable firmware test."""
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
    old_squashfs = release == "9" and flavor in ("centos", "centos-hyperscale")
    squashfs_tools = (
        flavor_package + "buildroot-binary-seed" + release_arch_suffix
        if old_squashfs
        else flavor_package + "buildroot-image-tools" + release_arch_suffix
    )

    rootfs_overlay(
        name = rootfs_name,
        rootfs = flavor_package + "rootfs-live" + image_suffix,
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
    squashfs(
        name = squashfs_name,
        buildroot = squashfs_tools,
        mksquashfs_source = "//tools:squashfs-tools-4.6.1-source" if old_squashfs else None,
        rootfs = ":" + rootfs_name,
        selinux_relabel = expect_selinux,
        default_target_platform = platform,
        exec_compatible_with = exec_constraints,
    )
    iso_image(
        name = iso_name,
        buildroot = flavor_package + "buildroot-image-tools" + release_arch_suffix,
        kernel = flavor_package + "kernel-live" + image_suffix,
        initramfs = flavor_package + "initramfs-live" + image_suffix,
        squashfs = ":" + squashfs_name,
        volume_label = "VERIFY-{}-{}-{}".format(flavor[:8], release, architecture),
        kernel_args = "console=tty0 {} systemd.unit=buckos-verify.service".format(
            "console=ttyAMA0,115200" if architecture == "aarch64" else "console=ttyS0,115200",
        ),
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
            iso = ":" + iso_name,
            architecture = architecture,
            firmware = firmware,
            expected_flavor = flavor,
            expected_version = release,
            expect_selinux = expect_selinux,
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
        live_iso_boot_tests("ubuntu", "26.04", architecture, layout = "ubuntu")
