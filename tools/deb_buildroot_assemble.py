#!/usr/bin/env python3
"""Assemble a Debian buildroot from SHA-256-pinned binary packages."""

import argparse
import os
import shutil
import tempfile

from _deb import ensure_base_files, extract_deb, register_debs
from _isolation import (
    ISOLATION_MODES,
    require_target_execution,
    resolve_isolation,
    run_isolated,
)
from _rpm import make_dirs_writable, reproducible_env


def _refresh_library_cache(out, isolation, target_cpu, source_date_epoch):
    """Build the tree's own /etc/ld.so.cache, which dpkg triggers would.

    ldconfig runs from maintainer scripts and dpkg triggers, so a
    payload-composed buildroot never has a cache.  Most things survive
    that, because the loader still searches the standard directories; a
    ctypes caller does not, because ctypes.util.find_library shells out
    to `ldconfig -p` and reports the library missing when the cache is
    absent.  xkeyboard-config's generator fails exactly there.

    In the sandbox, not host-side, and that distinction is the whole
    reason this is not three lines in ensure_base_files.  Running the
    host's ldconfig with -r against a foreign-architecture tree exits 0
    and writes a 137-byte cache holding no libraries at all, so the
    failure would be a cache that exists and answers nothing.  The
    tree's own ldconfig, reached through the target-architecture binfmt
    handler, writes a real one.
    """
    require_target_execution(target_cpu)
    work = tempfile.mkdtemp(prefix="buckos-distro-deb-ldconfig-")
    try:
        run_isolated(
            ["/usr/sbin/ldconfig"],
            isolation,
            work=work,
            chdir=work,
            sysroot=out,
            env=reproducible_env(source_date_epoch=source_date_epoch),
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)
        # The sandbox binds the work area at its own absolute path and
        # has to create that path inside the tree to do it.  The mount
        # goes with the namespace, the empty directory does not, and its
        # name carries mkdtemp's random suffix -- so leaving it would
        # make this output differ on every build.
        leftover = os.path.join(out, os.path.relpath(work, "/"))
        shutil.rmtree(leftover, ignore_errors=True)
        make_dirs_writable(out)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--deb", action="append", default=[])
    parser.add_argument("--out", required=True)
    parser.add_argument("--isolation", choices=ISOLATION_MODES, default="auto")
    parser.add_argument("--target-cpu", default="x86_64")
    parser.add_argument("--source-date-epoch", default="1700000000")
    args = parser.parse_args()

    out = os.path.abspath(args.out)
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    ensure_base_files(out)

    for deb in args.deb:
        extract_deb(deb, out)
        make_dirs_writable(out)

    ensure_base_files(out)
    register_debs(args.deb, out)
    _refresh_library_cache(
        out,
        resolve_isolation(args.isolation),
        args.target_cpu,
        args.source_date_epoch,
    )


if __name__ == "__main__":
    main()
