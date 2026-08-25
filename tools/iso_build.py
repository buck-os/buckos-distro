#!/usr/bin/env python3
"""Lay out live media and call the target's xorriso over it.

The kernel, the initramfs and the squashfs are already built; this is the
cheap half that arranges them and stamps a bootloader on the front.  Why
that is a separate rule at all is defs/rules/image.bzl's opening argument:
changing a kernel argument should not recompress a root filesystem.

Everything runs inside the image-tools buildroot rather than against host
binaries.  Not purity -- capability.  xorriso, mkfs.vfat, mcopy and
grub2-mkimage are simply not present on many build machines, and where
they are present they are a different vintage than the distro being built.
The grub EFI binary in particular is assembled from the *target's* grub
modules, so it is a Fedora 43 grub booting a Fedora 43 kernel.

One trip into the sandbox, not three.  Unlike the initramfs and squashfs
builds there is no rootfs to unpack, so nothing here creates a file owned
by a subordinate id: every path is written by this process as the
namespace's root, which is the caller's own uid on disk, and the work area
is removable from outside afterwards.

The layout, which is Fedora's and is not arbitrary -- dracut's
dmsquash-live module and the two firmware paths all look for exact names:

    /LiveOS/squashfs.img        the root filesystem
    /isolinux/vmlinuz           kernel, shared by both boot paths
    /isolinux/initrd.img        initramfs, likewise
    /isolinux/isolinux.bin      BIOS stage 1, El Torito default entry
    /isolinux/ldlinux.c32       isolinux's own loader, mandatory since 5.x
    /isolinux/isolinux.cfg      BIOS boot config
    /EFI/BOOT/BOOTX64.EFI       UEFI stage 1
    /EFI/BOOT/grub.cfg          UEFI boot config
    /images/efiboot.img         FAT image holding the two above, for
                                El Torito's alternate entry

/EFI/BOOT exists twice on purpose, and it took a boot failure to learn
why.  Firmware does not read the ISO9660 filesystem to find a loader; it
reads the FAT image named by the El Torito alternate entry, so
BOOTX64.EFI and grub.cfg have to be *inside* efiboot.img.  The copies on
the ISO proper are for the other case -- an ISO written to a USB stick and
booted as a disk, where firmware mounts the ISO9660 tree directly.  Ship
one and not the other and the image boots in exactly one of the two ways
someone will try.

The kernel command line is built here rather than passed in whole, so
root=live:CDLABEL= cannot disagree with the volume id.  When they disagree
the initramfs waits for a device that never appears, and says nothing
about why.
"""

import argparse
import os
import shlex
import shutil
import sys

from _isolation import ISOLATION_MODES, resolve_isolation, run_isolated
from _rpm import make_dirs_writable, reproducible_env, scratch_dir

_ISO_ROOT = "iso"
_OUT = "out.iso"

# grub modules baked into the EFI binary.  Taken from lorax's x86 template
# rather than assembled by reasoning: a grub that is missing a module does
# not report a missing module, it drops to a rescue prompt with a message
# about an unknown filesystem, and working out which of forty candidates
# was the missing one is a bad afternoon.
#
# The list is filtered against what the buildroot actually ships before it
# is used, because a module named here and absent there is a hard
# grub2-mkimage error -- and which modules exist moves between releases.
_GRUB_MODULES = (
    "all_video blscfg boot btrfs cat configfile chain echo efi_gop efi_uga "
    "efifwsetup ext2 fat font gcry_rijndael gcry_rsa gcry_serpent "
    "gcry_sha256 gcry_twofish gcry_whirlpool gfxmenu gfxterm gzio halt "
    "hfsplus iso9660 jpeg linux loadenv loopback lvm luks luks2 mdraid09 "
    "mdraid1x minicmd normal part_apple part_gpt part_msdos password_pbkdf2 "
    "png reboot regexp search search_fs_file search_fs_uuid search_label "
    "serial sleep syslinuxcfg test video xfs zstd"
).split()

