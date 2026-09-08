#!/usr/bin/env python3
"""Inspect and maintain the repository's distro lockfiles.

Selectors use FLAVOR[:RELEASE[:ARCH]], for example `fedora:45:x86_64`.
An exact repository-relative lockfile path is also accepted.
"""

import argparse
import contextlib
import filecmp
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import deb_generate
import generate as rpm_generate
from _lockfile import (
    GZIP_SUFFIX,
    LOCKFILE_SUFFIXES,
    atomic_dump_lockfile,
    configured_compression,
    configured_max_tracked_file_size,
    convert_lockfile,
    dump_lockfile,
    load_lockfile,
    strip_lockfile_suffix,
)


ARCHITECTURES = ("x86_64", "aarch64")
RPM_FLAVORS = ("fedora", "centos", "centos-hyperscale")
DEB_FLAVORS = ("debian", "ubuntu")
@dataclass(frozen=True)
class Lockfile:
    root: Path
    path: Path
    flavor: str
    release: str
    architecture: str

    @property
    def relative(self):
        return self.path.relative_to(self.root).as_posix()

    @property
    def selector(self):
        return "{}:{}:{}".format(
            self.flavor, self.release, self.architecture,
        )

    @property
    def compression(self):
        return "gzip" if str(self.path).endswith(GZIP_SUFFIX) else "none"

    @property
    def stem(self):
        return strip_lockfile_suffix(self.path.name)


def repo_root(start=None):
    """Find the checkout root from a path inside it."""
    directory = Path(start or os.getcwd()).resolve()
    if directory.is_file():
        directory = directory.parent
    for candidate in (directory, *directory.parents):
        if (candidate / ".buckroot").is_file():
            return candidate
    raise ValueError("cannot find repository root above {}".format(directory))


def record_from_path(root, path):
    path = Path(path).resolve()
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise ValueError("lockfile is outside the repository: {}".format(path)) from error

    parts = relative.parts
    if len(parts) != 4 or parts[0] != "flavors" or parts[2] != "lock":
        raise ValueError(
            "lockfile must be under flavors/<flavor>/lock: {}".format(relative)
        )
    flavor = parts[1]
    stem = strip_lockfile_suffix(parts[3])
    prefix = flavor + "-"
    if not stem.startswith(prefix):
        raise ValueError("lockfile name does not start with {}: {}".format(
            prefix, relative,
        ))
    release_arch = stem[len(prefix):]
    release, separator, architecture = release_arch.rpartition("-")
    if not separator or not release or architecture not in ARCHITECTURES:
        raise ValueError(
            "lockfile name must end in -<release>-<architecture>: {}".format(
                relative,
            )
        )
    return Lockfile(root, path, flavor, release, architecture)


def discover(root):
    """Find every checked-in lockfile and reject duplicate encodings."""
    root = Path(root).resolve()
    records = []
    flavor_root = root / "flavors"
    if not flavor_root.is_dir():
        raise ValueError("no flavors directory under {}".format(root))
    for flavor_dir in sorted(flavor_root.iterdir()):
        lock_dir = flavor_dir / "lock"
        if not lock_dir.is_dir():
            continue
        for path in sorted(lock_dir.iterdir()):
            if path.is_file() and any(
                    path.name.endswith(suffix) for suffix in LOCKFILE_SUFFIXES):
                records.append(record_from_path(root, path))

    by_identity = {}
    for record in records:
        previous = by_identity.get(record.selector)
        if previous is not None:
            raise ValueError(
                "{} exists in both {} and {}".format(
                    record.selector, previous.relative, record.relative,
                )
            )
        by_identity[record.selector] = record
    return sorted(
        records,
        key=record_sort_key,
    )


def natural_key(value):
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in re.split(r"(\d+)", value)
    )


def record_sort_key(record):
    architecture = ARCHITECTURES.index(record.architecture)
    return record.flavor, natural_key(record.release), architecture


def select(records, selectors):
    """Resolve logical selectors or exact paths to a deduplicated list."""
    if not selectors:
        return list(records)
    selected = {}
    for selector in selectors:
        candidate = Path(selector)
        if not candidate.is_absolute() and records:
            candidate = records[0].root / candidate
        try:
            normalized = candidate.resolve().relative_to(records[0].root).as_posix()
        except (IndexError, ValueError):
            normalized = selector.replace(os.sep, "/")
        matches = [record for record in records if record.relative == normalized]
        if not matches:
            fields = selector.split(":")
            if not 1 <= len(fields) <= 3 or any(not field for field in fields):
                raise ValueError(
                    "invalid selector {!r}; use FLAVOR[:RELEASE[:ARCH]]".format(
                        selector,
                    )
                )
            matches = [
                record for record in records
                if record.flavor == fields[0]
                and (len(fields) < 2 or record.release == fields[1])
                and (len(fields) < 3 or record.architecture == fields[2])
            ]
        if not matches:
            raise ValueError("selector matched no lockfiles: {}".format(selector))
        for record in matches:
            selected[record.selector] = record
    return sorted(
        selected.values(),
        key=record_sort_key,
    )


