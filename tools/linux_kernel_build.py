#!/usr/bin/env python3
"""Build a Linux kernel as a cacheable, producer-neutral artifact bundle."""

import argparse
import datetime
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile

from _isolation import (
    ISOLATION_MODES,
    require_target_execution,
    resolve_isolation,
    run_isolated,
    sandbox_path,
)
from _kernel import read_kernel_release, write_certificate_pem
from _rpm import reproducible_env, scratch_dir


_ARCHITECTURES = {
    "x86_64": ("x86", "bzImage", "arch/x86/boot/bzImage"),
    "aarch64": ("arm64", "Image", "arch/arm64/boot/Image"),
}


def _safe_archive_member(name):
    while name.startswith("./"):
        name = name[2:]
    parts = [part for part in name.split("/") if part not in ("", ".")]
    return not name.startswith("/") and all(part != ".." for part in parts)


def _extract_source(archive_path, destination):
    with tarfile.open(archive_path) as archive:
        pending_links = []
        for member in archive.getmembers():
            if not _safe_archive_member(member.name):
                raise ValueError("unsafe kernel source member {!r}".format(member.name))
            name = member.name
            while name.startswith("./"):
                name = name[2:]
            target = os.path.abspath(os.path.join(destination, name))
            root = os.path.abspath(destination)
            if target != root and not target.startswith(root + os.sep):
                raise ValueError("kernel source member escapes its root")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if member.isdir():
                os.makedirs(target, exist_ok=True)
            elif member.isfile():
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError("cannot read kernel source member {}".format(name))
                with open(target, "wb") as output:
                    shutil.copyfileobj(source, output)
            elif member.issym():
                if os.path.isabs(member.linkname):
                    raise ValueError("absolute kernel source symlink {!r}".format(member.linkname))
                resolved = os.path.abspath(os.path.join(os.path.dirname(target), member.linkname))
                if resolved != root and not resolved.startswith(root + os.sep):
                    raise ValueError("kernel source symlink escapes its root")
                os.symlink(member.linkname, target)
            elif member.islnk():
                pending_links.append((target, member.linkname))
            else:
                raise ValueError("unsupported kernel source member {}".format(name))
            if not member.issym() and not member.islnk():
                os.chmod(target, stat.S_IMODE(member.mode))
        for target, linkname in pending_links:
            link_target = os.path.abspath(os.path.join(destination, linkname))
            root = os.path.abspath(destination)
            if not link_target.startswith(root + os.sep) or not os.path.isfile(link_target):
                raise ValueError("unresolved kernel source hardlink {}".format(linkname))
            os.link(link_target, target)


def _validate_source_symlinks(destination):
    root = os.path.realpath(destination)
    for directory, dirnames, filenames in os.walk(
        destination, topdown=True, followlinks=False
    ):
        for name in dirnames + filenames:
            path = os.path.join(directory, name)
            if not os.path.islink(path):
                continue
            linkname = os.readlink(path)
            if os.path.isabs(linkname):
                raise ValueError(
                    "absolute kernel source symlink {!r}".format(linkname)
                )
            resolved = os.path.realpath(path)
            if resolved != root and not resolved.startswith(root + os.sep):
                raise ValueError("kernel source symlink escapes its root")


def stage_source(source, destination):
    if os.path.isdir(source):
        shutil.copytree(source, destination, symlinks=True)
    elif tarfile.is_tarfile(source):
        os.makedirs(destination)
        _extract_source(source, destination)
    else:
        raise ValueError("kernel source is neither a directory nor a tar archive")
    _validate_source_symlinks(destination)

    if os.path.isfile(os.path.join(destination, "Makefile")):
        return destination
    children = [
        os.path.join(destination, name)
        for name in sorted(os.listdir(destination))
        if name not in (".", "..")
    ]
    directories = [path for path in children if os.path.isdir(path)]
    files = [path for path in children if not os.path.isdir(path)]
    if not files and len(directories) == 1 and os.path.isfile(os.path.join(directories[0], "Makefile")):
        return directories[0]
    raise ValueError("kernel source has no top-level Makefile")


