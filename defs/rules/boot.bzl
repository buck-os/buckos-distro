"""Turning a rootfs into something a bootloader can load.

Two rules, and the split between them is the point.

  kernel_image  reads the rootfs tarball's index and copies one member
                out.  No privilege, no namespace, no distro tooling -- so
                it is a cheap, cacheable, RE-eligible action.

  initramfs     unpacks the whole image and runs its own dracut inside it.
                Expensive, and needs a sandbox with the image as /.

Fusing them would be less code and worse: everything downstream needs the
kernel version, and asking for it would then re-run dracut.  Splitting on
"what does this actually cost" is the same reason srpm_unpack is not part
of the replay action.

Neither rule is where the initramfs would have happened by default.
kernel-core's %posttrans runs kernel-install, which builds one inside the
rpm transaction; tools/rootfs_install.py bypasses that with
KERNEL_INSTALL_BYPASS=1, so the kernel stays at
/usr/lib/modules/<kver>/vmlinuz and the initramfs becomes a rule with its
own declared inputs. That is what makes it reviewable -- an initramfs built
by a scriptlet has inputs nothing can see.
"""

load(
    "//defs:buildroot_helpers.bzl",
    "BUILDROOT_ATTRS",
    "buildroot_cache_upload",
    "buildroot_local_only",
    "buildroot_sysroot_args",
)
load("//defs:providers.bzl", "BootInfo")

def _rootfs_artifact(dep):
    """The tarball out of a rootfs target.

    rootfs targets carry no provider of their own -- deliberately, so
    buckos-build's image rules, which take attrs.dep() and read
    DefaultInfo, still accept them directly.
    """
    outputs = dep[DefaultInfo].default_outputs
    if not outputs:
        fail("{} produces no output to read a kernel from".format(dep.label))
    return outputs[0]

def _kernel_image_impl(ctx: AnalysisContext) -> list[Provider]:
    vmlinuz = ctx.actions.declare_output("vmlinuz")
    kver = ctx.actions.declare_output("kver.txt")
    rootfs = _rootfs_artifact(ctx.attrs.rootfs)

    cmd = cmd_args(
        ctx.attrs._extract[RunInfo],
        "--rootfs",
        rootfs,
        "--out",
        vmlinuz.as_output(),
        "--out-kver",
        kver.as_output(),
    )
    if ctx.attrs.kver:
        cmd.add("--kver", ctx.attrs.kver)

    # re-contract: buildroot-independent.  This action reads a tar index
    # with Python's own tarfile and copies bytes; it runs no distro tool,
    # so the buildroot's provenance cannot change its output.  Nothing
    # host-specific can reach the result.
    ctx.actions.run(
        cmd,
        category = "kernel_extract",
        identifier = ctx.attrs.name,
        local_only = False,
        allow_cache_upload = True,
    )

    return [
        DefaultInfo(
            default_output = vmlinuz,
            # So `buck2 build //...[kver]` can print it without a rule that
            # exists only to expose one string.
            sub_targets = {"kver": [DefaultInfo(default_output = kver)]},
        ),
        BootInfo(vmlinuz = vmlinuz, initramfs = None, kver = kver),
    ]

kernel_image = rule(
    impl = _kernel_image_impl,
    attrs = {
        # Optional, and only meaningful for an image with more than one
        # kernel.  Left unset the tool refuses to guess, which is the right
        # failure: "which kernel boots" is a decision, not a detail.
        "kver": attrs.string(default = ""),
        "rootfs": attrs.dep(),
        "_extract": attrs.default_only(
            attrs.exec_dep(default = "//tools:kernel_extract"),
        ),
    },
)

def _initramfs_impl(ctx: AnalysisContext) -> list[Provider]:
    out = ctx.actions.declare_output(ctx.attrs.name + ".img")
    rootfs = _rootfs_artifact(ctx.attrs.rootfs)

    cmd = cmd_args(
        ctx.attrs._build[RunInfo],
        "--rootfs",
        rootfs,
        "--out",
        out.as_output(),
    )
    cmd.add(buildroot_sysroot_args(ctx))

    if ctx.attrs.kver:
        cmd.add("--kver", ctx.attrs.kver)
    for module in ctx.attrs.add_modules:
        cmd.add("--add-module", module)
    for module in ctx.attrs.omit_modules:
        cmd.add("--omit-module", module)
    for arg in ctx.attrs.dracut_args:
        cmd.add("--dracut-arg", arg)
    cmd.add("--source-date-epoch", ctx.attrs.source_date_epoch)

    ctx.actions.run(
        cmd,
        category = "initramfs",
        identifier = ctx.attrs.name,
        # dracut records a build id and, in some modules, timestamps, so
        # this is reproducible in content rather than bit-for-bit even with
        # --reproducible.  Same treatment as the rootfs: caching is keyed
        # on inputs, and provenance still governs whether the bytes may be
        # served to another machine.
        allow_cache_upload = buildroot_cache_upload(ctx),
        local_only = buildroot_local_only(ctx),
    )

    return [
        DefaultInfo(default_output = out),
        BootInfo(vmlinuz = None, initramfs = out, kver = None),
    ]

initramfs = rule(
    impl = _initramfs_impl,
    attrs = {
        # dracut modules to force in. For a live ISO this is where
        # dmsquash-live goes: without it the initramfs has no idea how to
        # find a squashfs on a CD and the boot stops at a dracut shell.
        "add_modules": attrs.list(attrs.string(), default = []),
        "dracut_args": attrs.list(attrs.string(), default = []),
        "kver": attrs.string(default = ""),
        "omit_modules": attrs.list(attrs.string(), default = []),
        "rootfs": attrs.dep(),
        "source_date_epoch": attrs.string(default = "1700000000"),
        "_build": attrs.default_only(
            attrs.exec_dep(default = "//tools:initramfs_build"),
        ),
    } | BUILDROOT_ATTRS,
)