def select_one(records, selector):
    matches = select(records, [selector])
    if len(matches) != 1:
        raise ValueError(
            "selector matched {} lockfiles; add release and architecture: {}".format(
                len(matches), selector,
            )
        )
    return matches[0]


def lock_identity(lock):
    if not isinstance(lock, dict):
        raise ValueError("top-level JSON value must be an object")
    flavor = lock.get("flavor", lock.get("distro"))
    return flavor, str(lock.get("release")), lock.get("target_cpu")


def identity_errors(record, lock):
    actual = lock_identity(lock)
    expected = (record.flavor, record.release, record.architecture)
    if actual == expected:
        return []
    return [
        "path identifies {}, but JSON identifies {}".format(
            ":".join(expected),
            ":".join("<missing>" if value is None else str(value) for value in actual),
        )
    ]


def canonical_error(record, lock):
    with tempfile.TemporaryDirectory() as directory:
        candidate = Path(directory) / record.path.name
        dump_lockfile(lock, str(candidate))
        if not filecmp.cmp(record.path, candidate, shallow=False):
            return (
                "storage is not canonical; run `./lockfiles convert {}`"
                .format(record.selector)
            )
    return None


@contextlib.contextmanager
def in_directory(path):
    previous = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def generated_error(record):
    destination = (
        record.root / "flavors" / record.flavor / "generated"
        / (record.stem + ".bzl")
    )
    if not destination.is_file():
        return "generated data is missing: {}".format(
            destination.relative_to(record.root),
        )

    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / (record.stem + ".bzl")
        diagnostics = io.StringIO()
        expected = [output]
        try:
            with in_directory(record.root), contextlib.redirect_stderr(diagnostics):
                if record.flavor in RPM_FLAVORS:
                    rpm_generate.main([
                        record.relative,
                        "--out-dir",
                        directory,
                    ])
                elif record.flavor in DEB_FLAVORS:
                    expected = [Path(path) for path in deb_generate.main([
                        record.relative,
                        "--output",
                        str(output),
                    ])]
                else:
                    return "no generator for flavor {!r}".format(record.flavor)
        except (Exception, SystemExit) as error:
            detail = diagnostics.getvalue().strip()
            return "generator rejected lockfile: {}{}".format(
                error,
                ": " + detail if detail else "",
            )
        expected_names = {path.name for path in expected}
        max_file_size = configured_max_tracked_file_size(record.root)
        for expected_path in expected:
            actual = destination.parent / expected_path.name
            if not actual.is_file():
                return "generated data is missing: {}".format(
                    actual.relative_to(record.root),
                )
            if not filecmp.cmp(actual, expected_path, shallow=False):
                return "generated data is stale: {}".format(
                    actual.relative_to(record.root),
                )
            size = actual.stat().st_size
            if size >= max_file_size:
                return "generated data is {} bytes, exceeding the {} byte repository limit: {}".format(
                    size,
                    max_file_size,
                    actual.relative_to(record.root),
                )

        if record.flavor in DEB_FLAVORS:
            prefix = destination.stem + "-sources-"
            actual_shards = {
                path.name
                for path in destination.parent.iterdir()
                if path.is_file()
                and path.name.startswith(prefix)
                and path.suffix == ".bzl"
            }
            stale = sorted(actual_shards - expected_names)
            if stale:
                return "generated data has stale source shard: {}".format(
                    destination.parent.joinpath(stale[0]).relative_to(record.root),
                )
    return None


def generated_index_errors(root, records, flavors):
    """Check that each selected flavor's generated index names every lock."""
    errors = []
    for flavor in sorted(flavors):
        flavor_records = [record for record in records if record.flavor == flavor]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            for record in flavor_records:
                (output / (record.stem + ".bzl")).touch()
            if flavor in RPM_FLAVORS:
                rpm_generate.write_index(str(output), flavor)
            elif flavor in DEB_FLAVORS:
                deb_generate.write_index(str(output), flavor)
            else:
                errors.append((flavor, "no generator for flavor {!r}".format(flavor)))
                continue
            actual = root / "flavors" / flavor / "generated" / "index.bzl"
            expected = output / "index.bzl"
            if not actual.is_file():
                errors.append((flavor, "generated index is missing: {}".format(
                    actual.relative_to(root),
                )))
            elif not filecmp.cmp(actual, expected, shallow=False):
                errors.append((flavor, "generated index is stale: {}".format(
                    actual.relative_to(root),
                )))
    return errors


