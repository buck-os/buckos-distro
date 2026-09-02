#!/usr/bin/env python3
"""Append a producer-neutral kernel bundle to a rootfs tar archive."""

import argparse
import io
import os
import shutil
import sys
import tarfile

from _kernel import certificate_der, read_kernel_release


def _metadata(info, mtime):
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = mtime
    return info


def _add_directory(archive, name, mtime, mode=0o755):
    info = tarfile.TarInfo(name.rstrip("/") + "/")
    info.type = tarfile.DIRTYPE
    info.mode = mode
    archive.addfile(_metadata(info, mtime))


def _add_file(archive, name, source, mtime, mode):
    with open(source, "rb") as stream:
        data = stream.read()
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    archive.addfile(_metadata(info, mtime), io.BytesIO(data))


def _add_tree(archive, source, mtime):
    for directory, dirnames, filenames in os.walk(source, topdown=True, followlinks=False):
        dirnames.sort()
        filenames.sort()
        relative = os.path.relpath(directory, source)
        if relative != ".":
            archive.add(directory, arcname=relative, recursive=False, filter=lambda info: _metadata(info, mtime))
        symlink_dirs = []
        for name in dirnames:
            path = os.path.join(directory, name)
            if os.path.islink(path):
                symlink_dirs.append(name)
                archive.add(path, arcname=os.path.relpath(path, source), recursive=False, filter=lambda info: _metadata(info, mtime))
        dirnames[:] = [name for name in dirnames if name not in symlink_dirs]
        for name in filenames:
            path = os.path.join(directory, name)
            archive.add(path, arcname=os.path.relpath(path, source), recursive=False, filter=lambda info: _metadata(info, mtime))


def compose_rootfs(
        rootfs,
        entries,
        output,
        source_date_epoch=1700000000,
        expected_ima_certificate=None):
    if not entries:
        raise ValueError("at least one kernel entry is required")
    releases = []
    expected_certificate = (
        certificate_der(expected_ima_certificate)
        if expected_ima_certificate else None
    )
    normalized = []
    for kernel, version_file, modules, config, system_map, ima_certificate in entries:
        release = read_kernel_release(version_file)
        if release in releases:
            raise ValueError("kernel release {!r} is configured twice".format(release))
        releases.append(release)
        if expected_certificate is not None:
            if not ima_certificate:
                raise ValueError("kernel {} declares no IMA certificate".format(release))
            if certificate_der(ima_certificate) != expected_certificate:
                raise ValueError("kernel {} trusts a different IMA certificate".format(release))
        normalized.append((release, kernel, modules, config, system_map))

    destination = os.path.abspath(output)
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    temporary = destination + ".tmp"
    shutil.copyfile(rootfs, temporary)
    try:
        with tarfile.open(temporary, "a") as archive:
            for release, kernel, modules, config, system_map in normalized:
                if modules:
                    expected = os.path.join(modules, "usr", "lib", "modules", release)
                    if not os.path.isdir(expected):
                        raise ValueError(
                            "normalized module tree has no usr/lib/modules/{}".format(release)
                        )
                    _add_tree(archive, modules, source_date_epoch)
                else:
                    for path in ("usr", "usr/lib", "usr/lib/modules", "usr/lib/modules/" + release):
                        _add_directory(archive, path, source_date_epoch)

                module_dir = "usr/lib/modules/" + release
                _add_file(archive, module_dir + "/vmlinuz", kernel, source_date_epoch, 0o644)
                if config:
                    _add_directory(archive, "boot", source_date_epoch)
                    _add_file(archive, module_dir + "/config", config, source_date_epoch, 0o644)
                    _add_file(archive, "boot/config-" + release, config, source_date_epoch, 0o644)
                if system_map:
                    _add_directory(archive, "boot", source_date_epoch)
                    _add_file(archive, module_dir + "/System.map", system_map, source_date_epoch, 0o644)
                    _add_file(archive, "boot/System.map-" + release, system_map, source_date_epoch, 0o644)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return releases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rootfs", required=True)
    parser.add_argument(
        "--entry",
        action="append",
        nargs=6,
        metavar=("KERNEL", "VERSION", "MODULES", "CONFIG", "SYSTEM_MAP", "IMA_CERT"),
        required=True,
    )
    parser.add_argument("--expected-ima-certificate")
    parser.add_argument("--source-date-epoch", type=int, default=1700000000)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    try:
        entries = [
            tuple(None if value == "-" else os.path.abspath(value) for value in entry)
            for entry in args.entry
        ]
        releases = compose_rootfs(
            os.path.abspath(args.rootfs),
            entries,
            os.path.abspath(args.out),
            args.source_date_epoch,
            os.path.abspath(args.expected_ima_certificate) if args.expected_ima_certificate else None,
        )
        print("installed custom kernels {} into rootfs".format(
            ", ".join(releases)
        ), file=sys.stderr)
    except (OSError, tarfile.TarError, ValueError) as error:
        print("kernel rootfs composition failed: {}".format(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
