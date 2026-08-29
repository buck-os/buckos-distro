#!/usr/bin/env python3
"""Validate the checked-in NativeLink deployment contract."""

import argparse
import ipaddress
import json
import logging
import os
import re
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple


IMAGE_REPOSITORY = "ghcr.io/tracemachina/nativelink"
IMAGE_VERSION = "v1.6.6"
IMAGE_COMMIT = "a21edb0fc56879124e308bb9a67be679f8eaf885"
IMAGE_DIGEST = (
    "sha256:5c2e6eca51c6d3ac40b94f703e08a243" "fd036cc136cc858a99040ca90fa57d61"
)
IMAGE_REFERENCE = IMAGE_REPOSITORY + "@" + IMAGE_DIGEST
INSTANCE = "main"
WORKERS = {"x86_64": "worker-x86_64", "aarch64": "worker-aarch64"}
PUBLIC_SERVICES = frozenset(
    {
        "ac",
        "bytestream",
        "capabilities",
        "cas",
        "execution",
        "experimental_bep",
        "fetch",
        "push",
    }
)
ENV_ADDRESS = re.compile(
    r"^\$\{[A-Z][A-Z0-9_]*(?::-(?P<default>[^}]+))?\}:(?P<port>[0-9]+)$"
)
ENV_BOUND = re.compile(r"^\$\{[A-Z][A-Z0-9_]*:-[1-9][0-9]*\}$")
JsonObject = Dict[str, Any]


class NativeLinkConfigError(ValueError):
    def __init__(self, errors: Sequence[str]) -> None:
        super().__init__("\n".join(errors))
        self.errors = tuple(errors)


def repo_root() -> str:
    for start in (os.getcwd(), os.path.dirname(os.path.abspath(__file__))):
        path = os.path.abspath(start)
        while True:
            if os.path.isfile(os.path.join(path, ".buckroot")):
                return path
            parent = os.path.dirname(path)
            if parent == path:
                break
            path = parent
    raise NativeLinkConfigError(["cannot locate repository root"])


def deployment_directory() -> str:
    return os.path.join(repo_root(), "infra", "remote-execution", "nativelink")


def _load_json(path: str) -> JsonObject:
    try:
        with open(path, encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, json.JSONDecodeError) as error:
        raise NativeLinkConfigError(
            ["{}: cannot load strict JSON-compatible JSON5: {}".format(path, error)]
        ) from error
    if not isinstance(value, dict):
        raise NativeLinkConfigError(["{}: top level must be an object".format(path)])
    return value


def load_deployment(
    directory: str,
) -> Tuple[JsonObject, JsonObject, Dict[str, JsonObject], str]:
    workers = {
        arch: _load_json(os.path.join(directory, "worker-{}.json5".format(arch)))
        for arch in WORKERS
    }
    unit_path = os.path.join(directory, "nativelink.service")
    try:
        with open(unit_path, encoding="utf-8") as stream:
            unit = stream.read()
    except OSError as error:
        raise NativeLinkConfigError(
            ["{}: cannot load systemd unit: {}".format(unit_path, error)]
        ) from error
    return (
        _load_json(os.path.join(directory, "deployment.json")),
        _load_json(os.path.join(directory, "control.json5")),
        workers,
        unit,
    )


def _named(entries: Sequence[JsonObject]) -> Dict[str, JsonObject]:
    return {entry["name"]: entry for entry in entries}


def _bounded(filesystem: JsonObject) -> bool:
    eviction = filesystem.get("eviction_policy", {})
    value = eviction.get("max_bytes") if isinstance(eviction, dict) else None
    return (isinstance(value, int) and not isinstance(value, bool) and value > 0) or (
        isinstance(value, str) and ENV_BOUND.fullmatch(value) is not None
    )


def _address(value: Any) -> Tuple[Optional[str], Optional[int]]:
    if not isinstance(value, str):
        return None, None
    match = ENV_ADDRESS.fullmatch(value)
    if match is not None:
        return match.group("default"), int(match.group("port"))
    try:
        host, port = value.rsplit(":", 1)
        return host.strip("[]"), int(port)
    except (ValueError, AttributeError):
        return None, None