def validate(record, check_generated=True):
    errors = []
    try:
        lock = load_lockfile(str(record.path))
    except Exception as error:
        return ["cannot read JSON: {}".format(error)]

    try:
        errors.extend(identity_errors(record, lock))
    except ValueError as error:
        errors.append(str(error))

    size = record.path.stat().st_size
    max_file_size = configured_max_tracked_file_size(record.root)
    if size >= max_file_size:
        errors.append(
            "{} bytes exceeds the {} byte repository limit".format(
                size, max_file_size,
            )
        )

    if record.compression == "gzip":
        header = record.path.read_bytes()[:10]
        if len(header) != 10 or header[:2] != b"\x1f\x8b":
            errors.append("file has a .gz suffix but no gzip header")
        else:
            if header[4:8] != b"\0\0\0\0":
                errors.append("gzip header records a timestamp")
            if header[3] & 0x08:
                errors.append("gzip header records a source filename")

    error = canonical_error(record, lock)
    if error:
        errors.append(error)
    if check_generated and not errors:
        error = generated_error(record)
        if error:
            errors.append(error)
    return errors


def json_pointer(value, pointer):
    """Resolve an RFC 6901 JSON pointer."""
    if pointer == "":
        return value
    if not pointer.startswith("/"):
        raise ValueError("JSON pointer must be empty or start with /")
    current = value
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        try:
            if isinstance(current, list):
                current = current[int(token)]
            else:
                current = current[token]
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise ValueError(
                "JSON pointer component {!r} does not exist".format(token)
            ) from error
    return current


def generate_records(records):
    if not records:
        raise ValueError("no lockfiles selected")
    with in_directory(records[0].root):
        rpm = [record.relative for record in records if record.flavor in RPM_FLAVORS]
        if rpm:
            rpm_generate.main(rpm)
        for record in records:
            if record.flavor in DEB_FLAVORS:
                deb_generate.main([record.relative])


def validate_replacement(record, lock):
    errors = identity_errors(record, lock)
    if errors:
        raise ValueError("; ".join(errors))
    if record.flavor in RPM_FLAVORS:
        if lock.get("schema") != rpm_generate.LOCK_SCHEMA:
            raise ValueError(
                "unsupported RPM lock schema {}; expected {}".format(
                    lock.get("schema"), rpm_generate.LOCK_SCHEMA,
                )
            )
        rpm_generate.validate_lock_source_policy(lock, record.relative)
    elif record.flavor in DEB_FLAVORS:
        deb_generate.validate_lock(lock)
    else:
        raise ValueError("no generator for flavor {!r}".format(record.flavor))


def replace_record(record, lock, regenerate=True):
    """Validate and atomically replace one lock, preserving its encoding."""
    validate_replacement(record, lock)
    with tempfile.TemporaryDirectory() as directory:
        candidate = Path(directory) / record.path.name
        dump_lockfile(lock, str(candidate))
        max_file_size = configured_max_tracked_file_size(record.root)
        if candidate.stat().st_size >= max_file_size:
            raise ValueError(
                "replacement would exceed the {} byte repository limit".format(
                    max_file_size,
                )
            )
    atomic_dump_lockfile(lock, str(record.path))
    if regenerate:
        generate_records([record])


def require_mutation_selection(parser, args):
    if args.selectors and args.all:
        parser.error("pass selectors or --all, not both")
    if not args.selectors and not args.all:
        parser.error("name at least one selector, or pass --all")


def human_size(size):
    if size < 1024:
        return "{} B".format(size)
    if size < 1024 * 1024:
        return "{:.1f} KiB".format(size / 1024)
    return "{:.2f} MiB".format(size / (1024 * 1024))


