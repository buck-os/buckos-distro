#!/usr/bin/env python3
"""Resolve Debian-family source buildroots with APT and write a lockfile."""

import argparse
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import tempfile
import urllib.parse

from _deb import parse_control_paragraphs


TARGET_RE = re.compile(r"[^A-Za-z0-9_-]+")
LOG = logging.getLogger("deb-lock")
APT_CONFIG = []
AVAILABLE_BY_FILENAME = None


def run_output(command):
    LOG.debug("+ %s", " ".join(shlex.quote(part) for part in command))
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as error:
        raise RuntimeError(
            "{} failed with status {}: {}".format(
                " ".join(shlex.quote(part) for part in command),
                error.returncode,
                error.stderr.strip(),
            )
        ) from error
    return result.stdout


def apt_output(command):
    return run_output(command[:1] + APT_CONFIG + command[1:])


def apt_uri_lines(output):
    entries = []
    for line in output.splitlines():
        if not line.startswith("'"):
            continue
        parts = shlex.split(line)
        if len(parts) < 3:
            raise ValueError("malformed apt URI line: {!r}".format(line))
        digest_kind = ""
        digest = ""
        if len(parts) >= 4:
            digest_kind, _, digest = parts[3].partition(":")
        entries.append({
            "url": parts[0],
            "filename": urllib.parse.unquote(parts[1]),
            "size": int(parts[2]),
            "digest_kind": digest_kind.lower(),
            "digest": digest.lower(),
        })
    if not entries:
        raise ValueError("APT produced no download URIs")
    return entries


def apt_options(status, archives, architecture=None, lists=None, sources=None):
    options = [
        "-o", "Dir::State::status={}".format(status),
        "-o", "Dir::Cache::archives={}".format(archives),
        "-o", "APT::Install-Recommends=false",
        "-o", "APT::Install-Suggests=false",
    ]
    if architecture:
        options += [
            "-o", "APT::Architecture={}".format(architecture),
            "-o", "APT::Architectures::={}".format(architecture),
        ]
    if lists:
        options += ["-o", "Dir::State::lists={}".format(lists)]
    if sources:
        options += [
            "-o", "Dir::Etc::sourcelist={}".format(sources),
            "-o", "Dir::Etc::sourceparts=-",
        ]
    return options + [
        "--print-uris",
        "--yes",
        "--download-only",
    ]


def essential_packages():
    packages = []
    for fields in parse_control_paragraphs(apt_output(["apt-cache", "dumpavail"])):
        if fields.get("Essential") == "yes":
            packages.append(fields["Package"])
    return sorted(set(packages))


def binary_record(entry):
    global AVAILABLE_BY_FILENAME
    url_path = urllib.parse.unquote(urllib.parse.urlsplit(entry["url"]).path)
    if AVAILABLE_BY_FILENAME is None:
        AVAILABLE_BY_FILENAME = {
            fields.get("Filename"): fields
            for fields in parse_control_paragraphs(apt_output(["apt-cache", "dumpavail"]))
            if fields.get("Filename")
        }
    matches = [fields for filename, fields in AVAILABLE_BY_FILENAME.items()
               if url_path.endswith("/" + filename)]
    if not matches:
        package, version, architecture = entry["filename"][:-4].rsplit("_", 2)
        records = parse_control_paragraphs(apt_output([
            "apt-cache",
            "show",
            "{}:{}={}".format(package, architecture, version),
        ]))
        matches = [
            fields for fields in records
            if url_path.endswith("/" + fields.get("Filename", ""))
        ]
    if len(matches) != 1:
        raise ValueError(
            "{}: expected one apt-cache record for {}, got {}".format(
                entry["url"], entry["filename"], len(matches)
            )
        )
    fields = matches[0]
    sha256 = fields.get("SHA256")
    if not sha256:
        raise ValueError("{} has no SHA256 in apt metadata".format(package))
    return {
        "architecture": fields["Architecture"],
        "filename": os.path.basename(fields["Filename"]),
        "package": fields["Package"],
        "sha256": sha256,
        "size": int(fields["Size"]),
        "source": fields.get("Source", fields["Package"]).split()[0],
        "target": target_name("deb", fields["Package"], fields["Version"], fields["Architecture"]),
        "url": entry["url"],
        "version": fields["Version"],
    }