_GRUB_MODULE_DIR = "/usr/lib/grub/x86_64-efi"

# isolinux's loader modules.  isolinux.bin refuses to start without
# ldlinux.c32 -- silently, with a blinking cursor -- so it is required
# rather than best-effort; the rest are only needed by menu.c32 and are
# copied when present so a richer config stays a one-line change.
_SYSLINUX_DIR = "/usr/share/syslinux"
_SYSLINUX_REQUIRED = ("isolinux.bin", "ldlinux.c32")
_SYSLINUX_OPTIONAL = ("libcom32.c32", "libutil.c32", "menu.c32",
                      "vesamenu.c32")

# The MBR xorriso stamps on the front so the same file works written raw
# to a USB stick.  Optional: without it the ISO still boots from optical
# media and from UEFI, it just is not `dd`-able for BIOS.
_ISOHDPFX = "/usr/share/syslinux/isohdpfx.bin"


def _isolinux_cfg(kernel_args, timeout_deciseconds):
    """BIOS boot config.

    `prompt 0` with a timeout, rather than a menu: menu.c32 pulls in two
    more modules and a font, and the thing this image needs to prove is
    that it boots, not that it has a nice menu.
    """
    return "\n".join([
        "default linux",
        "prompt 0",
        "timeout {}".format(timeout_deciseconds),
        "",
        "label linux",
        "  kernel /isolinux/vmlinuz",
        "  append initrd=/isolinux/initrd.img {}".format(kernel_args),
        "",
    ])


def _grub_cfg(label, kernel_args, timeout_seconds):
    """UEFI boot config.

    The `search` is load-bearing.  grub is loaded from efiboot.img, so its
    $root starts out as that few-megabyte FAT image -- which contains
    neither the kernel nor the squashfs.  Without re-rooting onto the
    ISO9660 filesystem by label, `linux /isolinux/vmlinuz` is a file-not-
    found and the boot stops at a grub prompt.
    """
    return "\n".join([
        "set default=0",
        "set timeout={}".format(timeout_seconds),
        "",
        "search --no-floppy --set=root -l {}".format(shlex.quote(label)),
        "",
        "menuentry {} {{".format(shlex.quote("Start " + label)),
        "    linux /isolinux/vmlinuz {}".format(kernel_args),
        "    initrd /isolinux/initrd.img",
        "}",
        "",
    ])


def _stage(src, dest):
    """Hardlink an input into the work area, copying if that is refused.

    Same reasoning as stage_rootfs in tools/initramfs_build.py: Buck's
    output paths are not mounted inside the chroot, only the work area is,
    so an input has to be moved under it before the sandbox can see it.
    """
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        os.link(src, dest)
    except OSError:
        shutil.copy2(src, dest)


