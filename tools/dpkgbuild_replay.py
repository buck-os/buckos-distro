#!/usr/bin/env python3
"""Replay Debian packaging with dpkg-buildpackage inside a Buck action."""

import argparse
import glob
import json
import os
import re
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
from _isolation import (
    require_target_execution,
    resolve_isolation,
    run_isolated,
    sandbox_path,
)
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
    # The archive's own `find` calls the system getcwd, on both amd64 and
    # arm64: getcwd present, lstat, readlink and rewinddir absent.  Left
    # unpinned this probe sometimes produces the other binary instead --
    # one Debian never ships -- so letting it run is the deviation from
    # upstream and pinning it is the fidelity.
    #
    # What the pin overrides is a timeout, not a finding.  gnulib decides
    # whether the system getcwd copes with paths past PATH_MAX by
    # compiling a probe that mkdir/chdirs a deep chain and calls it there,
    # and the probe allows itself five seconds -- alarm(5).  Every failing
    # run measured here died on SIGALRM, exit 142, six of six, none of
    # them reaching a verdict about getcwd at all.  A "no" here means
    # could not determine, never determined no.
    #
    # And the platform is sound wherever the probe is allowed to finish,
    # emulation included: tar and cpio completed it and returned yes in
    # the same builds that timed findutils out.  Nothing anywhere suggests
    # the system getcwd is inadequate on either architecture.
    #
    # The cost of leaving it is that build-farm load picks the binary: a
    # "no" compiles in gnulib's replacement and `find` gains 1536 bytes of
    # text, silently, with nothing failing.
    env["gl_cv_func_getcwd_path_max"] = "yes"
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


_CHANGELOG_RE = re.compile(r"^(?P<name>[a-z0-9][a-z0-9+.\-]*) \((?P<version>[^)]+)\)")


def dpkg_source_directory(work, tree):
    """Rename the unpacked tree to the name dpkg-source would have given it.

    `dpkg-source -x` unpacks to <source>-<upstream version>, and packages
    are entitled to rely on that.  plymouth's scripts/generate-version.sh
    recovers its own version with

        basename "$PWD" | sed -n 's/^plymouth-\\([^-]*\\)$/\\1/p'

    and exits 1 when that yields nothing, which stops meson at
    meson.build:3 before a single file is compiled.  Unpacking to a fixed
    "source" made the package unbuildable for a reason that has nothing to
    do with the package, and any other package deriving its version the
    same way was equally unbuildable.

    Epoch and Debian revision are dropped because upstream's directory
    name carries neither: 1:24.004.60+git20250831-0ubuntu8 unpacks to
    24.004.60+git20250831.  A native package has no revision to drop.

    The name is still a function of the package alone, so the work area
    remains reproducible: two builds of the same source agree, and the
    path no longer depends on the scratch directory.
    """
    changelog = os.path.join(tree, "debian", "changelog")
    try:
        with open(changelog, encoding="utf-8", errors="replace") as stream:
            first = stream.readline()
    except OSError as exc:
        sys.exit("cannot read {}: {}".format(changelog, exc))

    match = _CHANGELOG_RE.match(first)
    if not match:
        sys.exit(
            "debian/changelog does not start with a package stanza, so the "
            "source directory cannot be named the way dpkg-source would: "
            "{!r}".format(first.strip())
        )

    version = match.group("version").split(":", 1)[-1]
    if "-" in version:
        version = version.rsplit("-", 1)[0]
    renamed = os.path.join(work, "{}-{}".format(match.group("name"), version))
    os.rename(tree, renamed)
    return renamed


def compose_buildroot(seed_root, dep_roots, dest):
    os.makedirs(dest, exist_ok=True)
    layers = ([seed_root] if seed_root else []) + list(dep_roots)
    for layer in layers:
        if os.path.isdir(layer):
            overlay_tree(layer, dest)
    return dest


def refresh_library_cache(sysroot, isolation, work, env):
    """Rebuild /etc/ld.so.cache for the tree this package builds against.

    The seed buildroot arrives with a cache its own assembler built, and
    that cache is correct for the seed and stale for this package: the
    dependency layers overlaid above add libraries it does not mention.
    A ctypes caller then fails on a library sitting on disk, because
    ctypes.util.find_library asks `ldconfig -p`.  Measured on
    xkeyboard-config: cache present, libxkbcommon.so.0 present, zero
    xkbcommon entries in the cache.

    Stale is worse than absent here.  An absent cache makes the loader
    fall back to searching; a stale one answers, wrongly.

    The RPM family gets this for free.  Its per-package overlay runs
    triggers, and glibc's file trigger rebuilds the cache -- the reason
    triggers are enabled there rather than for the shared base.  The
    Debian family has no trigger to turn on, so the overlay has to say
    so itself.
    """
    if isolation == "none":
        return
    run_isolated(
        ["/usr/sbin/ldconfig"],
        isolation,
        work=work,
        chdir=work,
        sysroot=sysroot,
        env=env,
    )


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
    source = dpkg_source_directory(work, source)
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

    refresh_library_cache(sysroot, isolation, work, env)

    fakeroot = stage_fakeroot_runtime(sysroot, work, isolation)
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
