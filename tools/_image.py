#!/usr/bin/env python3
"""Reading a rootfs tarball without unpacking it.

The rootfs is a tar for reasons defs/rules/rootfs.bzl explains at length
(subordinate-uid ownership Buck cannot delete, and a systemd unit whose
filename contains a literal backslash that buck2's path types cannot
represent).  Both reasons make *unpacking* it a privileged, namespaced
operation.

But the two facts the image pipeline needs most -- which kernel version is
in there, and where its vmlinuz lives -- are in the tar's index, not its
payload.  Reading them with `tarfile` costs one pass over the headers, no
namespace, no extraction, and nothing that can leave an undeletable file
behind.  So the rules that only need to *ask* about the rootfs do not pay
for a sandbox.

Deliberately not a general tar utility.  Everything here answers a
question the ISO pipeline actually asks.
"""

import tarfile

# Where kernel-core puts the kernel when kernel-install is bypassed.
#
# Not /boot.  kernel-core's %posttrans runs kernel-install, which is what
# would normally copy vmlinuz into /boot and build an initramfs beside it;
# tools/rootfs_install.py sets KERNEL_INSTALL_BYPASS=1 because that
# scriptlet cannot work inside the transaction and its output does not
# belong in the rootfs tarball anyway.  So the kernel stays where the rpm
# put it, which is here, and this is the only place worth looking.
_MODULES_DIR = "usr/lib/modules"
_KERNEL_NAME = "vmlinuz"


def _normalise(name):
    """Strip the leading `./` that tar writes for a relative archive."""
    if name.startswith("./"):
        return name[2:]
    return name


def find_kernels(tar_path):
    """Every (kver, member_name) pair in the archive, sorted by kver.

    Sorted so the result is deterministic rather than dependent on the
    order rpm happened to write the members, which is what an image's
    identity would otherwise rest on.
    """
    found = []
    with tarfile.open(tar_path, "r|*") as tar:
        # Streaming mode ("r|*"): a rootfs tar is hundreds of megabytes and
        # the seekable reader wants to build a full member list in memory.
        for member in tar:
            if not member.isfile():
                continue
            name = _normalise(member.name)
            parts = name.split("/")
            # usr/lib/modules/<kver>/vmlinuz -- exactly, so a stray
            # vmlinuz deeper in the tree is not mistaken for a kernel.
            if len(parts) != 5 or parts[-1] != _KERNEL_NAME:
                continue
            if "/".join(parts[:3]) != _MODULES_DIR:
                continue
            found.append((parts[3], member.name))
    return sorted(found)


def find_kernel(tar_path, kver=None):
    """The one kernel to build an image around, as (kver, member_name).

    Ambiguity is an error rather than a choice.  An image with two kernels
    is a legitimate thing to build, but *which one boots* is then a
    decision belonging to whoever assembles the bootloader config, not to
    whichever member tar happened to list first.  Naming it with --kver
    makes that decision visible in the target's own attributes.
    """
    kernels = find_kernels(tar_path)
    if not kernels:
        raise SystemExit(
            "{}: no kernel found under {}/<version>/{}. Either the image "
            "set has no kernel package in it, or kernel-install was not "
            "bypassed and the kernel went somewhere else.".format(
                tar_path, _MODULES_DIR, _KERNEL_NAME
            )
        )

    if kver is None:
        if len(kernels) > 1:
            raise SystemExit(
                "{}: {} kernels present ({}); pass --kver to say which one "
                "this image boots".format(
                    tar_path,
                    len(kernels),
                    ", ".join(version for version, _ in kernels),
                )
            )
        return kernels[0]

    for version, member in kernels:
        if version == kver:
            return version, member
    raise SystemExit(
        "{}: no kernel {!r}; the image has {}".format(
            tar_path, kver, ", ".join(version for version, _ in kernels)
        )
    )


def extract_member(tar_path, member_name, out_path):
    """Copy one member's bytes out to a plain file.

    A plain file on purpose: the caller gets something Buck can hash and
    delete, owned by whoever ran the action, with none of the ownership or
    filename problems that make the rootfs itself a tarball.
    """
    with tarfile.open(tar_path, "r|*") as tar:
        for member in tar:
            if member.name != member_name:
                continue
            source = tar.extractfile(member)
            if source is None:
                raise SystemExit(
                    "{}: {} is not a regular file".format(tar_path, member_name)
                )
            with open(out_path, "wb") as out:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
            return
    raise SystemExit("{}: no member {!r}".format(tar_path, member_name))
