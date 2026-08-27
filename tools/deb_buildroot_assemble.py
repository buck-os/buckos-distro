#!/usr/bin/env python3
"""Assemble a Debian buildroot from SHA-256-pinned binary packages."""

import argparse
import glob
import os
import shutil
import subprocess
import tarfile
import tempfile

from _deb import deb_field, extract_deb, require_tool, run
from _rpm import make_dirs_writable


SKELETON = (
    "builddir",
    "dev",
    "etc",
    "proc",
    "sys",
    "tmp",
    "var/lib/dpkg/info",
    "var/tmp",
)

STATUS_FIELDS = (
    "Package",
    "Essential",
    "Status",
    "Priority",
    "Section",
    "Installed-Size",
    "Maintainer",
    "Architecture",
    "Multi-Arch",
    "Source",
    "Version",
    "Replaces",
    "Provides",
    "Pre-Depends",
    "Depends",
    "Conflicts",
    "Breaks",
    "Description",
)

CONTROL_FILES = (
    "conffiles",
    "md5sums",
    "shlibs",
    "symbols",
    "triggers",
)


def payload_paths(deb):
    process = subprocess.Popen(
        [require_tool("dpkg-deb"), "--fsys-tarfile", deb],
        stdout=subprocess.PIPE,
    )
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            paths = []
            for member in archive:
                name = member.name.removeprefix("./").rstrip("/")
                if name:
                    paths.append("/" + name)
    finally:
        process.stdout.close()
    status = process.wait()
    if status != 0:
        raise subprocess.CalledProcessError(status, process.args)
    return sorted(set(paths))


def package_key(deb):
    package = deb_field(deb, "Package")
    arch = deb_field(deb, "Architecture")
    multi_arch = deb_field(deb, "Multi-Arch")
    if multi_arch == "same":
        return "{}:{}".format(package, arch)
    return package


def extract_control(deb, root):
    dpkg_deb = require_tool("dpkg-deb")
    with tempfile.TemporaryDirectory(prefix="buckos-deb-control-") as tmp:
        run([dpkg_deb, "--control", deb, tmp])
        key = package_key(deb)
        info = os.path.join(root, "var", "lib", "dpkg", "info")
        for name in CONTROL_FILES:
            source = os.path.join(tmp, name)
            if os.path.isfile(source):
                shutil.copy2(source, os.path.join(info, "{}.{}".format(key, name)))
        with open(os.path.join(info, key + ".list"), "w", encoding="utf-8") as stream:
            for path in payload_paths(deb):
                stream.write(path + "\n")


def status_paragraph(deb):
    result = run(
        [require_tool("dpkg-deb"), "--field", deb],
        capture_output=True,
        text=True,
    )
    fields = {}
    current = None
    for line in result.stdout.splitlines():
        if line.startswith((" ", "\t")) and current:
            fields[current] += "\n" + line
        elif ":" in line:
            current, value = line.split(":", 1)
            fields[current] = value.lstrip()
    fields["Status"] = "install ok installed"

    lines = []
    for name in STATUS_FIELDS:
        value = fields.get(name)
        if value:
            lines.append("{}: {}".format(name, value))
    return "\n".join(lines) + "\n"


def ensure_base_files(root):
    for rel in SKELETON:
        path = os.path.join(root, rel)
        os.makedirs(path, exist_ok=True)
    os.chmod(os.path.join(root, "tmp"), 0o1777)
    os.chmod(os.path.join(root, "var", "tmp"), 0o1777)

    info_format = os.path.join(root, "var", "lib", "dpkg", "info", "format")
    if not os.path.exists(info_format):
        with open(info_format, "w", encoding="utf-8") as stream:
            stream.write("1\n")

    for name, target in (
        ("bin", "usr/bin"),
        ("sbin", "usr/sbin"),
        ("lib", "usr/lib"),
        ("lib64", "usr/lib64"),
    ):
        path = os.path.join(root, name)
        target_path = os.path.join(root, target)
        if not os.path.lexists(path) and os.path.exists(target_path):
            os.symlink(target, path)

    passwd = os.path.join(root, "etc", "passwd")
    if not os.path.exists(passwd):
        with open(passwd, "w", encoding="utf-8") as stream:
            stream.write("root:x:0:0:root:/root:/bin/bash\n")
    group = os.path.join(root, "etc", "group")
    if not os.path.exists(group):
        with open(group, "w", encoding="utf-8") as stream:
            stream.write("root:x:0:\n")

    bindir = os.path.join(root, "usr", "bin")
    for name in ("aclocal", "automake"):
        link = os.path.join(bindir, name)
        candidates = sorted(glob.glob(link + "-*"))
        if not os.path.lexists(link) and len(candidates) == 1:
            os.symlink(os.path.basename(candidates[0]), link)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--deb", action="append", default=[])
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    out = os.path.abspath(args.out)
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    ensure_base_files(out)

    paragraphs = []
    for deb in args.deb:
        extract_deb(deb, out)
        make_dirs_writable(out)
        extract_control(deb, out)
        paragraphs.append((package_key(deb), status_paragraph(deb)))

    ensure_base_files(out)
    status = os.path.join(out, "var", "lib", "dpkg", "status")
    with open(status, "w", encoding="utf-8") as stream:
        for _, paragraph in sorted(paragraphs):
            stream.write(paragraph)
            stream.write("\n")


if __name__ == "__main__":
    main()