def target_name(*parts):
    return TARGET_RE.sub("-", "-".join(parts)).strip("-")


def source_record(package):
    uri_entries = apt_uri_lines(apt_output([
        "apt-get", "source", "--print-uris", "--download-only", package,
    ]))
    dsc_entries = [entry for entry in uri_entries if entry["filename"].endswith(".dsc")]
    if len(dsc_entries) != 1:
        raise ValueError("{}: expected one .dsc URI, got {}".format(package, len(dsc_entries)))
    dsc_name = dsc_entries[0]["filename"]

    records = parse_control_paragraphs(apt_output(["apt-cache", "showsrc", package]))
    matches = []
    for fields in records:
        checksums = fields.get("Checksums-Sha256", "")
        if any(line.split()[-1:] == [dsc_name] for line in checksums.splitlines()):
            matches.append(fields)
    if len(matches) != 1:
        raise ValueError(
            "{}: expected one source record for {}, got {}".format(
                package, dsc_name, len(matches)
            )
        )
    fields = matches[0]
    full_version = fields["Version"]
    upstream, separator, revision = full_version.rpartition("-")
    if not separator:
        upstream, revision = full_version, ""

    files = []
    for entry in uri_entries:
        if entry["digest_kind"] != "sha256":
            raise ValueError("{}: apt did not report SHA256".format(entry["filename"]))
        files.append({
            "filename": entry["filename"],
            "sha256": entry["digest"],
            "size": entry["size"],
            "target": target_name("source", entry["filename"]),
            "url": entry["url"],
        })

    binaries = [item.strip() for item in fields.get("Binary", package).split(",")]
    build_dep_fields = (
        fields.get("Build-Depends", ""),
        fields.get("Build-Depends-Arch", ""),
        fields.get("Build-Depends-Indep", ""),
    )
    return {
        "architecture": fields.get("Architecture", "any"),
        "binaries": binaries,
        "build_depends": ", ".join(value for value in build_dep_fields if value),
        "files": files,
        "homepage": fields.get("Homepage"),
        "name": fields["Package"],
        "release": revision,
        "version": upstream,
        "version_full": full_version,
    }


def os_release():
    fields = {}
    with open("/etc/os-release", encoding="utf-8") as stream:
        for line in stream:
            if "=" not in line:
                continue
            key, value = line.rstrip().split("=", 1)
            fields[key] = value.strip('"')
    return fields


def parse_named_packages(value):
    name, separator, packages = value.partition("=")
    roots = [item.strip() for item in packages.split(",") if item.strip()]
    if not separator or not name or not roots:
        raise argparse.ArgumentTypeError("expected NAME=package,package")
    return name, roots


