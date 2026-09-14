"""Turning a rootfs into media: the squashfs, and the ISO around it.

Two rules, split for the same reason boot.bzl splits kernel_image from
initramfs -- what each one costs.

  squashfs   compresses a whole root filesystem.  Minutes, and the
             expensive half by a wide margin.
  iso_image  lays out a few dozen files and calls xorriso.  Seconds.

The kernel command line, the volume label and the bootloader config all
live in the cheap half, so changing any of them does not recompress the
rootfs.  Fusing the rules would make "fix a typo in the boot menu" cost a
full squashfs rebuild.

Both run inside seeded target buildroots rather than against host binaries.
mksquashfs and xorriso are build inputs like any other: an ISO built by
whatever versions the machine happened to have is not reproducible, and on
a machine that has none it is not buildable at all.  Most releases use the
image-tools buildroot for both.  Enterprise Linux 9 compiles the pinned
mksquashfs source in its binary-seed buildroot because its packaged version
predates pseudo-file xattrs.  Seeded buildroots also make these actions
hermetic and remote-executable; see defs/buildroot_helpers.bzl.

The layout is Fedora's live-media layout, and it is not arbitrary; dracut's
dmsquash-live module goes looking for specific paths and hangs at an
emergency shell when they are absent:

    /LiveOS/squashfs.img    the root filesystem
    /isolinux/              BIOS boot (isolinux.bin + the c32 modules)
    /EFI/BOOT/              UEFI boot (generated GRUB or signed EFI image)
    /images/efiboot.img     a FAT image holding the same, for El Torito

One choice inside the squashfs is worth naming, because the alternative is
what everyone writes first.  Fedora's own images put an ext4 `rootfs.img`
inside the squashfs under LiveOS/.  This does not, and dracut supports
both: dmsquash-live-root.sh only looks for LiveOS/rootfs.img if the
squashfs has a top-level LiveOS directory, and falls back to using the
squashfs itself as the root filesystem when it does not.  Building the
ext4 variant would mean creating a sized filesystem image and populating
it, and the only unprivileged way to do that is `mkfs.ext4 -d`, which
needs a size guessed in advance and silently truncates when the guess is
low.  Using the squashfs directly has no size to guess.
"""

load(
    "//defs:buildroot_helpers.bzl",
    "BUILDROOT_ATTRS",
    "buildroot_cache_upload",
    "buildroot_info",
    "buildroot_local_only",
    "buildroot_sysroot_args",
)
load("//defs:providers.bzl", "BootInfo", "EfiImageInfo", "RootfsInfo")
load("//defs/rules:rootfs.bzl", "rootfs_artifact")

_LIVE_ROOT_ARGS = {
    "rpm": "root=live:CDLABEL={label} rd.live.image",
    "debian": "boot=live components",
    "ubuntu": "boot=casper",
}

def _single_output(dep, what):
    outputs = dep[DefaultInfo].default_outputs
    if not outputs:
        fail("{} produces no {}".format(dep.label, what))
    return outputs[0]

def _squashfs_impl(ctx: AnalysisContext) -> list[Provider]:
    out = ctx.actions.declare_output(ctx.attrs.name + ".img")

    cmd = cmd_args(
        ctx.attrs._build[RunInfo],
        "--rootfs",
        rootfs_artifact(ctx.attrs.rootfs),
        "--out",
        out.as_output(),
        "--compressor",
        ctx.attrs.compressor,
    )
    cmd.add(buildroot_sysroot_args(ctx))
    for path in ctx.attrs.exclude:
        cmd.add("--exclude", path)
    if ctx.attrs.ima_manifest != None:
        cmd.add(
            "--xattr-pseudo",
            _single_output(ctx.attrs.ima_manifest, "IMA xattr manifest"),
        )
    if ctx.attrs.selinux_relabel:
        cmd.add("--selinux-relabel")
    if ctx.attrs.mksquashfs_source != None:
        cmd.add(
            "--mksquashfs-source",
            _single_output(ctx.attrs.mksquashfs_source, "mksquashfs source archive"),
        )

    ctx.actions.run(
        cmd,
        category = "squashfs",
        identifier = ctx.attrs.name,
        # mksquashfs is given -no-exports and a fixed timestamp, so this is
        # reproducible bit-for-bit in principle.  Uploading is still gated
        # on provenance for the same reason every other image action gates
        # it: a non-hermetic buildroot's output is machine-specific no
        # matter how deterministic the tool inside it is.
        allow_cache_upload = buildroot_cache_upload(ctx),
        local_only = buildroot_local_only(ctx),
    )

    return [DefaultInfo(default_output = out)]

