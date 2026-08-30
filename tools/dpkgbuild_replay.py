#!/usr/bin/env python3
"""Replay Debian packaging with dpkg-buildpackage inside a Buck action."""

import argparse
import glob
import json
import os
import shutil
import sys

from _deb import (
    deb_fields,
    ensure_base_files,
    extract_deb,
    fakeroot_command,
    register_debs,
    require_tool,
    run,
    stage_fakeroot_runtime,
)
from _isolation import require_target_execution, resolve_isolation, run_isolated
from _rpm import make_dirs_writable, overlay_tree, reproducible_env, scratch_dir

BUILD_OPTIONS = {
    "arch": "-B",
    "binary": "-b",
    "indep": "-A",
}


def build_option(build_type: str) -> str:
    return BUILD_OPTIONS[build_type]


def build_environment(
    source_date_epoch: str,
    build_options: list[str] | None = None,
) -> dict[str, str]:
    env = reproducible_env(source_date_epoch=source_date_epoch)
    env["FAKEROOTDONTTRYCHOWN"] = "1"
    # Bubblewrap maps the build user to UID 0 inside its private user
    # namespace. GNU tar's configure script rejects that safe arrangement
    # unless the standard container-build override is explicit.
    env["FORCE_UNSAFE_CONFIGURE"] = "1"
    if build_options:
        env["DEB_BUILD_OPTIONS"] = " ".join(dict.fromkeys(build_options))
    return env


def copy_source(src, dst):
    if os.path.isdir(src):
        shutil.copytree(src, dst, symlinks=True)
    else:
        os.makedirs(dst)
        run([
            require_tool("tar"),
            "--extract",
            "--file", src,
            "--directory", dst,
        ])
    if not os.path.isfile(os.path.join(dst, "debian", "rules")):
        sys.exit("source tree has no debian/rules: {}".format(src))
    make_dirs_writable(dst)


def compose_buildroot(seed_root, dep_roots, dest):
    os.makedirs(dest, exist_ok=True)
    layers = ([seed_root] if seed_root else []) + list(dep_roots)
    for layer in layers:
        if os.path.isdir(layer):
            overlay_tree(layer, dest)
    return dest


def host_sysroot_env(sysroot, env, target_cpu="x86_64"):
    root = os.path.abspath(sysroot)
    usr = os.path.join(root, "usr")
    lib = os.path.join(usr, "lib")
    multiarch = {
        "x86_64": "x86_64-linux-gnu",
        "aarch64": "aarch64-linux-gnu",
    }[target_cpu]
    overrides = {
        "PATH": os.pathsep.join([
            os.path.join(usr, "bin"),
            os.path.join(usr, "sbin"),
            # os.environ rather than the sandbox environment: this is
            # the host-provenance path, and reaching host tools is the
            # only thing it exists for.  The declared sandbox PATH is
            # deliberately narrow and would drop /usr/local/bin here.
            # Stated rather than inherited, which is the same principle
            # as the allowlist applied to the one mode that wants the
            # host.
            os.environ.get("PATH", "/usr/bin:/bin"),
        ]),
        "PKG_CONFIG_PATH": os.pathsep.join([
            os.path.join(lib, multiarch, "pkgconfig"),
            os.path.join(lib, "pkgconfig"),
            os.path.join(usr, "share", "pkgconfig"),
        ]),
        "C_INCLUDE_PATH": os.path.join(usr, "include"),
        "CPLUS_INCLUDE_PATH": os.path.join(usr, "include"),
        "LIBRARY_PATH": os.pathsep.join([
            os.path.join(lib, multiarch),
            lib,
        ]),
    }
    for key in ("PKG_CONFIG_PATH", "C_INCLUDE_PATH", "CPLUS_INCLUDE_PATH", "LIBRARY_PATH"):
        if env.get(key):
            overrides[key] += os.pathsep + env[key]
    return overrides


def collect_debs(parent, out):
    os.makedirs(out, exist_ok=True)
    found = []
    for pattern in ("*.deb", "*.ddeb"):
        for path in sorted(glob.glob(os.path.join(parent, pattern))):
            dest = os.path.join(out, os.path.basename(path))
            shutil.copy2(path, dest)
            found.append(dest)
    if not found:
        sys.exit("dpkg-buildpackage produced no .deb or .ddeb files under {}".format(parent))
    return found