def _private_worker_address(value: Any) -> bool:
    host, port = _address(value)
    if port != 50061 or not host or host in ("0.0.0.0", "::", "*"):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host == "localhost"
    return address.is_private or address.is_loopback or address.is_link_local


def _unit_values(text: str) -> Dict[str, List[str]]:
    values: Dict[str, List[str]] = {}
    in_service = False
    for raw in text.splitlines():
        line = raw.strip()
        if line == "[Service]":
            in_service = True
            continue
        if line.startswith("["):
            in_service = False
        if not in_service or not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values.setdefault(key, []).append(value)
    return values


def validate_documents(
    metadata: JsonObject,
    control: JsonObject,
    workers: Dict[str, JsonObject],
    unit: str,
) -> None:
    errors: List[str] = []

    def check(condition: bool, message: str) -> None:
        if not condition:
            errors.append(message)

    try:
        image = metadata["image"]
        reference = image.get("reference")
        check(
            image.get("repository") == IMAGE_REPOSITORY,
            "deployment image repository is wrong",
        )
        check(
            image.get("version") == IMAGE_VERSION,
            "deployment image version must be v1.6.6",
        )
        check(
            image.get("source_commit") == IMAGE_COMMIT,
            "deployment image commit must match v1.6.6",
        )
        check(
            image.get("digest") == IMAGE_DIGEST,
            "deployment image digest must match v1.6.6",
        )
        check(
            isinstance(reference, str) and "@sha256:" in reference,
            "deployment image reference must be digest-pinned, not tagged",
        )
        if isinstance(reference, str) and "@sha256:" in reference:
            check(
                reference == IMAGE_REFERENCE,
                "deployment image reference does not match its digest",
            )
        check(
            metadata.get("instance_name") == INSTANCE,
            "deployment instance_name must be 'main'",
        )

        stores = _named(control["stores"])
        cas = stores["CAS_MAIN_STORE"]["compression"]["backend"]["filesystem"]
        ac = stores["AC_MAIN_STORE"]["filesystem"]
        check(_bounded(cas), "control CAS max_bytes must be bounded")
        check(_bounded(ac), "control AC max_bytes must be bounded")

        properties = _named(control["schedulers"])["MAIN_SCHEDULER"]["simple"][
            "supported_platform_properties"
        ]
        for key in ("platform.OSFamily", "platform.arch"):
            check(
                properties.get(key) == "exact",
                "control scheduler property {!r} must be exact".format(key),
            )

        public = []
        private = []
        for server in control["servers"]:
            services = server["services"]
            if PUBLIC_SERVICES.intersection(services):
                public.append(server)
            if "worker_api" in services:
                private.append(server)
                check(
                    not PUBLIC_SERVICES.intersection(services),
                    "worker_api must not share a listener with public REAPI services",
                )
        check(len(public) == 1, "control must define exactly one public REAPI listener")
        check(
            len(private) == 1,
            "control must define exactly one private worker_api listener",
        )

        if len(public) == 1:
            server = public[0]
            _, port = _address(server["listener"]["http"]["socket_address"])
            check(port == 50051, "public REAPI listener must use port 50051")
            services = server["services"]
            for name in ("cas", "ac", "bytestream", "execution", "capabilities"):
                entries = services.get(name)
                check(
                    isinstance(entries, list) and bool(entries),
                    "public service {} must use the array form".format(name),
                )
                if isinstance(entries, list):
                    for entry in entries:
                        check(
                            entry.get("instance_name") == INSTANCE,
                            "{}.instance_name must be 'main'".format(name),
                        )

        if len(private) == 1:
            server = private[0]
            address = server["listener"]["http"]["socket_address"]
            check(
                _private_worker_address(address),
                "worker_api listener must default to a private address",
            )
            check(
                set(server["services"]) <= {"worker_api", "health", "admin"},
                "worker_api listener contains a public service",
            )

        for arch, worker_name in WORKERS.items():
            worker = workers[arch]
            worker_stores = _named(worker["stores"])
            for name in ("REMOTE_CAS", "REMOTE_AC"):
                check(
                    worker_stores[name]["grpc"].get("instance_name") == INSTANCE,
                    "worker-{}.{}.grpc.instance_name must be 'main'".format(arch, name),
                )
            fast_slow = worker_stores["WORKER_CAS"]["fast_slow"]
            check(
                _bounded(fast_slow["fast"]["filesystem"]),
                "worker-{} local CAS max_bytes must be bounded".format(arch),
            )
            local_workers = worker["workers"]
            check(
                len(local_workers) == 1,
                "worker-{} must define exactly one worker".format(arch),
            )
            local = local_workers[0]["local"]
            check(
                local.get("name") == worker_name,
                "worker-{} has the wrong identity".format(arch),
            )
            check(
                local.get("max_inflight_tasks") == 1,
                "worker-{} max_inflight_tasks must initially be 1".format(arch),
            )
            properties = local["platform_properties"]
            check(
                properties.get("platform.OSFamily", {}).get("values") == ["linux"],
                "worker-{} platform.OSFamily must be exactly ['linux']".format(arch),
            )
            check(
                properties.get("platform.arch", {}).get("values") == [arch],
                "worker-{} platform.arch must be exactly [{!r}]".format(arch, arch),
            )
            for key in ("use_namespaces", "use_mount_namespace"):
                check(
                    local.get(key) is False,
                    "worker-{}.{} must be false".format(arch, key),
                )
            check(
                worker.get("servers") == [],
                "worker-{} must not expose listeners".format(arch),
            )

        service = _unit_values(unit)
        for key, value in {
            "Type": "exec",
            "User": "nativelink",
            "Group": "nativelink",
            "ExecStart": "/usr/bin/nativelink ${NATIVELINK_CONFIG}",
            "Restart": "on-failure",
            "LimitNOFILE": "524288",
            "LimitCORE": "0",
            "TasksMax": "65536",
            "ProtectSystem": "strict",
            "ProtectKernelTunables": "yes",
            "ProtectKernelModules": "yes",
            "ProtectControlGroups": "yes",
            "RestrictRealtime": "yes",
            "RestrictAddressFamilies": "AF_UNIX AF_INET AF_INET6 AF_NETLINK",
        }.items():
            check(
                service.get(key) == [value],
                "nativelink.service {} must be {!r}".format(key, value),
            )
        check(
            "NATIVELINK_CONFIG=/etc/nativelink/control.json5"
            in service.get("Environment", []),
            "nativelink.service must select the control config by default",
        )
        for key in (
            "NoNewPrivileges",
            "PrivateUsers",
            "RestrictNamespaces",
            "RestrictSUIDSGID",
            "CapabilityBoundingSet",
        ):
            check(
                key not in service,
                "nativelink.service must not set {} because actions create "
                "user namespaces with ID-map helpers".format(key),
            )
    except (KeyError, IndexError, TypeError, AttributeError) as error:
        errors.append("malformed NativeLink deployment: {}".format(error))

    if errors:
        raise NativeLinkConfigError(errors)


def validate_deployment(directory: str) -> None:
    validate_documents(*load_deployment(directory))


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="validate the NativeLink deployment configuration"
    )
    parser.add_argument(
        "directory",
        nargs="?",
        help="deployment directory (default: checked-in NativeLink directory)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="report validated files",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="nativelink-config: %(message)s",
    )
    directory = os.path.abspath(args.directory or deployment_directory())
    logging.debug("validating %s", directory)
    try:
        validate_deployment(directory)
    except NativeLinkConfigError as error:
        for message in error.errors:
            logging.error("%s", message)
        return 1
    logging.debug("validated NativeLink deployment")
    return 0


if __name__ == "__main__":
    sys.exit(main())
