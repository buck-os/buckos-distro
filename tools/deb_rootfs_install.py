#!/usr/bin/env python3
"""Install a pinned Debian-family package set into a bootable rootfs."""

import argparse
import os
import shlex
import shutil
import sys

from _isolation import (
    ISOLATION_MODES,
    require_target_execution,
    resolve_isolation,
    run_isolated,
)
from _rpm import make_dirs_writable, reproducible_env, scratch_dir


def collect_debs(paths):
    found = []
    for path in paths:
        if os.path.isdir(path):
            for root, _dirs, names in os.walk(path):
                found.extend(
                    os.path.join(root, name)
                    for name in names
                    if name.endswith((".deb", ".udeb"))
                )
        elif os.path.isfile(path):
            found.append(path)
        else:
            sys.exit("no such deb or directory of debs: {}".format(path))
    if not found:
        sys.exit("no debs to install")
    return sorted(set(found))


def stage_debs(debs, destination):
    os.makedirs(destination, exist_ok=True)
    for index, source in enumerate(debs):
        destination_path = os.path.join(destination, "{:05d}.deb".format(index))
        try:
            os.link(source, destination_path)
        except OSError:
            shutil.copy2(source, destination_path)


def normalize_merged_usr_script(target):
    """Restore top-level merged-/usr links after raw archive extraction.

    Some packages still carry paths such as /lib/modules.  Extracting their
    data archives directly can replace base-files' /lib -> usr/lib symlink
    with a directory, which prevents an emulated target binary from finding
    its dynamic loader before dpkg has had a chance to run.
    """
    return "\n".join([
        "set -e",
        "root={}".format(shlex.quote(target.rstrip("/") or "/")),
        "for directory in bin sbin lib lib64; do",
        "  path=\"${root%/}/$directory\"",
        "  usr=\"${root%/}/usr/$directory\"",
        "  if [ -d \"$path\" ] && [ ! -L \"$path\" ]; then",
        "    mkdir -p \"$usr\"",
        "    cp -a \"$path/.\" \"$usr/\"",
        "    rm -rf \"$path\"",
        "    ln -s \"usr/$directory\" \"$path\"",
        "  elif [ ! -e \"$path\" ] && [ -d \"$usr\" ]; then",
        "    ln -s \"usr/$directory\" \"$path\"",
        "  fi",
        "done",
    ])


def bootstrap_script(target, staging):
    quoted_target = shlex.quote(target)
    policy = os.path.join(target, "usr", "sbin", "policy-rc.d")
    return "\n".join([
        "set -e",
        "mkdir -p {0}/root {0}/var/lib/dpkg {0}/var/log {0}/run {0}/tmp {0}/usr/sbin".format(quoted_target),
        "for package in {}/*.deb; do dpkg-deb --extract \"$package\" {}; done".format(
            shlex.quote(staging),
            quoted_target,
        ),
        normalize_merged_usr_script(target),
        ": > {}/var/lib/dpkg/status".format(quoted_target),
        "printf '#!/bin/sh\\nexit 101\\n' > {}".format(shlex.quote(policy)),
        "chmod 0755 {}".format(shlex.quote(policy)),
        "test -x {}/usr/bin/dpkg".format(quoted_target),
        "test -x {}/bin/sh".format(quoted_target),
    ])


def transaction_script(staging):
    return "\n".join([
        "set -e",
        "export DEBIAN_FRONTEND=noninteractive",
        "export HOME=/root",
        "export RUNLEVEL=1",
        "export TMPDIR=/tmp",
        "dpkg --force-depends --force-confnew --unpack {}/*.deb".format(
            shlex.quote(staging),
        ),
        "if [ -x /usr/sbin/update-initramfs ]; then",
        "  mv /usr/sbin/update-initramfs /usr/sbin/update-initramfs.buckos-real",
        "  printf '#!/bin/sh\\nexit 0\\n' > /usr/sbin/update-initramfs",
        "  chmod 0755 /usr/sbin/update-initramfs",
        "fi",
        "dpkg --force-confnew --configure -a",
        "if [ -e /usr/sbin/update-initramfs.buckos-real ]; then",
        "  mv -f /usr/sbin/update-initramfs.buckos-real /usr/sbin/update-initramfs",
        "fi",
        "rm -f /usr/sbin/policy-rc.d",
        "rm -f /boot/initrd.img-* /etc/ssh/ssh_host_* /var/lib/systemd/random-seed",
        ": > /etc/machine-id",
        "test -x /usr/lib/systemd/systemd",
    ])


def archive_script(target, tarball, source_date_epoch):
    quoted_target = shlex.quote(target)
    return "\n".join([
        "set -e",
        "tar --create --numeric-owner --sort=name --xattrs --xattrs-include='*'"
        " --acls --format=posix --mtime=@{epoch} --file {tarball}"
        " --directory {target} .".format(
            epoch=shlex.quote(source_date_epoch),
            tarball=shlex.quote(tarball),
            target=quoted_target,
        ),
    ])


def cleanup_script(target):
    """Remove the target tree from the namespace that owns its files."""
    return "set -e\nrm -rf {}".format(shlex.quote(target))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--deb", action="append", default=[])
    parser.add_argument("--buildroot-tree", required=True)
    parser.add_argument("--isolation", default="auto", choices=ISOLATION_MODES)
    parser.add_argument("--target-cpu", default="x86_64")
    parser.add_argument("--source-date-epoch", default="1700000000")
    parser.add_argument("--work", default=None)
    parser.add_argument("--keep-work", action="store_true")
    args = parser.parse_args()

    require_target_execution(args.target_cpu)
    isolation = resolve_isolation(args.isolation)
    if isolation == "none":
        sys.exit("Debian-family rootfs assembly requires an isolated binary-seed buildroot")

    debs = collect_debs(args.deb)
    work = os.path.abspath(args.work) if args.work else scratch_dir("buckos-distro-deb-rootfs-")
    if args.work:
        shutil.rmtree(work, ignore_errors=True)
        os.makedirs(work)
    target = os.path.join(work, "rootfs")
    staging = os.path.join(work, "debs")
    tarball = os.path.join(work, "rootfs.tar")
    sysroot = os.path.join(work, "sysroot")
    os.makedirs(target)
    stage_debs(debs, staging)
    shutil.copytree(args.buildroot_tree, sysroot, symlinks=True, dirs_exist_ok=True)
    make_dirs_writable(sysroot)

    env = reproducible_env(source_date_epoch=args.source_date_epoch)
    try:
        try:
            run_isolated(
                ["/bin/sh", "-c", bootstrap_script(target, staging)],
                isolation,
                work,
                work,
                sysroot,
                env=env,
            )
            run_isolated(
                ["/bin/sh", "-c", transaction_script(staging)],
                isolation,
                work,
                work,
                target,
                env=env,
            )
            run_isolated(
                ["/bin/sh", "-c", normalize_merged_usr_script(target)],
                isolation,
                work,
                work,
                sysroot,
                env=env,
            )
            run_isolated(
                ["/bin/sh", "-c", archive_script(
                    target,
                    tarball,
                    args.source_date_epoch,
                )],
                isolation,
                work,
                work,
                sysroot,
                env=env,
            )
        finally:
            run_isolated(
                ["/bin/sh", "-c", cleanup_script(target)],
                isolation,
                work,
                work,
                sysroot,
                env=env,
            )
        if not os.path.isfile(tarball):
            sys.exit("dpkg transaction produced no rootfs archive")
        out = os.path.abspath(args.out)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        try:
            os.rename(tarball, out)
        except OSError:
            shutil.move(tarball, out)
    finally:
        if not args.keep_work and not args.work:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