def select_installroot_debs(
    paths: list[str],
    packages: list[str],
    metadata: dict[str, dict[str, str]] | None = None,
) -> list[str]:
    """Select only the declared binary packages for the aggregate prefix."""
    metadata = metadata or {path: deb_fields(path) for path in paths}
    wanted = set(packages)
    selected = []
    found = set()
    for path in paths:
        package = metadata[path]["Package"]
        if package in wanted:
            selected.append(path)
            found.add(package)
    missing = sorted(wanted - found)
    if missing:
        raise ValueError(
            "dpkg-buildpackage did not produce declared installroot packages: {}".format(
                ", ".join(missing),
            )
        )
    return selected


def write_manifest(paths, out, metadata=None):
    metadata = metadata or {path: deb_fields(path) for path in paths}
    packages = []
    for path in paths:
        fields = metadata[path]
        packages.append({
            "architecture": fields["Architecture"],
            "file": os.path.basename(path),
            "package": fields["Package"],
            "version": fields["Version"],
        })
    with open(out, "w", encoding="utf-8") as stream:
        json.dump({"packages": packages}, stream, indent=2, sort_keys=True)
        stream.write("\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--out-debs", required=True)
    parser.add_argument("--out-installroot", required=True)
    parser.add_argument("--out-manifest", required=True)
    parser.add_argument("--buildroot-tree", default=None)
    parser.add_argument("--dep-installroot", action="append", default=[])
    parser.add_argument("--dep-deb", action="append", default=[])
    parser.add_argument("--isolation", choices=("auto", "bwrap", "unshare", "none"), default="auto")
    parser.add_argument("--dpkg-buildpackage", default="dpkg-buildpackage")
    parser.add_argument("--env", action="append", default=[])
    parser.add_argument("--build-profile", action="append", default=[])
    parser.add_argument("--build-option", action="append", default=[])
    parser.add_argument("--build-type", choices=sorted(BUILD_OPTIONS), default="binary")
    parser.add_argument("--install-package", action="append", default=[])
    parser.add_argument("--nocheck", action="store_true")
    parser.add_argument("--source-date-epoch", default="1700000000")
    parser.add_argument("--target-cpu", default="x86_64")
    args = parser.parse_args()

    work = scratch_dir("buckos-dpkgbuild-", key=os.path.abspath(args.out_debs))
    source = os.path.join(work, "source")
    sysroot = os.path.join(work, "sysroot")
    copy_source(args.source, source)
    compose_buildroot(args.buildroot_tree, args.dep_installroot, sysroot)
    if args.dep_deb:
        register_debs(args.dep_deb, sysroot)
    ensure_base_files(sysroot)

    env = build_environment(args.source_date_epoch, args.build_option)
    for item in args.env:
        if "=" not in item:
            sys.exit("--env must be KEY=VALUE: {!r}".format(item))
        key, value = item.split("=", 1)
        env[key] = value
    if args.nocheck:
        current = env.get("DEB_BUILD_OPTIONS", "").split()
        if "nocheck" not in current:
            current.append("nocheck")
        env["DEB_BUILD_OPTIONS"] = " ".join(current)
    if args.build_profile:
        env["DEB_BUILD_PROFILES"] = " ".join(sorted(set(args.build_profile)))

    require_target_execution(args.target_cpu, "host" if args.isolation == "none" else "binary-seed")
    isolation = resolve_isolation(args.isolation)
    if isolation == "none":
        env.update(host_sysroot_env(sysroot, env, args.target_cpu))
    else:
        env["TMPDIR"] = "/tmp"

    fakeroot = stage_fakeroot_runtime(sysroot, work)
    command = fakeroot_command(fakeroot, [
        require_tool(args.dpkg_buildpackage),
        build_option(args.build_type),
        "-us",
        "-uc",
        "-d",
    ])
    run_isolated(
        command,
        isolation,
        work=work,
        chdir=source,
        sysroot=sysroot,
        env=env,
    )

    debs = collect_debs(work, os.path.abspath(args.out_debs))
    metadata = {path: deb_fields(path) for path in debs}
    installroot = os.path.abspath(args.out_installroot)
    shutil.rmtree(installroot, ignore_errors=True)
    os.makedirs(installroot)
    install_debs = select_installroot_debs(debs, args.install_package, metadata)
    for path in install_debs:
        extract_deb(path, installroot)
        make_dirs_writable(installroot)
    write_manifest(debs, os.path.abspath(args.out_manifest), metadata)


if __name__ == "__main__":
    main()
