#!/usr/bin/env python3
"""Validate a buckos-distro remote worker before it registers."""

import argparse
import fcntl
import importlib.util
import json
import logging
import os
import platform
import pwd
import re
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


LOG = logging.getLogger("buckos-worker-preflight")

REQUIRED_TOOLS = (
    "bash",
    "python3",
    "tar",
    "rpm2archive",
    "rpm",
    "dpkg-source",
    "dpkg-deb",
    "dpkg-buildpackage",
    "bwrap",
    "unshare",
    "mount",
    "chroot",
    "newuidmap",
    "newgidmap",
)

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

CONTAINER_SOCKETS = (
    "/run/containerd/containerd.sock",
    "/run/crio/crio.sock",
    "/run/docker.sock",
    "/run/podman/podman.sock",
    "/var/run/docker.sock",
)

MOUNT_ESCAPE = re.compile(r"\\([0-7]{3})")


class UsageError(ValueError):
    pass


class CheckError(RuntimeError):
    pass


class PreflightArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


class Reporter:
    def __init__(self):
        self.failures = 0

    def record(self, level, name, detail):
        clean = " ".join(str(detail).splitlines())
        print("{} {} {}".format(level, name, clean).rstrip(), flush=True)
        if level == "FAIL":
            self.failures += 1

    def passed(self, name, detail):
        self.record("PASS", name, detail)

    def failed(self, name, detail):
        self.record("FAIL", name, detail)

    def warned(self, name, detail):
        self.record("WARN", name, detail)


@dataclass(frozen=True)
class ProbeSysroot:
    root: Path
    python_path: str


@dataclass(frozen=True)
class SubordinateRanges:
    uid_base: int
    uid_count: int
    gid_base: int
    gid_count: int


@dataclass(frozen=True)
class MountInfo:
    mountpoint: str
    options: frozenset
    filesystem: str
    super_options: frozenset


def _positive_integer(value):
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if result <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return result


