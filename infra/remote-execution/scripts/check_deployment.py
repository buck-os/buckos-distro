#!/usr/bin/env python3
"""Run non-mutating NativeLink stage-zero deployment checks.

The worker evidence input is JSON with this shape:

    {
      "observed_at": "2026-08-29T12:00:00Z",
      "workers": [
        {
          "name": "worker-x86_64",
          "connected": true,
          "properties": {
            "platform.OSFamily": ["linux"],
            "platform.arch": ["x86_64"]
          }
        }
      ]
    }

The gRPC command is an argv template, not a shell command. It must contain
the tokens ``{endpoint}``, ``{method}``, and ``{json}``, for example:

    grpcurl -plaintext -d {json} {endpoint} {method}

All checks are read-only. Standard output contains stable tab-separated
PASS, FAIL, and WARN records. Verbose diagnostics go to standard error.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import logging
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import time
from typing import Any, Callable, Iterable, Mapping, Sequence
import urllib.error
import urllib.parse
import urllib.request


LOG = logging.getLogger("deployment-check")
STATUS_VALUES = ("PASS", "FAIL", "WARN")
EPHEMERAL_FILESYSTEMS = frozenset({"overlay", "ramfs", "tmpfs"})
MOUNT_ESCAPE_RE = re.compile(r"\\([0-7]{3})")


class CheckFailure(RuntimeError):
    """A failed admission check."""


class CheckWarning(RuntimeError):
    """A non-fatal but visible admission finding."""


@dataclasses.dataclass(frozen=True)
class MountInfo:
    mount_id: int
    device: str
    root: str
    mount_point: str
    mount_options: frozenset[str]
    fs_type: str
    source: str
    super_options: frozenset[str]

    @property
    def identity(self) -> str:
        return "{}|{}|{}|{}|{}".format(
            self.device,
            self.root,
            self.mount_point,
            self.fs_type,
            self.source,
        )


class Reporter:
    def __init__(self, stream: Any = None) -> None:
        self.stream = stream or sys.stdout
        self.records: list[dict[str, Any]] = []

    def emit(self, status: str, check: str, message: str, **details: Any) -> None:
        if status not in STATUS_VALUES:
            raise ValueError("unsupported status: {}".format(status))
        payload = {"message": message, **details}
        self.records.append({"status": status, "check": check, **payload})
        self.stream.write(
            "{}\t{}\t{}\n".format(
                status,
                check,
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
            )
        )

    def run(self, check: str, callback: Callable[[], Mapping[str, Any]]) -> bool:
        try:
            details = dict(callback())
        except CheckWarning as error:
            self.emit("WARN", check, str(error))
            return True
        except (CheckFailure, OSError, ValueError, subprocess.SubprocessError) as error:
            self.emit("FAIL", check, str(error))
            return False
        self.emit("PASS", check, "ok", **details)
        return True

    @property
    def failures(self) -> int:
        return sum(record["status"] == "FAIL" for record in self.records)

    @property
    def warnings(self) -> int:
        return sum(record["status"] == "WARN" for record in self.records)


def parse_bytes(value: str) -> int:
    match = re.fullmatch(r"([0-9]+)([KMGTP]i?B)?", value.strip(), re.IGNORECASE)
    if not match:
        raise argparse.ArgumentTypeError("invalid byte quantity: {!r}".format(value))
    amount = int(match.group(1))
    suffix = (match.group(2) or "B").upper()
    multipliers = {
        "B": 1,
        "KB": 1000,
        "KIB": 1024,
        "MB": 1000 ** 2,
        "MIB": 1024 ** 2,
        "GB": 1000 ** 3,
        "GIB": 1024 ** 3,
        "TB": 1000 ** 4,
        "TIB": 1024 ** 4,
        "PB": 1000 ** 5,
        "PIB": 1024 ** 5,
    }
    return amount * multipliers[suffix]


def _decode_mount_field(value: str) -> str:
    return MOUNT_ESCAPE_RE.sub(lambda match: chr(int(match.group(1), 8)), value)


def parse_mountinfo(text: str) -> list[MountInfo]:
    mounts = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            left, right = line.split(" - ", 1)
        except ValueError as error:
            raise CheckFailure("malformed mountinfo line") from error
        left_fields = left.split()
        right_fields = right.split()
        if len(left_fields) < 6 or len(right_fields) < 3:
            raise CheckFailure("malformed mountinfo fields")
        mounts.append(MountInfo(
            mount_id=int(left_fields[0]),
            device=left_fields[2],
            root=_decode_mount_field(left_fields[3]),
            mount_point=_decode_mount_field(left_fields[4]),
            mount_options=frozenset(left_fields[5].split(",")),
            fs_type=right_fields[0],
            source=_decode_mount_field(right_fields[1]),
            super_options=frozenset(right_fields[2].split(",")),
        ))
    return mounts


def find_mount(path: Path, mounts: Sequence[MountInfo]) -> MountInfo:
    resolved = str(path.resolve(strict=True))
    candidates = []
    for mount in mounts:
        try:
            if os.path.commonpath((resolved, mount.mount_point)) == mount.mount_point:
                candidates.append(mount)
        except ValueError:
            continue
    if not candidates:
        raise CheckFailure("no mountinfo entry covers {}".format(resolved))
    return max(candidates, key=lambda item: len(item.mount_point))


def read_mountinfo(path: Path = Path("/proc/self/mountinfo")) -> list[MountInfo]:
    try:
        return parse_mountinfo(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise CheckFailure("cannot read {}: {}".format(path, error)) from error


def _writable_directory(path: Path) -> None:
    if not path.is_dir():
        raise CheckFailure("{} is not a directory".format(path))
    try:
        writable = os.access(path, os.W_OK | os.X_OK, effective_ids=True)
    except TypeError:
        writable = os.access(path, os.W_OK | os.X_OK)
    if not writable:
        raise CheckFailure("{} is not writable/searchable by the effective identity".format(path))
    readonly = getattr(os, "ST_RDONLY", 1)
    if os.statvfs(path).f_flag & readonly:
        raise CheckFailure("{} is on a read-only filesystem".format(path))


def check_storage_identity(
    cas_path: Path,
    ac_path: Path,
    storage_mount: Path,
    expected_source: str,
    expected_fs_type: str,
    mounts: Sequence[MountInfo],
) -> Mapping[str, Any]:
    cas = cas_path.resolve(strict=True)
    ac = ac_path.resolve(strict=True)
    expected_mount = storage_mount.resolve(strict=True)
    if cas == ac:
        raise CheckFailure("CAS and action cache paths must be distinct")
    cas_mount = find_mount(cas, mounts)
    ac_mount = find_mount(ac, mounts)
    if cas_mount.mount_id != ac_mount.mount_id:
        raise CheckFailure("CAS and action cache resolve to different mounts")
    if Path(cas_mount.mount_point) != expected_mount:
        raise CheckFailure(
            "storage resolves to mount {}, expected {}".format(
                cas_mount.mount_point, expected_mount,
            )
        )
    if cas_mount.source != expected_source:
        raise CheckFailure(
            "storage source is {!r}, expected {!r}".format(
                cas_mount.source, expected_source,
            )
        )
    if cas_mount.fs_type != expected_fs_type:
        raise CheckFailure(
            "storage filesystem is {!r}, expected {!r}".format(
                cas_mount.fs_type, expected_fs_type,
            )
        )
    if cas_mount.fs_type in EPHEMERAL_FILESYSTEMS:
        raise CheckFailure("storage filesystem {!r} is ephemeral".format(cas_mount.fs_type))
    return {
        "device": cas_mount.device,
        "filesystem": cas_mount.fs_type,
        "identity": cas_mount.identity,
        "mount": cas_mount.mount_point,
    }


def check_storage_writable(
    cas_path: Path,
    ac_path: Path,
    mounts: Sequence[MountInfo],
) -> Mapping[str, Any]:
    for path in (cas_path.resolve(strict=True), ac_path.resolve(strict=True)):
        _writable_directory(path)
        mount = find_mount(path, mounts)
        if "rw" not in mount.mount_options and "rw" not in mount.super_options:
            raise CheckFailure("{} is not mounted read-write".format(path))
    return {"cas_path": str(cas_path.resolve()), "ac_path": str(ac_path.resolve())}


def check_watermarks(
    cas_low: int,
    cas_high: int,
    ac_low: int,
    ac_high: int,
) -> Mapping[str, Any]:
    for name, low, high in (
        ("CAS", cas_low, cas_high),
        ("action cache", ac_low, ac_high),
    ):
        if low <= 0 or high <= 0:
            raise CheckFailure("{} watermarks must be positive".format(name))
        if low >= high:
            raise CheckFailure("{} low watermark must be below high watermark".format(name))
    return {
        "ac_high_bytes": ac_high,
        "ac_low_bytes": ac_low,
        "cas_high_bytes": cas_high,
        "cas_low_bytes": cas_low,
    }


def check_storage_capacity(
    storage_mount: Path,
    cas_high: int,
    ac_high: int,
    reserve_bytes: int,
    min_free_inodes: int,
) -> Mapping[str, Any]:
    stats = os.statvfs(storage_mount.resolve(strict=True))
    total = stats.f_blocks * stats.f_frsize
    available = stats.f_bavail * stats.f_frsize
    available_inodes: int | None = stats.f_favail
    required = cas_high + ac_high + reserve_bytes
    if total < required:
        raise CheckFailure(
            "storage capacity {} is below configured stores plus reserve {}".format(
                total, required,
            )
        )
    if available < reserve_bytes:
        raise CheckFailure(
            "available bytes {} are below reserve {}".format(available, reserve_bytes)
        )
    inode_model = "fixed"
    if stats.f_files == 0:
        inode_model = "dynamic"
        available_inodes = None
        if min_free_inodes != 0:
            raise CheckFailure(
                "filesystem has dynamic inode allocation; configure a zero inode floor"
            )
    elif available_inodes < min_free_inodes:
        raise CheckFailure(
            "available inodes {} are below minimum {}".format(
                available_inodes, min_free_inodes,
            )
        )
    return {
        "available_bytes": available,
        "available_inodes": available_inodes,
        "inode_model": inode_model,
        "required_capacity_bytes": required,
        "total_bytes": total,
    }


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


def check_http_health(url: str, timeout: float) -> Mapping[str, Any]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise CheckFailure("health URL must use http or https with a hostname")
    if parsed.username or parsed.password:
        raise CheckFailure("health URL must not contain credentials")
    try:
        port = parsed.port
    except ValueError as error:
        raise CheckFailure("health URL has an invalid port") from error
    if port is None:
        raise CheckFailure("health URL must contain an explicit port")
    request = urllib.request.Request(url, headers={"User-Agent": "buckos-re-check/1"})
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            status_code = response.status
            response.read(4096)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise CheckFailure(
            "health request failed ({})".format(type(error).__name__)
        ) from error
    if status_code < 200 or status_code >= 300:
        raise CheckFailure("health endpoint returned HTTP {}".format(status_code))
    return {"http_status": status_code, "scheme": parsed.scheme}


def _validate_executable(command: Sequence[str]) -> list[str]:
    if not command:
        raise CheckFailure("command is empty")
    candidate = command[0]
    resolved = shutil.which(candidate) if os.path.sep not in candidate else candidate
    if not resolved:
        raise CheckFailure("command executable {!r} was not found".format(candidate))
    executable = Path(resolved).resolve(strict=True)
    mode = executable.stat().st_mode
    if not stat.S_ISREG(mode) or not os.access(executable, os.X_OK):
        raise CheckFailure("{} is not a regular executable".format(executable))
    if mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise CheckFailure("{} is group- or world-writable".format(executable))
    parent = executable.parent
    while True:
        parent_mode = parent.stat().st_mode
        writable_by_others = parent_mode & (stat.S_IWGRP | stat.S_IWOTH)
        if writable_by_others and not parent_mode & stat.S_ISVTX:
            raise CheckFailure("executable ancestor {} is unsafely writable".format(parent))
        if parent == parent.parent:
            break
        parent = parent.parent
    return [str(executable), *command[1:]]


def render_command(template: str, replacements: Mapping[str, str]) -> list[str]:
    try:
        parts = shlex.split(template)
    except ValueError as error:
        raise CheckFailure("invalid command quoting: {}".format(error)) from error
    required = {"{endpoint}", "{method}", "{json}"}
    text = " ".join(parts)
    missing = sorted(item for item in required if item not in text)
    if missing:
        raise CheckFailure("command template is missing {}".format(", ".join(missing)))
    rendered = []
    for part in parts:
        for key, value in replacements.items():
            part = part.replace("{" + key + "}", value)
        rendered.append(part)
    if any(re.search(r"\{[a-z]+\}", part) for part in rendered):
        raise CheckFailure("command template contains an unsupported placeholder")
    return _validate_executable(rendered)


def _grpc_json(
    command_template: str,
    endpoint: str,
    method: str,
    payload: Mapping[str, Any],
    timeout: float,
    operation: str,
) -> Mapping[str, Any]:
    command = render_command(command_template, {
        "endpoint": endpoint,
        "method": method,
        "json": json.dumps(payload, separators=(",", ":")),
    })
    LOG.debug("running %s with %s", operation, Path(command[0]).name)
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise CheckFailure("{} timed out".format(operation)) from error
    if result.returncode != 0:
        LOG.debug("gRPC client stderr: %s", result.stderr[:4096])
        raise CheckFailure(
            "{} client exited {}".format(operation, result.returncode)
        )
    return _json_output(result.stdout)


def _json_output(output: str) -> Mapping[str, Any]:
    start = output.find("{")
    end = output.rfind("}")
    if start == -1 or end < start:
        raise CheckFailure("gRPC client returned no JSON object")
    try:
        value = json.loads(output[start:end + 1])
    except json.JSONDecodeError as error:
        raise CheckFailure("gRPC client returned invalid JSON") from error
    if not isinstance(value, dict):
        raise CheckFailure("gRPC client response is not an object")
    return value


def _mapping_value(mapping: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    return None


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def check_grpc_health(
    command_template: str,
    endpoint: str,
    service: str,
    timeout: float,
) -> Mapping[str, Any]:
    response = _grpc_json(
        command_template,
        endpoint,
        "grpc.health.v1.Health/Check",
        {"service": service},
        timeout,
        "gRPC health check",
    )
    status_value = _mapping_value(response, "status")
    if status_value not in (1, "SERVING"):
        raise CheckFailure(
            "gRPC health status is {!r}, expected SERVING".format(status_value)
        )
    return {"service": service, "status": "SERVING"}


def check_capabilities(
    command_template: str,
    endpoint: str,
    instance_name: str,
    timeout: float,
    require_execution: bool,
) -> Mapping[str, Any]:
    method = "build.bazel.remote.execution.v2.Capabilities/GetCapabilities"
    response = _grpc_json(
        command_template,
        endpoint,
        method,
        {"instanceName": instance_name},
        timeout,
        "GetCapabilities",
    )
    cache = _mapping_value(response, "cacheCapabilities", "cache_capabilities")
    if not isinstance(cache, dict):
        raise CheckFailure("GetCapabilities omitted cache capabilities")
    digest_functions = {item.upper() for item in _strings(cache)}
    if "SHA256" not in digest_functions:
        raise CheckFailure("GetCapabilities does not advertise SHA256 for cache operations")
    execution = _mapping_value(
        response,
        "executionCapabilities",
        "execution_capabilities",
    )
    if require_execution:
        if not isinstance(execution, dict):
            raise CheckFailure("GetCapabilities omitted execution capabilities")
        enabled = _mapping_value(execution, "execEnabled", "exec_enabled")
        if enabled is not True:
            raise CheckFailure("GetCapabilities does not advertise execution enabled")
        execution_digests = {item.upper() for item in _strings(execution)}
        if "SHA256" not in execution_digests:
            raise CheckFailure("GetCapabilities does not advertise SHA256 for execution")
    return {
        "cache_sha256": True,
        "execution_enabled": bool(require_execution),
        "instance_name": instance_name,
    }


def _load_json_bytes(data: bytes, source: str) -> Mapping[str, Any]:
    if len(data) > 1024 * 1024:
        raise CheckFailure("{} exceeds one MiB".format(source))
    try:
        value = json.loads(data)
    except json.JSONDecodeError as error:
        raise CheckFailure("{} is not valid JSON".format(source)) from error
    if not isinstance(value, dict):
        raise CheckFailure("{} must contain a JSON object".format(source))
    return value


def load_worker_evidence(
    evidence_file: Path | None,
    evidence_command: str | None,
    timeout: float,
) -> Mapping[str, Any]:
    if (evidence_file is None) == (evidence_command is None):
        raise CheckFailure("configure exactly one worker evidence file or command")
    if evidence_file is not None:
        path = evidence_file.resolve(strict=True)
        if not path.is_file():
            raise CheckFailure("worker evidence path is not a regular file")
        return _load_json_bytes(path.read_bytes(), "worker evidence file")
    command = _validate_executable(shlex.split(evidence_command or ""))
    LOG.debug("running worker evidence command %s", Path(command[0]).name)
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise CheckFailure("worker evidence command timed out") from error
    if result.returncode != 0:
        LOG.debug("worker evidence stderr: %s", result.stderr.decode(errors="replace")[:4096])
        raise CheckFailure("worker evidence command exited {}".format(result.returncode))
    return _load_json_bytes(result.stdout, "worker evidence command")


def _parse_observed_at(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            return dt.datetime.fromisoformat(normalized).timestamp()
        except ValueError as error:
            raise CheckFailure("worker evidence has invalid observed_at") from error
    raise CheckFailure("worker evidence is missing observed_at")


def _property_values(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return set(value)
    if isinstance(value, dict):
        return _property_values(value.get("values"))
    return set()


def validate_worker_evidence(
    evidence: Mapping[str, Any],
    maximum_age: float,
    now: float | None = None,
) -> Mapping[str, list[str]]:
    observed_at = _parse_observed_at(evidence.get("observed_at"))
    age = (time.time() if now is None else now) - observed_at
    if age < -30:
        raise CheckFailure("worker evidence timestamp is in the future")
    if age > maximum_age:
        raise CheckFailure("worker evidence is {:.1f} seconds old".format(age))
    workers = evidence.get("workers")
    if not isinstance(workers, list):
        raise CheckFailure("worker evidence must contain a workers array")
    ready: dict[str, list[str]] = {"x86_64": [], "aarch64": []}
    names = set()
    for worker in workers:
        if not isinstance(worker, dict):
            raise CheckFailure("worker record must be an object")
        name = worker.get("name")
        if not isinstance(name, str) or not name:
            raise CheckFailure("worker record is missing name")
        if name in names:
            raise CheckFailure("duplicate worker name {!r}".format(name))
        names.add(name)
        if worker.get("connected") is not True:
            continue
        properties = worker.get("properties")
        if not isinstance(properties, dict):
            raise CheckFailure("connected worker {!r} has no properties".format(name))
        required_properties = {"platform.OSFamily", "platform.arch"}
        if set(properties) != required_properties:
            raise CheckFailure(
                "connected worker {!r} has property keys {}, expected {}".format(
                    name,
                    sorted(properties),
                    sorted(required_properties),
                )
            )
        os_family = _property_values(properties.get("platform.OSFamily"))
        architecture = _property_values(properties.get("platform.arch"))
        if os_family != {"linux"}:
            raise CheckFailure(
                "connected worker {!r} has platform.OSFamily {}".format(
                    name, sorted(os_family),
                )
            )
        if len(architecture) != 1 or next(iter(architecture), None) not in ready:
            raise CheckFailure(
                "connected worker {!r} has platform.arch {}".format(
                    name, sorted(architecture),
                )
            )
        ready[next(iter(architecture))].append(name)
    return ready


def _endpoint(url: str, what: str) -> tuple[str, int, str]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https", "grpc", "grpcs"):
        raise CheckFailure("{} endpoint has unsupported scheme".format(what))
    if not parsed.hostname:
        raise CheckFailure("{} endpoint has no hostname".format(what))
    if parsed.username or parsed.password:
        raise CheckFailure("{} endpoint must not contain credentials".format(what))
    try:
        port = parsed.port
    except ValueError as error:
        raise CheckFailure("{} endpoint has an invalid port".format(what)) from error
    if port is None:
        raise CheckFailure("{} endpoint must contain an explicit port".format(what))
    return parsed.hostname, port, parsed.scheme


def check_otlp_config(endpoint: str, export_interval_ms: int) -> Mapping[str, Any]:
    _host, port, scheme = _endpoint(endpoint, "OTLP")
    if export_interval_ms <= 0 or export_interval_ms > 60000:
        raise CheckFailure("OTLP export interval must be between 1 and 60000 ms")
    return {"export_interval_ms": export_interval_ms, "port": port, "scheme": scheme}


def check_tcp_reachability(endpoint: str, timeout: float) -> Mapping[str, Any]:
    host, port, scheme = _endpoint(endpoint, "OTLP")
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except OSError as error:
        raise CheckFailure(
            "OTLP collector is unreachable ({})".format(type(error).__name__)
        ) from error
    return {"reachable": True, "scheme": scheme}


def _integer_file(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError) as error:
        raise CheckFailure("cannot read integer from {}".format(path)) from error


def _host_pid_namespace_visible(proc_root: Path) -> bool:
    try:
        status = (proc_root / "self" / "status").read_text(encoding="utf-8")
    except OSError as error:
        raise CheckFailure("cannot read current process status") from error
    match = re.search(r"^NSpid:\s+(.+)$", status, re.MULTILINE)
    if match and len(match.group(1).split()) > 1:
        return False
    return True


def read_inotify_usage(
    proc_root: Path = Path("/proc"),
    uid: int | None = None,
) -> Mapping[str, int]:
    if not _host_pid_namespace_visible(proc_root):
        raise CheckFailure("client check is not running in the host PID namespace")
    target_uid = os.getuid() if uid is None else uid
    instances = 0
    watches = 0
    processes = 0
    hidden = []
    try:
        entries = list(proc_root.iterdir())
    except OSError as error:
        raise CheckFailure("cannot enumerate process table") from error
    for process in entries:
        if not process.name.isdigit():
            continue
        try:
            if process.stat().st_uid != target_uid:
                continue
            processes += 1
            fds = list((process / "fd").iterdir())
        except FileNotFoundError:
            continue
        except PermissionError:
            hidden.append(process.name)
            continue
        for descriptor in fds:
            try:
                target = os.readlink(descriptor)
            except FileNotFoundError:
                continue
            except PermissionError:
                hidden.append(process.name)
                break
            if not target.startswith("anon_inode") or "inotify" not in target:
                continue
            instances += 1
            try:
                fdinfo = (process / "fdinfo" / descriptor.name).read_text(
                    encoding="utf-8",
                    errors="replace",
                )
            except FileNotFoundError:
                continue
            except PermissionError:
                hidden.append(process.name)
                break
            watches += sum(line.startswith("inotify wd:") for line in fdinfo.splitlines())
    if hidden:
        raise CheckFailure(
            "cannot inspect same-UID process(es): {}".format(
                ", ".join(sorted(set(hidden))[:10]),
            )
        )
    return {"instances": instances, "processes": processes, "watches": watches}


def repository_directory_count(root: Path, ignored_names: set[str]) -> int:
    resolved = root.resolve(strict=True)
    if not resolved.is_dir():
        raise CheckFailure("client repository root is not a directory")
    count = 0
    for _dirpath, dirnames, _filenames in os.walk(resolved, followlinks=False):
        dirnames[:] = [
            name for name in dirnames
            if name not in ignored_names and not Path(_dirpath, name).is_symlink()
        ]
        count += 1
    return count


def inotify_snapshot(
    repo_root: Path,
    ignored_names: set[str],
    proc_root: Path = Path("/proc"),
    sysctl_root: Path = Path("/proc/sys/fs/inotify"),
) -> Mapping[str, int | bool]:
    usage = read_inotify_usage(proc_root)
    return {
        "directories": repository_directory_count(repo_root, ignored_names),
        "host_pid_namespace": True,
        "instances": usage["instances"],
        "instances_limit": _integer_file(sysctl_root / "max_user_instances"),
        "processes": usage["processes"],
        "queued_limit": _integer_file(sysctl_root / "max_queued_events"),
        "watches": usage["watches"],
        "watches_limit": _integer_file(sysctl_root / "max_user_watches"),
    }


def check_inotify_instances(
    snapshot: Mapping[str, int | bool],
    additional_daemons: int,
) -> Mapping[str, Any]:
    if additional_daemons < 0:
        raise CheckFailure("additional daemon count must not be negative")
    required_instances = 32 + 4 * additional_daemons
    instance_headroom = int(snapshot["instances_limit"]) - int(snapshot["instances"])
    if instance_headroom < required_instances:
        raise CheckFailure(
            "inotify instance headroom {} is below required {}".format(
                instance_headroom, required_instances,
            )
        )
    return {
        "headroom": instance_headroom,
        "limit": snapshot["instances_limit"],
        "required_headroom": required_instances,
        "used": snapshot["instances"],
    }


def check_inotify_watches(
    snapshot: Mapping[str, int | bool],
    additional_daemons: int,
) -> Mapping[str, Any]:
    if additional_daemons < 0:
        raise CheckFailure("additional daemon count must not be negative")
    directories = int(snapshot["directories"])
    required_watches = 65536 + additional_daemons * (2 * directories)
    watch_headroom = int(snapshot["watches_limit"]) - int(snapshot["watches"])
    if watch_headroom < required_watches:
        raise CheckFailure(
            "inotify watch headroom {} is below required {}".format(
                watch_headroom, required_watches,
            )
        )
    return {
        "headroom": watch_headroom,
        "limit": snapshot["watches_limit"],
        "repository_directories": directories,
        "required_headroom": required_watches,
        "used": snapshot["watches"],
    }


def check_inotify_queue(snapshot: Mapping[str, int | bool]) -> Mapping[str, Any]:
    queued_limit = int(snapshot["queued_limit"])
    if queued_limit < 65536:
        raise CheckFailure(
            "max_queued_events {} is below required 65536".format(queued_limit)
        )
    return {"limit": queued_limit, "required": 65536}


def _argument_parser() -> argparse.ArgumentParser:
    env = os.environ
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog=(
            "Arguments may use BUCKOS_RE_GRPC_COMMAND, BUCKOS_RE_ENDPOINT, "
            "BUCKOS_RE_GRPC_HEALTH_SERVICE, BUCKOS_RE_INSTANCE, "
            "BUCKOS_RE_CAS_PATH, "
            "BUCKOS_RE_AC_PATH, BUCKOS_RE_STORAGE_MOUNT, "
            "BUCKOS_RE_STORAGE_SOURCE, BUCKOS_RE_STORAGE_FSTYPE, "
            "BUCKOS_RE_CAS_LOW_BYTES, BUCKOS_RE_CAS_HIGH_BYTES, "
            "BUCKOS_RE_AC_LOW_BYTES, BUCKOS_RE_AC_HIGH_BYTES, "
            "BUCKOS_RE_STORAGE_RESERVE_BYTES, BUCKOS_RE_MIN_FREE_INODES, "
            "BUCKOS_RE_WORKER_EVIDENCE_FILE, "
            "BUCKOS_RE_WORKER_EVIDENCE_COMMAND, "
            "BUCKOS_RE_WORKER_EVIDENCE_MAX_AGE_SECONDS, NL_OTEL_ENDPOINT, "
            "OTEL_METRIC_EXPORT_INTERVAL, BUCKOS_RE_COLLECTOR_HEALTH_URL, "
            "BUCKOS_RE_CLIENT_REPO_ROOT, BUCKOS_RE_ADDITIONAL_DAEMONS, and "
            "BUCKOS_RE_CHECK_TIMEOUT_SECONDS as environment fallbacks."
        ),
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="write diagnostics to stderr",
    )
    parser.add_argument(
        "--grpc-command", default=env.get("BUCKOS_RE_GRPC_COMMAND"),
        help="gRPC argv template with {json}, {endpoint}, and {method}",
    )
    parser.add_argument(
        "--reapi-endpoint", default=env.get("BUCKOS_RE_ENDPOINT"),
        help="NativeLink client host:port",
    )
    parser.add_argument(
        "--grpc-health-service",
        default=env.get("BUCKOS_RE_GRPC_HEALTH_SERVICE", ""),
        help="gRPC health service name; empty checks overall server health",
    )
    parser.add_argument(
        "--instance-name", default=env.get("BUCKOS_RE_INSTANCE"),
        help="REAPI instance name",
    )
    parser.add_argument(
        "--cache-only", action="store_true",
        help="do not require execution capabilities",
    )
    parser.add_argument(
        "--cas-path", type=Path, default=env.get("BUCKOS_RE_CAS_PATH"),
        help="persistent CAS content directory",
    )
    parser.add_argument(
        "--ac-path", type=Path, default=env.get("BUCKOS_RE_AC_PATH"),
        help="persistent action-cache directory",
    )
    parser.add_argument(
        "--storage-mount", type=Path,
        default=env.get("BUCKOS_RE_STORAGE_MOUNT"),
        help="exact persistent mount containing CAS and AC",
    )
    parser.add_argument(
        "--expected-storage-source",
        default=env.get("BUCKOS_RE_STORAGE_SOURCE"),
        help="expected mountinfo source",
    )
    parser.add_argument(
        "--expected-storage-fstype",
        default=env.get("BUCKOS_RE_STORAGE_FSTYPE"),
        help="expected non-ephemeral filesystem type",
    )
    parser.add_argument(
        "--cas-low-bytes", type=parse_bytes,
        default=env.get("BUCKOS_RE_CAS_LOW_BYTES"),
    )
    parser.add_argument(
        "--cas-high-bytes", type=parse_bytes,
        default=env.get("BUCKOS_RE_CAS_HIGH_BYTES"),
    )
    parser.add_argument(
        "--ac-low-bytes", type=parse_bytes,
        default=env.get("BUCKOS_RE_AC_LOW_BYTES"),
    )
    parser.add_argument(
        "--ac-high-bytes", type=parse_bytes,
        default=env.get("BUCKOS_RE_AC_HIGH_BYTES"),
    )
    parser.add_argument(
        "--storage-reserve-bytes", type=parse_bytes,
        default=env.get("BUCKOS_RE_STORAGE_RESERVE_BYTES"),
    )
    parser.add_argument(
        "--min-free-inodes", type=int,
        default=env.get("BUCKOS_RE_MIN_FREE_INODES"),
    )
    evidence = parser.add_mutually_exclusive_group()
    evidence.add_argument(
        "--worker-evidence-file", type=Path,
        default=env.get("BUCKOS_RE_WORKER_EVIDENCE_FILE"),
    )
    evidence.add_argument(
        "--worker-evidence-command",
        default=env.get("BUCKOS_RE_WORKER_EVIDENCE_COMMAND"),
    )
    parser.add_argument(
        "--worker-evidence-max-age-seconds", type=float,
        default=env.get("BUCKOS_RE_WORKER_EVIDENCE_MAX_AGE_SECONDS", "120"),
    )
    parser.add_argument("--otel-endpoint", default=env.get("NL_OTEL_ENDPOINT"))
    parser.add_argument(
        "--otel-export-interval-ms", type=int,
        default=env.get("OTEL_METRIC_EXPORT_INTERVAL", "60000"),
    )
    parser.add_argument(
        "--collector-health-url",
        default=env.get("BUCKOS_RE_COLLECTOR_HEALTH_URL"),
    )
    parser.add_argument(
        "--client-repo-root", type=Path,
        default=env.get("BUCKOS_RE_CLIENT_REPO_ROOT"),
    )
    parser.add_argument(
        "--additional-buck-daemons", type=int,
        default=env.get("BUCKOS_RE_ADDITIONAL_DAEMONS", "2"),
    )
    parser.add_argument("--ignore-directory", action="append", default=[])
    parser.add_argument(
        "--timeout-seconds", type=float,
        default=env.get("BUCKOS_RE_CHECK_TIMEOUT_SECONDS", "5"),
    )
    return parser


def _require_arguments(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    required = (
        "grpc_command",
        "reapi_endpoint",
        "instance_name",
        "cas_path",
        "ac_path",
        "storage_mount",
        "expected_storage_source",
        "expected_storage_fstype",
        "cas_low_bytes",
        "cas_high_bytes",
        "ac_low_bytes",
        "ac_high_bytes",
        "storage_reserve_bytes",
        "min_free_inodes",
        "otel_endpoint",
        "collector_health_url",
        "client_repo_root",
    )
    missing = [name.replace("_", "-") for name in required if getattr(args, name) is None]
    if not args.worker_evidence_file and not args.worker_evidence_command:
        missing.append("worker-evidence-file-or-command")
    if missing:
        parser.error(
            "missing required arguments or environment values: {}".format(
                ", ".join(missing),
            )
        )
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    if args.worker_evidence_max_age_seconds <= 0:
        parser.error("--worker-evidence-max-age-seconds must be positive")
    if args.min_free_inodes < 0:
        parser.error("--min-free-inodes must not be negative")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _argument_parser()
    args = parser.parse_args(argv)
    _require_arguments(parser, args)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="deployment-check: %(message)s",
    )
    reporter = Reporter()

    reporter.run(
        "nativelink.health",
        lambda: check_grpc_health(
            args.grpc_command,
            args.reapi_endpoint,
            args.grpc_health_service,
            args.timeout_seconds,
        ),
    )
    reporter.run(
        "reapi.capabilities",
        lambda: check_capabilities(
            args.grpc_command,
            args.reapi_endpoint,
            args.instance_name,
            args.timeout_seconds,
            not args.cache_only,
        ),
    )

    try:
        mounts = read_mountinfo()
    except CheckFailure as error:
        reporter.emit("FAIL", "storage.identity", str(error))
        reporter.emit("FAIL", "storage.writable", "mount identity is unavailable")
    else:
        reporter.run(
            "storage.identity",
            lambda: check_storage_identity(
                args.cas_path,
                args.ac_path,
                args.storage_mount,
                args.expected_storage_source,
                args.expected_storage_fstype,
                mounts,
            ),
        )
        reporter.run(
            "storage.writable",
            lambda: check_storage_writable(args.cas_path, args.ac_path, mounts),
        )
    reporter.run(
        "storage.watermarks",
        lambda: check_watermarks(
            args.cas_low_bytes,
            args.cas_high_bytes,
            args.ac_low_bytes,
            args.ac_high_bytes,
        ),
    )
    reporter.run(
        "storage.capacity",
        lambda: check_storage_capacity(
            args.storage_mount,
            args.cas_high_bytes,
            args.ac_high_bytes,
            args.storage_reserve_bytes,
            args.min_free_inodes,
        ),
    )

    evidence_ok = True
    try:
        evidence = load_worker_evidence(
            args.worker_evidence_file,
            args.worker_evidence_command,
            args.timeout_seconds,
        )
        ready = validate_worker_evidence(
            evidence,
            args.worker_evidence_max_age_seconds,
        )
    except (CheckFailure, OSError, ValueError, subprocess.SubprocessError) as error:
        reporter.emit("FAIL", "workers.evidence", str(error))
        evidence_ok = False
        ready = {"x86_64": [], "aarch64": []}
    else:
        reporter.emit(
            "PASS",
            "workers.evidence",
            "ok",
            connected=sum(len(names) for names in ready.values()),
        )
    for architecture in ("x86_64", "aarch64"):
        if evidence_ok and ready[architecture]:
            reporter.emit(
                "PASS",
                "workers." + architecture,
                "ok",
                count=len(ready[architecture]),
                workers=sorted(ready[architecture]),
            )
        else:
            reporter.emit(
                "FAIL",
                "workers." + architecture,
                "no connected worker with exact linux/{} properties".format(architecture),
            )

    reporter.run(
        "otlp.config",
        lambda: check_otlp_config(args.otel_endpoint, args.otel_export_interval_ms),
    )
    reporter.run(
        "otlp.collector",
        lambda: check_tcp_reachability(args.otel_endpoint, args.timeout_seconds),
    )
    reporter.run(
        "otlp.collector_health",
        lambda: check_http_health(args.collector_health_url, args.timeout_seconds),
    )

    try:
        inotify = inotify_snapshot(
            args.client_repo_root,
            {".git", "buck-out", *args.ignore_directory},
        )
    except (CheckFailure, OSError, ValueError) as error:
        for check in (
            "client.inotify.visibility",
            "client.inotify.instances",
            "client.inotify.watches",
            "client.inotify.queue",
        ):
            reporter.emit("FAIL", check, str(error))
    else:
        reporter.emit(
            "PASS",
            "client.inotify.visibility",
            "ok",
            host_pid_namespace=inotify["host_pid_namespace"],
            same_uid_processes=inotify["processes"],
        )
        reporter.run(
            "client.inotify.instances",
            lambda: check_inotify_instances(inotify, args.additional_buck_daemons),
        )
        reporter.run(
            "client.inotify.watches",
            lambda: check_inotify_watches(inotify, args.additional_buck_daemons),
        )
        reporter.run(
            "client.inotify.queue",
            lambda: check_inotify_queue(inotify),
        )

    status = "FAIL" if reporter.failures else "PASS"
    reporter.emit(
        status,
        "stage-zero",
        "admission denied" if reporter.failures else "admission checks passed",
        failures=reporter.failures,
        warnings=reporter.warnings,
    )
    return 1 if reporter.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
