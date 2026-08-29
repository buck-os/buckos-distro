#!/usr/bin/env python3
"""Replay Debian packaging with dpkg-buildpackage inside a Buck action."""

import argparse
import glob
import json
import os
import shutil
import sys

from _deb import deb_field, extract_deb, register_debs, require_tool, run
from _isolation import require_target_execution, resolve_isolation, run_isolated
from _rpm import make_dirs_writable, overlay_tree, reproducible_env, scratch_dir


def copy_source(src, dst):
    shutil.copytree(src, dst, symlinks=True)
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
            env.get("PATH", "/usr/bin:/bin"),
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


def write_manifest(paths, out):
    packages = []
    for path in paths:
        packages.append({
            "architecture": deb_field(path, "Architecture"),
            "file": os.path.basename(path),
            "package": deb_field(path, "Package"),
            "version": deb_field(path, "Version"),
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

    env = reproducible_env(source_date_epoch=args.source_date_epoch)
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

    command = [require_tool(args.dpkg_buildpackage), "-b", "-us", "-uc", "-d"]
    run_isolated(
        command,
        isolation,
        work=work,
        chdir=source,
        sysroot=sysroot,
        env=env,
    )

    debs = collect_debs(work, os.path.abspath(args.out_debs))
    installroot = os.path.abspath(args.out_installroot)
    shutil.rmtree(installroot, ignore_errors=True)
    os.makedirs(installroot)
    for path in debs:
        extract_deb(path, installroot)
        make_dirs_writable(installroot)
    write_manifest(debs, os.path.abspath(args.out_manifest))


if __name__ == "__main__":
    main()
