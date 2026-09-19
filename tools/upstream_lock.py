#!/usr/bin/env python3
"""Resolve a binary-only remote repository overlay against a base lockfile.

The result is a separate, reviewable lockfile.  It contains the complete
package transaction (because a remote repository may replace a base package),
but is tied to the exact semantic digest of the base lock it was solved with.
"""

import argparse
import hashlib
import ipaddress
import os
import re
import sys
import tempfile
import urllib.parse

import deb_lock
import generate
from _lockfile import dump_lockfile, load_lockfile, lockfile_digest
import relock
import rpm_relock
import solve


SCHEMA = 1
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
PPA_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_.-]*/[A-Za-z0-9][A-Za-z0-9+_.-]*$")


def family_of(lock):
    if lock.get("distro") in ("debian", "ubuntu"):
        return "deb"
    if lock.get("flavor") in ("centos", "centos-hyperscale", "fedora"):
        return "rpm"
    raise ValueError("base lockfile is not a supported Debian or RPM family")


def base_identity(lock, path):
    family = family_of(lock)
    return {
        "architecture": lock["target_cpu"],
        "flavor": lock.get("distro") or lock.get("flavor"),
        "path": os.path.basename(path),
        "release": str(lock["release"]),
        "sha256": lockfile_digest(lock),
    }, family


def is_kernel_entry(family, entry):
    if family == "rpm":
        return entry["name"] == "kernel" or entry["name"].startswith("kernel-")
    source = entry.get("source", "")
    return (
        source == "linux"
        or source.startswith("linux@")
        or source.startswith("linux-main-")
        or source.startswith("linux-meta")
        or source.startswith("linux-signed")
    )


def base_image_packages(lock, family, image_set):
    sets = lock.get("image_sets", {})
    selected = image_set
    if selected is None:
        selected = "base" if "base" in sets else "live"
    if selected not in sets:
        raise ValueError(
            "base image set {!r} is missing (have: {})".format(
                selected, ", ".join(sorted(sets)) or "none"
            )
        )
    entries = list(sets[selected])
    # Existing flavor macros derive their reusable base from live this way.
    if selected == "live":
        entries = [entry for entry in entries if not is_kernel_entry(family, entry)]
    if not entries:
        raise ValueError(
            "base image set {!r} contains no userspace packages".format(selected)
        )
    return selected, entries


def package_key(family, entry):
    if family == "rpm":
        return entry["name"], entry["arch"]
    return entry["package"], entry["architecture"]


def package_version(family, entry):
    return entry["evr"] if family == "rpm" else entry["version"]


def package_name(family, entry):
    return entry["name"] if family == "rpm" else entry["package"]


def change_summary(family, base_entries, resolved):
    old = {package_key(family, entry): entry for entry in base_entries}
    new = {package_key(family, entry): entry for entry in resolved}
    additions = sorted(
        package_name(family, entry)
        for key, entry in new.items()
        if key not in old
    )
    replacements = []
    for key in sorted(set(old) & set(new)):
        before, after = old[key], new[key]
        if before.get("sha256") == after.get("sha256"):
            continue
        replacements.append({
            "architecture": key[1],
            "from": package_version(family, before),
            "name": key[0],
            "to": package_version(family, after),
        })
    removals = sorted(
        package_name(family, entry)
        for key, entry in old.items()
        if key not in new
    )
    return {
        "additions": additions,
        "removals": removals,
        "replacements": replacements,
    }


