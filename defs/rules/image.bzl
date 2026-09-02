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
    /EFI/BOOT/              UEFI boot (shim + grub)
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
    "buildroot_local_only",
    "buildroot_sysroot_args",
)
load("//defs:providers.bzl", "BootInfo")

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
        _single_output(ctx.attrs.rootfs, "rootfs tarball"),
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

def _iso_image_impl(ctx: AnalysisContext) -> list[Provider]:
    out = ctx.actions.declare_output(ctx.attrs.name + ".iso")
    if len(ctx.attrs.additional_kernels) != len(ctx.attrs.additional_initramfs):
        fail("additional_kernels and additional_initramfs must have equal length")

    # The kernel target carries BootInfo, which is how the kver comes along
    # without a second action to ask for it -- boot.bzl's whole reason for
    # splitting kernel_image out.
    boot = ctx.attrs.kernel[BootInfo]

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
        ctx.attrs.kernel_args,
        "--boot-mode",
        ctx.attrs.boot_mode,
        "--target-cpu",
        ctx.attrs.target_cpu,
        "--layout",
        ctx.attrs.layout,
    )
    for index in range(len(ctx.attrs.additional_kernels)):
        additional_boot = ctx.attrs.additional_kernels[index][BootInfo]
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
        "squashfs": attrs.dep(),
        "target_cpu": attrs.enum(["x86_64", "aarch64"], default = "x86_64"),
        "volume_label": attrs.string(default = "BUCKOS"),
        "_build": attrs.default_only(
            attrs.exec_dep(default = "//tools:iso_build"),
        ),
    } | BUILDROOT_ATTRS,
)
