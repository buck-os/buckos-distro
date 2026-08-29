#!/usr/bin/env python3
"""Prepare an immutable Python probe root from an imported SDME rootfs."""

import argparse
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
from collections import deque
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


LOG = logging.getLogger("buckos-probe-root")
REPO_ROOT = Path(__file__).resolve().parents[3]
ROOTFS_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
PYTHON_BINARY = re.compile(r"^python(3\.\d+)$")

CPU_ALIASES = {
    "amd64": "x86_64",
    "arm64": "aarch64",
    "x86_64": "x86_64",
    "aarch64": "aarch64",
}

ELF_MACHINES = {
    "x86_64": 62,
    "aarch64": 183,
}

LIBRARY_TRIPLETS = {
    "x86_64": "x86_64-linux-gnu",
    "aarch64": "aarch64-linux-gnu",
}


class PreparationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ElfMetadata:
    machine: int
    interpreter: str | None
    needed: tuple[str, ...]


@dataclass(frozen=True)
class ExistingProbeRoot:
    digest: str
    runtime_fs: str
    architecture: str


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("plan", "apply"))
    parser.add_argument(
        "--runtime-fs",
        required=True,
        help="exact name of an already imported SDME runtime rootfs",
    )
    parser.add_argument(
        "--arch",
        required=True,
        choices=sorted(ELF_MACHINES),
        help="native worker architecture",
    )
    parser.add_argument(
        "--destination",
        required=True,
        type=Path,
        help="absolute immutable probe-root destination",
    )
    parser.add_argument(
        "--sdme",
        default="sdme",
        help="SDME executable name or absolute path",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.epilog = (
        "plan performs no SDME calls or filesystem writes; apply requires root "
        "and prints the lowercase SHA-256 digest on stdout"
    )
    return parser


def _path_is_beneath(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _canonical_future_path(path: Path) -> Path:
    missing = []
    current = path
    while not os.path.lexists(current):
        if current.parent == current:
            raise PreparationError("destination has no existing ancestor")
        missing.append(current.name)
        current = current.parent
    try:
        resolved = current.resolve(strict=True)
    except OSError as exc:
        raise PreparationError(str(exc)) from exc
    for component in reversed(missing):
        resolved /= component
    return resolved


def validate_destination(path: Path, repo_root: Path = REPO_ROOT) -> Path:
    if not path.is_absolute():
        raise PreparationError("--destination must be absolute")
    if os.path.lexists(path) and path.is_symlink():
        raise PreparationError("destination must not be a symlink")
    destination = _canonical_future_path(path)
    if destination == Path("/"):
        raise PreparationError("destination must not be /")

    home_roots = {Path("/home"), Path("/root"), Path.home().resolve()}
    if any(_path_is_beneath(destination, root) for root in home_roots):
        raise PreparationError("destination must not be inside a home directory")
    if _path_is_beneath(destination, repo_root.resolve()):
        raise PreparationError("destination must not be inside the repository checkout")
    if any(part in (".git", ".hg", ".sl") for part in destination.parts):
        raise PreparationError("destination must not be inside a repository checkout")
    for ancestor in destination.parents:
        if any(os.path.lexists(ancestor / name) for name in (".git", ".hg", ".sl")):
            raise PreparationError("destination must not be inside a repository checkout")
    if not destination.parent.is_dir():
        raise PreparationError(
            "destination parent does not exist: {}".format(destination.parent)
        )
    return destination


def _normalize_cpu(value: str) -> str:
    return CPU_ALIASES.get(value, value)


def validate_native_architecture(architecture: str, machine: str | None = None) -> None:
    native = _normalize_cpu(machine or platform.machine())
    if native != architecture:
        raise PreparationError(
            "native host architecture is {}, requested {}".format(native, architecture)
        )


def _unsafe_executable_path(path: Path) -> Path | None:
    current = path
    while True:
        metadata = current.stat()
        if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022:
            return current
        if current.parent == current:
            return None
        current = current.parent


def resolve_executable(value: str) -> Path:
    candidate = value if "/" in value else shutil.which(value)
    if not candidate:
        raise PreparationError("executable not found: {}".format(value))
    path = Path(candidate)
    if "/" in value and not path.is_absolute():
        raise PreparationError("executable path must be absolute: {}".format(value))
    try:
        path = path.resolve(strict=True)
        metadata = path.stat()
    except OSError as exc:
        raise PreparationError(str(exc)) from exc
    if not stat.S_ISREG(metadata.st_mode) or not os.access(path, os.X_OK):
        raise PreparationError("executable is not a regular executable file: {}".format(path))
    try:
        unsafe = _unsafe_executable_path(path)
    except OSError as exc:
        raise PreparationError(str(exc)) from exc
    if unsafe is not None:
        raise PreparationError(
            "executable path is not root-owned and non-writable: {}".format(unsafe)
        )
    return path


def parse_rootfs_inventory(value: str) -> set[str]:
    try:
        items = json.loads(value)
    except json.JSONDecodeError as exc:
        raise PreparationError("invalid SDME filesystem inventory: {}".format(exc)) from exc
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise PreparationError("invalid SDME filesystem inventory shape")
    names = set()
    for item in items:
        name = item.get("name")
        if not isinstance(name, str):
            raise PreparationError("invalid SDME filesystem inventory name")
        names.add(name)
    return names


class SdmeClient:
    def __init__(self, executable: Path, verbose: bool = False) -> None:
        self.executable = executable
        self.verbose = verbose

    def _run(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        command = [str(self.executable), *arguments]
        LOG.debug("run: %s", " ".join(command))
        return subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def rootfs_names(self) -> set[str]:
        result = self._run(["fs", "ls", "--json"])
        if result.returncode != 0:
            raise PreparationError(
                "SDME filesystem inventory failed: {}".format(result.stderr.strip())
            )
        return parse_rootfs_inventory(result.stdout)

    def copy(self, runtime_fs: str, source: PurePosixPath, destination: Path) -> None:
        result = self._run([
            "cp",
            "fs:{}:{}".format(runtime_fs, source),
            str(destination),
        ])
        if result.returncode != 0:
            raise PreparationError(result.stderr.strip() or "SDME copy failed")


def _normalize_root_path(path: PurePosixPath) -> PurePosixPath:
    if not path.is_absolute():
        raise PreparationError("rootfs path must be absolute: {}".format(path))
    parts = []
    for component in path.parts[1:]:
        if component in ("", "."):
            continue
        if component == "..":
            if not parts:
                raise PreparationError("rootfs path escapes /: {}".format(path))
            parts.pop()
        else:
            parts.append(component)
    return PurePosixPath("/", *parts)


def _remote_symlink_target(path: PurePosixPath, target: str) -> PurePosixPath:
    candidate = PurePosixPath(target)
    if not candidate.is_absolute():
        candidate = path.parent / candidate
    return _normalize_root_path(candidate)


class SourceTree:
    def __init__(
        self,
        client: SdmeClient,
        runtime_fs: str,
        output: Path,
        fetch_root: Path,
    ) -> None:
        self.client = client
        self.runtime_fs = runtime_fs
        self.output = output
        self.fetch_root = fetch_root
        self.counter = 0

    def local_path(self, remote: PurePosixPath) -> Path:
        remote = _normalize_root_path(remote)
        return self.output.joinpath(*remote.parts[1:])

    def _fetch(self, remote: PurePosixPath) -> Path:
        remote = _normalize_root_path(remote)
        local = self.local_path(remote)
        if os.path.lexists(local):
            return local

        self.counter += 1
        attempt = self.fetch_root / "fetch-{:04d}".format(self.counter)
        attempt.mkdir(mode=0o700)
        try:
            self.client.copy(self.runtime_fs, remote, attempt)
            fetched = attempt / remote.name
            if not os.path.lexists(fetched):
                raise PreparationError(
                    "SDME did not materialize requested path {}".format(remote)
                )
            local.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            os.rename(fetched, local)
            LOG.debug("copied %s", remote)
        finally:
            shutil.rmtree(attempt, ignore_errors=True)
        return local

    def copy_directory(self, remote: PurePosixPath) -> Path:
        local = self._fetch(remote)
        if local.is_symlink() or not local.is_dir():
            raise PreparationError("expected rootfs directory: {}".format(remote))
        return local

    def copy_symlink_chain(
        self,
        remote: PurePosixPath,
    ) -> tuple[PurePosixPath, Path]:
        current = _normalize_root_path(remote)
        seen = set()
        while True:
            if current in seen:
                raise PreparationError("rootfs symlink loop at {}".format(current))
            seen.add(current)
            local = self._fetch(current)
            if local.is_symlink():
                current = _remote_symlink_target(current, os.readlink(local))
                continue
            if not local.is_file():
                raise PreparationError("expected rootfs regular file: {}".format(current))
            return current, local

    def ensure_tree_symlinks(self) -> None:
        while True:
            missing = []
            for path in sorted(self.output.rglob("*")):
                if not path.is_symlink():
                    continue
                remote = PurePosixPath("/", *path.relative_to(self.output).parts)
                target = _remote_symlink_target(remote, os.readlink(path))
                if not os.path.lexists(self.local_path(target)):
                    missing.append(target)
            if not missing:
                return
            for target in sorted(set(missing), key=str):
                self.copy_symlink_chain(target)


def parse_elf(path: Path) -> ElfMetadata:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise PreparationError(str(exc)) from exc
    if len(data) < 16 or data[:4] != b"\x7fELF":
        raise PreparationError("not an ELF file: {}".format(path))

    elf_class = data[4]
    byte_order = data[5]
    endian = {1: "<", 2: ">"}.get(byte_order)
    if endian is None or elf_class not in (1, 2):
        raise PreparationError("unsupported ELF encoding: {}".format(path))

    if elf_class == 2:
        header_format = endian + "HHIQQQIHHHHHH"
        program_format = endian + "IIQQQQQQ"
        dynamic_format = endian + "QQ"
        program_indexes = (0, 2, 3, 5)
    else:
        header_format = endian + "HHIIIIIHHHHHH"
        program_format = endian + "IIIIIIII"
        dynamic_format = endian + "II"
        program_indexes = (0, 1, 2, 4)

    header_size = struct.calcsize(header_format)
    if len(data) < 16 + header_size:
        raise PreparationError("truncated ELF header: {}".format(path))
    header = struct.unpack_from(header_format, data, 16)
    machine = header[1]
    program_offset = header[4]
    program_entry_size = header[8]
    program_count = header[9]
    minimum_program_size = struct.calcsize(program_format)
    if program_entry_size < minimum_program_size:
        raise PreparationError("invalid ELF program header size: {}".format(path))

    programs = []
    for index in range(program_count):
        offset = program_offset + index * program_entry_size
        if offset + minimum_program_size > len(data):
            raise PreparationError("truncated ELF program headers: {}".format(path))
        values = struct.unpack_from(program_format, data, offset)
        kind, file_offset, virtual_address, file_size = (
            values[index] for index in program_indexes
        )
        if file_offset + file_size > len(data):
            raise PreparationError("ELF segment exceeds file: {}".format(path))
        programs.append((kind, file_offset, virtual_address, file_size))

    interpreter = None
    for kind, file_offset, _virtual_address, file_size in programs:
        if kind != 3:
            continue
        raw = data[file_offset:file_offset + file_size].split(b"\0", 1)[0]
        try:
            interpreter = raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise PreparationError("invalid ELF interpreter: {}".format(path)) from exc
        if not interpreter.startswith("/"):
            raise PreparationError("ELF interpreter is not absolute: {}".format(path))

    dynamic = next((item for item in programs if item[0] == 2), None)
    if dynamic is None:
        return ElfMetadata(machine, interpreter, ())

    _kind, dynamic_offset, _dynamic_address, dynamic_size = dynamic
    dynamic_entry_size = struct.calcsize(dynamic_format)
    needed_offsets = []
    string_address = None
    string_size = None
    for offset in range(
        dynamic_offset,
        dynamic_offset + dynamic_size,
        dynamic_entry_size,
    ):
        if offset + dynamic_entry_size > len(data):
            raise PreparationError("truncated ELF dynamic table: {}".format(path))
        tag, value = struct.unpack_from(dynamic_format, data, offset)
        if tag == 0:
            break
        if tag == 1:
            needed_offsets.append(value)
        elif tag == 5:
            string_address = value
        elif tag == 10:
            string_size = value

    if not needed_offsets:
        return ElfMetadata(machine, interpreter, ())
    if string_address is None or string_size is None:
        raise PreparationError("ELF dynamic strings are missing: {}".format(path))

    string_offset = None
    for kind, file_offset, virtual_address, file_size in programs:
        if kind == 1 and virtual_address <= string_address < virtual_address + file_size:
            string_offset = file_offset + string_address - virtual_address
            break
    if string_offset is None or string_offset + string_size > len(data):
        raise PreparationError("ELF dynamic string table is invalid: {}".format(path))

    needed = []
    for offset in needed_offsets:
        if offset >= string_size:
            raise PreparationError("ELF dependency offset is invalid: {}".format(path))
        start = string_offset + offset
        end = data.find(b"\0", start, string_offset + string_size)
        if end < 0:
            raise PreparationError("ELF dependency is unterminated: {}".format(path))
        try:
            name = data[start:end].decode("ascii")
        except UnicodeDecodeError as exc:
            raise PreparationError("ELF dependency is not ASCII: {}".format(path)) from exc
        if not name or PurePosixPath(name).name != name:
            raise PreparationError("unsafe ELF dependency name: {!r}".format(name))
        needed.append(name)
    return ElfMetadata(machine, interpreter, tuple(needed))


def _is_elf(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        with path.open("rb") as stream:
            return stream.read(4) == b"\x7fELF"
    except OSError as exc:
        raise PreparationError(str(exc)) from exc


def _library_candidates(architecture: str, name: str) -> tuple[PurePosixPath, ...]:
    triplet = LIBRARY_TRIPLETS[architecture]
    return tuple(
        PurePosixPath(directory) / name
        for directory in (
            "/lib/{}".format(triplet),
            "/usr/lib/{}".format(triplet),
            "/lib64",
            "/usr/lib64",
            "/lib",
            "/usr/lib",
        )
    )


def _copy_library(
    source: SourceTree,
    architecture: str,
    name: str,
) -> tuple[PurePosixPath, Path]:
    for candidate in _library_candidates(architecture, name):
        try:
            return source.copy_symlink_chain(candidate)
        except PreparationError:
            pass
    raise PreparationError(
        "cannot copy ELF dependency {} from {}".format(name, source.runtime_fs)
    )


def copy_runtime_closure(
    source: SourceTree,
    architecture: str,
) -> None:
    python_remote, python_local = source.copy_symlink_chain(
        PurePosixPath("/usr/bin/python3")
    )
    python_metadata = parse_elf(python_local)
    expected_machine = ELF_MACHINES[architecture]
    if python_metadata.machine != expected_machine:
        raise PreparationError(
            "Python ELF machine={} expected={}".format(
                python_metadata.machine,
                expected_machine,
            )
        )
    match = PYTHON_BINARY.match(python_remote.name)
    if match is None:
        raise PreparationError(
            "cannot derive Python standard library from {}".format(python_remote)
        )
    stdlib_remote = PurePosixPath("/usr/lib") / python_remote.name
    source.copy_directory(stdlib_remote)
    source.ensure_tree_symlinks()

    queue = deque()
    queued = set()
    for path in sorted(source.output.rglob("*")):
        if _is_elf(path):
            remote = PurePosixPath("/", *path.relative_to(source.output).parts)
            queue.append((remote, path))
            queued.add(remote)

    libraries = {}
    processed = set()
    while queue:
        remote, local = queue.popleft()
        if remote in processed:
            continue
        processed.add(remote)
        metadata = parse_elf(local)
        if metadata.machine != expected_machine:
            raise PreparationError(
                "{} ELF machine={} expected={}".format(
                    remote,
                    metadata.machine,
                    expected_machine,
                )
            )
        dependencies = list(metadata.needed)
        if metadata.interpreter is not None:
            interpreter_remote, interpreter_local = source.copy_symlink_chain(
                PurePosixPath(metadata.interpreter)
            )
            if interpreter_remote not in queued:
                queue.append((interpreter_remote, interpreter_local))
                queued.add(interpreter_remote)
        for name in dependencies:
            if name not in libraries:
                libraries[name] = _copy_library(source, architecture, name)
            library_remote, library_local = libraries[name]
            if library_remote not in queued:
                queue.append((library_remote, library_local))
                queued.add(library_remote)


def create_mountpoints(destination: Path) -> None:
    for name in ("proc", "dev", "tmp"):
        path = destination / name
        path.mkdir(mode=0o755, exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise PreparationError("mountpoint is not a directory: /{}".format(name))


def freeze_tree(destination: Path) -> None:
    for current, directories, files in os.walk(destination, topdown=False):
        root = Path(current)
        for name in files:
            path = root / name
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise PreparationError("unsupported file in probe root: {}".format(path))
            mode = 0o555 if stat.S_IMODE(metadata.st_mode) & 0o111 else 0o444
            path.chmod(mode)
        for name in directories:
            path = root / name
            if not path.is_symlink():
                path.chmod(0o555)
    destination.chmod(0o555)


def _make_tree_writable(destination: Path) -> None:
    if not os.path.lexists(destination):
        return
    for current, directories, files in os.walk(destination, topdown=False):
        root = Path(current)
        for name in files:
            path = root / name
            if not path.is_symlink():
                path.chmod(stat.S_IMODE(path.stat().st_mode) | 0o600)
        for name in directories:
            path = root / name
            if not path.is_symlink():
                path.chmod(stat.S_IMODE(path.stat().st_mode) | 0o700)
    if not destination.is_symlink():
        destination.chmod(stat.S_IMODE(destination.stat().st_mode) | 0o700)


def validate_frozen_tree(destination: Path) -> None:
    for path in (destination, *destination.rglob("*")):
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            continue
        if not stat.S_ISDIR(metadata.st_mode) and not stat.S_ISREG(metadata.st_mode):
            raise PreparationError("unsupported file in probe root: {}".format(path))
        if stat.S_IMODE(metadata.st_mode) & 0o222:
            raise PreparationError("existing probe root is writable: {}".format(path))


def tree_digest(destination: Path, tar: Path) -> str:
    command = [
        str(tar),
        "--sort=name",
        "--mtime=UTC 1970-01-01",
        "--owner=0",
        "--group=0",
        "--numeric-owner",
        "--format=posix",
        "--pax-option=delete=atime,delete=ctime",
        "-C",
        str(destination),
        "-cf",
        "-",
        ".",
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise PreparationError(str(exc)) from exc
    if result.returncode != 0:
        raise PreparationError(
            "cannot digest probe root: {}".format(
                result.stderr.decode("utf-8", errors="replace").strip()
            )
        )
    return hashlib.sha256(result.stdout).hexdigest()


def manifest_path(destination: Path) -> Path:
    return destination.with_name(destination.name + ".manifest.json")


def _read_existing(
    destination: Path,
    runtime_fs: str,
    architecture: str,
    tar: Path,
) -> ExistingProbeRoot | None:
    marker = manifest_path(destination)
    destination_exists = os.path.lexists(destination)
    marker_exists = os.path.lexists(marker)
    if not destination_exists and not marker_exists:
        return None
    if not destination_exists or not marker_exists:
        raise PreparationError("incomplete existing probe root or manifest")
    if destination.is_symlink() or not destination.is_dir():
        raise PreparationError("existing destination is not a directory")
    if marker.is_symlink() or not marker.is_file():
        raise PreparationError("probe-root manifest is not a regular file")
    try:
        record = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PreparationError("invalid probe-root manifest: {}".format(exc)) from exc
    if not isinstance(record, dict):
        raise PreparationError("invalid probe-root manifest shape")
    expected = {
        "architecture": architecture,
        "runtime_fs": runtime_fs,
    }
    for key, value in expected.items():
        if record.get(key) != value:
            raise PreparationError(
                "probe-root manifest {} mismatch: expected {!r}, got {!r}".format(
                    key,
                    value,
                    record.get(key),
                )
            )
    recorded = record.get("sha256")
    if not isinstance(recorded, str) or re.fullmatch(r"[0-9a-f]{64}", recorded) is None:
        raise PreparationError("probe-root manifest has an invalid sha256")
    validate_frozen_tree(destination)
    actual = tree_digest(destination, tar)
    if actual != recorded:
        raise PreparationError(
            "existing probe-root digest mismatch: recorded {}, got {}".format(
                recorded,
                actual,
            )
        )
    return ExistingProbeRoot(recorded, runtime_fs, architecture)


def _write_manifest(
    destination: Path,
    runtime_fs: str,
    architecture: str,
    digest: str,
) -> None:
    marker = manifest_path(destination)
    temporary = marker.with_name(".{}.tmp.{}".format(marker.name, os.getpid()))
    payload = json.dumps(
        {
            "architecture": architecture,
            "runtime_fs": runtime_fs,
            "sha256": digest,
        },
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"
    descriptor = None
    linked = False
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o444)
        os.link(temporary, marker)
        linked = True
        temporary.unlink()
    except OSError as exc:
        if linked:
            try:
                marker.unlink()
            except OSError:
                pass
        raise PreparationError("cannot write probe-root manifest: {}".format(exc)) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if os.path.lexists(temporary):
            try:
                temporary.unlink()
            except OSError:
                pass


def _remove_created_output(destination: Path, marker_created: bool) -> None:
    errors = []
    marker = manifest_path(destination)
    if marker_created:
        try:
            marker.unlink()
        except OSError as exc:
            errors.append("manifest: {}".format(exc))
    if os.path.lexists(destination):
        try:
            _make_tree_writable(destination)
            shutil.rmtree(destination)
        except OSError as exc:
            errors.append("destination: {}".format(exc))
    if os.path.lexists(destination):
        errors.append("destination remains at {}".format(destination))
    if errors:
        raise PreparationError("probe-root cleanup failed: {}".format("; ".join(errors)))


def plan(
    runtime_fs: str,
    architecture: str,
    destination: Path,
    tar: Path,
) -> str | None:
    existing = _read_existing(destination, runtime_fs, architecture, tar)
    print("PLAN runtime-fs fs:{}".format(runtime_fs))
    print("PLAN architecture {}".format(architecture))
    print("PLAN destination {}".format(destination))
    if existing is not None:
        print("PLAN reuse sha256={}".format(existing.digest))
        return existing.digest
    print("PLAN copy /usr/bin/python3 symlink chain")
    print("PLAN copy /usr/lib/pythonX.Y standard library")
    print("PLAN copy recursive ELF interpreter and library closure")
    print("PLAN create /proc /dev /tmp")
    print("PLAN freeze tree and record deterministic digest")
    return None


def apply(
    client: SdmeClient,
    runtime_fs: str,
    architecture: str,
    destination: Path,
    tar: Path,
) -> str:
    names = client.rootfs_names()
    if runtime_fs not in names:
        raise PreparationError("SDME runtime rootfs is not imported: {}".format(runtime_fs))
    existing = _read_existing(destination, runtime_fs, architecture, tar)
    if existing is not None:
        return existing.digest

    created = False
    marker_created = False
    try:
        with tempfile.TemporaryDirectory(
            prefix=".{}-fetch-".format(destination.name),
            dir=destination.parent,
        ) as temporary:
            destination.mkdir(mode=0o700)
            created = True
            source = SourceTree(client, runtime_fs, destination, Path(temporary))
            copy_runtime_closure(source, architecture)
            create_mountpoints(destination)
            freeze_tree(destination)
            digest = tree_digest(destination, tar)
            _write_manifest(destination, runtime_fs, architecture, digest)
            marker_created = True
        return digest
    except BaseException as exc:
        if created:
            try:
                _remove_created_output(destination, marker_created)
            except PreparationError as cleanup_error:
                raise PreparationError(
                    "preparation failed: {}; {}".format(exc, cleanup_error)
                ) from exc
        raise


def main(argv: list[str] | None = None, effective_uid: int | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="probe-root: %(message)s",
    )
    try:
        if ROOTFS_NAME.fullmatch(args.runtime_fs) is None:
            raise PreparationError("invalid SDME runtime rootfs name")
        destination = validate_destination(args.destination)
        validate_native_architecture(args.arch)
        tar = resolve_executable("tar")
        if args.operation == "plan":
            plan(args.runtime_fs, args.arch, destination, tar)
            return 0
        uid = os.geteuid() if effective_uid is None else effective_uid
        if uid != 0:
            raise PreparationError("apply must run as root")
        sdme = resolve_executable(args.sdme)
        digest = apply(
            SdmeClient(sdme, args.verbose),
            args.runtime_fs,
            args.arch,
            destination,
            tar,
        )
        print(digest)
        return 0
    except PreparationError as exc:
        print("error: {}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