def set_config_values(path, values):
    prefixes = tuple("CONFIG_{}=".format(name) for name in values)
    disabled = tuple("# CONFIG_{} is not set".format(name) for name in values)
    with open(path, "r", encoding="utf-8") as stream:
        lines = [
            line for line in stream
            if not line.startswith(prefixes) and line.rstrip("\n") not in disabled
        ]
    for name, value in sorted(values.items()):
        if value is True:
            lines.append("CONFIG_{}=y\n".format(name))
        elif value is False:
            lines.append("# CONFIG_{} is not set\n".format(name))
        else:
            escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
            lines.append('CONFIG_{}="{}"\n'.format(name, escaped))
    with open(path, "w", encoding="utf-8", newline="\n") as stream:
        stream.writelines(lines)


def _make_command(make, source, build, arch, make_args, targets, jobs=None):
    command = [make, "-C", source, "O=" + build, "ARCH=" + arch]
    command += make_args
    if jobs and jobs > 0:
        command.append("-j{}".format(jobs))
    command += targets
    return " ".join(shlex.quote(part) for part in command)


def _copy_file(source, destination, required=True):
    if not os.path.isfile(source):
        if required:
            raise ValueError("kernel build produced no {}".format(source))
        os.makedirs(os.path.dirname(os.path.abspath(destination)), exist_ok=True)
        with open(destination, "wb"):
            pass
        return
    os.makedirs(os.path.dirname(os.path.abspath(destination)), exist_ok=True)
    shutil.copy2(source, destination)


def _copy_modules(stage, output, release):
    source = os.path.join(stage, "lib", "modules", release)
    destination = os.path.join(output, "usr", "lib", "modules", release)
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    if os.path.isdir(source):
        shutil.copytree(source, destination, symlinks=True)
    else:
        os.makedirs(destination)
    for name in ("build", "source"):
        path = os.path.join(destination, name)
        if os.path.lexists(path):
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.unlink(path)


