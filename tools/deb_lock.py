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

from _deb import dsc_files, parse_control_paragraphs, source_identity
from source_policy import build_source_policy


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
    unique_matches = {}
    for fields in matches:
        key = (
            fields.get("Package"),
            fields.get("Version"),
            fields.get("Checksums-Sha256"),
        )
        unique_matches[key] = fields
    matches = list(unique_matches.values())
    if len(matches) != 1:
        raise ValueError(
            "{}: expected one apt-cache record for {}, got {}".format(
                entry["url"], entry["filename"], len(matches)
            )
        )
    fields = matches[0]
    sha256 = fields.get("SHA256")
    if not sha256:
        raise ValueError("{} has no SHA256 in apt metadata".format(fields["Package"]))
    source_name, source_version = source_identity(fields)
    return {
        "architecture": fields["Architecture"],
        "filename": os.path.basename(fields["Filename"]),
        "package": fields["Package"],
        "sha256": sha256,
        "size": int(fields["Size"]),
        "source": "{}@{}".format(source_name, source_version),
        "source_name": source_name,
        "source_version": source_version,
        "target": target_name("deb", fields["Package"], fields["Version"], fields["Architecture"]),
        "url": entry["url"],
        "version": fields["Version"],
    }


def target_name(*parts):
    return TARGET_RE.sub("-", "-".join(parts)).strip("-")


def apt_source_selector(package, version=None):
    selector = "src:{}".format(package)
    if version:
        selector += "={}".format(version)
    return selector


def apt_source_command(package, version=None):
    return [
        "apt-get", "source", "--print-uris", "--download-only",
        apt_source_selector(package, version),
    ]


def apt_build_dep_command(package, version):
    return [
        "apt-get", "-Pnocheck", "build-dep",
        apt_source_selector(package, version),
    ]


def source_files_from_metadata(uri_entries, fields):
    """Bind APT source URIs to authoritative SHA-256 source metadata."""
    try:
        metadata = dsc_files(fields)
    except ValueError as error:
        raise ValueError(
            "invalid source Checksums-Sha256 metadata: {}".format(error)
        ) from error
    files = []
    seen = set()
    for entry in uri_entries:
        filename = os.path.basename(entry["filename"])
        if filename in seen:
            raise ValueError("duplicate source URI filename: {!r}".format(filename))
        seen.add(filename)
        if filename not in metadata:
            raise ValueError(
                "{} is missing from source Checksums-Sha256 metadata".format(filename)
            )
        sha256, size = metadata[filename]
        if entry["size"] != size:
            raise ValueError(
                "{}: source size mismatch: URI reports {}, metadata reports {}".format(
                    filename, entry["size"], size,
                )
            )
        if entry["digest_kind"] == "sha256" and entry["digest"] != sha256:
            raise ValueError(
                "{}: source SHA-256 mismatch: URI reports {}, metadata reports {}".format(
                    filename, entry["digest"], sha256,
                )
            )
        files.append({
            "filename": filename,
            "sha256": sha256,
            "size": size,
            "target": target_name("source", filename),
            "url": entry["url"],
        })

    missing = sorted(set(metadata) - seen)
    if missing:
        raise ValueError(
            "APT produced no URI for source metadata file(s): {}".format(
                ", ".join(missing)
            )
        )
    return files


def source_record(package, version=None):
    uri_entries = apt_uri_lines(apt_output(apt_source_command(package, version)))
    dsc_entries = [entry for entry in uri_entries if entry["filename"].endswith(".dsc")]
    if len(dsc_entries) != 1:
        raise ValueError("{}: expected one .dsc URI, got {}".format(package, len(dsc_entries)))
    dsc_name = os.path.basename(dsc_entries[0]["filename"])

    records = parse_control_paragraphs(apt_output(["apt-cache", "showsrc", package]))
    matches = []
    for fields in records:
        if fields.get("Package") != package:
            continue
        if version and fields.get("Version") != version:
            continue
        checksums = fields.get("Checksums-Sha256", "")
        if any(line.split()[-1:] == [dsc_name] for line in checksums.splitlines()):
            matches.append(fields)
    unique_matches = {}
    for fields in matches:
        key = (
            fields.get("Package"),
            fields.get("Version"),
            fields.get("Checksums-Sha256"),
        )
        unique_matches[key] = fields
    matches = list(unique_matches.values())
    if len(matches) != 1:
        raise ValueError(
            "{}: expected one source record for {}, got {}".format(
                package, dsc_name, len(matches)
            )
        )
    fields = matches[0]
    full_version = fields["Version"]
    if version and full_version != version:
        raise ValueError(
            "{}: selected source version {}, expected {}".format(
                package, full_version, version,
            )
        )
    upstream, separator, revision = full_version.rpartition("-")
    if not separator:
        upstream, revision = full_version, ""

    files = source_files_from_metadata(uri_entries, fields)

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


