#!/usr/bin/env python3
"""Focused tests for the non-mutating deployment admission checks."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


SCRIPT = Path(__file__).with_name("check_deployment.py")
SPEC = importlib.util.spec_from_file_location("check_deployment", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
check = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = check
SPEC.loader.exec_module(check)


class ParseTest(unittest.TestCase):
    def test_parse_bytes(self) -> None:
        self.assertEqual(1024, check.parse_bytes("1KiB"))
        self.assertEqual(2 * 1024 ** 3, check.parse_bytes("2GiB"))
        self.assertEqual(2 * 1000 ** 3, check.parse_bytes("2GB"))
        self.assertEqual(42, check.parse_bytes("42"))

    def test_parse_bytes_rejects_fraction_and_unknown_suffix(self) -> None:
        with self.assertRaisesRegex(Exception, "invalid byte quantity"):
            check.parse_bytes("1.5GiB")
        with self.assertRaisesRegex(Exception, "invalid byte quantity"):
            check.parse_bytes("10XB")

    def test_reporter_emits_stable_records(self) -> None:
        stream = io.StringIO()
        reporter = check.Reporter(stream)
        reporter.emit("PASS", "example", "ok", value=1)
        self.assertEqual(
            'PASS\texample\t{"message":"ok","value":1}\n',
            stream.getvalue(),
        )


class StorageTest(unittest.TestCase):
    def test_mountinfo_decodes_and_selects_deepest_mount(self) -> None:
        mounts = check.parse_mountinfo(
            "1 0 0:1 / / rw - ext4 /dev/root rw\n"
            "2 1 0:2 /sub\\040root /srv/cache rw - btrfs /dev/cache rw\n"
        )
        with tempfile.TemporaryDirectory(dir="/tmp") as root:
            path = Path(root, "cas")
            path.mkdir()
            adjusted = [
                mounts[0],
                dataclasses_replace(mounts[1], mount_point=root),
            ]
            selected = check.find_mount(path, adjusted)
        self.assertEqual("/sub root", selected.root)
        self.assertEqual("/dev/cache", selected.source)

    def test_storage_checks_identity_writability_and_capacity(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as root:
            mount = Path(root)
            cas = mount / "cas"
            ac = mount / "ac"
            cas.mkdir()
            ac.mkdir()
            info = check.MountInfo(
                mount_id=7,
                device="0:7",
                root="/",
                mount_point=str(mount),
                mount_options=frozenset({"rw"}),
                fs_type="ext4",
                source="/dev/test",
                super_options=frozenset({"rw"}),
            )
            identity = check.check_storage_identity(
                cas, ac, mount, "/dev/test", "ext4", [info],
            )
            writable = check.check_storage_writable(cas, ac, [info])
            watermarks = check.check_watermarks(100, 200, 10, 20)
            capacity = check.check_storage_capacity(mount, 200, 20, 10, 1)
        self.assertEqual("/dev/test", identity["identity"].split("|")[-1])
        self.assertEqual(str(cas), writable["cas_path"])
        self.assertEqual(200, watermarks["cas_high_bytes"])
        self.assertGreater(capacity["total_bytes"], 230)

    def test_storage_rejects_ephemeral_or_invalid_watermarks(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as root:
            mount = Path(root)
            cas = mount / "cas"
            ac = mount / "ac"
            cas.mkdir()
            ac.mkdir()
            info = check.MountInfo(
                mount_id=7,
                device="0:7",
                root="/",
                mount_point=str(mount),
                mount_options=frozenset({"rw"}),
                fs_type="tmpfs",
                source="tmpfs",
                super_options=frozenset({"rw"}),
            )
            with self.assertRaisesRegex(check.CheckFailure, "ephemeral"):
                check.check_storage_identity(cas, ac, mount, "tmpfs", "tmpfs", [info])
        with self.assertRaisesRegex(check.CheckFailure, "below high"):
            check.check_watermarks(200, 200, 10, 20)

    def test_dynamic_inode_filesystem_requires_explicit_zero_floor(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as root:
            stats = mock.Mock(
                f_blocks=1000,
                f_frsize=4096,
                f_bavail=900,
                f_files=0,
                f_favail=0,
            )
            with mock.patch.object(check.os, "statvfs", return_value=stats):
                result = check.check_storage_capacity(Path(root), 100, 100, 100, 0)
                self.assertEqual("dynamic", result["inode_model"])
                self.assertIsNone(result["available_inodes"])
                with self.assertRaisesRegex(check.CheckFailure, "zero inode floor"):
                    check.check_storage_capacity(Path(root), 100, 100, 100, 1)


class ProtocolTest(unittest.TestCase):
    def test_health_uses_command_template(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as root:
            helper = Path(root, "grpc-helper")
            helper.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "assert sys.argv[-2] == 're.example.invalid:50051'\n"
                "assert sys.argv[-1] == 'grpc.health.v1.Health/Check'\n"
                "assert json.loads(sys.argv[-3]) == {'service': ''}\n"
                "print(json.dumps({'status': 'SERVING'}))\n",
                encoding="utf-8",
            )
            helper.chmod(0o755)
            result = check.check_grpc_health(
                "{} --data {{json}} {{endpoint}} {{method}}".format(helper),
                "re.example.invalid:50051",
                "",
                2,
            )
        self.assertEqual("SERVING", result["status"])

    def test_capabilities_uses_command_template(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as root:
            helper = Path(root, "grpc-helper")
            helper.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "assert sys.argv[-2] == 're.example.invalid:50051'\n"
                "assert sys.argv[-1].endswith('/GetCapabilities')\n"
                "print(json.dumps({'cacheCapabilities': "
                "{'digestFunctions': ['SHA256']}, "
                "'executionCapabilities': "
                "{'digestFunction': 'SHA256', 'execEnabled': True}}))\n",
                encoding="utf-8",
            )
            helper.chmod(0o755)
            result = check.check_capabilities(
                "{} --data {{json}} {{endpoint}} {{method}}".format(helper),
                "re.example.invalid:50051",
                "main",
                2,
                True,
            )
        self.assertTrue(result["cache_sha256"])
        self.assertTrue(result["execution_enabled"])

    def test_otlp_config_and_tcp_reachability(self) -> None:
        with socket.create_server(("127.0.0.1", 0)) as server:
            port = server.getsockname()[1]
            thread = threading.Thread(target=lambda: server.accept()[0].close())
            thread.start()
            endpoint = "http://127.0.0.1:{}".format(port)
            config = check.check_otlp_config(endpoint, 15000)
            reachable = check.check_tcp_reachability(endpoint, 2)
            thread.join(timeout=2)
        self.assertEqual(15000, config["export_interval_ms"])
        self.assertTrue(reachable["reachable"])


class WorkerEvidenceTest(unittest.TestCase):
    def evidence(self, observed_at: float | None = None) -> dict[str, object]:
        return {
            "observed_at": time.time() if observed_at is None else observed_at,
            "workers": [
                {
                    "name": "worker-x86_64",
                    "connected": True,
                    "properties": {
                        "platform.OSFamily": {"values": ["linux"]},
                        "platform.arch": {"values": ["x86_64"]},
                    },
                },
                {
                    "name": "worker-aarch64",
                    "connected": True,
                    "properties": {
                        "platform.OSFamily": ["linux"],
                        "platform.arch": ["aarch64"],
                    },
                },
            ],
        }

    def test_requires_exact_architecture_properties(self) -> None:
        ready = check.validate_worker_evidence(self.evidence(), 120)
        self.assertEqual(["worker-x86_64"], ready["x86_64"])
        self.assertEqual(["worker-aarch64"], ready["aarch64"])
        bad = self.evidence()
        bad["workers"][0]["properties"]["platform.arch"] = ["x86_64", "aarch64"]
        with self.assertRaisesRegex(check.CheckFailure, "platform.arch"):
            check.validate_worker_evidence(bad, 120)
        extra = self.evidence()
        extra["workers"][0]["properties"]["container-image"] = ["unexpected"]
        with self.assertRaisesRegex(check.CheckFailure, "property keys"):
            check.validate_worker_evidence(extra, 120)

    def test_rejects_stale_evidence(self) -> None:
        with self.assertRaisesRegex(check.CheckFailure, "old"):
            check.validate_worker_evidence(self.evidence(observed_at=100), 10, now=1000)


class InotifyTest(unittest.TestCase):
    def test_counts_same_uid_instances_and_watches(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as root:
            base = Path(root)
            proc = base / "proc"
            sysctl = base / "sysctl"
            repo = base / "repo"
            (proc / "100" / "fd").mkdir(parents=True)
            (proc / "100" / "fdinfo").mkdir()
            (proc / "100" / "status").write_text("NSpid:\t100\n", encoding="utf-8")
            (proc / "self").symlink_to("100", target_is_directory=True)
            (proc / "100" / "fd" / "7").symlink_to("anon_inode:inotify")
            (proc / "100" / "fdinfo" / "7").write_text(
                "pos:\t0\ninotify wd:1 ino:1\ninotify wd:2 ino:2\n",
                encoding="utf-8",
            )
            sysctl.mkdir()
            (sysctl / "max_user_instances").write_text("100\n", encoding="utf-8")
            (sysctl / "max_user_watches").write_text("100000\n", encoding="utf-8")
            (sysctl / "max_queued_events").write_text("65536\n", encoding="utf-8")
            (repo / "src" / "nested").mkdir(parents=True)
            (repo / ".git" / "objects").mkdir(parents=True)
            snapshot = check.inotify_snapshot(
                repo,
                ignored_names={".git", "buck-out"},
                proc_root=proc,
                sysctl_root=sysctl,
            )
            instances = check.check_inotify_instances(snapshot, 1)
            watches = check.check_inotify_watches(snapshot, 1)
            queue = check.check_inotify_queue(snapshot)
        self.assertTrue(snapshot["host_pid_namespace"])
        self.assertEqual(1, instances["used"])
        self.assertEqual(2, watches["used"])
        self.assertEqual(3, watches["repository_directories"])
        self.assertEqual(65536, queue["limit"])

    def test_rejects_nested_pid_namespace(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as root:
            proc = Path(root, "proc")
            (proc / "1").mkdir(parents=True)
            (proc / "1" / "status").write_text("NSpid:\t123 1\n", encoding="utf-8")
            (proc / "self").symlink_to("1", target_is_directory=True)
            with self.assertRaisesRegex(check.CheckFailure, "host PID namespace"):
                check.read_inotify_usage(proc)

    def test_headroom_budgets_fail_independently(self) -> None:
        snapshot = {
            "directories": 100,
            "host_pid_namespace": True,
            "instances": 90,
            "instances_limit": 100,
            "processes": 1,
            "queued_limit": 10,
            "watches": 900,
            "watches_limit": 1000,
        }
        with self.assertRaisesRegex(check.CheckFailure, "instance headroom"):
            check.check_inotify_instances(snapshot, 1)
        with self.assertRaisesRegex(check.CheckFailure, "watch headroom"):
            check.check_inotify_watches(snapshot, 1)
        with self.assertRaisesRegex(check.CheckFailure, "max_queued_events"):
            check.check_inotify_queue(snapshot)


class MainTest(unittest.TestCase):
    def test_emits_complete_passing_stage_zero_record_set(self) -> None:
        checks = (
            "check_grpc_health",
            "check_capabilities",
            "check_storage_identity",
            "check_storage_writable",
            "check_watermarks",
            "check_storage_capacity",
            "check_otlp_config",
            "check_tcp_reachability",
            "check_http_health",
            "check_inotify_instances",
            "check_inotify_watches",
            "check_inotify_queue",
        )
        inotify = {
            "directories": 1,
            "host_pid_namespace": True,
            "instances": 0,
            "instances_limit": 128,
            "processes": 1,
            "queued_limit": 65536,
            "watches": 0,
            "watches_limit": 131072,
        }
        argv = [
            "--grpc-command", "grpc-client {json} {endpoint} {method}",
            "--reapi-endpoint", "re.example.invalid:50051",
            "--instance-name", "main",
            "--cas-path", "/storage/cas",
            "--ac-path", "/storage/ac",
            "--storage-mount", "/storage",
            "--expected-storage-source", "/dev/storage",
            "--expected-storage-fstype", "ext4",
            "--cas-low-bytes", "1GiB",
            "--cas-high-bytes", "2GiB",
            "--ac-low-bytes", "1GiB",
            "--ac-high-bytes", "2GiB",
            "--storage-reserve-bytes", "1GiB",
            "--min-free-inodes", "0",
            "--worker-evidence-file", str(SCRIPT),
            "--otel-endpoint", "http://otel.example.invalid:4317",
            "--collector-health-url", "http://otel.example.invalid:13133/status",
            "--client-repo-root", ".",
        ]
        output = io.StringIO()
        with contextlib.ExitStack() as patches:
            for name in checks:
                patches.enter_context(mock.patch.object(check, name, return_value={}))
            patches.enter_context(mock.patch.object(check, "read_mountinfo", return_value=[]))
            patches.enter_context(mock.patch.object(check, "load_worker_evidence", return_value={}))
            patches.enter_context(mock.patch.object(
                check,
                "validate_worker_evidence",
                return_value={"x86_64": ["worker-x86_64"], "aarch64": ["worker-aarch64"]},
            ))
            patches.enter_context(mock.patch.object(
                check,
                "inotify_snapshot",
                return_value=inotify,
            ))
            with contextlib.redirect_stdout(output):
                return_code = check.main(argv)
        records = [line.split("\t", 2) for line in output.getvalue().splitlines()]
        self.assertEqual(0, return_code)
        self.assertEqual(17, len(records))
        self.assertTrue(all(record[0] == "PASS" for record in records))
        self.assertEqual("stage-zero", records[-1][1])
        self.assertEqual(0, json.loads(records[-1][2])["failures"])


def dataclasses_replace(value: object, **changes: object) -> object:
    return check.dataclasses.replace(value, **changes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
