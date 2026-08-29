#!/usr/bin/env python3
"""Validate the checked-in NativeLink deployment contract."""

import argparse
import ipaddress
import json
import logging
import os
import re
import sys
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple


IMAGE_REPOSITORY = "ghcr.io/tracemachina/nativelink"
IMAGE_VERSION = "v1.6.6"
IMAGE_COMMIT = "a21edb0fc56879124e308bb9a67be679f8eaf885"
IMAGE_DIGEST = (
    "sha256:5c2e6eca51c6d3ac40b94f703e08a243" "fd036cc136cc858a99040ca90fa57d61"
)
IMAGE_REFERENCE = IMAGE_REPOSITORY + "@" + IMAGE_DIGEST
INSTANCE = "main"
WORKERS = {"x86_64": "worker-x86_64", "aarch64": "worker-aarch64"}
PROFILES = ("plaintext", "mtls")
CONTROL_CONFIGS = {
    "plaintext": "control.json5",
    "mtls": "control-mtls.json5",
}
WORKER_CONFIGS = {
    "plaintext": {
        arch: "worker-{}.json5".format(arch) for arch in WORKERS
    },
    "mtls": {
        arch: "worker-{}-mtls.json5".format(arch) for arch in WORKERS
    },
}
MTLS_SERVER_CERT = "/etc/nativelink/tls/control-chain.pem"
MTLS_SERVER_KEY = "/etc/nativelink/tls/control-key.pem"
MTLS_REAPI_CLIENT_CA = "/etc/nativelink/tls/reapi-client-ca.pem"
MTLS_WORKER_CLIENT_CA = "/etc/nativelink/tls/worker-client-ca.pem"
MTLS_CONTROL_CA = "/etc/nativelink/tls/control-ca.pem"
MTLS_WORKER_CERT = "/etc/nativelink/tls/worker-chain.pem"
MTLS_WORKER_KEY = "/etc/nativelink/tls/worker-key.pem"
MTLS_REAPI_ADDRESS = "https://${NATIVELINK_CONTROL_DNS}:50051"
MTLS_WORKER_API_ADDRESS = "https://${NATIVELINK_CONTROL_DNS}:50061"
PLAINTEXT_REAPI_ADDRESS = "grpc://${NATIVELINK_REAPI_ADDRESS:-127.0.0.1}:50051"
PLAINTEXT_WORKER_API_ADDRESS = (
    "grpc://${NATIVELINK_WORKER_API_ADDRESS:-127.0.0.1}:50061"
)
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
) -> Tuple[
    JsonObject,
    Dict[str, JsonObject],
    Dict[str, Dict[str, JsonObject]],
    str,
]:
    controls = {
        profile: _load_json(os.path.join(directory, filename))
        for profile, filename in CONTROL_CONFIGS.items()
    }
    workers = {
        profile: {
            arch: _load_json(os.path.join(directory, filename))
            for arch, filename in filenames.items()
        }
        for profile, filenames in WORKER_CONFIGS.items()
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
        controls,
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


def _validate_control_profile(
    profile: str,
    control: JsonObject,
    check: Callable[[bool, str], None],
) -> None:
    stores = _named(control["stores"])
    cas = stores["CAS_MAIN_STORE"]["compression"]["backend"]["filesystem"]
    ac = stores["AC_MAIN_STORE"]["filesystem"]
    check(_bounded(cas), "{} control CAS max_bytes must be bounded".format(profile))
    check(_bounded(ac), "{} control AC max_bytes must be bounded".format(profile))

    properties = _named(control["schedulers"])["MAIN_SCHEDULER"]["simple"][
        "supported_platform_properties"
    ]
    for key in ("platform.OSFamily", "platform.arch"):
        check(
            properties.get(key) == "exact",
            "{} control scheduler property {!r} must be exact".format(profile, key),
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
                "{} worker_api must not share a listener with public REAPI services".format(
                    profile
                ),
            )
    check(
        len(public) == 1,
        "{} control must define exactly one public REAPI listener".format(profile),
    )
    check(
        len(private) == 1,
        "{} control must define exactly one private worker_api listener".format(
            profile
        ),
    )

    if len(public) == 1:
        server = public[0]
        http = server["listener"]["http"]
        _, port = _address(http["socket_address"])
        check(port == 50051, "{} public REAPI listener must use port 50051".format(profile))
        services = server["services"]
        for name in ("cas", "ac", "bytestream", "execution", "capabilities"):
            entries = services.get(name)
            check(
                isinstance(entries, list) and bool(entries),
                "{} public service {} must use the array form".format(profile, name),
            )
            if isinstance(entries, list):
                for entry in entries:
                    check(
                        entry.get("instance_name") == INSTANCE,
                        "{} {}.instance_name must be 'main'".format(profile, name),
                    )
        if profile == "plaintext":
            check(
                "tls" not in http,
                "plaintext public REAPI listener must not configure TLS",
            )
        else:
            check(
                http.get("tls")
                == {
                    "cert_file": MTLS_SERVER_CERT,
                    "key_file": MTLS_SERVER_KEY,
                    "client_ca_file": MTLS_REAPI_CLIENT_CA,
                },
                "mtls public REAPI listener must use the combined client trust bundle",
            )

    if len(private) == 1:
        server = private[0]
        http = server["listener"]["http"]
        check(
            _private_worker_address(http["socket_address"]),
            "{} worker_api listener must default to a private address".format(profile),
        )
        check(
            set(server["services"]) <= {"worker_api", "health", "admin"},
            "{} worker_api listener contains a public service".format(profile),
        )
        if profile == "plaintext":
            check(
                "tls" not in http,
                "plaintext worker_api listener must not configure TLS",
            )
        else:
            check(
                http.get("tls")
                == {
                    "cert_file": MTLS_SERVER_CERT,
                    "key_file": MTLS_SERVER_KEY,
                    "client_ca_file": MTLS_WORKER_CLIENT_CA,
                },
                "mtls worker_api listener must trust only the worker client CA",
            )


def _validate_worker_profile(
    profile: str,
    arch: str,
    worker_name: str,
    worker: JsonObject,
    check: Callable[[bool, str], None],
) -> None:
    worker_stores = _named(worker["stores"])
    expected_reapi_address = (
        PLAINTEXT_REAPI_ADDRESS if profile == "plaintext" else MTLS_REAPI_ADDRESS
    )
    expected_tls = {
        "ca_file": MTLS_CONTROL_CA,
        "cert_file": MTLS_WORKER_CERT,
        "key_file": MTLS_WORKER_KEY,
    }
    for name in ("REMOTE_CAS", "REMOTE_AC"):
        grpc = worker_stores[name]["grpc"]
        check(
            grpc.get("instance_name") == INSTANCE,
            "{} worker-{}.{}.grpc.instance_name must be 'main'".format(
                profile, arch, name
            ),
        )
        endpoints = grpc.get("endpoints")
        check(
            isinstance(endpoints, list) and len(endpoints) == 1,
            "{} worker-{}.{} must define exactly one endpoint".format(
                profile, arch, name
            ),
        )
        if isinstance(endpoints, list) and len(endpoints) == 1:
            endpoint = endpoints[0]
            check(
                endpoint.get("address") == expected_reapi_address,
                "{} worker-{}.{} endpoint has the wrong scheme or address".format(
                    profile, arch, name
                ),
            )
            if profile == "plaintext":
                check(
                    "tls_config" not in endpoint,
                    "plaintext worker-{}.{} must not configure TLS".format(
                        arch, name
                    ),
                )
            else:
                check(
                    endpoint.get("tls_config") == expected_tls,
                    "mtls worker-{}.{} must use the complete worker TLS identity".format(
                        arch, name
                    ),
                )

    fast_slow = worker_stores["WORKER_CAS"]["fast_slow"]
    check(
        _bounded(fast_slow["fast"]["filesystem"]),
        "{} worker-{} local CAS max_bytes must be bounded".format(profile, arch),
    )
    local_workers = worker["workers"]
    check(
        len(local_workers) == 1,
        "{} worker-{} must define exactly one worker".format(profile, arch),
    )
    local = local_workers[0]["local"]
    check(
        local.get("name") == worker_name,
        "{} worker-{} has the wrong identity".format(profile, arch),
    )
    check(
        local.get("max_inflight_tasks") == 1,
        "{} worker-{} max_inflight_tasks must initially be 1".format(
            profile, arch
        ),
    )
    properties = local["platform_properties"]
    check(
        properties.get("platform.OSFamily", {}).get("values") == ["linux"],
        "{} worker-{} platform.OSFamily must be exactly ['linux']".format(
            profile, arch
        ),
    )
    check(
        properties.get("platform.arch", {}).get("values") == [arch],
        "{} worker-{} platform.arch must be exactly [{!r}]".format(
            profile, arch, arch
        ),
    )
    worker_api = local["worker_api_endpoint"]
    expected_worker_api = (
        PLAINTEXT_WORKER_API_ADDRESS
        if profile == "plaintext"
        else MTLS_WORKER_API_ADDRESS
    )
    check(
        worker_api.get("uri") == expected_worker_api,
        "{} worker-{} worker_api endpoint has the wrong scheme or address".format(
            profile, arch
        ),
    )
    if profile == "plaintext":
        check(
            "tls_config" not in worker_api,
            "plaintext worker-{} worker_api must not configure TLS".format(arch),
        )
    else:
        check(
            worker_api.get("tls_config") == expected_tls,
            "mtls worker-{} worker_api must use the complete worker TLS identity".format(
                arch
            ),
        )
    for key in ("use_namespaces", "use_mount_namespace"):
        check(
            local.get(key) is False,
            "{} worker-{}.{} must be false".format(profile, arch, key),
        )
    check(
        worker.get("servers") == [],
        "{} worker-{} must not expose listeners".format(profile, arch),
    )


def validate_documents(
    metadata: JsonObject,
    controls: Dict[str, JsonObject],
    workers: Dict[str, Dict[str, JsonObject]],
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
        configs = metadata["configs"]
        check(
            configs.get("control") == CONTROL_CONFIGS["plaintext"],
            "deployment plaintext control config is wrong",
        )
        check(
            configs.get("workers") == WORKER_CONFIGS["plaintext"],
            "deployment plaintext worker configs are wrong",
        )
        mtls_configs = configs.get("mtls", {})
        check(
            mtls_configs.get("control") == CONTROL_CONFIGS["mtls"],
            "deployment mTLS control config is wrong",
        )
        check(
            mtls_configs.get("workers") == WORKER_CONFIGS["mtls"],
            "deployment mTLS worker configs are wrong",
        )
        check(
            configs.get("systemd_unit") == "nativelink.service",
            "deployment systemd unit config is wrong",
        )

        check(set(controls) == set(PROFILES), "control profiles are incomplete")
        check(set(workers) == set(PROFILES), "worker profiles are incomplete")
        for profile in PROFILES:
            _validate_control_profile(profile, controls[profile], check)
            check(
                set(workers[profile]) == set(WORKERS),
                "{} worker architectures are incomplete".format(profile),
            )
            for arch, worker_name in WORKERS.items():
                _validate_worker_profile(
                    profile,
                    arch,
                    worker_name,
                    workers[profile][arch],
                    check,
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
            "nativelink.service must select the plaintext control config by default",
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
