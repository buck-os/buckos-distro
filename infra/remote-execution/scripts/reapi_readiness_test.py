#!/usr/bin/env python3
"""Focused offline tests for the REAPI readiness helper."""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import unittest
from unittest import mock


SCRIPT = Path(__file__).with_name("reapi_readiness.py")
SPEC = importlib.util.spec_from_file_location("reapi_readiness", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
reapi = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = reapi
SPEC.loader.exec_module(reapi)


def semver(major: int, minor: int = 0, patch: int = 0) -> bytes:
    fields = [reapi._encode_int(1, major)]
    if minor:
        fields.append(reapi._encode_int(2, minor))
    if patch:
        fields.append(reapi._encode_int(3, patch))
    return b"".join(fields)


def capabilities_response(
    *,
    cache_sha256: bool = True,
    cache_update: bool = True,
    execution_sha256: bool = True,
    execution_enabled: bool = True,
    low_major: int = 2,
    high_major: int = 2,
) -> bytes:
    cache_fields = []
    if cache_sha256:
        cache_fields.append(
            reapi._encode_bytes(1, reapi._encode_varint(reapi.SHA256))
        )
    cache_fields.append(
        reapi._encode_bytes(
            2,
            reapi._encode_int(1, int(cache_update)),
        )
    )
    execution_fields = [
        reapi._encode_int(2, int(execution_enabled)),
    ]
    if execution_sha256:
        execution_fields.extend((
            reapi._encode_int(1, reapi.SHA256),
            reapi._encode_bytes(4, reapi._encode_varint(reapi.SHA256)),
        ))
    return b"".join((
        reapi._encode_bytes(1, b"".join(cache_fields)),
        reapi._encode_bytes(2, b"".join(execution_fields)),
        reapi._encode_bytes(4, semver(low_major)),
        reapi._encode_bytes(5, semver(high_major, 3)),
    ))


class FakeReapiServer:
    def __init__(self, capabilities: bytes | None = None) -> None:
        self.capabilities = capabilities or capabilities_response()
        self.blobs: dict[tuple[str, int], bytes] = {}
        self.calls: list[str] = []
        self.corrupt_reads = False

    def __enter__(self) -> FakeReapiServer:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    @staticmethod
    def _resource_identity(resource_name: str) -> tuple[str, int]:
        parts = resource_name.split("/")
        try:
            blobs = parts.index("blobs")
            digest = parts[blobs + 1]
            size = int(parts[blobs + 2])
        except (ValueError, IndexError) as error:
            raise AssertionError("malformed resource name") from error
        return digest, size

    def unary_unary(self, method: str, request: bytes) -> bytes:
        self.calls.append(method)
        fields = reapi.parse_fields(request)
        if method == reapi.CAPABILITIES_METHOD:
            instance = reapi._decode_text(
                reapi._one_bytes(fields, 1, "instance"), "instance"
            )
            if instance != "main":
                raise AssertionError("wrong capabilities instance")
            return self.capabilities
        if method == reapi.FIND_MISSING_METHOD:
            instance = reapi._decode_text(
                reapi._one_bytes(fields, 1, "instance"), "instance"
            )
            if instance != "main":
                raise AssertionError("wrong CAS instance")
            digest = reapi._decode_digest(
                reapi._one_bytes(fields, 2, "blob digest")
            )
            if digest in self.blobs:
                return b""
            return reapi._encode_bytes(
                2, reapi._encode_digest(digest[0], digest[1])
            )
        raise AssertionError("unexpected unary method: {}".format(method))

    def stream_unary(self, method: str, requests: object) -> bytes:
        self.calls.append(method)
        if method != reapi.WRITE_METHOD:
            raise AssertionError("unexpected client-streaming method")
        messages = list(requests)
        if len(messages) != 1:
            raise AssertionError("expected one bounded write request")
        fields = reapi.parse_fields(messages[0])
        resource = reapi._decode_text(
            reapi._one_bytes(fields, 1, "resource name"), "resource name"
        )
        if reapi._one_int(fields, 2, "write offset") != 0:
            raise AssertionError("write offset is not zero")
        if reapi._one_int(fields, 3, "finish write") != 1:
            raise AssertionError("write was not finished")
        data = reapi._one_bytes(fields, 10, "write data")
        identity = self._resource_identity(resource)
        if identity[1] != len(data):
            raise AssertionError("resource size differs from data")
        self.blobs[identity] = data
        return reapi._encode_int(1, len(data))

    def unary_stream(self, method: str, request: bytes) -> object:
        self.calls.append(method)
        if method != reapi.READ_METHOD:
            raise AssertionError("unexpected server-streaming method")
        fields = reapi.parse_fields(request)
        resource = reapi._decode_text(
            reapi._one_bytes(fields, 1, "resource name"), "resource name"
        )
        identity = self._resource_identity(resource)
        data = self.blobs[identity]
        if self.corrupt_reads:
            data = bytes([data[0] ^ 0xFF]) + data[1:]
        return iter(
            reapi._encode_bytes(10, data[offset:offset + 17])
            for offset in range(0, len(data), 17)
        )


class ProtoTest(unittest.TestCase):
    def test_varint_and_fields_round_trip(self) -> None:
        message = b"".join((
            reapi._encode_int(1, 300),
            reapi._encode_string(2, "main"),
            reapi._encode_bytes(3, b"\x00\xff"),
        ))

        fields = reapi.parse_fields(message)

        self.assertEqual([300], reapi._int_fields(fields, 1))
        self.assertEqual([b"main"], reapi._bytes_fields(fields, 2))
        self.assertEqual([b"\x00\xff"], reapi._bytes_fields(fields, 3))

    def test_truncated_message_is_rejected(self) -> None:
        with self.assertRaisesRegex(reapi.CheckFailure, "truncated"):
            reapi.parse_fields(b"\x0a\x04abc")

    def test_oversized_varint_is_rejected(self) -> None:
        with self.assertRaisesRegex(reapi.CheckFailure, "exceeds 64 bits"):
            reapi.parse_fields(b"\x08" + b"\xff" * 9 + b"\x02")


class RuntimeContractTest(unittest.TestCase):
    def test_uses_the_distro_python_interpreter(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")

        self.assertEqual("#!/usr/bin/python3", source.splitlines()[0])
        ast.parse(source, filename=str(SCRIPT), feature_version=(3, 9))


class CapabilitiesTest(unittest.TestCase):
    def test_accepts_reapi_v2_execution_cache_and_sha256(self) -> None:
        result = reapi.decode_capabilities(
            capabilities_response(), "main"
        )

        self.assertEqual("2.0.0", result["api_low"])
        self.assertEqual("2.3.0", result["api_high"])
        self.assertTrue(result["cache_update_enabled"])
        self.assertTrue(result["execution_enabled"])
        self.assertEqual("SHA256", result["digest_function"])

    def test_rejects_missing_capabilities(self) -> None:
        cases = (
            (capabilities_response(cache_sha256=False), "cache digests"),
            (capabilities_response(cache_update=False), "action-cache updates"),
            (capabilities_response(execution_sha256=False), "execution digests"),
            (capabilities_response(execution_enabled=False), "remote execution"),
            (capabilities_response(low_major=3, high_major=3), "excludes REAPI v2"),
        )
        for response, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(reapi.CheckFailure, message):
                    reapi.decode_capabilities(response, "main")


class CasRoundTripTest(unittest.TestCase):
    def test_uploads_finds_and_reads_the_same_blob(self) -> None:
        server = FakeReapiServer()

        result = reapi.check_cas_round_trip(server, "main", blob_bytes=97)

        self.assertEqual(97, result["bytes"])
        self.assertEqual(1, result["find_missing_before"])
        self.assertEqual(0, result["find_missing_after"])
        self.assertEqual(
            [
                reapi.FIND_MISSING_METHOD,
                reapi.WRITE_METHOD,
                reapi.FIND_MISSING_METHOD,
                reapi.READ_METHOD,
            ],
            server.calls,
        )

    def test_rejects_corrupt_read_back(self) -> None:
        server = FakeReapiServer()
        server.corrupt_reads = True

        with self.assertRaisesRegex(reapi.CheckFailure, "content or digest"):
            reapi.check_cas_round_trip(server, "main", blob_bytes=31)

    def test_rejects_oversized_probe(self) -> None:
        with self.assertRaisesRegex(reapi.CheckFailure, "between 1"):
            reapi.check_cas_round_trip(
                FakeReapiServer(), "main", reapi.MAX_BLOB_BYTES + 1
            )


class CommandTest(unittest.TestCase):
    def run_main(
        self,
        operation: str,
        server: FakeReapiServer,
        *extra: str,
    ) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        factory_calls = []

        def factory(endpoint: str, tls: bool, timeout: float) -> FakeReapiServer:
            factory_calls.append((endpoint, tls, timeout))
            return server

        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = reapi.main(
                [
                    operation,
                    "--endpoint", "re.test.invalid:50051",
                    "--instance-name", "main",
                    "--tls", "false",
                    "--timeout-seconds", "2",
                    *extra,
                ],
                transport_factory=factory,
            )
        self.assertEqual(
            [("re.test.invalid:50051", False, 2.0)], factory_calls
        )
        return result, stdout.getvalue(), stderr.getvalue()

    def test_capabilities_command_emits_stable_pass_record(self) -> None:
        result, stdout, stderr = self.run_main(
            "capabilities", FakeReapiServer()
        )

        self.assertEqual(0, result)
        self.assertEqual("", stderr)
        self.assertTrue(stdout.startswith("PASS\treapi.capabilities\t"))
        self.assertIn('"digest_function":"SHA256"', stdout)

    def test_cas_command_emits_stable_pass_record(self) -> None:
        result, stdout, _ = self.run_main(
            "cas-round-trip",
            FakeReapiServer(),
            "--blob-bytes", "43",
        )

        self.assertEqual(0, result)
        self.assertTrue(stdout.startswith("PASS\treapi.cas-round-trip\t"))
        self.assertIn('"bytes":43', stdout)

    def test_invalid_endpoint_fails_without_opening_transport(self) -> None:
        stdout = io.StringIO()
        factory = mock.Mock()

        with contextlib.redirect_stdout(stdout):
            result = reapi.main(
                [
                    "capabilities",
                    "--endpoint", "https://re.test.invalid:50051",
                    "--instance-name", "main",
                    "--tls", "true",
                ],
                transport_factory=factory,
            )

        self.assertEqual(1, result)
        self.assertIn("FAIL\treapi.capabilities\t", stdout.getvalue())
        factory.assert_not_called()

    def test_missing_grpcio_has_a_clear_failure(self) -> None:
        with mock.patch.object(
            reapi.importlib,
            "import_module",
            side_effect=ModuleNotFoundError,
        ):
            with self.assertRaisesRegex(reapi.CheckFailure, "python3-grpcio"):
                reapi._load_grpc()


if __name__ == "__main__":
    unittest.main()
