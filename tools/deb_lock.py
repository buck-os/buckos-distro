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


def run_output(command):
    LOG.debug("+ %s", " ".join(shlex.quote(part) for part in command))
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    return result.stdout


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


def apt_options(status, archives):
    return [
        "-o", "Dir::State::status={}".format(status),
        "-o", "Dir::Cache::archives={}".format(archives),
        "-o", "APT::Install-Recommends=false",
        "-o", "APT::Install-Suggests=false",
        "--print-uris",
        "--yes",
        "--download-only",
    ]


def essential_packages():
    packages = []
    for fields in parse_control_paragraphs(run_output(["apt-cache", "dumpavail"])):
        if fields.get("Essential") == "yes":
            packages.append(fields["Package"])
    return sorted(set(packages))


def binary_record(entry):
    package = entry["filename"].split("_", 1)[0]
    url_path = urllib.parse.unquote(urllib.parse.urlsplit(entry["url"]).path)
    records = parse_control_paragraphs(run_output(["apt-cache", "show", package]))
    matches = [
        fields for fields in records
        if url_path.endswith("/" + fields.get("Filename", ""))
    ]
    if len(matches) != 1:
        raise ValueError(
            "{}: expected one apt-cache record for {}, got {}".format(
                entry["url"], package, len(matches)
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
    uri_entries = apt_uri_lines(run_output([
        "apt-get", "source", "--print-uris", "--download-only", package,
    ]))
    dsc_entries = [entry for entry in uri_entries if entry["filename"].endswith(".dsc")]
    if len(dsc_entries) != 1:
        raise ValueError("{}: expected one .dsc URI, got {}".format(package, len(dsc_entries)))
    dsc_name = dsc_entries[0]["filename"]

    records = parse_control_paragraphs(run_output(["apt-cache", "showsrc", package]))
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--distro", choices=("debian", "ubuntu"), required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--codename", required=True)
    parser.add_argument("--architecture", default="amd64")
    parser.add_argument("--source", action="append", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="deb-lock: %(message)s",
    )

    release = os_release()
    if release.get("ID") != args.distro or release.get("VERSION_ID") != args.release:
        sys.exit(
            "{} lock must run on {} {}; found {} {}".format(
                args.distro,
                args.distro,
                args.release,
                release.get("ID", "unknown"),
                release.get("VERSION_ID", "unknown"),
            )
        )
    if release.get("VERSION_CODENAME") != args.codename:
        sys.exit(
            "{} codename mismatch: expected {}, found {}".format(
                args.distro, args.codename, release.get("VERSION_CODENAME", "unknown")
            )
        )
    architecture = run_output(["dpkg", "--print-architecture"]).strip()
    if architecture != args.architecture:
        sys.exit(
            "{} architecture mismatch: expected {}, found {}".format(
                args.distro, args.architecture, architecture or "unknown"
            )
        )

    LOG.info(
        "resolving %s %s (%s) for %s",
        args.distro,
        args.release,
        args.codename,
        args.architecture,
    )
    sources = [source_record(name) for name in args.source]
    with tempfile.TemporaryDirectory(prefix="buckos-apt-state-") as apt_state:
        status = os.path.join(apt_state, "status")
        archives = os.path.join(apt_state, "archives")
        with open(status, "w", encoding="utf-8"):
            pass
        os.makedirs(os.path.join(archives, "partial"))
        build_output = run_output(
            ["apt-get"] + apt_options(status, archives) + ["build-dep"] + args.source
        )
        base_roots = essential_packages() + ["build-essential", "fakeroot"]
        base_output = run_output(
            ["apt-get"] + apt_options(status, archives) + ["install"] + base_roots
        )

    by_target = {}
    for entry in apt_uri_lines(build_output) + apt_uri_lines(base_output):
        record = binary_record(entry)
        previous = by_target.get(record["target"])
        if previous and previous["sha256"] != record["sha256"]:
            raise ValueError("conflicting pins for {}".format(record["target"]))
        by_target[record["target"]] = record

    lock = {
        "architecture": args.architecture,
        "codename": args.codename,
        "distro": args.distro,
        "release": args.release,
        "schema": 1,
        "seed_debs": sorted(by_target.values(), key=lambda item: item["target"]),
        "sources": sorted(sources, key=lambda item: item["name"]),
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
