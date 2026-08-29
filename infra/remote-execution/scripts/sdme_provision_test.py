#!/usr/bin/env python3

import hashlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "infra/remote-execution/scripts/sdme-provision.sh"
ROOTFS = ROOT / "infra/remote-execution/sdme/worker-rootfs.sdme"
DROP_IN = ROOT / "infra/remote-execution/sdme/worker-preflight.conf"


def tree_digest(path: Path) -> str:
    command = [
        "tar",
        "--sort=name",
        "--mtime=UTC 1970-01-01",
        "--owner=0",
        "--group=0",
        "--numeric-owner",
        "--format=posix",
        "--pax-option=delete=atime,delete=ctime",
        "-C",
        str(path),
        "-cf",
        "-",
        ".",
    ]
    result = subprocess.run(command, check=True, stdout=subprocess.PIPE)
    return hashlib.sha256(result.stdout).hexdigest()


class ProvisionPlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.repo = base / "repo"
        self.external = base / "external"
        scripts = self.repo / "infra/remote-execution/scripts"
        sdme = self.repo / "infra/remote-execution/sdme"
        nativelink = self.repo / "infra/remote-execution/nativelink"
        tools = self.repo / "tools"
        for directory in (scripts, sdme, nativelink, tools, self.external):
            directory.mkdir(parents=True, exist_ok=True)

        shutil.copy2(SCRIPT, scripts / SCRIPT.name)
        shutil.copy2(ROOTFS, sdme / ROOTFS.name)
        shutil.copy2(DROP_IN, sdme / DROP_IN.name)
        for name in ("control.json5", "worker-x86_64.json5", "worker-aarch64.json5"):
            (nativelink / name).write_text("{}\n", encoding="utf-8")
        (nativelink / "nativelink.service").write_text("[Service]\n", encoding="utf-8")
        (nativelink / "deployment.json").write_text(
            """{
  "image": {
    "version": "v1.6.6",
    "reference": "ghcr.io/tracemachina/nativelink@sha256:5c2e6eca51c6d3ac40b94f703e08a243fd036cc136cc858a99040ca90fa57d61"
  }
}\n""",
            encoding="utf-8",
        )
        (scripts / "preflight-worker.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        (scripts / "preflight-worker.sh").chmod(0o755)
        (scripts / "preflight_worker.py").write_text("# probe\n", encoding="utf-8")
        (tools / "_isolation.py").write_text("# isolation\n", encoding="utf-8")
        (tools / "_rpm.py").write_text("# rpm\n", encoding="utf-8")
        (tools / "nativelink_config.py").write_text("# validator\n", encoding="utf-8")
        self.script = scripts / SCRIPT.name
        self.script.chmod(0o755)

        self.probe = self.external / "probe"
        for name in ("proc", "dev", "tmp", "usr/bin"):
            (self.probe / name).mkdir(parents=True, exist_ok=True)
        (self.probe / "usr/bin/python3").write_bytes(b"probe-python")
        (self.probe / "usr/bin/python3").chmod(0o755)
        self.digest = tree_digest(self.probe)

    def run_script(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.script), *arguments],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def worker_arguments(self) -> list[str]:
        return [
            "plan",
            "worker",
            "--arch",
            "x86_64",
            "--data-root",
            str(self.external / "data"),
            "--control-address",
            "buckos-re-control",
            "--probe-sysroot",
            str(self.probe),
            "--probe-sysroot-sha256",
            self.digest,
            "--min-scratch-bytes",
            "1000000",
            "--min-scratch-inodes",
            "0",
        ]

    def test_worker_plan_has_required_isolation_and_storage(self) -> None:
        result = self.run_script(*self.worker_arguments())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--userns-nested 1", result.stdout)
        self.assertIn("worker-x86_64/scratch:/var/tmp", result.stdout)
        self.assertIn("probe:/opt/buckos-re/probe-sysroot:ro", result.stdout)
        self.assertIn("preflight-worker.sh", result.stdout)
        self.assertIn("BUCKOS_RE_MIN_SCRATCH_BYTES=1000000", result.stdout)
        self.assertIn("BUCKOS_RE_MIN_SCRATCH_INODES=0", result.stdout)
        self.assertNotIn("--hardened", result.stdout)
        self.assertIn("sha256:2260313b31c8", result.stdout)
        self.assertIn("sha256:5c2e6eca51c6", result.stdout)
        self.assertFalse((self.external / "data").exists())

    def test_rootfs_and_drop_in_preserve_worker_contract(self) -> None:
        rootfs = ROOTFS.read_text(encoding="utf-8")
        drop_in = DROP_IN.read_text(encoding="utf-8")
        for package in ("bubblewrap", "uidmap", "rpm2cpio", "dpkg-dev"):
            self.assertIn(package, rootfs)
        self.assertIn("nativelink:65536:65536", rootfs)
        self.assertIn("nativelink --version", rootfs)
        self.assertIn("PrivateTmp=no", drop_in)
        self.assertIn("ReadWritePaths=/var/tmp", drop_in)
        self.assertIn("preflight-worker.sh", drop_in)
        self.assertNotIn("NoNewPrivileges", drop_in)

    def test_aarch64_plan_selects_native_worker_assets(self) -> None:
        arguments = self.worker_arguments()
        arguments[arguments.index("x86_64")] = "aarch64"
        result = self.run_script(*arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("worker-aarch64.json5", result.stdout)
        self.assertIn("--name buckos-re-worker-aarch64", result.stdout)
        self.assertIn("--platform linux/arm64", result.stdout)

    def test_control_is_private_until_publish_is_explicit(self) -> None:
        result = self.run_script(
            "plan",
            "control",
            "--arch",
            "x86_64",
            "--data-root",
            str(self.external / "data"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("--port", result.stdout)
        self.assertIn("--network-zone buckos-re", result.stdout)

    def test_rejects_placeholder_control_address(self) -> None:
        arguments = self.worker_arguments()
        index = arguments.index("buckos-re-control")
        arguments[index] = "re.example.invalid"
        result = self.run_script(*arguments)
        self.assertEqual(result.returncode, 2)
        self.assertIn("placeholder", result.stderr)

    def test_rejects_wrong_probe_digest(self) -> None:
        arguments = self.worker_arguments()
        index = arguments.index(self.digest)
        arguments[index] = "0" * 64
        result = self.run_script(*arguments)
        self.assertEqual(result.returncode, 2)
        self.assertIn("digest mismatch", result.stderr)

    def test_publish_requires_restricted_policy(self) -> None:
        result = self.run_script(
            "plan",
            "control",
            "--arch",
            "x86_64",
            "--data-root",
            str(self.external / "data"),
            "--publish",
            "--client-cidrs",
            "0.0.0.0/0",
            "--worker-cidrs",
            "10.0.0.2/32",
            "--firewall-check",
            "/bin/true",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("public catch-all", result.stderr)

    def test_publish_plan_names_distinct_network_policies(self) -> None:
        checker = self.external / "check-firewall"
        checker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        checker.chmod(0o755)
        result = self.run_script(
            "plan",
            "control",
            "--arch",
            "x86_64",
            "--data-root",
            str(self.external / "data"),
            "--publish",
            "--client-cidrs",
            "10.20.0.0/24",
            "--worker-cidrs",
            "10.30.0.0/24",
            "--firewall-check",
            str(checker),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--client-port 50051", result.stdout)
        self.assertIn("--worker-port 50061", result.stdout)
        self.assertIn("--port tcp:50051:50051", result.stdout)
        self.assertIn("--port tcp:50061:50061", result.stdout)

    def test_rejects_malformed_client_cidr(self) -> None:
        checker = self.external / "check-firewall"
        checker.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        checker.chmod(0o755)
        result = self.run_script(
            "plan",
            "control",
            "--arch",
            "x86_64",
            "--data-root",
            str(self.external / "data"),
            "--publish",
            "--client-cidrs",
            "10.20.0.999/24",
            "--worker-cidrs",
            "10.30.0.0/24",
            "--firewall-check",
            str(checker),
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid CIDR", result.stderr)


if __name__ == "__main__":
    unittest.main()
