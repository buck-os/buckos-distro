#!/usr/bin/env python3

import hashlib
import json
import platform
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "infra/remote-execution/scripts/sdme-provision.sh"
ROOTFS = ROOT / "infra/remote-execution/sdme/worker-rootfs.sdme"
DROP_IN = ROOT / "infra/remote-execution/sdme/worker-preflight.conf"
ADDRESS_SELECTOR = ROOT / "infra/remote-execution/scripts/sdme_select_address.py"
RUNTIME_FS = "buckos-re-runtime-5c2e6eca51c6"


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
        shutil.copy2(ADDRESS_SELECTOR, scripts / ADDRESS_SELECTOR.name)
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

    def install_fake_runtime_tools(self, architecture: str) -> Path:
        fake_bin = self.external / "fake-bin"
        fake_bin.mkdir(mode=0o755)
        log = self.external / "commands.log"
        quoted_log = shlex.quote(str(log))
        (fake_bin / "sdme").write_text(
            """#!/bin/sh
set -eu
printf 'sdme %s\\n' "$*" >> {log}
if [ "$1" = fs ] && [ "$2" = ls ]; then
  printf '[]\\n'
elif [ "$1" = cp ]; then
  printf '%s\\n' \\
    'ubuntu_image=docker.io/library/ubuntu@sha256:2260313b31c8c011cd2eebe728008efac1b3982be73eb71348ea2648d2c0e09b' \\
    'nativelink_image=ghcr.io/tracemachina/nativelink@sha256:5c2e6eca51c6d3ac40b94f703e08a243fd036cc136cc858a99040ca90fa57d61' \\
    'architecture={architecture}' > "$3/runtime-images"
fi
""".format(log=quoted_log, architecture=architecture),
            encoding="utf-8",
        )
        (fake_bin / "podman").write_text(
            """#!/bin/sh
set -eu
printf 'podman %s\\n' "$*" >> {log}
while [ "$#" -gt 0 ]; do
  if [ "$1" = --output ]; then
    shift
    : > "$1"
  fi
  shift
done
""".format(log=quoted_log),
            encoding="utf-8",
        )
        (fake_bin / "systemctl").write_text(
            """#!/bin/sh
printf 'systemctl %s\\n' "$*" >> {log}
exit 1
""".format(log=quoted_log),
            encoding="utf-8",
        )
        for executable in fake_bin.iterdir():
            executable.chmod(0o755)

        fixed_path = "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        script = self.script.read_text(encoding="utf-8")
        self.script.write_text(
            script.replace(fixed_path, "PATH={}:{}".format(fake_bin, fixed_path[5:])),
            encoding="utf-8",
        )
        self.script.chmod(0o755)
        return log

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
        self.assertIn("prepare-runtime worker", result.stdout)
        self.assertIn("prepare-worker-probe-root.sh apply", result.stdout)
        self.assertIn("# 3. Apply the worker with that probe path and digest:", result.stdout)
        self.assertFalse((self.external / "data").exists())

    def test_prepare_runtime_has_no_container_or_service_operations(self) -> None:
        architecture = {"amd64": "x86_64", "arm64": "aarch64"}.get(
            platform.machine(), platform.machine()
        )
        if architecture not in ("x86_64", "aarch64"):
            self.skipTest("unsupported test architecture")
        log = self.install_fake_runtime_tools(architecture)
        data_root = self.external / "data"

        result = self.run_script(
            "prepare-runtime",
            "worker",
            "--arch",
            architecture,
            "--data-root",
            str(data_root),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        commands = log.read_text(encoding="utf-8")
        self.assertIn("podman pull", commands)
        self.assertIn("sdme fs import", commands)
        self.assertIn("sdme fs build {}".format(RUNTIME_FS), commands)
        self.assertIn("sdme cp fs:{}:".format(RUNTIME_FS), commands)
        for forbidden in (
            "sdme create",
            "sdme start",
            "sdme exec",
            "systemctl ",
            "nativelink.env",
            "--port",
        ):
            self.assertNotIn(forbidden, commands)
        self.assertTrue((data_root / "images").is_dir())
        self.assertTrue((data_root / "provision").is_dir())
        self.assertFalse((data_root / "worker-{}".format(architecture)).exists())

    def test_worker_apply_still_requires_probe_contract(self) -> None:
        architecture = {"amd64": "x86_64", "arm64": "aarch64"}.get(
            platform.machine(), platform.machine()
        )
        if architecture not in ("x86_64", "aarch64"):
            self.skipTest("unsupported test architecture")
        log = self.install_fake_runtime_tools(architecture)

        result = self.run_script(
            "apply",
            "worker",
            "--arch",
            architecture,
            "--data-root",
            str(self.external / "data"),
            "--control-address",
            "buckos-re-control",
            "--min-scratch-bytes",
            "1000000",
            "--min-scratch-inodes",
            "0",
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("--probe-sysroot must be absolute", result.stderr)
        self.assertFalse(log.exists())

    def test_prepare_runtime_rejects_deployment_options(self) -> None:
        self.install_fake_runtime_tools("x86_64")
        result = self.run_script(
            "prepare-runtime",
            "worker",
            "--arch",
            "x86_64",
            "--data-root",
            str(self.external / "data"),
            "--control-address",
            "buckos-re-control",
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("accepts only --data-root, --arch, and acquisition options", result.stderr)

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
        self.assertNotIn("NATIVELINK_WORKER_BIND_ADDRESS=0.0.0.0", result.stdout)
        self.assertIn("preferring RFC1918/ULA over link-local", result.stdout)

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


class AddressSelectionTest(unittest.TestCase):
    def select(self, addresses: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(ADDRESS_SELECTOR)],
            input=json.dumps({"addresses": addresses}),
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_prefers_rfc1918_over_link_local(self) -> None:
        result = self.select(["169.254.42.8", "10.77.0.3"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "10.77.0.3")

    def test_accepts_link_local_fallback(self) -> None:
        result = self.select(["169.254.42.8"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "169.254.42.8")

    def test_rejects_non_routable_candidates(self) -> None:
        result = self.select(["127.0.0.1", "0.0.0.0", "224.0.0.1", "::", "ff02::1"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("no private or link-local non-wildcard", result.stderr)


if __name__ == "__main__":
    unittest.main()