squashfs = rule(
    impl = _squashfs_impl,
    attrs = {
        # zstd rather than mksquashfs's default gzip: comparable ratio at a
        # much faster decompression, which on a live image is paid on every
        # single file read for the life of the boot.
        "compressor": attrs.string(default = "zstd"),
        # Paths dropped from the image, relative to the rootfs root.
        # Empty by default and deliberately so: the rootfs is the thing
        # the solve produced, and quietly deleting parts of it here would
        # mean the image no longer matches the package list that was
        # reviewed.  A caller that wants /var/cache gone can say so.
        "exclude": attrs.list(attrs.string(), default = []),
        # A pseudo-file produced by signing.ima_manifest.  Keeping signing
        # separate from compression lets production use an HSM-backed,
        # non-cacheable signer while this deterministic image action only
        # consumes public signature bytes.
        "ima_manifest": attrs.option(attrs.dep(), default = None),
        "rootfs": attrs.dep(),
        # Optional source archive for releases whose packaged mksquashfs is
        # too old to add per-file xattrs through a pseudo file.  The source
        # is compiled inside the target buildroot, so cross builds still run
        # a target-architecture binary rather than a host tool.
        "mksquashfs_source": attrs.option(attrs.dep(), default = None),
        # Write security.selinux into the image, computed from the image's
        # own policy.  Off by default because it is only meaningful for an
        # image that ships one: a tools tree or a minimal rootfs has no
        # policy to consult, and the driver hard-fails rather than
        # producing an unlabelled image that claims to be labelled.
        #
        # For anything that boots, this is the difference between an image
        # that runs SELinux and one that needs selinux=0 on the kernel
        # command line -- see the block comment in tools/squashfs_build.py.
        "selinux_relabel": attrs.bool(default = False),
        "_build": attrs.default_only(
            attrs.exec_dep(default = "//tools:squashfs_build"),
        ),
    } | BUILDROOT_ATTRS,
)

def _os_release_value(value):
    return '"{}"'.format(value.replace("\\", "\\\\").replace('"', '\\"'))

