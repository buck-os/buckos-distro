#!/usr/bin/python3
"""Opt-in execution regression for the pinned Buck2 and NativeLink pair."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import signal
import socket
import subprocess
import tarfile
import tempfile
import time
import unittest
from typing import Optional


EXPECTED_BUCK_VERSION = "buck2 2026-08-26-0e31bbaa34f4e4842f1cff72395ff7de813202db"
EXPECTED_NATIVELINK_SHA256 = (
    "7ea68447000a0d4f59c948634a6ff5094a3868f8d9961320aab6c0878bc67ab9"
)
PROBE_TARGET = "//infra/remote-execution:worker-architecture-x86_64"
TEST_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = TEST_ROOT.parents[2]
SMOKE_TEST = REPOSITORY_ROOT / "infra/remote-execution/scripts/smoke-test.sh"


def run(
    arguments: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        check=False,
        cwd=cwd,
        env=environment,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def wait_for_port(port: int, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"NativeLink exited with {process.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError(f"NativeLink did not listen on 127.0.0.1:{port}")


def stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def create_certificate_authority(root: Path, environment: dict[str, str]) -> None:
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=remote-execution-test-ca",
            "-keyout",
            str(root / "ca-key.pem"),
            "-out",
            str(root / "ca.pem"),
        ],
        check=True,
        cwd=root,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def create_identity(
    root: Path,
    name: str,
    purpose: str,
    environment: dict[str, str],
) -> None:
    key = root / f"{name}-key.pem"
    request = root / f"{name}.csr"
    certificate = root / f"{name}.pem"
    extensions = root / f"{name}.ext"
    extension_lines = [f"extendedKeyUsage={purpose}"]
    if purpose == "serverAuth":
        extension_lines.append("subjectAltName=DNS:localhost,IP:127.0.0.1")
    extensions.write_text("\n".join(extension_lines) + "\n", encoding="utf-8")
    subprocess.run(
        [
            "openssl",
            "req",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-subj",
            f"/CN={name}",
            "-keyout",
            str(key),
            "-out",
            str(request),
        ],
        check=True,
        cwd=root,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        [
            "openssl",
            "x509",
            "-req",
            "-days",
            "1",
            "-in",
            str(request),
            "-CA",
            str(root / "ca.pem"),
            "-CAkey",
            str(root / "ca-key.pem"),
            "-CAcreateserial",
            "-extfile",
            str(extensions),
            "-out",
            str(certificate),
        ],
        check=True,
        cwd=root,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    key.chmod(0o600)


def combine(output: Path, *inputs: Path) -> None:
    with output.open("wb") as stream:
        for path in inputs:
            stream.write(path.read_bytes())
    output.chmod(0o600)


def extract_regular_archive(archive: Path, destination: Path) -> list[str]:
    destination.mkdir(exist_ok=True)
    with tarfile.open(archive) as source:
        members = source.getmembers()
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise AssertionError(f"unsafe archive member: {member.name}")
            if not member.isfile() and not member.isdir():
                raise AssertionError(f"non-regular archive member: {member.name}")
        source.extractall(destination)
    return [member.name for member in members]


def write_nativelink_configs(
    root: Path,
    reapi_port: int,
    worker_port: int,
) -> tuple[Path, Path]:
    config_root = REPOSITORY_ROOT / "infra/remote-execution/nativelink"
    control = json.loads((config_root / "control-mtls.json5").read_text())
    worker = json.loads((config_root / "worker-x86_64-mtls.json5").read_text())

    control["stores"][0]["compression"]["backend"]["filesystem"].update(
        content_path=str(root / "cas/content"),
        temp_path=str(root / "cas/tmp"),
    )
    control["stores"][1]["filesystem"].update(
        content_path=str(root / "ac/content"),
        temp_path=str(root / "ac/tmp"),
    )
    for server, port in zip(control["servers"], (reapi_port, worker_port)):
        server["listener"]["http"]["socket_address"] = f"127.0.0.1:{port}"
        tls = server["listener"]["http"]["tls"]
        tls.update(
            cert_file=str(root / "server-chain.pem"),
            key_file=str(root / "server-key.pem"),
            client_ca_file=str(root / "ca.pem"),
        )

    for store in worker["stores"][:2]:
        endpoint = store["grpc"]["endpoints"][0]
        endpoint["address"] = f"https://localhost:{reapi_port}"
        endpoint["tls_config"].update(
            ca_file=str(root / "ca.pem"),
            cert_file=str(root / "client-chain.pem"),
            key_file=str(root / "client-key.pem"),
        )
    worker_store = worker["stores"][2]["fast_slow"]["fast"]["filesystem"]
    worker_store.update(
        content_path=str(root / "worker-cas/content"),
        temp_path=str(root / "worker-cas/tmp"),
    )
    local_worker = worker["workers"][0]["local"]
    local_worker["work_directory"] = str(root / "worker-work")
    worker_endpoint = local_worker["worker_api_endpoint"]
    worker_endpoint["uri"] = f"https://localhost:{worker_port}"
    worker_endpoint["tls_config"].update(
        ca_file=str(root / "ca.pem"),
        cert_file=str(root / "client-chain.pem"),
        key_file=str(root / "client-key.pem"),
    )

    control_path = root / "control.json"
    worker_path = root / "worker.json"
    control_path.write_text(json.dumps(control), encoding="utf-8")
    worker_path.write_text(json.dumps(worker), encoding="utf-8")
    return control_path, worker_path


def export_client(
    destination: Path,
    environment: dict[str, str],
    source_archive: Optional[Path] = None,
) -> None:
    destination.mkdir()
    archive = source_archive
    remove_archive = False
    if archive is None:
        archive = destination.parent / f"{destination.name}.tar"
        remove_archive = True
        with archive.open("wb") as output:
            subprocess.run(
                ["git", "archive", "--format=tar", "HEAD"],
                check=True,
                cwd=REPOSITORY_ROOT,
                env=environment,
                stdout=output,
            )
    extract_regular_archive(archive, destination)
    if remove_archive:
        archive.unlink()
    (destination / "prelude").mkdir(exist_ok=True)


@unittest.skipUnless(
    os.environ.get("BUCKOS_RUN_PINNED_MTLS_REGRESSION") == "1",
    "set BUCKOS_RUN_PINNED_MTLS_REGRESSION=1 for the exact-binary test",
)
class PinnedMtlsRegressionTest(unittest.TestCase):
    def test_static_file_performs_remote_action_and_overlays_fail(self) -> None:
        buck = Path(os.environ["BUCKOS_PINNED_BUCK2"]).resolve(strict=True)
        archive = Path(os.environ["BUCKOS_PINNED_NATIVELINK_ARCHIVE"]).resolve(
            strict=True
        )
        source_archive_value = os.environ.get("BUCKOS_PINNED_SOURCE_ARCHIVE")
        source_archive = (
            Path(source_archive_value).resolve(strict=True)
            if source_archive_value
            else None
        )
        self.assertEqual(
            EXPECTED_NATIVELINK_SHA256,
            hashlib.sha256(archive.read_bytes()).hexdigest(),
        )

        with tempfile.TemporaryDirectory(prefix="pinned-mtls-") as directory:
            root = Path(directory)
            environment = os.environ.copy()
            environment.update(
                HOME=str(root / "home"),
                XDG_CACHE_HOME=str(root / "cache"),
                OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:4317",
                OTEL_LOGS_EXPORTER="none",
                OTEL_METRICS_EXPORTER="none",
                OTEL_TRACES_EXPORTER="none",
                OTEL_SDK_DISABLED="true",
            )
            (root / "home").mkdir()
            (root / "cache").mkdir()
            create_certificate_authority(root, environment)
            create_identity(root, "server", "serverAuth", environment)
            create_identity(root, "client", "clientAuth", environment)
            combine(
                root / "server-chain.pem",
                root / "server.pem",
                root / "ca.pem",
            )
            combine(
                root / "client-chain.pem",
                root / "client.pem",
                root / "ca.pem",
            )
            combine(
                root / "buck-client.pem",
                root / "client.pem",
                root / "ca.pem",
                root / "client-key.pem",
            )
            for directory_name in (
                "cas/content",
                "cas/tmp",
                "ac/content",
                "ac/tmp",
                "worker-cas/content",
                "worker-cas/tmp",
                "worker-work",
            ):
                (root / directory_name).mkdir(parents=True)

            self.assertEqual(
                ["nativelink", "LICENSE", "README.md"],
                extract_regular_archive(archive, root / "release"),
            )
            nativelink = root / "release/nativelink"
            nativelink.chmod(0o755)
            version = run(
                [str(buck), "--version"],
                cwd=REPOSITORY_ROOT,
                environment=environment,
            )
            self.assertEqual(0, version.returncode, version.stderr)
            self.assertEqual(EXPECTED_BUCK_VERSION, version.stdout.strip())

            reapi_port = free_port()
            worker_port = free_port()
            self.assertNotEqual(reapi_port, worker_port)
            control_config, worker_config = write_nativelink_configs(
                root, reapi_port, worker_port
            )
            processes = []
            buck_daemons = []
            try:
                control_log = (root / "control.log").open("w", encoding="utf-8")
                worker_log = (root / "worker.log").open("w", encoding="utf-8")
                control = subprocess.Popen(
                    [str(nativelink), str(control_config)],
                    cwd=root,
                    env=environment,
                    text=True,
                    stdout=control_log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                processes.append(control)
                wait_for_port(reapi_port, control)
                worker = subprocess.Popen(
                    [str(nativelink), str(worker_config)],
                    cwd=root,
                    env=environment,
                    text=True,
                    stdout=worker_log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                processes.append(worker)
                wait_for_port(worker_port, control)
                time.sleep(0.5)

                clients = [
                    root / f"client-{name}" for name in ("cli", "file", "a", "b")
                ]
                for client in clients:
                    export_client(client, environment, source_archive)
                buck_daemons.extend(
                    [
                        (clients[0], "pinned-cli-only"),
                        (clients[1], "pinned-config-file-only"),
                        (clients[2], "pinned-file-a"),
                        (clients[3], "pinned-file-b"),
                    ]
                )

                static_values = [
                    "--config",
                    f"buck2_re_client.engine_address=localhost:{reapi_port}",
                    "--config",
                    f"buck2_re_client.action_cache_address=localhost:{reapi_port}",
                    "--config",
                    f"buck2_re_client.cas_address=localhost:{reapi_port}",
                    "--config",
                    "buck2_re_client.instance_name=main",
                    "--config",
                    "buck2_re_client.tls=true",
                    "--config",
                    f"buck2_re_client.tls_ca_certs={root / 'ca.pem'}",
                    "--config",
                    f"buck2_re_client.tls_client_cert={root / 'buck-client.pem'}",
                ]
                build_values = [
                    "--config",
                    "buckos.remote_cache=true",
                    "--config",
                    "buckos.remote_execution=true",
                    "--config",
                    "buckos.aarch64_emulation=false",
                    "--config",
                    "buckos.remote_x86_64_properties=platform.OSFamily=linux,platform.arch=x86_64",
                    "--config",
                    "buckos.remote_aarch64_properties=platform.OSFamily=linux,platform.arch=aarch64",
                    "--config",
                    "buckos.remote_x86_64_use_case=buck2-default",
                    "--config",
                    "buckos.remote_aarch64_use_case=buck2-default",
                ]
                cli_output = root / "cli-arch.out"
                cli_event = root / "cli-event.json-lines.gz"
                cli_result = run(
                    [
                        str(buck),
                        "--isolation-dir",
                        "pinned-cli-only",
                        "build",
                        PROBE_TARGET,
                        "--remote-only",
                        "--no-remote-cache",
                        "--out",
                        str(cli_output),
                        "--event-log",
                        str(cli_event),
                        *build_values,
                        *static_values,
                    ],
                    cwd=clients[0],
                    environment=environment,
                )
                cli_cleanup = run(
                    [str(buck), "--isolation-dir", "pinned-cli-only", "kill"],
                    cwd=clients[0],
                    environment=environment,
                )
                self.assertEqual(
                    0,
                    cli_cleanup.returncode,
                    cli_cleanup.stdout + cli_cleanup.stderr,
                )
                self.assertNotEqual(0, cli_result.returncode)
                self.assertIn(
                    "No engine address", cli_result.stdout + cli_result.stderr
                )
                self.assertFalse(cli_output.exists())

                overlay = root / "remote.buckconfig"
                overlay.write_text(
                    "\n".join(
                        [
                            "[buck2_re_client]",
                            f"  engine_address = localhost:{reapi_port}",
                            f"  action_cache_address = localhost:{reapi_port}",
                            f"  cas_address = localhost:{reapi_port}",
                            "  instance_name = main",
                            "  tls = true",
                            f"  tls_ca_certs = {root / 'ca.pem'}",
                            f"  tls_client_cert = {root / 'buck-client.pem'}",
                            "[buckos]",
                            "  remote_cache = true",
                            "  remote_execution = true",
                            "  aarch64_emulation = false",
                            "  remote_x86_64_properties = platform.OSFamily=linux,platform.arch=x86_64",
                            "  remote_aarch64_properties = platform.OSFamily=linux,platform.arch=aarch64",
                            "  remote_x86_64_use_case = buck2-default",
                            "  remote_aarch64_use_case = buck2-default",
                            "",
                        ]
                    ),
                    encoding="utf-8",
                )
                overlay.chmod(0o600)
                file_output = root / "config-file-arch.out"
                file_event = root / "config-file-event.json-lines.gz"
                file_result = run(
                    [
                        str(buck),
                        "--isolation-dir",
                        "pinned-config-file-only",
                        "build",
                        PROBE_TARGET,
                        "--remote-only",
                        "--no-remote-cache",
                        "--out",
                        str(file_output),
                        "--event-log",
                        str(file_event),
                        "--config-file",
                        str(overlay),
                    ],
                    cwd=clients[1],
                    environment=environment,
                )
                file_cleanup = run(
                    [
                        str(buck),
                        "--isolation-dir",
                        "pinned-config-file-only",
                        "kill",
                    ],
                    cwd=clients[1],
                    environment=environment,
                )
                self.assertEqual(
                    0,
                    file_cleanup.returncode,
                    file_cleanup.stdout + file_cleanup.stderr,
                )
                self.assertNotEqual(0, file_result.returncode)
                self.assertIn(
                    "No engine address", file_result.stdout + file_result.stderr
                )
                self.assertFalse(file_output.exists())

                evidence = root / "evidence"
                positive = run(
                    [
                        "bash",
                        str(SMOKE_TEST),
                        "--stage",
                        "probe-x86_64",
                        "--client-a",
                        str(clients[2]),
                        "--client-b",
                        str(clients[3]),
                        "--client-a-isolation",
                        "pinned-file-a",
                        "--client-b-isolation",
                        "pinned-file-b",
                        "--buck",
                        str(buck),
                        "--endpoint",
                        f"localhost:{reapi_port}",
                        "--instance-name",
                        "main",
                        "--tls",
                        "true",
                        "--tls-ca",
                        str(root / "ca.pem"),
                        "--tls-client-chain",
                        str(root / "client-chain.pem"),
                        "--tls-client-key",
                        str(root / "client-key.pem"),
                        "--buck-tls-client-cert",
                        str(root / "buck-client.pem"),
                        "--cross-host",
                        "--event-dir",
                        str(evidence),
                        "--timeout-seconds",
                        "60",
                    ],
                    cwd=REPOSITORY_ROOT,
                    environment=environment,
                    timeout=120,
                )
                self.assertEqual(
                    0,
                    positive.returncode,
                    positive.stdout
                    + positive.stderr
                    + (evidence / "probe-x86_64-what-ran.jsonl").read_text(
                        encoding="utf-8"
                    ),
                )
                self.assertIn("PASS probe-x86_64.executor executor=Re", positive.stdout)
                self.assertIn(
                    "PASS probe-x86_64.remote-actions value=1", positive.stdout
                )
                self.assertFalse((clients[2] / ".buckconfig.local").exists())
                self.assertFalse((clients[3] / ".buckconfig.local").exists())
            finally:
                for client, isolation in buck_daemons:
                    try:
                        run(
                            [str(buck), "--isolation-dir", isolation, "kill"],
                            cwd=client,
                            environment=environment,
                            timeout=30,
                        )
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                for process in reversed(processes):
                    stop_process(process)
                for stream_name in ("control_log", "worker_log"):
                    stream = locals().get(stream_name)
                    if stream is not None:
                        stream.close()
            self.assertTrue(all(process.poll() is not None for process in processes))
            for port in (reapi_port, worker_port):
                with self.assertRaises(OSError):
                    socket.create_connection(("127.0.0.1", port), timeout=0.2)


if __name__ == "__main__":
    unittest.main()