def print_json(value):
    json.dump(value, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        default=None,
        help="repository root (normally discovered automatically)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    list_parser = commands.add_parser("list", help="list available lockfiles")
    list_parser.add_argument("selectors", nargs="*")

    path_parser = commands.add_parser("path", help="print one lockfile path")
    path_parser.add_argument("selector")

    show_parser = commands.add_parser("show", help="print decoded JSON")
    show_parser.add_argument("selector")

    get_parser = commands.add_parser("get", help="print one value by JSON pointer")
    get_parser.add_argument("selector")
    get_parser.add_argument("pointer", help="RFC 6901 pointer, such as /solve/build")

    check_parser = commands.add_parser("check", help="validate lockfiles and generated data")
    check_parser.add_argument("selectors", nargs="*")
    check_parser.add_argument(
        "--no-generated",
        action="store_true",
        help="skip the generated Starlark freshness check",
    )

    generate_parser = commands.add_parser("generate", help="regenerate Starlark data")
    generate_parser.add_argument("selectors", nargs="*")
    generate_parser.add_argument("--all", action="store_true")

    convert_parser = commands.add_parser(
        "convert", help="atomically convert or normalize lockfiles",
    )
    convert_parser.add_argument("selectors", nargs="*")
    convert_parser.add_argument("--all", action="store_true")
    convert_parser.add_argument(
        "--compression",
        choices=("none", "gzip"),
        default=None,
        help="override [buckos.lockfiles] compression",
    )
    convert_parser.add_argument(
        "--no-generate",
        action="store_true",
        help="do not update generated Starlark after conversion",
    )

    replace_parser = commands.add_parser(
        "replace", help="replace one lock from decoded JSON",
    )
    replace_parser.add_argument("selector")
    replace_parser.add_argument(
        "input",
        nargs="?",
        default="-",
        help="JSON file to read, or - for stdin (default: -)",
    )
    replace_parser.add_argument("--no-generate", action="store_true")

    edit_parser = commands.add_parser(
        "edit", help="edit one lock as temporary plain JSON",
    )
    edit_parser.add_argument("selector")
    edit_parser.add_argument(
        "--editor",
        default=None,
        help="editor command (default: VISUAL, then EDITOR)",
    )
    edit_parser.add_argument("--no-generate", action="store_true")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        root = repo_root(args.root)
        records = discover(root)

        if args.command == "list":
            chosen = select(records, args.selectors)
            print("SELECTOR                              FORMAT  SIZE      SCHEMA  SOURCES")
            for record in chosen:
                lock = load_lockfile(str(record.path))
                source_count = len(lock.get("packages", lock.get("sources", [])))
                print("{:<37} {:<7} {:<9} {:<7} {}".format(
                    record.selector,
                    record.compression,
                    human_size(record.path.stat().st_size),
                    lock.get("schema", "?"),
                    source_count,
                ))
            return 0

        if args.command in ("path", "show", "get"):
            record = select_one(records, args.selector)
            if args.command == "path":
                print(record.relative)
                return 0
            value = load_lockfile(str(record.path))
            if args.command == "get":
                value = json_pointer(value, args.pointer)
            print_json(value)
            return 0

        if args.command == "replace":
            record = select_one(records, args.selector)
            if args.input == "-":
                if sys.stdin.isatty():
                    parser.error("replace needs a JSON file or JSON on stdin")
                replacement = json.load(sys.stdin)
            else:
                with open(args.input, encoding="utf-8") as stream:
                    replacement = json.load(stream)
            replace_record(
                record,
                replacement,
                regenerate=not args.no_generate,
            )
            print("updated {}".format(record.relative))
            return 0

        if args.command == "edit":
            record = select_one(records, args.selector)
            editor = args.editor or os.environ.get("VISUAL") or os.environ.get("EDITOR")
            if not editor:
                parser.error("edit needs --editor, VISUAL, or EDITOR")
            before = load_lockfile(str(record.path))
            fd, temporary = tempfile.mkstemp(
                prefix=record.stem + "-",
                suffix=".lock.json",
            )
            os.close(fd)
            try:
                dump_lockfile(before, temporary)
                result = subprocess.run(shlex.split(editor) + [temporary])
                if result.returncode:
                    raise ValueError(
                        "editor exited with status {}".format(result.returncode)
                    )
                edited = load_lockfile(temporary)
                if edited == before:
                    print("unchanged {}".format(record.relative))
                    return 0
                replace_record(
                    record,
                    edited,
                    regenerate=not args.no_generate,
                )
                print("updated {}".format(record.relative))
                return 0
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

        if args.command == "check":
            chosen = select(records, args.selectors)
            failures = 0
            for record in chosen:
                errors = validate(record, check_generated=not args.no_generated)
                if errors:
                    failures += 1
                    print("FAIL {}".format(record.selector))
                    for error in errors:
                        print("  {}".format(error))
                else:
                    print("ok   {} ({}, {})".format(
                        record.selector,
                        record.compression,
                        human_size(record.path.stat().st_size),
                    ))
            if not args.no_generated:
                flavors = {record.flavor for record in chosen}
                for flavor, error in generated_index_errors(root, records, flavors):
                    failures += 1
                    print("FAIL {}:index\n  {}".format(flavor, error))
            print("{} lockfile(s) checked, {} failure(s)".format(
                len(chosen), failures,
            ))
            return 1 if failures else 0

        require_mutation_selection(parser, args)
        chosen = select(records, args.selectors)
        if args.command == "generate":
            generate_records(chosen)
            return 0

        compression = args.compression or configured_compression(str(root))
        converted = []
        for record in chosen:
            destination = convert_lockfile(str(record.path), compression)
            converted.append(record_from_path(root, destination))
            print("{} -> {}".format(
                record.relative,
                Path(destination).relative_to(root),
            ))
        if not args.no_generate:
            generate_records(converted)
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.exit(1, "lockfile: {}\n".format(error))


if __name__ == "__main__":
    sys.exit(main())