def _uki_image_impl(ctx: AnalysisContext) -> list[Provider]:
    boot = ctx.attrs.kernel[BootInfo]
    if boot.architecture != ctx.attrs.architecture:
        fail("kernel {} is {}, but UKI {} is {}".format(
            ctx.attrs.kernel.label,
            boot.architecture,
            ctx.attrs.name,
            ctx.attrs.architecture,
        ))

    metadata = ctx.attrs.rootfs[RootfsInfo]
    if metadata.architecture != ctx.attrs.architecture:
        fail("rootfs {} is {}, but UKI {} is {}".format(
            ctx.attrs.rootfs.label,
            metadata.architecture,
            ctx.attrs.name,
            ctx.attrs.architecture,
        ))

    buildroot_architecture = buildroot_info(ctx).target_cpu
    if buildroot_architecture and buildroot_architecture != ctx.attrs.architecture:
        fail("UKI {} is {}, but its buildroot is {}".format(
            ctx.attrs.name,
            ctx.attrs.architecture,
            buildroot_architecture,
        ))

    stub = ctx.attrs.efi_stub if ctx.attrs.efi_stub != None else boot.efi_stub
    if stub == None:
        fail(
            "UKI {} needs an efi_stub attribute or a kernel that provides one".format(
                ctx.attrs.name,
            ),
        )

    os_release = ctx.actions.write(
        ctx.attrs.name + ".os-release",
        "ID={}\nVERSION_ID={}\n".format(
            _os_release_value(metadata.flavor),
            _os_release_value(metadata.release),
        ),
    )
    root_args = _LIVE_ROOT_ARGS[ctx.attrs.layout].format(
        label = ctx.attrs.volume_label.upper(),
    )
    command_line = "{} {} {}".format(
        root_args,
        ctx.attrs.kernel_args,
        " ".join(boot.boot_args),
    ).strip()
    out = ctx.actions.declare_output(ctx.attrs.name + ".efi")
    command = cmd_args(
        ctx.attrs._assemble[RunInfo],
        "--stub",
        stub,
        "--linux",
        boot.vmlinuz,
        "--initrd",
        _single_output(ctx.attrs.initramfs, "initramfs image"),
        "--osrel",
        os_release,
        "--uname",
        boot.kver,
        "--cmdline",
        command_line,
        "--architecture",
        ctx.attrs.architecture,
        "--source-date-epoch",
        ctx.attrs.source_date_epoch,
        "--out",
        out.as_output(),
    )
    command.add(buildroot_sysroot_args(ctx))
    ctx.actions.run(
        command,
        category = "uki",
        identifier = ctx.attrs.name,
        allow_cache_upload = buildroot_cache_upload(ctx),
        local_only = buildroot_local_only(ctx),
    )
    return [
        DefaultInfo(default_output = out),
        EfiImageInfo(
            image = out,
            architecture = ctx.attrs.architecture,
            signed = False,
            signing_certificate = None,
        ),
    ]

uki_image = rule(
    impl = _uki_image_impl,
    attrs = {
        "architecture": attrs.enum(["x86_64", "aarch64"]),
        "efi_stub": attrs.option(attrs.source(), default = None),
        "initramfs": attrs.dep(),
        "kernel": attrs.dep(providers = [BootInfo]),
        "kernel_args": attrs.string(default = "quiet"),
        "layout": attrs.enum(["rpm", "debian", "ubuntu"], default = "rpm"),
        "rootfs": attrs.dep(providers = [RootfsInfo]),
        "source_date_epoch": attrs.string(default = "1700000000"),
        "volume_label": attrs.string(default = "BUCKOS"),
        "_assemble": attrs.default_only(
            attrs.exec_dep(default = "//tools:uki_assemble"),
        ),
    } | BUILDROOT_ATTRS,
)