def validate_remote_url(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("repository URL must be absolute http(s): {!r}".format(url))
    if parts.username or parts.password:
        raise ValueError("repository URL must not contain credentials")
    if parts.port not in (None, 80, 443):
        raise ValueError("repository URL must use the default HTTP(S) port")
    host = parts.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("repository URL is not publicly addressable: {!r}".format(url))
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host:
            raise ValueError("repository URL host is not public: {!r}".format(host))
    else:
        if not address.is_global:
            raise ValueError("repository URL IP is not public: {!r}".format(host))
    return url.rstrip("/")


def parse_named_repository(value):
    name, separator, url = value.partition("=")
    if not separator or not NAME_RE.fullmatch(name):
        raise argparse.ArgumentTypeError("expected NAME=https://public.example/repo")
    try:
        url = validate_remote_url(url)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error
    return name, url


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_ppa(value, codename, architecture):
    identity, separator, keyring = value.partition("=")
    if not separator or not PPA_RE.fullmatch(identity) or not keyring:
        raise ValueError("expected OWNER/ARCHIVE=/path/to/keyring.gpg")
    keyring = os.path.abspath(keyring)
    if not os.path.isfile(keyring):
        raise ValueError("PPA keyring does not exist: {}".format(keyring))
    owner, archive = identity.split("/", 1)
    url = "https://ppa.launchpadcontent.net/{}/{}/ubuntu".format(owner, archive)
    line = "deb [arch={} signed-by={}] {} {} main".format(
        architecture, keyring, url, codename
    )
    return line, {
        "archive": archive,
        "components": ["main"],
        "key_sha256": sha256_file(keyring),
        "kind": "ppa",
        "owner": owner,
        "suite": codename,
        "url": url,
    }


def apt_repository_record(line):
    if not line.startswith("deb "):
        raise ValueError("additional APT repository must be a binary 'deb' line")
    if "trusted=yes" in line.lower():
        raise ValueError("additional APT repositories may not use trusted=yes")
    match = re.search(r"(?:^|[\s\[])signed-by=([^\]\s]+)", line)
    if match is None:
        raise ValueError("additional APT repository requires signed-by=/path/to/keyring")
    keyring = os.path.abspath(match.group(1))
    if not os.path.isfile(keyring):
        raise ValueError("APT repository keyring does not exist: {}".format(keyring))
    urls = [part for part in line.split() if part.startswith(("http://", "https://"))]
    if len(urls) != 1:
        raise ValueError("APT repository must contain exactly one http(s) URL")
    validate_remote_url(urls[0])
    return {
        "key_sha256": sha256_file(keyring),
        "kind": "apt",
        "source": line.replace(match.group(1), os.path.basename(keyring)),
        "url": urls[0].rstrip("/"),
    }


def resolve_deb(base, base_entries, packages, repository_lines):
    repositories = list(base["repositories"]) + list(repository_lines)
    architecture = base["architecture"]
    with tempfile.TemporaryDirectory(prefix="buckos-overlay-apt-") as state:
        status = os.path.join(state, "status")
        archives = os.path.join(state, "archives")
        lists = os.path.join(state, "lists")
        sources = os.path.join(state, "sources.list")
        open(status, "w", encoding="utf-8").close()
        os.makedirs(os.path.join(archives, "partial"))
        os.makedirs(os.path.join(lists, "partial"))
        with open(sources, "w", encoding="utf-8") as stream:
            stream.write("\n".join(repositories) + "\n")
        deb_lock.APT_CONFIG = deb_lock.apt_options(
            status, archives, architecture=architecture, lists=lists, sources=sources
        )[:-3]
        deb_lock.AVAILABLE_BY_FILENAME = None
        deb_lock.apt_output(["apt-get", "update"])
        roots = sorted({entry["package"] for entry in base_entries} | set(packages))
        output = deb_lock.run_output(
            ["apt-get"]
            + deb_lock.apt_options(status, archives, architecture, lists, sources)
            + ["install"]
            + roots
        )
        records = deb_lock.records_by_target(deb_lock.apt_uri_lines(output))
    return sorted(records.values(), key=lambda entry: entry["target"])


def rpm_pin(name, universe):
    package = universe["binary_index"][name]
    if package.get("checksum_type") != "sha256" or not package.get("checksum"):
        raise ValueError("{} is not pinned by SHA-256 metadata".format(name))
    epoch = package.get("epoch")
    evr = "{}{}-{}".format(
        "{}:".format(epoch) if epoch and epoch != "0" else "",
        package["version"],
        package["release"],
    )
    entry = {
        "arch": package["arch"],
        "evr": evr,
        "location": package["location"],
        "name": package["name"],
        "repo": package.get("repo"),
        "sha256": package["checksum"],
    }
    entry["target"] = generate._target_name(entry)
    return entry


def resolve_rpm(base, base_entries, packages, extra_repositories, cache, offline,
                previous=None):
    previous_repositories = (
        previous.get("repositories", [])
        if previous
        and previous.get("base", {}).get("sha256") == lockfile_digest(base)
        else []
    )
    recorded_extra = {
        repo["name"]: repo.get("primary")
        for repo in previous_repositories
        if repo.get("kind") == "rpm"
    }
    repos = []
    for repo in base.get("repos", []):
        if repo.get("kind") != "binary":
            continue
        repos.append({"name": repo["name"], "base": repo["base"], "primary": repo.get("primary")})
    names = {repo["name"] for repo in repos}
    for name, url in extra_repositories:
        if name in names:
            existing = next(repo for repo in repos if repo["name"] == name)
            if existing["base"].rstrip("/") != url.rstrip("/"):
                raise ValueError(
                    "repository name {!r} already exists with base {}".format(
                        name, existing["base"]
                    )
                )
            # Selecting a repository already present in an older monolithic
            # base lock (notably EPEL) is intentionally idempotent.
            continue
        names.add(name)
        repos.append({"name": name, "base": url, "primary": recorded_extra.get(name)})

    groups = []
    synced = []
    for repo in repos:
        path = rpm_relock.repository_primary(
            repo["base"],
            os.path.join(cache, repo["name"]),
            repo["name"],
            repo.get("primary"),
            offline=offline,
        )
        groups.append((repo["name"], solve.parse_primary(path, repo=repo["name"])))
        synced.append({
            "base": repo["base"],
            "kind": "rpm",
            "name": repo["name"],
            "primary": os.path.basename(path),
        })

    merged, _replacements, superseded = solve.merge_packages(groups)
    universe = solve.build_universe(
        merged, [], target_cpu=base["target_cpu"], superseded=superseded
    )
    overrides = {}
    for item in base.get("solve", {}).get("overrides", []):
        capability, provider = solve.parse_override(item, "base override")
        overrides[capability] = provider
    roots = sorted({entry["name"] for entry in base_entries} | set(packages))
    closure, problems = solve.solve_package_set(
        universe, roots, overrides=overrides, scope="upstream overlay"
    )
    if problems:
        details = "; ".join("{}: {}".format(who, detail) for _kind, detail, who in problems)
        raise ValueError("RPM overlay solve failed: {}".format(details))
    return [rpm_pin(name, universe) for name in closure], synced


def validate_requested(family, requested, resolved):
    names = {package_name(family, entry) for entry in resolved}
    missing = sorted(set(requested) - names)
    if missing:
        raise ValueError(
            "requested package(s) absent from resolved closure: {}".format(
                ", ".join(missing)
            )
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-lock", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--base-image-set", default=None)
    parser.add_argument("--package", action="append", default=[], required=True)
    parser.add_argument("--apt-repository", action="append", default=[])
    parser.add_argument(
        "--ppa",
        action="append",
        default=[],
        metavar="OWNER/ARCHIVE=KEYRING",
    )
    parser.add_argument(
        "--rpm-repository",
        action="append",
        default=[],
        type=parse_named_repository,
        metavar="NAME=URL",
    )
    parser.add_argument("--repo-cache", default=None)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    if not NAME_RE.fullmatch(args.name):
        parser.error("--name must contain lowercase letters, digits, and hyphens")
    base = load_lockfile(args.base_lock)
    previous = load_lockfile(args.output) if os.path.isfile(args.output) else None
    try:
        identity, family = base_identity(base, args.base_lock)
        image_set, base_entries = base_image_packages(base, family, args.base_image_set)
        repositories = []
        if family == "deb":
            if args.rpm_repository:
                parser.error("--rpm-repository cannot be used with a Debian-family base")
            if args.offline:
                parser.error("--offline is currently supported only for RPM overlays")
            lines = list(args.apt_repository)
            for line in args.apt_repository:
                repositories.append(apt_repository_record(line))
            for value in args.ppa:
                line, record = parse_ppa(value, base["codename"], base["architecture"])
                lines.append(line)
                repositories.append(record)
            if not lines:
                parser.error("pass at least one --ppa or --apt-repository")
            resolved = resolve_deb(base, base_entries, args.package, lines)
        else:
            if args.ppa or args.apt_repository:
                parser.error("APT repositories cannot be used with an RPM-family base")
            if not args.rpm_repository:
                parser.error("pass at least one --rpm-repository")
            cache = args.repo_cache or os.path.join(
                os.path.dirname(os.path.abspath(args.output)),
                "repodata", args.name, base["target_cpu"],
            )
            resolved, repositories = resolve_rpm(
                base,
                base_entries,
                args.package,
                args.rpm_repository,
                cache,
                args.offline,
                previous=previous,
            )
        validate_requested(family, args.package, resolved)
    except ValueError as error:
        parser.error(str(error))

    lock = {
        "base": dict(identity, image_set=image_set),
        "changes": change_summary(family, base_entries, resolved),
        "family": family,
        "name": args.name,
        "packages": resolved,
        "repositories": repositories,
        "requested_packages": sorted(set(args.package)),
        "schema": SCHEMA,
    }
    dump_lockfile(lock, args.output)
    print(
        "{}: {} package(s), {} addition(s), {} replacement(s)".format(
            args.output,
            len(resolved),
            len(lock["changes"]["additions"]),
            len(lock["changes"]["replacements"]),
        ),
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