def _efi_script(iso_root, label):
    """Build BOOTX64.EFI, then the FAT image the firmware actually reads."""
    efi_dir = os.path.join(iso_root, "EFI", "BOOT")
    images = os.path.join(iso_root, "images")
    efiboot = os.path.join(images, "efiboot.img")

    return "\n".join([
        "set -e",
        "EFIDIR={}".format(shlex.quote(efi_dir)),
        "IMAGES={}".format(shlex.quote(images)),
        "EFIBOOT={}".format(shlex.quote(efiboot)),
        "MODDIR={}".format(shlex.quote(_GRUB_MODULE_DIR)),
        'mkdir -p "$EFIDIR" "$IMAGES"',
        "",
        'if [ ! -d "$MODDIR" ]; then',
        '  echo "buckos-distro: no grub modules at $MODDIR; the image-tools'
        ' set needs grub2-efi-x64-modules" >&2',
        "  exit 1",
        "fi",
        "",
        # Filtered rather than passed straight through: see _GRUB_MODULES.
        "MODS=",
        "for m in {}; do".format(" ".join(_GRUB_MODULES)),
        '  if [ -f "$MODDIR/$m.mod" ]; then MODS="$MODS $m"; fi',
        "done",
        "",
        # -p /EFI/BOOT is the prefix grub looks for grub.cfg under, and it
        # is resolved against whatever $root is at startup -- the FAT
        # image.  That is why grub.cfg is copied into efiboot.img below
        # and not merely onto the ISO.
        'grub2-mkimage -O x86_64-efi -d "$MODDIR" -p /EFI/BOOT'
        ' -o "$EFIDIR/BOOTX64.EFI" $MODS',
        'test -s "$EFIDIR/BOOTX64.EFI"',
        "",
        # Sized from the payload with generous slack, then floored at 8
        # MiB.  The floor is not padding for its own sake: mkfs.vfat picks
        # FAT12 below roughly 4 MB, and while UEFI tolerates FAT12 on
        # removable media, FAT16 is what every firmware is actually tested
        # against.  -s 1 keeps the cluster count high enough that mkfs
        # does not quietly fall back.
        'NEED=$(du -sk "$EFIDIR" | cut -f1)',
        'SIZE=$(( (NEED * 2) + 2048 ))',
        'if [ "$SIZE" -lt 8192 ]; then SIZE=8192; fi',
        'rm -f "$EFIBOOT"',
        'dd if=/dev/zero of="$EFIBOOT" bs=1024 count="$SIZE" status=none',
        'mkfs.vfat -F 16 -s 1 -n {} "$EFIBOOT" >/dev/null'.format(
            # FAT labels are 11 characters, uppercase, and mkfs.vfat warns
            # and truncates rather than failing.  Fixed rather than
            # derived from the volume id for that reason: nothing reads
            # this label, so a stable one is better than a mangled one.
            shlex.quote("EFIBOOT")
        ),
        'mmd -i "$EFIBOOT" ::/EFI ::/EFI/BOOT',
        'mcopy -i "$EFIBOOT" -s "$EFIDIR"/* ::/EFI/BOOT/',
        'test -s "$EFIBOOT"',
    ])


def _bios_script(iso_root):
    """Copy isolinux's loader out of the buildroot."""
    dest = os.path.join(iso_root, "isolinux")
    lines = [
        "set -e",
        "DEST={}".format(shlex.quote(dest)),
        "SRC={}".format(shlex.quote(_SYSLINUX_DIR)),
        'mkdir -p "$DEST"',
        'if [ ! -d "$SRC" ]; then',
        '  echo "buckos-distro: no syslinux at $SRC; the image-tools set'
        ' needs syslinux and syslinux-nonlinux" >&2',
        "  exit 1",
        "fi",
    ]
    for name in _SYSLINUX_REQUIRED:
        lines += [
            'if [ ! -f "$SRC/{0}" ]; then'.format(name),
            '  echo "buckos-distro: $SRC/{0} missing" >&2'.format(name),
            "  exit 1",
            "fi",
            'cp "$SRC/{0}" "$DEST/{0}"'.format(name),
        ]
    for name in _SYSLINUX_OPTIONAL:
        lines.append(
            'if [ -f "$SRC/{0}" ]; then cp "$SRC/{0}" "$DEST/{0}"; fi'.format(
                name
            )
        )
    # isolinux.bin is patched in place by -boot-info-table, so it has to
    # be writable; cp out of a read-only buildroot preserves 0444.
    lines.append('chmod u+w "$DEST/isolinux.bin"')
    return "\n".join(lines)


