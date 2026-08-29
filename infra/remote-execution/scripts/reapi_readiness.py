#!/usr/bin/env python3
"""Validate REAPI capabilities and perform a bounded CAS round trip.

The protocol field numbers used here come from the canonical
``build/bazel/remote/execution/v2/remote_execution.proto`` and
``google/bytestream/bytestream.proto`` definitions. The helper deliberately
uses grpcio's generic byte transport instead of vendoring generated bindings.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
import contextlib
from dataclasses import dataclass
import hashlib
import importlib
import json
import logging
import re
import secrets
import sys
from types import ModuleType
from typing import Any, Protocol
import urllib.parse
import uuid


LOG = logging.getLogger("reapi-readiness")
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_BLOB_BYTES = 64 * 1024
MAX_BLOB_BYTES = 1024 * 1024
SHA256 = 1

CAPABILITIES_METHOD = (
    "/build.bazel.remote.execution.v2.Capabilities/GetCapabilities"
)
FIND_MISSING_METHOD = (
    "/build.bazel.remote.execution.v2.ContentAddressableStorage/FindMissingBlobs"
)
WRITE_METHOD = "/google.bytestream.ByteStream/Write"
READ_METHOD = "/google.bytestream.ByteStream/Read"


class CheckFailure(RuntimeError):
    """A readiness check failed."""


@dataclass(frozen=True)
class ProtoField:
    number: int
    wire_type: int
    value: int | bytes


class Transport(Protocol):
    def unary_unary(self, method: str, request: bytes) -> bytes: ...

    def stream_unary(self, method: str, requests: Iterable[bytes]) -> bytes: ...

    def unary_stream(self, method: str, request: bytes) -> Iterable[bytes]: ...


TransportFactory = Callable[[str, bool, float], contextlib.AbstractContextManager[Transport]]


def _encode_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("protobuf varints must not be negative")
    encoded = bytearray()
    while value >= 0x80:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _encode_key(number: int, wire_type: int) -> bytes:
    if number <= 0:
        raise ValueError("protobuf field numbers must be positive")
    return _encode_varint((number << 3) | wire_type)


def _encode_int(number: int, value: int) -> bytes:
    return _encode_key(number, 0) + _encode_varint(value)


def _encode_bytes(number: int, value: bytes) -> bytes:
    return _encode_key(number, 2) + _encode_varint(len(value)) + value


def _encode_string(number: int, value: str) -> bytes:
    return _encode_bytes(number, value.encode("utf-8"))


def _decode_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while shift < 70:
        if offset >= len(data):
            raise CheckFailure("truncated protobuf varint")
        byte = data[offset]
        offset += 1
        if shift == 63 and byte > 1:
            raise CheckFailure("protobuf varint exceeds 64 bits")
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    raise CheckFailure("protobuf varint exceeds 64 bits")


def parse_fields(data: bytes) -> list[ProtoField]:
    fields = []
    offset = 0
    while offset < len(data):
        key, offset = _decode_varint(data, offset)
        number = key >> 3
        wire_type = key & 0x07
        if number == 0:
            raise CheckFailure("protobuf field number zero is invalid")
        if number >= 1 << 29:
            raise CheckFailure("protobuf field number exceeds the protocol limit")
        if wire_type == 0:
            value, offset = _decode_varint(data, offset)
        elif wire_type == 1:
            end = offset + 8
            if end > len(data):
                raise CheckFailure("truncated protobuf fixed64 field")
            value = data[offset:end]
            offset = end
        elif wire_type == 2:
            length, offset = _decode_varint(data, offset)
            end = offset + length
            if end > len(data):
                raise CheckFailure("truncated protobuf length-delimited field")
            value = data[offset:end]
            offset = end
        elif wire_type == 5:
            end = offset + 4
            if end > len(data):
                raise CheckFailure("truncated protobuf fixed32 field")
            value = data[offset:end]
            offset = end
        else:
            raise CheckFailure(
                "unsupported protobuf wire type {}".format(wire_type)
            )
        fields.append(ProtoField(number, wire_type, value))
    return fields


def _bytes_fields(fields: Iterable[ProtoField], number: int) -> list[bytes]:
    values = []
    for field in fields:
        if field.number != number:
            continue
        if field.wire_type != 2 or not isinstance(field.value, bytes):
            raise CheckFailure(
                "protobuf field {} has the wrong wire type".format(number)
            )
        values.append(field.value)
    return values


def _int_fields(fields: Iterable[ProtoField], number: int) -> list[int]:
    values = []
    for field in fields:
        if field.number != number:
            continue
        if field.wire_type != 0 or not isinstance(field.value, int):
            raise CheckFailure(
                "protobuf field {} has the wrong wire type".format(number)
            )
        values.append(field.value)
    return values


def _one_bytes(fields: Iterable[ProtoField], number: int, name: str) -> bytes:
    values = _bytes_fields(fields, number)
    if len(values) != 1:
        raise CheckFailure("{} must occur exactly once".format(name))
    return values[0]


def _one_int(fields: Iterable[ProtoField], number: int, name: str) -> int:
    values = _int_fields(fields, number)
    if len(values) != 1:
        raise CheckFailure("{} must occur exactly once".format(name))
    return values[0]


def _optional_int(
    fields: Iterable[ProtoField],
    number: int,
    name: str,
    default: int = 0,
) -> int:
    values = _int_fields(fields, number)
    if len(values) > 1:
        raise CheckFailure("{} must not occur more than once".format(name))
    return values[0] if values else default


def _enum_values(fields: Iterable[ProtoField], number: int) -> set[int]:
    values = set()
    for field in fields:
        if field.number != number:
            continue
        if field.wire_type == 0 and isinstance(field.value, int):
            values.add(field.value)
        elif field.wire_type == 2 and isinstance(field.value, bytes):
            offset = 0
            while offset < len(field.value):
                value, offset = _decode_varint(field.value, offset)
                values.add(value)
        else:
            raise CheckFailure(
                "protobuf enum field {} has the wrong wire type".format(number)
            )
    return values


def _decode_text(data: bytes, name: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CheckFailure("{} is not UTF-8".format(name)) from error


def _encode_digest(digest: str, size: int) -> bytes:
    return _encode_string(1, digest) + _encode_int(2, size)


def _decode_digest(data: bytes) -> tuple[str, int]:
    fields = parse_fields(data)
    digest = _decode_text(_one_bytes(fields, 1, "digest hash"), "digest hash")
    size = _one_int(fields, 2, "digest size")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise CheckFailure("server returned a malformed SHA-256 digest")
    return digest, size


def encode_get_capabilities_request(instance_name: str) -> bytes:
    return _encode_string(1, instance_name)


def encode_find_missing_request(
    instance_name: str,
    digest: str,
    size: int,
) -> bytes:
    return _encode_string(1, instance_name) + _encode_bytes(
        2, _encode_digest(digest, size)
    )


def decode_find_missing_response(data: bytes) -> list[tuple[str, int]]:
    return [
        _decode_digest(value)
        for value in _bytes_fields(parse_fields(data), 2)
    ]


def encode_write_request(resource_name: str, data: bytes) -> bytes:
    return b"".join((
        _encode_string(1, resource_name),
        _encode_int(2, 0),
        _encode_int(3, 1),
        _encode_bytes(10, data),
    ))


def decode_write_response(data: bytes) -> int:
    return _one_int(parse_fields(data), 1, "committed size")


def encode_read_request(resource_name: str, size: int) -> bytes:
    return b"".join((
        _encode_string(1, resource_name),
        _encode_int(2, 0),
        _encode_int(3, size),
    ))


def decode_read_response(data: bytes) -> bytes:
    chunks = _bytes_fields(parse_fields(data), 10)
    if len(chunks) != 1:
        raise CheckFailure("ByteStream ReadResponse must contain one data field")
    return chunks[0]


def _decode_semver(data: bytes, name: str) -> tuple[int, int, int]:
    fields = parse_fields(data)
    major = _optional_int(fields, 1, "{} major".format(name))
    minor = _optional_int(fields, 2, "{} minor".format(name))
    patch = _optional_int(fields, 3, "{} patch".format(name))
    return major, minor, patch


def _format_semver(version: tuple[int, int, int]) -> str:
    return "{}.{}.{}".format(*version)


def decode_capabilities(
    data: bytes,
    instance_name: str,
) -> Mapping[str, Any]:
    fields = parse_fields(data)
    cache_data = _one_bytes(fields, 1, "cache capabilities")
    execution_data = _one_bytes(fields, 2, "execution capabilities")
    low_data = _one_bytes(fields, 4, "low API version")
    high_data = _one_bytes(fields, 5, "high API version")

    cache_fields = parse_fields(cache_data)
    if SHA256 not in _enum_values(cache_fields, 1):
        raise CheckFailure(
            "GetCapabilities does not advertise SHA-256 cache digests"
        )
    update_data = _one_bytes(
        cache_fields, 2, "action-cache update capabilities"
    )
    update_enabled = _optional_int(
        parse_fields(update_data), 1, "action-cache update flag"
    )
    if update_enabled != 1:
        raise CheckFailure("GetCapabilities disables action-cache updates")

    execution_fields = parse_fields(execution_data)
    exec_enabled = _optional_int(
        execution_fields, 2, "remote execution enabled flag"
    )
    if exec_enabled != 1:
        raise CheckFailure("GetCapabilities disables remote execution")
    execution_digests = _enum_values(execution_fields, 1)
    execution_digests.update(_enum_values(execution_fields, 4))
    if SHA256 not in execution_digests:
        raise CheckFailure(
            "GetCapabilities does not advertise SHA-256 execution digests"
        )

    low = _decode_semver(low_data, "low API version")
    high = _decode_semver(high_data, "high API version")
    if low > high:
        raise CheckFailure("GetCapabilities returned an inverted API range")
    if not low[0] <= 2 <= high[0]:
        raise CheckFailure(
            "GetCapabilities API range {}..{} excludes REAPI v2".format(
                _format_semver(low), _format_semver(high)
            )
        )

    return {
        "api_high": _format_semver(high),
        "api_low": _format_semver(low),
        "cache_update_enabled": True,
        "digest_function": "SHA256",
        "execution_enabled": True,
        "instance_name": instance_name,
    }


def check_capabilities(
    transport: Transport,
    instance_name: str,
) -> Mapping[str, Any]:
    response = transport.unary_unary(
        CAPABILITIES_METHOD,
        encode_get_capabilities_request(instance_name),
    )
    return decode_capabilities(response, instance_name)


def _resource_prefix(instance_name: str) -> str:
    return instance_name + "/" if instance_name else ""


def _find_missing(
    transport: Transport,
    instance_name: str,
    digest: str,
    size: int,
) -> list[tuple[str, int]]:
    response = transport.unary_unary(
        FIND_MISSING_METHOD,
        encode_find_missing_request(instance_name, digest, size),
    )
    return decode_find_missing_response(response)


def check_cas_round_trip(
    transport: Transport,
    instance_name: str,
    blob_bytes: int = DEFAULT_BLOB_BYTES,
) -> Mapping[str, Any]:
    if blob_bytes <= 0 or blob_bytes > MAX_BLOB_BYTES:
        raise CheckFailure(
            "blob size must be between 1 and {} bytes".format(MAX_BLOB_BYTES)
        )

    blob = secrets.token_bytes(blob_bytes)
    digest = hashlib.sha256(blob).hexdigest()
    identity = (digest, len(blob))
    missing_before = _find_missing(
        transport, instance_name, digest, len(blob)
    )
    if missing_before != [identity]:
        raise CheckFailure(
            "FindMissingBlobs did not report the fresh random blob as missing"
        )

    upload_id = uuid.uuid4().hex
    prefix = _resource_prefix(instance_name)
    write_resource = "{}uploads/{}/blobs/{}/{}".format(
        prefix, upload_id, digest, len(blob)
    )
    write_response = transport.stream_unary(
        WRITE_METHOD,
        [encode_write_request(write_resource, blob)],
    )
    committed_size = decode_write_response(write_response)
    if committed_size != len(blob):
        raise CheckFailure(
            "ByteStream committed {} bytes, expected {}".format(
                committed_size, len(blob)
            )
        )

    missing_after = _find_missing(
        transport, instance_name, digest, len(blob)
    )
    if missing_after:
        raise CheckFailure("FindMissingBlobs still reports the uploaded blob missing")

    read_resource = "{}blobs/{}/{}".format(prefix, digest, len(blob))
    downloaded = bytearray()
    for response in transport.unary_stream(
        READ_METHOD,
        encode_read_request(read_resource, len(blob)),
    ):
        downloaded.extend(decode_read_response(response))
        if len(downloaded) > len(blob):
            raise CheckFailure("ByteStream returned more data than requested")
    downloaded_bytes = bytes(downloaded)
    downloaded_digest = hashlib.sha256(downloaded_bytes).hexdigest()
    if downloaded_digest != digest or downloaded_bytes != blob:
        raise CheckFailure("ByteStream read-back content or digest differs")

    return {
        "bytes": len(blob),
        "digest": digest,
        "find_missing_after": 0,
        "find_missing_before": 1,
        "instance_name": instance_name,
    }


def parse_bool(value: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise argparse.ArgumentTypeError("expected true or false")


def validate_endpoint(endpoint: str) -> str:
    if "://" in endpoint or any(character.isspace() for character in endpoint):
        raise CheckFailure(
            "endpoint must be a HOST:PORT authority without a URI scheme"
        )
    parsed = urllib.parse.urlsplit("//" + endpoint)
    if not parsed.hostname or parsed.username or parsed.password:
        raise CheckFailure("endpoint must contain a hostname without credentials")
    if parsed.path or parsed.query or parsed.fragment:
        raise CheckFailure("endpoint must not contain a path, query, or fragment")
    try:
        port = parsed.port
    except ValueError as error:
        raise CheckFailure("endpoint contains an invalid port") from error
    if port is None:
        raise CheckFailure("endpoint must contain an explicit port")
    if not 1 <= port <= 65535:
        raise CheckFailure("endpoint port must be between 1 and 65535")
    return endpoint


def validate_instance_name(instance_name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", instance_name):
        raise CheckFailure("instance name contains unsupported characters")
    if instance_name.endswith("/") or "//" in instance_name:
        raise CheckFailure("instance name must not contain empty path components")
    return instance_name


def _load_grpc() -> ModuleType:
    try:
        return importlib.import_module("grpc")
    except ImportError as error:
        raise CheckFailure(
            "Python grpcio is unavailable; install the python3-grpcio package"
        ) from error


class GrpcTransport:
    def __init__(
        self,
        grpc_module: ModuleType,
        endpoint: str,
        tls: bool,
        timeout: float,
    ) -> None:
        self.grpc = grpc_module
        self.timeout = timeout
        if tls:
            credentials = grpc_module.ssl_channel_credentials()
            self.channel = grpc_module.secure_channel(endpoint, credentials)
        else:
            self.channel = grpc_module.insecure_channel(endpoint)

    def __enter__(self) -> GrpcTransport:
        try:
            self.grpc.channel_ready_future(self.channel).result(
                timeout=self.timeout
            )
        except self.grpc.FutureTimeoutError as error:
            self.channel.close()
            raise CheckFailure("gRPC endpoint readiness timed out") from error
        except self.grpc.RpcError as error:
            self.channel.close()
            raise self._rpc_error("gRPC endpoint readiness", error) from error
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any,
    ) -> None:
        self.channel.close()

    def _rpc_error(self, operation: str, error: BaseException) -> CheckFailure:
        code = "UNKNOWN"
        details = ""
        if isinstance(error, self.grpc.RpcError):
            try:
                code = error.code().name
            except (AttributeError, TypeError):
                pass
            try:
                details = error.details() or ""
            except (AttributeError, TypeError):
                pass
        LOG.debug("%s failed: %s", operation, details or repr(error))
        return CheckFailure(
            "{} failed with gRPC {}".format(operation, code)
        )

    def unary_unary(self, method: str, request: bytes) -> bytes:
        call = self.channel.unary_unary(
            method,
            request_serializer=lambda value: value,
            response_deserializer=lambda value: value,
        )
        try:
            return call(request, timeout=self.timeout)
        except self.grpc.RpcError as error:
            raise self._rpc_error(method, error) from error

    def stream_unary(self, method: str, requests: Iterable[bytes]) -> bytes:
        call = self.channel.stream_unary(
            method,
            request_serializer=lambda value: value,
            response_deserializer=lambda value: value,
        )
        try:
            return call(iter(requests), timeout=self.timeout)
        except self.grpc.RpcError as error:
            raise self._rpc_error(method, error) from error

    def unary_stream(self, method: str, request: bytes) -> Iterator[bytes]:
        call = self.channel.unary_stream(
            method,
            request_serializer=lambda value: value,
            response_deserializer=lambda value: value,
        )
        try:
            yield from call(request, timeout=self.timeout)
        except self.grpc.RpcError as error:
            raise self._rpc_error(method, error) from error


def grpc_transport_factory(
    endpoint: str,
    tls: bool,
    timeout: float,
) -> GrpcTransport:
    grpc_module = _load_grpc()
    try:
        return GrpcTransport(grpc_module, endpoint, tls, timeout)
    except (AttributeError, RuntimeError, TypeError) as error:
        LOG.debug("could not initialize grpcio transport: %r", error)
        raise CheckFailure(
            "could not initialize the grpcio transport"
        ) from error


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _blob_size(value: str) -> int:
    parsed = int(value)
    if parsed <= 0 or parsed > MAX_BLOB_BYTES:
        raise argparse.ArgumentTypeError(
            "must be between 1 and {}".format(MAX_BLOB_BYTES)
        )
    return parsed


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="write gRPC diagnostics to stderr",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--endpoint", required=True, help="REAPI HOST:PORT")
    common.add_argument(
        "--instance-name", required=True, help="REAPI instance name"
    )
    common.add_argument("--tls", required=True, type=parse_bool)
    common.add_argument(
        "--timeout-seconds",
        type=_positive_float,
        default=DEFAULT_TIMEOUT_SECONDS,
    )
    subparsers = parser.add_subparsers(dest="operation", required=True)
    subparsers.add_parser("capabilities", parents=[common])
    cas_parser = subparsers.add_parser("cas-round-trip", parents=[common])
    cas_parser.add_argument(
        "--blob-bytes", type=_blob_size, default=DEFAULT_BLOB_BYTES
    )
    return parser


def _emit(status: str, check: str, details: Mapping[str, Any]) -> None:
    print(
        "{}\t{}\t{}".format(
            status,
            check,
            json.dumps(details, sort_keys=True, separators=(",", ":")),
        )
    )


def main(
    argv: Sequence[str] | None = None,
    transport_factory: TransportFactory = grpc_transport_factory,
) -> int:
    parser = _argument_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="reapi-readiness: %(message)s",
    )
    check = "reapi.{}".format(args.operation)
    try:
        endpoint = validate_endpoint(args.endpoint)
        instance_name = validate_instance_name(args.instance_name)
        with transport_factory(endpoint, args.tls, args.timeout_seconds) as transport:
            if args.operation == "capabilities":
                details = check_capabilities(transport, instance_name)
            elif args.operation == "cas-round-trip":
                details = check_cas_round_trip(
                    transport, instance_name, args.blob_bytes
                )
            else:
                raise CheckFailure(
                    "unsupported operation: {}".format(args.operation)
                )
    except (CheckFailure, OSError, ValueError) as error:
        _emit("FAIL", check, {"message": str(error)})
        return 1
    _emit("PASS", check, {"message": "ok", **details})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
