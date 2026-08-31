#!/usr/bin/env python3
"""Focused offline tests for the REAPI readiness helper."""

from __future__ import annotations

import ast
from concurrent import futures
import contextlib
import io
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import importlib.util
import unittest

from _skiploader import load_skips
from unittest import mock





_skips = load_skips()
environmental_skip = _skips.environmental_skip
environmental_skip_unless = _skips.environmental_skip_unless


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

        def factory(
            endpoint: str,
            tls_credentials: reapi.TlsCredentials | None,
            timeout: float,
        ) -> FakeReapiServer:
            factory_calls.append((endpoint, tls_credentials, timeout))
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
            [("re.test.invalid:50051", None, 2.0)], factory_calls
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

    def test_tls_loads_complete_client_identity(self) -> None:
        stdout = io.StringIO()
        factory_calls = []
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ca = root / "ca.pem"
            chain = root / "client-chain.pem"
            key = root / "client-key.pem"
            ca.write_bytes(b"test ca")
            chain.write_bytes(b"test chain")
            key.write_bytes(b"test key")

            def factory(
                endpoint: str,
                tls_credentials: reapi.TlsCredentials | None,
                timeout: float,
            ) -> FakeReapiServer:
                factory_calls.append((endpoint, tls_credentials, timeout))
                return FakeReapiServer()

            with contextlib.redirect_stdout(stdout):
                result = reapi.main(
                    [
                        "capabilities",
                        "--endpoint", "re.test.invalid:50051",
                        "--instance-name", "main",
                        "--tls", "true",
                        "--tls-ca", str(ca),
                        "--tls-client-chain", str(chain),
                        "--tls-client-key", str(key),
                    ],
                    transport_factory=factory,
                )

        self.assertEqual(0, result)
        self.assertEqual(1, len(factory_calls))
        credentials = factory_calls[0][1]
        self.assertEqual(b"test ca", credentials.root_certificates)
        self.assertEqual(b"test chain", credentials.certificate_chain)
        self.assertEqual(b"test key", credentials.private_key)
        self.assertNotIn("test key", repr(credentials))
        self.assertNotIn("test key", stdout.getvalue())

    def test_tls_rejects_missing_client_identity(self) -> None:
        stdout = io.StringIO()
        factory = mock.Mock()
        with tempfile.TemporaryDirectory() as temporary:
            ca = Path(temporary, "ca.pem")
            ca.write_bytes(b"test ca")
            with contextlib.redirect_stdout(stdout):
                result = reapi.main(
                    [
                        "capabilities",
                        "--endpoint", "re.test.invalid:50051",
                        "--instance-name", "main",
                        "--tls", "true",
                        "--tls-ca", str(ca),
                    ],
                    transport_factory=factory,
                )

        self.assertEqual(1, result)
        self.assertIn("--tls-client-chain", stdout.getvalue())
        self.assertIn("--tls-client-key", stdout.getvalue())
        factory.assert_not_called()

    def test_plaintext_rejects_tls_credentials(self) -> None:
        stdout = io.StringIO()
        factory = mock.Mock()
        with contextlib.redirect_stdout(stdout):
            result = reapi.main(
                [
                    "capabilities",
                    "--endpoint", "127.0.0.1:50051",
                    "--instance-name", "main",
                    "--tls", "false",
                    "--tls-ca", "/not/read",
                ],
                transport_factory=factory,
            )

        self.assertEqual(1, result)
        self.assertIn("require --tls true", stdout.getvalue())
        factory.assert_not_called()

    def test_grpc_transport_passes_all_credential_bytes(self) -> None:
        grpc_module = mock.Mock()
        grpc_module.ssl_channel_credentials.return_value = "credentials"
        credentials = reapi.TlsCredentials(b"ca", b"chain", b"key")

        transport = reapi.GrpcTransport(
            grpc_module,
            "re.test.invalid:50051",
            credentials,
            2.0,
        )

        grpc_module.ssl_channel_credentials.assert_called_once_with(
            root_certificates=b"ca",
            private_key=b"key",
            certificate_chain=b"chain",
        )
        grpc_module.secure_channel.assert_called_once_with(
            "re.test.invalid:50051", "credentials"
        )
        grpc_module.insecure_channel.assert_not_called()
        self.assertEqual(2.0, transport.timeout)

    def test_missing_grpcio_has_a_clear_failure(self) -> None:
        with mock.patch.object(
            reapi.importlib,
            "import_module",
            side_effect=ModuleNotFoundError,
        ):
            with self.assertRaisesRegex(reapi.CheckFailure, "python3-grpcio"):
                reapi._load_grpc()