def build_kernel(args):
    require_target_execution(args.target_cpu)
    isolation = resolve_isolation(args.isolation)
    work = scratch_dir("buckos-distro-kernel-", key=args.out_image)
    source_staging = os.path.join(work, "source")
    source = stage_source(os.path.abspath(args.source), source_staging)
    build = os.path.join(work, "output")
    modules_stage = os.path.join(work, "modules-stage")
    os.makedirs(build)
    os.makedirs(modules_stage)
    shutil.copy2(os.path.abspath(args.config), os.path.join(build, ".config"))

    kernel_arch, image_target, default_image_path = _ARCHITECTURES[args.architecture]
    image_path = args.image_path or default_image_path
    make_args = list(args.make_arg)
    if args.localversion:
        make_args.append("LOCALVERSION=" + args.localversion)

    if args.ima_certificate:
        certificate = os.path.join(work, "ima-signing-certificate.pem")
        write_certificate_pem(os.path.abspath(args.ima_certificate), certificate)
        config_path = os.path.join(build, ".config")
        set_config_values(config_path, {
            "ASYMMETRIC_KEY_TYPE": True,
            "IMA": True,
            "IMA_APPRAISE": True,
            "IMA_APPRAISE_BOOTPARAM": True,
            "IMA_APPRAISE_MODSIG": True,
            "IMA_LOAD_X509": True,
            "IMA_TRUSTED_KEYRING": True,
            "IMA_X509_PATH": "/etc/keys/x509_ima.der",
            "INTEGRITY": True,
            "INTEGRITY_ASYMMETRIC_KEYS": True,
            "INTEGRITY_SIGNATURE": True,
            "SYSTEM_TRUSTED_KEYRING": True,
            "SYSTEM_TRUSTED_KEYS": sandbox_path(certificate, work, isolation),
        })

    inside_source = sandbox_path(source, work, isolation)
    inside_build = sandbox_path(build, work, isolation)
    inside_modules = sandbox_path(modules_stage, work, isolation)
    make = args.make
    commands = [
        "set -e",
        _make_command(make, inside_source, inside_build, kernel_arch, make_args, ["olddefconfig"]),
        _make_command(
            make,
            inside_source,
            inside_build,
            kernel_arch,
            make_args,
            [image_target, "vmlinux", "modules"],
            args.jobs or os.cpu_count() or 1,
        ),
        _make_command(make, inside_source, inside_build, kernel_arch, make_args, ["-s", "kernelrelease"]) +
        " > " + shlex.quote(sandbox_path(os.path.join(work, "release"), work, isolation)),
        _make_command(
            make,
            inside_source,
            inside_build,
            kernel_arch,
            make_args,
            ["modules_install", "INSTALL_MOD_PATH=" + inside_modules],
        ),
    ]

    timestamp = datetime.datetime.fromtimestamp(
        int(args.source_date_epoch), datetime.timezone.utc
    ).strftime("%a %b %d %H:%M:%S UTC %Y")
    env = reproducible_env({
        "KBUILD_BUILD_HOST": "buckos",
        "KBUILD_BUILD_TIMESTAMP": timestamp,
        "KBUILD_BUILD_USER": "buckos",
        "KBUILD_BUILD_VERSION": "1",
    }, source_date_epoch=args.source_date_epoch)
    sysroot = os.path.abspath(args.buildroot_tree) if args.buildroot_tree else None
    try:
        run_isolated(
            ["/bin/sh", "-c", "\n".join(commands)],
            isolation,
            work,
            work,
            sysroot,
            env=env,
        )

        release_file = os.path.join(work, "release")
        release = read_kernel_release(release_file)
        if args.expected_release and release != args.expected_release:
            raise ValueError(
                "kernel release {!r} does not match expected {!r}".format(
                    release, args.expected_release
                )
            )

        _copy_file(os.path.join(build, image_path), args.out_image)
        _copy_file(release_file, args.out_version)
        _copy_modules(modules_stage, args.out_modules, release)
        _copy_file(os.path.join(build, ".config"), args.out_config)
        _copy_file(os.path.join(build, "vmlinux"), args.out_vmlinux)
        _copy_file(os.path.join(build, "System.map"), args.out_system_map)
        _copy_file(
            os.path.join(build, "Module.symvers"),
            args.out_module_symvers,
            required=False,
        )
        return release
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--architecture", choices=sorted(_ARCHITECTURES), required=True)
    parser.add_argument("--image-path", default="")
    parser.add_argument("--expected-release", default="")
    parser.add_argument("--localversion", default="")
    parser.add_argument("--make", default="/usr/bin/make")
    parser.add_argument("--make-arg", action="append", default=[])
    parser.add_argument("--jobs", type=int, default=0)
    parser.add_argument("--ima-certificate")
    parser.add_argument("--buildroot-tree")
    parser.add_argument("--isolation", choices=ISOLATION_MODES, default="auto")
    parser.add_argument("--target-cpu", default="")
    parser.add_argument("--source-date-epoch", default="1700000000")
    parser.add_argument("--out-image", required=True)
    parser.add_argument("--out-version", required=True)
    parser.add_argument("--out-modules", required=True)
    parser.add_argument("--out-config", required=True)
    parser.add_argument("--out-vmlinux", required=True)
    parser.add_argument("--out-system-map", required=True)
    parser.add_argument("--out-module-symvers", required=True)
    args = parser.parse_args()
    try:
        release = build_kernel(args)
        print("built Linux kernel {}".format(release), file=sys.stderr)
    except (OSError, subprocess.CalledProcessError, tarfile.TarError, ValueError) as error:
        print("Linux kernel build failed: {}".format(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