def _parser():
    default_tools = Path(__file__).resolve().parents[3] / "tools"
    parser = PreflightArgumentParser(description=__doc__)
    parser.add_argument(
        "--worker-user",
        required=True,
        help="service account that must be running this preflight",
    )
    parser.add_argument(
        "--arch",
        required=True,
        choices=sorted(ELF_MACHINES),
        help="architecture advertised by this worker pool",
    )
    parser.add_argument(
        "--scratch-root",
        required=True,
        type=Path,
        help="pre-existing service-owned worker scratch directory",
    )
    parser.add_argument(
        "--probe-sysroot",
        required=True,
        type=Path,
        help="immutable target-architecture root containing Python 3",
    )
    parser.add_argument(
        "--min-scratch-bytes",
        required=True,
        type=_positive_integer,
        help="minimum unprivileged bytes available on scratch",
    )
    parser.add_argument(
        "--min-scratch-inodes",
        required=True,
        type=_positive_integer,
        help="minimum unprivileged inodes available on scratch",
    )
    parser.add_argument(
        "--tools-dir",
        type=Path,
        default=default_tools,
        help="directory containing the production _isolation.py and _rpm.py",
    )
    parser.add_argument(
        "--emulated-aarch64",
        action="store_true",
        help="allow an x86_64 worker with persistent qemu-aarch64 binfmt",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.epilog = (
        "exit 0: pass; exit 1: worker contract failure; "
        "exit 2: usage or internal error"
    )
    return parser


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise CheckError("cannot load {} from {}".format(name, path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_isolation(tools_dir):
    tools_dir = tools_dir.resolve()
    isolation_path = tools_dir / "_isolation.py"
    rpm_path = tools_dir / "_rpm.py"
    for path in (isolation_path, rpm_path):
        if not path.is_file():
            raise CheckError("production launcher file is missing: {}".format(path))

    previous_rpm = sys.modules.get("_rpm")
    try:
        sys.modules["_rpm"] = _load_module("_rpm", rpm_path)
        return _load_module("_buckos_preflight_isolation", isolation_path)
    finally:
        if previous_rpm is None:
            sys.modules.pop("_rpm", None)
        else:
            sys.modules["_rpm"] = previous_rpm


def _normalize_cpu(value):
    return CPU_ALIASES.get(value, value)


def check_identity(worker_user, reporter):
    try:
        expected = pwd.getpwnam(worker_user)
    except KeyError:
        reporter.failed(
            "identity",
            "configured user {!r} does not exist".format(worker_user),
        )
        return False

    uid = os.getuid()
    gid = os.getgid()
    if uid == 0:
        reporter.failed("identity", "worker must not run as uid 0")
        return False
    if uid != expected.pw_uid or gid != expected.pw_gid:
        reporter.failed(
            "identity",
            "expected {} uid={} gid={}, running uid={} gid={}".format(
                worker_user,
                expected.pw_uid,
                expected.pw_gid,
                uid,
                gid,
            ),
        )
        return False
    reporter.passed("identity", "user={} uid={} gid={}".format(worker_user, uid, gid))
    return True


def check_user_namespace_policy(
    reporter,
    maximum_path=Path("/proc/sys/user/max_user_namespaces"),
    clone_path=Path("/proc/sys/kernel/unprivileged_userns_clone"),
):
    if not maximum_path.exists():
        reporter.failed("userns-policy", "{} is missing".format(maximum_path))
        return False
    paths = (
        (maximum_path, lambda value: value > 0),
        (clone_path, lambda value: value == 1),
    )
    observed = []
    for path, accepted in paths:
        if not path.exists():
            continue
        try:
            value = int(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError) as exc:
            reporter.failed("userns-policy", "{}: {}".format(path, exc))
            return False
        observed.append("{}={}".format(path.name, value))
        if not accepted(value):
            reporter.failed(
                "userns-policy",
                "{} disables unprivileged user namespaces".format(path),
            )
            return False
    reporter.passed("userns-policy", " ".join(observed))
    return True


def _unsafe_executable_path(path):
    current = path
    while True:
        mode = stat.S_IMODE(current.stat().st_mode)
        if mode & 0o022:
            return current
        if current.parent == current:
            return None
        current = current.parent


def resolve_executable(name):
    candidate = shutil.which(name)
    if not candidate:
        raise CheckError("not found on PATH")
    try:
        path = Path(candidate).resolve(strict=True)
        metadata = path.stat()
    except OSError as exc:
        raise CheckError(str(exc)) from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise CheckError("{} is not a regular file".format(path))
    if not os.access(path, os.X_OK):
        raise CheckError("{} is not executable".format(path))
    try:
        unsafe = _unsafe_executable_path(path)
    except OSError as exc:
        raise CheckError(str(exc)) from exc
    if unsafe is not None:
        raise CheckError("{} is writable by group or other".format(unsafe))
    return path


def _run_readonly(argv):
    try:
        return subprocess.run(
            [str(item) for item in argv],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CheckError(str(exc)) from exc


def check_tools(reporter):
    paths = {}
    ok = True
    for name in REQUIRED_TOOLS:
        try:
            path = resolve_executable(name)
        except CheckError as exc:
            reporter.failed("tool-{}".format(name), exc)
            ok = False
            continue
        paths[name] = path
        reporter.passed("tool-{}".format(name), path)

    tar = paths.get("tar")
    if tar is not None:
        try:
            result = _run_readonly([tar, "--version"])
            help_result = _run_readonly([tar, "--help"])
        except CheckError as exc:
            reporter.failed("tool-tar-features", exc)
            ok = False
        else:
            required = (
                "--delay-directory-restore",
                "--no-same-owner",
                "--transform",
                "--numeric-owner",
            )
            if (
                result.returncode != 0
                or "GNU tar" not in result.stdout
                or help_result.returncode != 0
                or any(option not in help_result.stdout for option in required)
            ):
                reporter.failed(
                    "tool-tar-features",
                    "GNU tar payload options are unavailable",
                )
                ok = False
            else:
                reporter.passed(
                    "tool-tar-features",
                    "GNU tar payload options available",
                )

    bwrap = paths.get("bwrap")
    if bwrap is not None:
        try:
            result = _run_readonly([bwrap, "--help"])
        except CheckError as exc:
            reporter.failed("tool-bwrap-userns", exc)
            ok = False
        else:
            if result.returncode != 0 or "--userns" not in result.stdout:
                reporter.failed(
                    "tool-bwrap-userns",
                    "bwrap lacks --userns support",
                )
                ok = False
            else:
                reporter.passed("tool-bwrap-userns", "--userns supported")

    dpkg_buildpackage = paths.get("dpkg-buildpackage")
    if dpkg_buildpackage is not None:
        expected = Path("/usr/bin/dpkg-buildpackage")
        if dpkg_buildpackage != expected.resolve(strict=False):
            reporter.failed(
                "tool-dpkg-buildpackage-path",
                "current launcher needs /usr/bin/dpkg-buildpackage, found {}".format(
                    dpkg_buildpackage
                ),
            )
            ok = False
        else:
            reporter.passed("tool-dpkg-buildpackage-path", dpkg_buildpackage)

    return ok


def check_subordinate_ranges(isolation, reporter):
    try:
        uid_range = isolation._subid_range(isolation._UID_MAP)
        gid_range = isolation._subid_range(isolation._GID_MAP)
        available = isolation.subid_mapping_available()
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        reporter.failed("subordinate-ids", exc)
        return None

    if (
        not available
        or uid_range is None
        or gid_range is None
    ):
        reporter.failed(
            "subordinate-ids",
            "missing UID/GID range or newuidmap/newgidmap",
        )
        return None

    uid_base, uid_count = uid_range
    gid_base, gid_count = gid_range
    maximum_id = (1 << 32) - 2
    if (
        uid_base <= 0
        or gid_base <= 0
        or uid_count < 65536
        or gid_count < 65536
        or uid_base + uid_count - 1 > maximum_id
        or gid_base + gid_count - 1 > maximum_id
    ):
        reporter.failed(
            "subordinate-ids",
            "need valid 65536-ID ranges, found uid={}:{} gid={}:{}".format(
                uid_base,
                uid_count,
                gid_base,
                gid_count,
            ),
        )
        return None

    if uid_base <= os.getuid() < uid_base + uid_count:
        reporter.failed("subordinate-ids", "UID range contains the worker UID")
        return None
    if gid_base <= os.getgid() < gid_base + gid_count:
        reporter.failed("subordinate-ids", "GID range contains the worker GID")
        return None

    reporter.passed(
        "subordinate-ids",
        "uid={}:{} gid={}:{}".format(
            uid_base,
            uid_count,
            gid_base,
            gid_count,
        ),
    )
    return SubordinateRanges(uid_base, uid_count, gid_base, gid_count)


def check_scratch(path, minimum_bytes, minimum_inodes, reporter):
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat()
        filesystem = os.statvfs(resolved)
    except OSError as exc:
        reporter.failed("scratch", exc)
        return None

    if not stat.S_ISDIR(metadata.st_mode):
        reporter.failed("scratch", "{} is not a directory".format(resolved))
        return None
    if metadata.st_uid != os.getuid():
        reporter.failed(
            "scratch",
            "{} is owned by uid {}, expected {}".format(
                resolved,
                metadata.st_uid,
                os.getuid(),
            ),
        )
        return None
    if not os.access(resolved, os.W_OK | os.X_OK):
        reporter.failed("scratch", "{} is not writable/searchable".format(resolved))
        return None

    readonly = getattr(os, "ST_RDONLY", 1)
    noexec = getattr(os, "ST_NOEXEC", 8)
    if filesystem.f_flag & readonly:
        reporter.failed("scratch", "{} is read-only".format(resolved))
        return None
    if filesystem.f_flag & noexec:
        reporter.failed("scratch", "{} is mounted noexec".format(resolved))
        return None

    available_bytes = filesystem.f_bavail * filesystem.f_frsize
    available_inodes = filesystem.f_favail
    if available_bytes < minimum_bytes:
        reporter.failed(
            "scratch-bytes",
            "available={} required={}".format(available_bytes, minimum_bytes),
        )
        return None
    reporter.passed(
        "scratch-bytes",
        "available={} required={}".format(available_bytes, minimum_bytes),
    )

    if available_inodes <= 0 or available_inodes < minimum_inodes:
        reporter.failed(
            "scratch-inodes",
            "available={} required={}".format(available_inodes, minimum_inodes),
        )
        return None
    reporter.passed(
        "scratch-inodes",
        "available={} required={}".format(available_inodes, minimum_inodes),
    )
    return resolved


def _parse_binfmt(path):
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise CheckError(str(exc)) from exc
    if not lines or lines[0] != "enabled":
        raise CheckError("{} is not enabled".format(path))
    values = {}
    for line in lines[1:]:
        if " " in line:
            key, value = line.split(" ", 1)
            values[key.rstrip(":")] = value.strip()
    interpreter = values.get("interpreter")
    flags = values.get("flags", "")
    if not interpreter or not os.path.isabs(interpreter):
        raise CheckError("binfmt interpreter is not an absolute path")
    if "F" not in flags:
        raise CheckError("binfmt flags do not contain F")
    if not os.path.isfile(interpreter) or not os.access(interpreter, os.X_OK):
        raise CheckError("binfmt interpreter is not executable: {}".format(interpreter))
    return interpreter, flags


def check_architecture(target, emulated_aarch64, reporter, machine=None, handler=None):
    native = _normalize_cpu(machine or platform.machine())
    if target == native:
        if emulated_aarch64:
            reporter.warned(
                "architecture-mode",
                "emulation requested on native aarch64",
            )
        reporter.passed("architecture", "native {}".format(native))
        return True

    if target != "aarch64" or native != "x86_64" or not emulated_aarch64:
        reporter.failed(
            "architecture",
            "worker={} target={} without supported explicit emulation".format(
                native,
                target,
            ),
        )
        return False

    handler = handler or Path("/proc/sys/fs/binfmt_misc/qemu-aarch64")
    try:
        interpreter, flags = _parse_binfmt(handler)
    except CheckError as exc:
        reporter.failed("architecture-binfmt", exc)
        return False
    reporter.passed(
        "architecture-binfmt",
        "handler={} interpreter={} flags={}".format(handler, interpreter, flags),
    )
    reporter.passed("architecture", "emulated aarch64 on x86_64")
    return True


def _resolve_in_root(root, absolute_path):
    pure = PurePosixPath(absolute_path)
    if not pure.is_absolute():
        raise CheckError("probe executable path must be absolute")
    root = root.resolve(strict=True)
    current = root
    pending = list(pure.parts[1:])
    links = 0
    while pending:
        component = pending.pop(0)
        if component in ("", "."):
            continue
        if component == "..":
            if current == root:
                raise CheckError("probe executable escapes the sysroot")
            current = current.parent
            continue
        candidate = current / component
        if candidate.is_symlink():
            links += 1
            if links > 40:
                raise CheckError("too many symlinks resolving {}".format(absolute_path))
            target = PurePosixPath(os.readlink(candidate))
            if target.is_absolute():
                current = root
                pending = list(target.parts[1:]) + pending
            else:
                pending = list(target.parts) + pending
            continue
        current = candidate
        try:
            current.relative_to(root)
        except ValueError as exc:
            raise CheckError("probe executable escapes the sysroot") from exc
    return current


def _elf_machine(path):
    try:
        with path.open("rb") as stream:
            header = stream.read(20)
    except OSError as exc:
        raise CheckError(str(exc)) from exc
    if len(header) < 20 or header[:4] != b"\x7fELF":
        raise CheckError("{} is not an ELF executable".format(path))
    byte_order = {1: "little", 2: "big"}.get(header[5])
    if byte_order is None:
        raise CheckError("{} has an invalid ELF byte order".format(path))
    return int.from_bytes(header[18:20], byte_order)


def check_probe_sysroot(root, target, reporter):
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        reporter.failed("probe-sysroot", exc)
        return None
    if root == Path("/") or not root.is_dir():
        reporter.failed("probe-sysroot", "must be a directory other than /")
        return None

    for relative in ("proc", "dev", "tmp"):
        if not (root / relative).is_dir():
            reporter.failed(
                "probe-sysroot",
                "missing required directory /{}".format(relative),
            )
            return None

    found = None
    for candidate in ("/usr/bin/python3", "/bin/python3"):
        try:
            resolved = _resolve_in_root(root, candidate)
        except CheckError:
            continue
        if resolved.is_file() and os.access(resolved, os.X_OK):
            found = (candidate, resolved)
            break
    if found is None:
        reporter.failed(
            "probe-sysroot",
            "no executable /usr/bin/python3 or /bin/python3",
        )
        return None

    python_path, resolved_python = found
    try:
        machine = _elf_machine(resolved_python)
    except CheckError as exc:
        reporter.failed("probe-sysroot", exc)
        return None
    if machine != ELF_MACHINES[target]:
        reporter.failed(
            "probe-sysroot",
            "{} ELF machine={} expected={}".format(
                python_path,
                machine,
                ELF_MACHINES[target],
            ),
        )
        return None

    reporter.passed(
        "probe-sysroot",
        "root={} python={} machine={}".format(root, python_path, machine),
    )
    return ProbeSysroot(root, python_path)


def check_probe_separation(probe, scratch, reporter):
    try:
        probe.root.relative_to(scratch)
        overlaps = True
    except ValueError:
        try:
            scratch.relative_to(probe.root)
            overlaps = True
        except ValueError:
            overlaps = False
    if overlaps:
        reporter.failed(
            "probe-paths",
            "probe sysroot and scratch must not contain one another",
        )
        return False
    reporter.passed("probe-paths", "probe sysroot and scratch are disjoint")
    return True


def _unescape_mount_path(value):
    return MOUNT_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), value)


def _mountinfo():
    mounts = []
    with open("/proc/self/mountinfo", encoding="utf-8") as stream:
        for line in stream:
            fields = line.split()
            separator = fields.index("-")
            mounts.append(MountInfo(
                mountpoint=_unescape_mount_path(fields[4]),
                options=frozenset(fields[5].split(",")),
                filesystem=fields[separator + 1],
                super_options=frozenset(fields[separator + 3].split(",")),
            ))
    return mounts


def _mount_at(mounts, path):
    matches = [mount for mount in mounts if mount.mountpoint == path]
    if not matches:
        raise CheckError("no mount at {}".format(path))
    return matches[-1]


def _inside_namespace(name, outer):
    observed = os.readlink("/proc/self/ns/{}".format(name))
    if observed == outer:
        raise CheckError("{} namespace did not change".format(name))
    return observed


def _inside_loopback():
    interfaces = sorted(name for _, name in socket.if_nameindex())
    if interfaces != ["lo"]:
        raise CheckError("interfaces={}".format(",".join(interfaces)))
    request = struct.pack("256s", b"lo")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
        flags_data = fcntl.ioctl(control.fileno(), 0x8913, request)
        address_data = fcntl.ioctl(control.fileno(), 0x8915, request)
    flags = struct.unpack("H", flags_data[16:18])[0]
    address = socket.inet_ntoa(address_data[20:24])
    if not flags & 0x1:
        raise CheckError("loopback is down")
    if address != "127.0.0.1":
        raise CheckError("loopback address={}".format(address))
    with open("/proc/net/route", encoding="utf-8") as stream:
        routes = [line.split() for line in stream.read().splitlines()[1:]]
    if any(len(route) > 3 and route[1] == "00000000" for route in routes):
        raise CheckError("network namespace has a default route")
    return "interface=lo address=127.0.0.1 state=up"


def _inside_ownership(work, uid, gid, name):
    path = work / name
    path.touch()
    os.chown(path, uid, gid)
    metadata = path.stat()
    if (metadata.st_uid, metadata.st_gid) != (uid, gid):
        raise CheckError(
            "owner={}:{} expected={}:{}".format(
                metadata.st_uid,
                metadata.st_gid,
                uid,
                gid,
            )
        )
    return "owner={}:{}".format(uid, gid)


def _inside_proc(mounts):
    mount = _mount_at(mounts, "/proc")
    readonly = "ro" in mount.options or "ro" in mount.super_options
    readonly = readonly and bool(
        os.statvfs("/proc").f_flag & getattr(os, "ST_RDONLY", 1)
    )
    if mount.filesystem != "proc" or not readonly:
        raise CheckError(
            "filesystem={} options={}".format(
                mount.filesystem,
                ",".join(sorted(mount.options | mount.super_options)),
            )
        )
    with open("/proc/self/status", encoding="utf-8") as stream:
        if not stream.read(16):
            raise CheckError("/proc/self/status is empty")
    return "filesystem=proc readonly=true"


def _inside_dev(mounts):
    mount = _mount_at(mounts, "/dev")
    if mount.filesystem not in ("devtmpfs", "tmpfs"):
        raise CheckError("filesystem={}".format(mount.filesystem))
    for path in ("/dev/null", "/dev/zero", "/dev/urandom"):
        if not stat.S_ISCHR(os.stat(path).st_mode):
            raise CheckError("{} is not a character device".format(path))
    with open("/dev/null", "wb") as stream:
        stream.write(b"probe")
    with open("/dev/zero", "rb") as stream:
        if stream.read(8) != b"\0" * 8:
            raise CheckError("/dev/zero returned unexpected data")
    with open("/dev/urandom", "rb") as stream:
        if len(stream.read(8)) != 8:
            raise CheckError("/dev/urandom returned too little data")
    for current, directories, files in os.walk("/dev"):
        for name in directories + files:
            if stat.S_ISBLK(os.lstat(os.path.join(current, name)).st_mode):
                raise CheckError(
                    "block device exposed at {}".format(
                        os.path.join(current, name)
                    )
                )
    return "filesystem={} block_devices=0".format(mount.filesystem)


def _inside_tmp(mounts):
    mount = _mount_at(mounts, "/tmp")
    mode = stat.S_IMODE(os.stat("/tmp").st_mode)
    if mount.filesystem != "tmpfs":
        raise CheckError("filesystem={}".format(mount.filesystem))
    if mode & 0o1777 != 0o1777:
        raise CheckError("mode={:04o}".format(mode))
    with tempfile.NamedTemporaryFile(dir="/tmp") as stream:
        stream.write(b"probe")
        stream.flush()
    return "filesystem=tmpfs mode={:04o} writable=true".format(mode)


def _beneath(path, root):
    root = root.rstrip("/") or "/"
    return path == root or path.startswith(root + "/")


def _inside_exposure(mounts, work):
    allowed = ("/proc", "/dev", "/tmp", str(work))
    unexpected = [
        mount.mountpoint
        for mount in mounts
        if mount.mountpoint != "/"
        and not any(_beneath(mount.mountpoint, prefix) for prefix in allowed)
    ]
    if unexpected:
        raise CheckError("unexpected mounts={}".format(",".join(sorted(unexpected))))
    exposed = [path for path in CONTAINER_SOCKETS if os.path.lexists(path)]
    if exposed:
        raise CheckError("container sockets={}".format(",".join(exposed)))
    return "unexpected_mounts=0 container_sockets=0"


def _inside_check(results, name, function):
    try:
        detail = function()
    except Exception as exc:
        results.append({
            "level": "FAIL",
            "name": name,
            "detail": "{}: {}".format(type(exc).__name__, exc),
        })
    else:
        results.append({"level": "PASS", "name": name, "detail": detail})


def inside_probe(argv):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--outer-namespaces", required=True)
    args = parser.parse_args(argv)

    outer = json.loads(args.outer_namespaces)
    mounts = _mountinfo()
    results = []
    for name in ("user", "net", "pid", "ipc", "mnt"):
        _inside_check(
            results,
            "namespace-{}".format(name),
            lambda name=name: _inside_namespace(name, outer[name]),
        )
    _inside_check(results, "loopback", _inside_loopback)
    _inside_check(
        results,
        "ownership-8-12",
        lambda: _inside_ownership(args.work, 8, 12, "owner-8-12"),
    )
    _inside_check(
        results,
        "ownership-65534",
        lambda: _inside_ownership(args.work, 65534, 65534, "owner-65534"),
    )
    _inside_check(results, "proc", lambda: _inside_proc(mounts))
    _inside_check(results, "dev", lambda: _inside_dev(mounts))
    _inside_check(results, "tmp", lambda: _inside_tmp(mounts))
    _inside_check(results, "exposure", lambda: _inside_exposure(mounts, args.work))
    args.result.write_text(json.dumps(results, sort_keys=True), encoding="utf-8")
    return 0


def _outer_namespaces():
    return {
        name: os.readlink("/proc/self/ns/{}".format(name))
        for name in ("user", "net", "pid", "ipc", "mnt")
    }


def _record_inside_results(path, reporter):
    try:
        results = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CheckError("cannot read sandbox results: {}".format(exc)) from exc
    if not isinstance(results, list):
        raise CheckError("sandbox result is not a list")
    for item in results:
        if (
            not isinstance(item, dict)
            or item.get("level") not in ("PASS", "FAIL", "WARN")
            or not isinstance(item.get("name"), str)
            or not isinstance(item.get("detail"), str)
        ):
            raise CheckError("sandbox result has an invalid record")
        reporter.record(item["level"], item["name"], item["detail"])


def _check_external_owner(path, expected_uid, expected_gid):
    metadata = path.stat()
    if (metadata.st_uid, metadata.st_gid) != (expected_uid, expected_gid):
        raise CheckError(
            "owner={}:{} expected={}:{}".format(
                metadata.st_uid,
                metadata.st_gid,
                expected_uid,
                expected_gid,
            )
        )
    return "owner={}:{}".format(expected_uid, expected_gid)


def run_sandbox_probe(isolation, probe, scratch, ranges, reporter):
    work = Path(tempfile.mkdtemp(prefix="buckos-worker-preflight-", dir=scratch))
    LOG.debug("scratch probe directory: %s", work)
    result = work / "result.json"
    probe_script = work / "preflight_worker.py"
    sysroot = work / "sysroot"
    try:
        shutil.copytree(probe.root, sysroot, symlinks=True)
        shutil.copy2(Path(__file__).resolve(), probe_script)
        command = [
            probe.python_path,
            "-I",
            "-B",
            str(probe_script),
            "--inside-probe",
            "--result", str(result),
            "--work", str(work),
            "--outer-namespaces", json.dumps(_outer_namespaces(), sort_keys=True),
        ]
        isolation.run_isolated(
            command,
            "bwrap",
            work=str(work),
            chdir=str(work),
            sysroot=str(sysroot),
            env={
                "LANG": "C",
                "LC_ALL": "C",
                "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                "TMPDIR": "/tmp",
            },
        )
        reporter.passed("sandbox", "production Bubblewrap launcher exited 0")
        _record_inside_results(result, reporter)

        for name, uid, gid in (
            ("8-12", ranges.uid_base + 7, ranges.gid_base + 11),
            ("65534", ranges.uid_base + 65533, ranges.gid_base + 65533),
        ):
            try:
                detail = _check_external_owner(work / "owner-{}".format(name), uid, gid)
            except (CheckError, OSError) as exc:
                reporter.failed("ownership-map-{}".format(name), exc)
            else:
                reporter.passed("ownership-map-{}".format(name), detail)
    except SystemExit as exc:
        reporter.failed("sandbox", exc)
    except subprocess.CalledProcessError as exc:
        reporter.failed(
            "sandbox",
            "production launcher exited {}".format(exc.returncode),
        )
    except (CheckError, OSError, ValueError) as exc:
        reporter.failed("sandbox", exc)
    finally:
        try:
            removed = isolation.remove_tree(str(work))
        except (OSError, subprocess.SubprocessError) as exc:
            reporter.failed("cleanup", exc)
        else:
            if removed and not work.exists():
                reporter.passed("cleanup", "scratch probe tree removed")
            else:
                reporter.failed(
                    "cleanup",
                    "scratch probe tree remains at {}".format(work),
                )
        if work.exists():
            shutil.rmtree(work, ignore_errors=True)


def run_preflight(args, isolation):
    reporter = Reporter()
    identity_ok = check_identity(args.worker_user, reporter)
    userns_ok = check_user_namespace_policy(reporter)
    tools_ok = check_tools(reporter)
    ranges = check_subordinate_ranges(isolation, reporter)
    scratch = check_scratch(
        args.scratch_root,
        args.min_scratch_bytes,
        args.min_scratch_inodes,
        reporter,
    )
    architecture_ok = check_architecture(
        args.arch,
        args.emulated_aarch64,
        reporter,
    )
    probe = check_probe_sysroot(args.probe_sysroot, args.arch, reporter)
    paths_ok = bool(
        probe
        and scratch
        and check_probe_separation(probe, scratch, reporter)
    )

    if (
        identity_ok
        and userns_ok
        and tools_ok
        and ranges
        and scratch
        and architecture_ok
        and probe
        and paths_ok
    ):
        run_sandbox_probe(isolation, probe, scratch, ranges, reporter)
    else:
        reporter.failed("sandbox", "prerequisite checks failed")
    return 1 if reporter.failures else 0


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "--inside-probe":
        return inside_probe(argv[1:])

    reporter = Reporter()
    try:
        args = _parser().parse_args(argv)
    except UsageError as exc:
        reporter.failed("arguments", exc)
        return 2

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="preflight: %(message)s",
    )
    if args.emulated_aarch64 and args.arch != "aarch64":
        reporter.failed("arguments", "--emulated-aarch64 requires --arch aarch64")
        return 2

    try:
        isolation = load_isolation(args.tools_dir)
    except (CheckError, ImportError, OSError) as exc:
        reporter.failed("production-launcher", exc)
        return 1
    reporter.passed(
        "production-launcher",
        Path(isolation.__file__).resolve(),
    )

    try:
        return run_preflight(args, isolation)
    except KeyboardInterrupt:
        reporter.failed("interrupted", "signal received")
        return 1
    except Exception as exc:
        LOG.exception("internal preflight error")
        reporter.failed("internal", "{}: {}".format(type(exc).__name__, exc))
        return 2


if __name__ == "__main__":
    sys.exit(main())