def parse_source_exception(value):
    try:
        exception = json.loads(value)
    except json.JSONDecodeError as error:
        raise argparse.ArgumentTypeError(
            "source exception must be a JSON object"
        ) from error
    if not isinstance(exception, dict):
        raise argparse.ArgumentTypeError("source exception must be a JSON object")
    return exception


def records_by_target(entries):
    records = {}
    for entry in entries:
        record = binary_record(entry)
        previous = records.get(record["target"])
        if previous and previous["sha256"] != record["sha256"]:
            raise ValueError("conflicting pins for {}".format(record["target"]))
        records[record["target"]] = record
    return records


def source_requests(image_sets, source_sets, exceptions):
    """Group selected live binaries by exact source name and source version."""
    exception_names = {entry["package"] for entry in exceptions}
    selected = {}
    requests = {}
    for set_name in source_sets:
        if set_name not in image_sets:
            raise ValueError("source policy names missing image set {!r}".format(set_name))
        for binary in image_sets[set_name]:
            name = binary["package"]
            previous = selected.get(name)
            identity = (binary["source_name"], binary["source_version"])
            if previous and previous != identity:
                raise ValueError(
                    "binary package {} maps to conflicting sources: {} and {}".format(
                        name, previous, identity,
                    )
                )
            selected[name] = identity
            if name in exception_names:
                continue
            requests.setdefault(identity, []).append(binary)

    unused = exception_names - set(selected)
    if unused:
        raise ValueError(
            "source exceptions do not match selected packages: {}".format(
                ", ".join(sorted(unused)),
            )
        )
    return requests, selected


def dependency_overlay(base_by_target, dependencies):
    """Return the package-specific dependency closure outside the common base."""
    return sorted(
        (
            entry
            for target, entry in dependencies.items()
            if target not in base_by_target
        ),
        key=lambda item: item["target"],
    )


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
    parser.add_argument("--source", action="append", default=[])
    parser.add_argument("--source-set", action="append", default=[])
    parser.add_argument(
        "--source-exception",
        action="append",
        default=[],
        type=parse_source_exception,
    )
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
        essential = essential_packages()
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

        base_by_target = records_by_target(apt_uri_lines(base_output))
        image_sets = {
            name: sorted(
                records_by_target(apt_uri_lines(output)).values(),
                key=lambda item: item["target"],
            )
            for name, output in image_output.items()
        }
        source_sets = args.source_set or ["live"]
        requests, selected = source_requests(
            image_sets,
            source_sets,
            args.source_exception,
        )

        sources = []
        source_names = {}
        for (name, version), binaries in sorted(requests.items()):
            previous = source_names.get(name)
            if previous and previous != version:
                raise ValueError(
                    "source package {} is required at both {} and {}".format(
                        name, previous, version,
                    )
                )
            source_names[name] = version
            source = source_record(name, version)
            declared = set(source["binaries"])
            missing = sorted({entry["package"] for entry in binaries} - declared)
            if missing:
                raise ValueError(
                    "source {} {} does not declare selected binaries: {}".format(
                        name, version, ", ".join(missing),
                    )
                )
            build_output = run_output(
                ["apt-get"] + apt_options(
                    status, archives, args.architecture, lists, sources_list,
                ) + apt_build_dep_command(name, version)[1:]
            )
            deps = records_by_target(apt_uri_lines(build_output))
            source["binary_metadata"] = sorted(
                binaries,
                key=lambda item: (item["package"], item["architecture"]),
            )
            source["build_deps"] = dependency_overlay(base_by_target, deps)
            sources.append(source)

        for name in args.source:
            if name in source_names:
                continue
            source = source_record(name)
            build_output = run_output(
                ["apt-get"] + apt_options(
                    status, archives, args.architecture, lists, sources_list,
                ) + apt_build_dep_command(name, source["version_full"])[1:]
            )
            deps = records_by_target(apt_uri_lines(build_output))
            source["binary_metadata"] = []
            source["build_deps"] = dependency_overlay(base_by_target, deps)
            sources.append(source)

    producers = {
        "{}@{}".format(source["name"], source["version_full"])
        for source in sources
    }
    source_policy = build_source_policy(
        image_sets,
        producers,
        args.source_exception,
        source_sets,
    )

    lock = {
        "architecture": args.architecture,
        "codename": args.codename,
        "distro": args.distro,
        "release": args.release,
        "image_sets": image_sets,
        "repositories": repositories,
        "schema": 3,
        "base_debs": sorted(base_by_target.values(), key=lambda item: item["target"]),
        "source_policy": source_policy,
        "sources": sorted(sources, key=lambda item: item["name"]),
        "target_cpu": target_cpu,
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as stream:
        json.dump(lock, stream, indent=2, sort_keys=True)
        stream.write("\n")
    LOG.info(
        "wrote %s: %d base debs, %d sources, %d/%d selected binaries from source",
        args.output,
        len(lock["base_debs"]),
        len(lock["sources"]),
        source_policy["summary"]["live"]["source"],
        source_policy["summary"]["live"]["total"],
    )


if __name__ == "__main__":
    main()