def _iso_image_impl(ctx: AnalysisContext) -> list[Provider]:
    out = ctx.actions.declare_output(ctx.attrs.name + ".iso")
    if len(ctx.attrs.additional_kernels) != len(ctx.attrs.additional_initramfs):
        fail("additional_kernels and additional_initramfs must have equal length")

    # The kernel target carries BootInfo, which is how the kver comes along
    # without a second action to ask for it -- boot.bzl's whole reason for
    # splitting kernel_image out.
    boot = ctx.attrs.kernel[BootInfo]
    if boot.architecture != ctx.attrs.target_cpu:
        fail("kernel {} is {}, but ISO {} is {}".format(
            ctx.attrs.kernel.label,
            boot.architecture,
            ctx.attrs.name,
            ctx.attrs.target_cpu,
        ))
    kernel_args = "{} {}".format(
        ctx.attrs.kernel_args,
        " ".join(boot.boot_args),
    ).strip()
    secure_boot_image = None
    if ctx.attrs.secure_boot_image != None:
        secure_boot = ctx.attrs.secure_boot_image[EfiImageInfo]
        if not secure_boot.signed:
            fail("secure_boot_image must be signed")
        if secure_boot.architecture != ctx.attrs.target_cpu:
            fail("Secure Boot image {} is {}, but ISO {} is {}".format(
                ctx.attrs.secure_boot_image.label,
                secure_boot.architecture,
                ctx.attrs.name,
                ctx.attrs.target_cpu,
            ))
        if ctx.attrs.boot_mode == "bios":
            fail("secure_boot_image requires a UEFI-capable boot mode")
        if ctx.attrs.additional_kernels:
            fail("Secure Boot UKIs currently support one kernel per ISO")
        secure_boot_image = secure_boot.image

    cmd = cmd_args(
        ctx.attrs._build[RunInfo],
        "--kernel",
        boot.vmlinuz,
        "--kernel-version-file",
        boot.kver,
        "--initramfs",
        _single_output(ctx.attrs.initramfs, "initramfs image"),
        "--squashfs",
        _single_output(ctx.attrs.squashfs, "squashfs image"),
        "--out",
        out.as_output(),
        "--volume-label",
        ctx.attrs.volume_label,
        "--kernel-args",
        kernel_args,
        "--boot-mode",
        ctx.attrs.boot_mode,
        "--target-cpu",
        ctx.attrs.target_cpu,
        "--layout",
        ctx.attrs.layout,
    )
    if secure_boot_image != None:
        cmd.add("--uefi-boot-image", secure_boot_image)
    for index in range(len(ctx.attrs.additional_kernels)):
        additional_boot = ctx.attrs.additional_kernels[index][BootInfo]
        if additional_boot.architecture != boot.architecture:
            fail("all kernels in an ISO must have the same architecture")
        if additional_boot.boot_args != boot.boot_args:
            fail("all kernels in an ISO must declare the same boot_args")
        cmd.add(
            "--additional-kernel",
            additional_boot.vmlinuz,
            "--additional-kernel-version-file",
            additional_boot.kver,
            "--additional-initramfs",
            _single_output(
                ctx.attrs.additional_initramfs[index],
                "additional initramfs image",
            ),
        )
    cmd.add(buildroot_sysroot_args(ctx))

    ctx.actions.run(
        cmd,
        category = "iso",
        identifier = ctx.attrs.name,
        allow_cache_upload = buildroot_cache_upload(ctx),
        local_only = buildroot_local_only(ctx),
    )

    return [DefaultInfo(default_output = out)]

iso_image = rule(
    impl = _iso_image_impl,
    attrs = {
        "additional_initramfs": attrs.list(attrs.dep(), default = []),
        "additional_kernels": attrs.list(
            attrs.dep(providers = [BootInfo]),
            default = [],
        ),
        # hybrid is both BIOS and UEFI.  Worth the extra El Torito catalog
        # entry: a live image that boots on one and not the other looks
        # like broken hardware to whoever tries it.
        "boot_mode": attrs.string(default = "hybrid"),
        "initramfs": attrs.dep(),
        # Note this does NOT include root=live:CDLABEL=..., which the rule
        # derives from volume_label.  Taking both would let them disagree,
        # and when they disagree the initramfs waits forever for a device
        # that never appears -- with no error naming the mismatch.
        "kernel_args": attrs.string(default = "quiet"),
        "layout": attrs.enum(["rpm", "debian", "ubuntu"], default = "rpm"),
        "kernel": attrs.dep(providers = [BootInfo]),
        "secure_boot_image": attrs.option(
            attrs.dep(providers = [EfiImageInfo]),
            default = None,
        ),
        "squashfs": attrs.dep(),
        "target_cpu": attrs.enum(["x86_64", "aarch64"], default = "x86_64"),
        "volume_label": attrs.string(default = "BUCKOS"),
        "_build": attrs.default_only(
            attrs.exec_dep(default = "//tools:iso_build"),
        ),
    } | BUILDROOT_ATTRS,
)