def _xorriso_script(args, iso_root, out, timestamp):
    """The mkisofs emulation, with whichever El Torito entries apply."""
    cmd = [
        "xorriso", "-as", "mkisofs",
        "-o", out,
        "-V", args.volume_label,
        # Rock Ridge with ownership rationalised to root, and Joliet for
        # anything that reads the ISO on Windows.  -rational-rock matters
        # for reproducibility: without it the uid of whoever ran the build
        # ends up in the filesystem.
        "-rational-rock", "-joliet", "-joliet-long",
        # Otherwise xorriso stamps "now" into the volume descriptors and
        # two identical inputs produce two different ISOs.
        "--modification-date={}".format(timestamp),
    ]

    if args.boot_mode in ("hybrid", "bios"):
        cmd += [
            "-b", "isolinux/isolinux.bin",
            "-c", "isolinux/boot.cat",
            "-no-emul-boot",
            "-boot-load-size", "4",
            # Patches isolinux.bin with the LBA of the ISO's own image, so
            # it can find the rest of itself.
            "-boot-info-table",
        ]

    if args.boot_mode in ("hybrid", "uefi"):
        if args.boot_mode == "hybrid":
            cmd.append("-eltorito-alt-boot")
        cmd += ["-e", "images/efiboot.img", "-no-emul-boot"]

    # Accumulated in the positional parameters rather than in a string.
    # The string version of this had a bug worth remembering: `ARGS=` plus
    # an unquoted command line is not an assignment of the whole line, it
    # is an assignment of the first word with the rest run as a command --
    # so the shell set ARGS=xorriso and tried to execute `-as`, and the
    # build failed with a bare 127 that read like a missing xorriso.
    # Quoting the value would work but then needs a second layer of
    # escaping to survive `eval`.  `set --` takes the words directly, so
    # there is no eval and no second layer.
    lines = [
        "set -e",
        "set -- " + " ".join(shlex.quote(part) for part in cmd),
    ]

    # isohybrid is what makes the ISO also work written raw to a USB
    # stick.  Both halves are conditional on their file existing rather
    # than assumed: a buildroot without syslinux-nonlinux still produces a
    # perfectly good optical image, and failing over a missing MBR would
    # be a worse trade than losing `dd`-ability.
    if args.boot_mode in ("hybrid", "bios"):
        lines += [
            'if [ -f {0} ]; then set -- "$@" -isohybrid-mbr {0}; fi'.format(
                shlex.quote(_ISOHDPFX)
            ),
        ]
    if args.boot_mode in ("hybrid", "uefi"):
        lines.append('set -- "$@" -isohybrid-gpt-basdat')

    lines.append('exec "$@" {}'.format(shlex.quote(iso_root)))
    return "\n".join(lines)


def _iso_timestamp(epoch):
    """xorriso's --modification-date format: YYYYMMDDhhmmsscc, UTC."""
    import time

    return time.strftime("%Y%m%d%H%M%S00", time.gmtime(int(epoch)))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--kernel", required=True, help="vmlinuz to boot")
    ap.add_argument("--initramfs", required=True, help="initramfs image")
    ap.add_argument("--squashfs", required=True, help="root filesystem image")
    ap.add_argument("--out", required=True, help="ISO to write")
    ap.add_argument("--buildroot-tree", default=None,
                    help="tree providing xorriso, grub2-mkimage and mtools")
    ap.add_argument("--isolation", default="auto", choices=ISOLATION_MODES)
    ap.add_argument("--volume-label", default="BUCKOS",
                    help="ISO9660 volume id; also the CDLABEL the initramfs "
                         "searches for")
    ap.add_argument("--kernel-args", default="quiet",
                    help="appended after the derived root= argument")
    ap.add_argument("--boot-mode", default="hybrid",
                    choices=("hybrid", "bios", "uefi"))
    ap.add_argument("--timeout", type=int, default=5,
                    help="bootloader countdown in seconds")
    ap.add_argument("--work", default=None,
                    help="scratch directory; a temp dir is used if omitted")
    ap.add_argument("--keep-work", action="store_true",
                    help="do not delete the scratch area, for debugging")
    ap.add_argument("--source-date-epoch", default="1700000000")
    args = ap.parse_args()

    isolation = resolve_isolation(args.isolation)
    if isolation == "none":
        sys.exit(
            "iso needs a sandbox: xorriso, grub2-mkimage and mtools come "
            "from the image-tools buildroot, and isolation=none would run "
            "whatever the host happens to have instead"
        )
    if not args.buildroot_tree:
        sys.exit(
            "--buildroot-tree is required: there is nowhere to find "
            "xorriso otherwise"
        )

    # Uppercased because ISO9660 stores volume ids uppercase regardless.
    # Doing it here rather than trusting the caller means the CDLABEL
    # written into the kernel command line is the string that will
    # actually be on the medium -- a lowercase label would be written
    # uppercase and then never match at boot.
    label = args.volume_label.upper()
    args.volume_label = label

    if args.work:
        work = os.path.abspath(args.work)
        shutil.rmtree(work, ignore_errors=True)
        os.makedirs(work, exist_ok=True)
    else:
        work = scratch_dir("buckos-distro-iso-")

    out = os.path.abspath(args.out)
    try:
        _build(args, isolation, label, work, out)
    finally:
        if not args.keep_work and not args.work:
            shutil.rmtree(work, ignore_errors=True)


