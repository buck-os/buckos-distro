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

Both run inside an image-tools buildroot rather than against host
binaries.  mksquashfs and xorriso are build inputs like any other: an ISO
built by whatever xorriso the machine happened to have is not
reproducible, and on a machine that has none it is not buildable at all.
Being a seeded buildroot also makes it hermetic, which is what lets these
actions run on RE and populate the shared cache -- see
defs/buildroot_helpers.bzl.

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
        "rootfs": attrs.dep(),
        "_build": attrs.default_only(
            attrs.exec_dep(default = "//tools:squashfs_build"),
        ),
    } | BUILDROOT_ATTRS,
)

def _iso_image_impl(ctx: AnalysisContext) -> list[Provider]:
    out = ctx.actions.declare_output(ctx.attrs.name + ".iso")

    # The kernel target carries BootInfo, which is how the kver comes along
    # without a second action to ask for it -- boot.bzl's whole reason for
    # splitting kernel_image out.
    boot = ctx.attrs.kernel[BootInfo]

    cmd = cmd_args(
        ctx.attrs._build[RunInfo],
        "--kernel",
        boot.vmlinuz,
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
        "kernel": attrs.dep(providers = [BootInfo]),
        "squashfs": attrs.dep(),
        "volume_label": attrs.string(default = "BUCKOS"),
        "_build": attrs.default_only(
            attrs.exec_dep(default = "//tools:iso_build"),
        ),
    } | BUILDROOT_ATTRS,
)