def default_repositories(distro, codename, architecture):
    if distro == "debian":
        signed_by = "[signed-by=/usr/share/keyrings/debian-archive-keyring.gpg]"
        return [
            "deb {} https://deb.debian.org/debian {} main".format(signed_by, codename),
            "deb-src {} https://deb.debian.org/debian {} main".format(signed_by, codename),
            "deb {} https://deb.debian.org/debian {}-updates main".format(signed_by, codename),
            "deb-src {} https://deb.debian.org/debian {}-updates main".format(signed_by, codename),
            "deb {} https://security.debian.org/debian-security {}-security main".format(signed_by, codename),
            "deb-src {} https://security.debian.org/debian-security {}-security main".format(signed_by, codename),
        ]
    archive = "http://ports.ubuntu.com/ubuntu-ports" if architecture == "arm64" else "http://archive.ubuntu.com/ubuntu"
    security = archive if architecture == "arm64" else "http://security.ubuntu.com/ubuntu"
    return [
        "deb {} {} main universe".format(archive, codename),
        "deb-src {} {} main universe".format(archive, codename),
        "deb {} {}-updates main universe".format(archive, codename),
        "deb-src {} {}-updates main universe".format(archive, codename),
        "deb {} {}-security main universe".format(security, codename),
        "deb-src {} {}-security main universe".format(security, codename),
    ]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--distro", choices=("debian", "ubuntu"), required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--codename", required=True)
    parser.add_argument("--architecture", default="amd64")
    parser.add_argument("--source", action="append", required=True)
    parser.add_argument("--image", action="append", default=[], type=parse_named_packages)
    parser.add_argument("--repository", action="append", default=[])
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="deb-lock: %(message)s",
    )

    target_cpu = {"amd64": "x86_64", "arm64": "aarch64"}.get(args.architecture)
    if target_cpu is None:
        sys.exit("unsupported Debian-family architecture: {}".format(args.architecture))

    LOG.info(
        "resolving %s %s (%s) for %s",
        args.distro,
        args.release,
        args.codename,
        args.architecture,
    )
    with tempfile.TemporaryDirectory(prefix="buckos-apt-state-") as apt_state:
        status = os.path.join(apt_state, "status")
        archives = os.path.join(apt_state, "archives")
        lists = os.path.join(apt_state, "lists")
        sources_list = os.path.join(apt_state, "sources.list")
        with open(status, "w", encoding="utf-8"):
            pass
        os.makedirs(os.path.join(archives, "partial"))
        os.makedirs(os.path.join(lists, "partial"))
        repositories = args.repository or default_repositories(
            args.distro,
            args.codename,
            args.architecture,
        )
        with open(sources_list, "w", encoding="utf-8") as stream:
            stream.write("\n".join(repositories) + "\n")

        global APT_CONFIG, AVAILABLE_BY_FILENAME
        APT_CONFIG = apt_options(
            status,
            archives,
            architecture=args.architecture,
            lists=lists,
            sources=sources_list,
        )[:-3]
        apt_output(["apt-get", "update"])
        sources = [source_record(name) for name in args.source]
        essential = essential_packages()
        build_output = run_output(
            ["apt-get"] + apt_options(
                status, archives, args.architecture, lists, sources_list,
            ) + ["build-dep"] + args.source
        )
        base_roots = essential + ["build-essential", "fakeroot"]
        base_output = run_output(
            ["apt-get"] + apt_options(
                status, archives, args.architecture, lists, sources_list,
            ) + ["install"] + base_roots
        )
        image_output = {}
        for name, roots in args.image:
            if name in image_output:
                sys.exit("duplicate image set: {}".format(name))
            image_output[name] = run_output(
                ["apt-get"] + apt_options(
                    status, archives, args.architecture, lists, sources_list,
                ) + ["install"] + essential + roots
            )
        AVAILABLE_BY_FILENAME = {
            fields.get("Filename"): fields
            for fields in parse_control_paragraphs(apt_output(["apt-cache", "dumpavail"]))
            if fields.get("Filename")
        }

    by_target = {}
    for entry in apt_uri_lines(build_output) + apt_uri_lines(base_output):
        record = binary_record(entry)
        previous = by_target.get(record["target"])
        if previous and previous["sha256"] != record["sha256"]:
            raise ValueError("conflicting pins for {}".format(record["target"]))
        by_target[record["target"]] = record

    image_sets = {}
    for name, output in image_output.items():
        records = {}
        for entry in apt_uri_lines(output):
            record = binary_record(entry)
            previous = records.get(record["target"])
            if previous and previous["sha256"] != record["sha256"]:
                raise ValueError("conflicting pins for {}".format(record["target"]))
            records[record["target"]] = record
        image_sets[name] = sorted(records.values(), key=lambda item: item["target"])

    lock = {
        "architecture": args.architecture,
        "codename": args.codename,
        "distro": args.distro,
        "release": args.release,
        "image_sets": image_sets,
        "repositories": repositories,
        "schema": 2,
        "seed_debs": sorted(by_target.values(), key=lambda item: item["target"]),
        "sources": sorted(sources, key=lambda item: item["name"]),
        "target_cpu": target_cpu,
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as stream:
        json.dump(lock, stream, indent=2, sort_keys=True)
        stream.write("\n")
    LOG.info(
        "wrote %s: %d seed debs, %d sources",
        args.output,
        len(lock["seed_debs"]),
        len(lock["sources"]),
    )


if __name__ == "__main__":
    main()