def _build(args, isolation, label, work, out):
    iso_root = os.path.join(work, _ISO_ROOT)
    image = os.path.join(work, _OUT)

    sysroot = os.path.join(work, "sysroot")
    shutil.copytree(args.buildroot_tree, sysroot, symlinks=True,
                    dirs_exist_ok=True)
    make_dirs_writable(sysroot)

    env = reproducible_env(source_date_epoch=args.source_date_epoch)

    # The three big inputs, plus the two configs.  Written from out here
    # rather than inside the sandbox because nothing about them needs the
    # target's tools -- they are bytes Buck already produced and two text
    # files.
    _stage(os.path.abspath(args.kernel),
           os.path.join(iso_root, "isolinux", "vmlinuz"))
    _stage(os.path.abspath(args.initramfs),
           os.path.join(iso_root, "isolinux", "initrd.img"))
    _stage(os.path.abspath(args.squashfs),
           os.path.join(iso_root, "LiveOS", "squashfs.img"))

    kernel_args = "root=live:CDLABEL={} {}".format(
        label, args.kernel_args
    ).strip()

    _write(os.path.join(iso_root, "isolinux", "isolinux.cfg"),
           _isolinux_cfg(kernel_args, args.timeout * 10))
    _write(os.path.join(iso_root, "EFI", "BOOT", "grub.cfg"),
           _grub_cfg(label, kernel_args, args.timeout))

    print(
        "buckos-distro: assembling {} ({}), cmdline: {}".format(
            label, args.boot_mode, kernel_args
        ),
        file=sys.stderr,
        flush=True,
    )

    if args.boot_mode in ("hybrid", "bios"):
        run_isolated(["/bin/sh", "-c", _bios_script(iso_root)],
                     isolation, work, work, sysroot, env=env)
    if args.boot_mode in ("hybrid", "uefi"):
        run_isolated(["/bin/sh", "-c", _efi_script(iso_root, label)],
                     isolation, work, work, sysroot, env=env)

    timestamp = _iso_timestamp(args.source_date_epoch)
    run_isolated(
        ["/bin/sh", "-c", _xorriso_script(args, iso_root, image, timestamp)],
        isolation, work, work, sysroot, env=env,
    )

    if not os.path.isfile(image):
        sys.exit("xorriso produced no image at {}".format(image))

    os.makedirs(os.path.dirname(out), exist_ok=True)
    try:
        os.rename(image, out)
    except OSError:
        shutil.move(image, out)

    print(
        "buckos-distro: iso -> {} ({} bytes)".format(
            os.path.basename(out), os.path.getsize(out)
        ),
        file=sys.stderr,
    )


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)


if __name__ == "__main__":
    main()
