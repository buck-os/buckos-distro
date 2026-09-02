#!/usr/bin/env python3
"""Normalize an arbitrary declared module output into a rootfs-shaped tree."""

import argparse
import os
import shutil
import stat
import sys
import tarfile

from _kernel import read_kernel_release


def _normal_name(name):
    while name.startswith("./"):
        name = name[2:]
    return name.rstrip("/")


def _safe_parts(name):
    parts = [part for part in _normal_name(name).split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ValueError("module archive path escapes its root: {!r}".format(name))
    return parts


def _directory_source(source, release, layout):
    candidates = [
        os.path.join(source, "usr", "lib", "modules", release),
        os.path.join(source, "lib", "modules", release),
    ]
    found = [path for path in candidates if os.path.isdir(path)]
    if layout == "version":
        return source
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        raise ValueError("module input contains both usr/lib and lib layouts")
    if layout == "auto" and (
        os.path.basename(os.path.normpath(source)) == release
        or os.path.exists(os.path.join(source, "modules.dep"))
    ):
        return source
    raise ValueError(
        "module input has no [usr/]lib/modules/{} directory".format(release)
    )


def _copy_directory(source, destination):
    shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=True)
    for name in ("build", "source"):
        path = os.path.join(destination, name)
        if os.path.lexists(path):
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.unlink(path)
    for directory, dirnames, filenames in os.walk(
        destination, topdown=True, followlinks=False
    ):
        for name in dirnames + filenames:
            path = os.path.join(directory, name)
            if os.path.islink(path):
                _safe_symlink_target(destination, path, os.readlink(path))


def _safe_symlink_target(destination, target, linkname):
    if os.path.isabs(linkname):
        raise ValueError("absolute module symlink is not portable: {!r}".format(linkname))
    resolved = os.path.realpath(os.path.join(os.path.dirname(target), linkname))
    root = os.path.realpath(destination)
    if resolved != root and not resolved.startswith(root + os.sep):
        raise ValueError("module symlink escapes its tree: {!r}".format(linkname))


def _tar_prefix(members, release, layout):
    names = [_normal_name(member.name) for member in members]
    prefixes = [
        "usr/lib/modules/" + release,
        "lib/modules/" + release,
    ]
    found = [
        prefix for prefix in prefixes
        if any(name == prefix or name.startswith(prefix + "/") for name in names)
    ]
    if layout == "version":
        return ""
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        raise ValueError("module archive contains both usr/lib and lib layouts")
    if layout == "auto":
        return ""
    raise ValueError(
        "module archive has no [usr/]lib/modules/{} directory".format(release)
    )


def _extract_tar(source, destination, release, layout):
    pending_links = []
    with tarfile.open(source) as archive:
        members = archive.getmembers()
        prefix = _tar_prefix(members, release, layout)
        selected = 0
        for member in members:
            name = _normal_name(member.name)
            if prefix:
                if name == prefix:
                    relative = ""
                elif name.startswith(prefix + "/"):
                    relative = name[len(prefix) + 1:]
                else:
                    continue
            else:
                relative = name
            parts = _safe_parts(relative)
            if not parts:
                continue
            if parts[0] in ("build", "source"):
                continue
            target = os.path.join(destination, *parts)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            selected += 1
            if member.isdir():
                os.makedirs(target, exist_ok=True)
            elif member.isfile():
                source_file = archive.extractfile(member)
                if source_file is None:
                    raise ValueError("cannot read module member {}".format(member.name))
                with open(target, "wb") as output:
                    shutil.copyfileobj(source_file, output)
            elif member.issym():
                _safe_symlink_target(destination, target, member.linkname)
                os.symlink(member.linkname, target)
            elif member.islnk():
                pending_links.append((target, member.linkname, prefix))
            else:
                raise ValueError("unsupported module archive entry {}".format(member.name))
            if not member.issym() and not member.islnk():
                os.chmod(target, stat.S_IMODE(member.mode))

        for target, linkname, link_prefix in pending_links:
            link = _normal_name(linkname)
            if link_prefix and link.startswith(link_prefix + "/"):
                link = link[len(link_prefix) + 1:]
            link_target = os.path.join(destination, *_safe_parts(link))
            if not os.path.isfile(link_target):
                raise ValueError("unresolved module hardlink {}".format(linkname))
            os.link(link_target, target)
    if not selected:
        raise ValueError("module archive selected no entries")


def normalize_modules(source, version_file, output, layout):
    release = read_kernel_release(version_file)
    destination = os.path.join(output, "usr", "lib", "modules", release)
    os.makedirs(destination, exist_ok=True)
    if os.path.isdir(source):
        _copy_directory(_directory_source(source, release, layout), destination)
    elif tarfile.is_tarfile(source):
        _extract_tar(source, destination, release, layout)
    else:
        raise ValueError("module input is neither a directory nor a tar archive")
    with os.scandir(destination) as entries:
        if not any(entries):
            raise ValueError("normalized module tree is empty for {}".format(release))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--modules", required=True)
    parser.add_argument("--version-file", required=True)
    parser.add_argument("--layout", choices=("auto", "rootfs", "version"), default="auto")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    try:
        normalize_modules(
            os.path.abspath(args.modules),
            os.path.abspath(args.version_file),
            os.path.abspath(args.out),
            args.layout,
        )
    except (OSError, tarfile.TarError, ValueError) as error:
        print("kernel module normalization failed: {}".format(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