@environmental_skip_unless(shutil.which("openssl"), "openssl is unavailable")
class TlsHandshakeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.grpc = reapi._load_grpc()
        except reapi.CheckFailure as error:
            # A second, independent gate. Provisioning openssl alone only
            # moves the skip here, which is how this coverage stayed dead
            # while the target reported a pass.
            environmental_skip(str(error))
        cls.temporary_directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary_directory.cleanup)
        cls.root = Path(cls.temporary_directory.name)

        def openssl(*arguments: str) -> None:
            subprocess.run(
                ["openssl", *arguments],
                cwd=cls.root,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )

        def create_ca(name: str) -> None:
            openssl(
                "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", "{}-key.pem".format(name),
                "-out", "{}.pem".format(name),
                "-subj", "/CN={}".format(name),
                "-days", "1",
            )

        def issue(
            name: str,
            ca: str,
            serial: int,
            extensions: str,
        ) -> None:
            extension_path = cls.root / "{}.ext".format(name)
            extension_path.write_text(extensions, encoding="utf-8")
            openssl(
                "req", "-newkey", "rsa:2048", "-nodes",
                "-keyout", "{}-key.pem".format(name),
                "-out", "{}.csr".format(name),
                "-subj", "/CN={}".format(name),
            )
            openssl(
                "x509", "-req",
                "-in", "{}.csr".format(name),
                "-CA", "{}.pem".format(ca),
                "-CAkey", "{}-key.pem".format(ca),
                "-set_serial", str(serial),
                "-out", "{}.pem".format(name),
                "-days", "1",
                "-extfile", extension_path.name,
            )

        create_ca("trusted-ca")
        create_ca("wrong-ca")
        issue(
            "server",
            "trusted-ca",
            2,
            "subjectAltName=DNS:localhost,IP:127.0.0.1\n"
            "extendedKeyUsage=serverAuth\n",
        )
        issue(
            "trusted-client",
            "trusted-ca",
            3,
            "extendedKeyUsage=clientAuth\n",
        )
        issue(
            "wrong-client",
            "wrong-ca",
            4,
            "extendedKeyUsage=clientAuth\n",
        )

        cls.executor = futures.ThreadPoolExecutor(max_workers=1)
        cls.addClassCleanup(cls.executor.shutdown, wait=True)
        cls.server = cls.grpc.server(cls.executor)
        server_credentials = cls.grpc.ssl_server_credentials(
            ((
                (cls.root / "server-key.pem").read_bytes(),
                (cls.root / "server.pem").read_bytes(),
            ),),
            root_certificates=(cls.root / "trusted-ca.pem").read_bytes(),
            require_client_auth=True,
        )
        cls.port = cls.server.add_secure_port("127.0.0.1:0", server_credentials)
        if cls.port == 0:
            raise RuntimeError("could not allocate the test mTLS listener")
        cls.server.start()
        cls.addClassCleanup(lambda: cls.server.stop(0).wait())

    @classmethod
    def credentials(cls, client: str) -> reapi.TlsCredentials:
        return reapi.TlsCredentials(
            root_certificates=(cls.root / "trusted-ca.pem").read_bytes(),
            certificate_chain=(cls.root / "{}.pem".format(client)).read_bytes(),
            private_key=(cls.root / "{}-key.pem".format(client)).read_bytes(),
        )

    def test_valid_client_identity_completes_mtls_handshake(self) -> None:
        with reapi.GrpcTransport(
            self.grpc,
            "localhost:{}".format(self.port),
            self.credentials("trusted-client"),
            2.0,
        ):
            pass

    def test_plaintext_to_tls_endpoint_is_rejected(self) -> None:
        with self.assertRaisesRegex(reapi.CheckFailure, "readiness"):
            with reapi.GrpcTransport(
                self.grpc,
                "localhost:{}".format(self.port),
                None,
                1.0,
            ):
                pass

    def test_missing_client_identity_is_rejected(self) -> None:
        credentials = self.grpc.ssl_channel_credentials(
            root_certificates=(self.root / "trusted-ca.pem").read_bytes()
        )
        channel = self.grpc.secure_channel(
            "localhost:{}".format(self.port), credentials
        )
        try:
            with self.assertRaises(self.grpc.FutureTimeoutError):
                self.grpc.channel_ready_future(channel).result(timeout=1.0)
        finally:
            channel.close()

    def test_untrusted_client_role_is_rejected(self) -> None:
        with self.assertRaisesRegex(reapi.CheckFailure, "readiness"):
            with reapi.GrpcTransport(
                self.grpc,
                "localhost:{}".format(self.port),
                self.credentials("wrong-client"),
                1.0,
            ):
                pass


if __name__ == "__main__":
    unittest.main()
