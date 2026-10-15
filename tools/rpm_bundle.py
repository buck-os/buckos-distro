#!/usr/bin/env python3
"""Assemble RPM payloads without installing packages or executing target code."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile

from _rpm import extract_rpm, make_dirs_writable, scratch_dir, reproducible_env


def relative_path(value):
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(p in ("..", ".") for p in value.split("/")):
        raise ValueError("expected a relative payload path without traversal: {!r}".format(value))
    return path


def digest(path):
    result = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def package_identity(rpm):
    result = subprocess.run(
        ["rpm", "-qp", "--qf", "%{NAME}\n%{VERSION}\n%{RELEASE}\n%{ARCH}\n", str(rpm)],
        check=True, capture_output=True, text=True, env=reproducible_env(),
    )
    name, version, release, architecture = result.stdout.strip().splitlines()
    return {"name": name, "version": version, "release": release, "architecture": architecture}


def inside(root, path):
    return os.path.commonpath([str(root), str(path)]) == str(root)


def merge_payload(source, destination):
    """Accept shared directories/identical files; reject differing payloads."""
    for src in sorted(source.iterdir()):
        dst = destination / src.name
        if src.is_symlink():
            if os.path.lexists(dst):
                if dst.is_symlink() and os.readlink(src) == os.readlink(dst):
                    continue
                raise ValueError("conflicting RPM payload: {}".format(dst))
            dst.symlink_to(os.readlink(src))
        elif src.is_dir():
            if os.path.lexists(dst) and (dst.is_symlink() or not dst.is_dir()):
                raise ValueError("conflicting RPM directory: {}".format(dst))
            dst.mkdir(exist_ok=True)
            dst.chmod(0o755)
            merge_payload(src, dst)
        elif src.is_file():
            if os.path.lexists(dst):
                if (not dst.is_symlink() and dst.is_file() and
                        digest(src) == digest(dst) and
                        (src.stat().st_mode & 0o777) == (dst.stat().st_mode & 0o777)):
                    continue
                raise ValueError("conflicting RPM payload: {}".format(dst))
            shutil.copyfile(src, dst)
            dst.chmod(src.stat().st_mode & 0o777)
        else:
            raise ValueError("unsupported special file in RPM: {}".format(src))


def relocate_symlinks(root):
    """Absolute package symlinks must refer to this payload after staging."""
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            if not path.is_symlink():
                continue
            target = os.readlink(path)
            if target.startswith("/"):
                target = os.path.relpath(root / target.lstrip("/"), path.parent)
                path.unlink()
                path.symlink_to(target)
            lexical = Path(os.path.abspath(path.parent / target))
            if not inside(root, lexical):
                raise ValueError("symlink escapes payload: {} -> {}".format(path, target))
    # Also catch an escape through a chain of symlinks.
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            if path.is_symlink() and not inside(root, path.resolve()):
                raise ValueError("symlink chain escapes payload: {}".format(path))


def inventory(root):
    records = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in sorted(dirs + files):
            path = Path(directory) / name
            record = {"path": path.relative_to(root).as_posix()}
            if path.is_symlink():
                record.update(type="symlink", target=os.readlink(path))
            elif path.is_dir():
                record["type"] = "directory"
            else:
                record.update(type="file", sha256=digest(path), mode=path.stat().st_mode & 0o777)
            records.append(record)
    return sorted(records, key=lambda record: record["path"])


def assemble(rpms, out, archive, manifest, architecture, exports=None, epoch=1700000000,
             export_outputs=None):
    if not rpms:
        raise ValueError("at least one RPM is required")
    exports = exports or {}
    for name, path in exports.items():
        relative_path(path)
        if name in ("tree", "archive", "manifest"):
            raise ValueError("reserved export name: {}".format(name))
    out = Path(out).absolute()
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise ValueError("output directory must be empty: {}".format(out))
    packages = []
    for rpm in rpms:
        identity = package_identity(rpm)
        if identity["architecture"] not in (architecture, "noarch"):
            raise ValueError("{} targets {}, bundle targets {}".format(
                rpm, identity["architecture"], architecture))
        scratch = Path(scratch_dir("buckos-rpm-bundle-"))
        try:
            extract_rpm(os.path.abspath(rpm), str(scratch))
            make_dirs_writable(str(scratch))
            merge_payload(scratch, out)
        finally:
            make_dirs_writable(str(scratch))
            shutil.rmtree(scratch)
        packages.append(dict(identity, filename=os.path.basename(rpm), sha256=digest(rpm)))
    relocate_symlinks(out)
    for name, relative in exports.items():
        path = out / relative
        if not inside(out, path.resolve()) or not path.exists():
            raise ValueError("missing or escaping export {}: {}".format(name, relative))
        if relative.endswith(".a"):
            with path.open("rb") as stream:
                if stream.read(8) != b"!<arch>\n":
                    raise ValueError("export must be a regular static archive (thin archives unsupported): {}".format(relative))
    records = inventory(out)
    # Standalone exports are copied from the complete tree, dereferencing
    # symlinks. Remote C++ actions then need only their declared exports,
    # including when a vendor archive/header link points outside its directory.
    for name, destination in (export_outputs or {}).items():
        source = out / exports[name]
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination, symlinks=False)
        else:
            shutil.copyfile(source, destination)
    data = {
        "schema": "buckos.rpm-bundle.v1",
        "architecture": architecture,
        "packages": packages,
        "exports": exports,
        "files": records,
        "static_archives": [r["path"] for r in records if r["path"].endswith(".a")],
        "installation": "payload-only",
        "runtime_compatibility": "unverified",
    }
    Path(manifest).parent.mkdir(parents=True, exist_ok=True)
    Path(manifest).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    Path(archive).parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "w", format=tarfile.PAX_FORMAT) as tar:
        for record in records:
            path = out / record["path"]
            info = tar.gettarinfo(str(path), arcname=record["path"])
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = epoch
            info.pax_headers = {}
            if info.isfile():
                with path.open("rb") as stream:
                    tar.addfile(info, stream)
            else:
                tar.addfile(info)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rpm", action="append", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--archive", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--exports")
    parser.add_argument("--export-output", action="append", nargs=2, default=[])
    parser.add_argument("--architecture", choices=["aarch64", "x86_64"], required=True)
    parser.add_argument("--source-date-epoch", type=int, default=1700000000)
    args = parser.parse_args(argv)
    exports = json.loads(Path(args.exports).read_text()) if args.exports else {}
    try:
        assemble(args.rpm, args.out, args.archive, args.manifest, args.architecture,
                 exports, args.source_date_epoch, dict(args.export_output))
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
