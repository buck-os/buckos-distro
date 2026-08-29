#!/usr/bin/env python3

import hashlib
import io
import json
import platform
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

import oci_archive


TEST_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = TEST_ROOT.parents[2]


def test_resource(name: str, checkout_path: str) -> Path:
    packaged = TEST_ROOT / name
    if packaged.is_file():
        return packaged
    return REPOSITORY_ROOT / checkout_path


SCRIPT = test_resource(
    "sdme-provision.sh", "infra/remote-execution/scripts/sdme-provision.sh"
)
ROOTFS = test_resource(
    "worker-rootfs.sdme", "infra/remote-execution/sdme/worker-rootfs.sdme"
)
DROP_IN = test_resource(
    "worker-preflight.conf", "infra/remote-execution/sdme/worker-preflight.conf"
)
ADDRESS_SELECTOR = test_resource(
    "sdme_select_address.py",
    "infra/remote-execution/scripts/sdme_select_address.py",
)
RUNTIME_FS = "buckos-re-runtime-5c2e6eca51c6"
ARCHIVE_TOOL = test_resource(
    "oci_archive.py", "infra/remote-execution/scripts/oci_archive.py"
)
UBUNTU_REFERENCE = "docker.io/library/ubuntu@sha256:" + "2260313b31c8c011cd2eebe728008efac1b3982be73eb71348ea2648d2c0e09b"
NATIVELINK_REFERENCE = "ghcr.io/tracemachina/nativelink@sha256:" + "5c2e6eca51c6d3ac40b94f703e08a243fd036cc136cc858a99040ca90fa57d61"
MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
CONFIG_MEDIA_TYPE = "application/vnd.oci.image.config.v1+json"
LAYER_MEDIA_TYPE = "application/vnd.oci.image.layer.v1.tar"


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


def json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def add_tar_file(archive: tarfile.TarFile, name: str, value: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(value)
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.mode = 0o644
    archive.addfile(info, io.BytesIO(value))


def image_payload(image: str, architecture: str) -> dict[str, object]:
    layer = "{} {} test layer".format(image, architecture).encode("utf-8")
    config = json_bytes({"architecture": architecture, "os": "linux"})
    manifest = json_bytes(
        {
            "config": {
                "digest": sha256_digest(config),
                "mediaType": CONFIG_MEDIA_TYPE,
                "size": len(config),
            },
            "layers": [
                {
                    "digest": sha256_digest(layer),
                    "mediaType": LAYER_MEDIA_TYPE,
                    "size": len(layer),
                }
            ],
            "mediaType": MANIFEST_MEDIA_TYPE,
            "schemaVersion": 2,
        }
    )
    descriptor = {
        "digest": sha256_digest(manifest),
        "mediaType": MANIFEST_MEDIA_TYPE,
        "platform": {"architecture": architecture, "os": "linux"},
        "size": len(manifest),
    }
    return {
        "config": config,
        "descriptor": descriptor,
        "layer": layer,
        "manifest": manifest,
    }


def write_oci_archive(
    path: Path,
    payload: dict[str, object],
    parent: bytes,
    reference: str,
    *,
    preserve_parent: bool,
) -> dict[str, object]:
    selected = payload["descriptor"]
    if preserve_parent:
        root = {
            "annotations": {"org.opencontainers.image.ref.name": reference},
            "digest": sha256_digest(parent),
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "size": len(parent),
        }
    else:
        root = selected
    index = json_bytes(
        {
            "manifests": [root],
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "schemaVersion": 2,
        }
    )
    manifest = payload["manifest"]
    config = payload["config"]
    layer = payload["layer"]
    assert isinstance(manifest, bytes)
    assert isinstance(config, bytes)
    assert isinstance(layer, bytes)
    with tarfile.open(path, "w") as archive:
        add_tar_file(archive, "oci-layout", json_bytes({"imageLayoutVersion": "1.0.0"}))
        add_tar_file(archive, "index.json", index)
        add_tar_file(
            archive,
            "blobs/sha256/" + sha256_digest(parent).split(":")[1],
            parent,
        )
        add_tar_file(archive, "blobs/sha256/" + sha256_digest(manifest).split(":")[1], manifest)
        add_tar_file(archive, "blobs/sha256/" + sha256_digest(config).split(":")[1], config)
        add_tar_file(archive, "blobs/sha256/" + sha256_digest(layer).split(":")[1], layer)
    return {
        "manifest": {
            "digest": sha256_digest(manifest),
            "media_type": MANIFEST_MEDIA_TYPE,
            "size": len(manifest),
        },
        "archive": {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size": path.stat().st_size,
        },
        "reference": reference,
    }


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
        shutil.copy2(ARCHIVE_TOOL, scripts / ARCHIVE_TOOL.name)
        for name in ("control.json5", "worker-x86_64.json5", "worker-aarch64.json5"):
            (nativelink / name).write_text("{}\n", encoding="utf-8")
        (nativelink / "nativelink.service").write_text("[Service]\n", encoding="utf-8")
        self.archives: dict[str, dict[str, Path]] = {}
        self.references: dict[str, str] = {}
        images = {}
        for image, stem in (
            ("ubuntu", "ubuntu-2604"),
            ("nativelink", "nativelink-166"),
        ):
            self.archives[image] = {}
            payloads = {
                target: image_payload(image, oci_architecture)
                for target, oci_architecture in (
                    ("x86_64", "amd64"),
                    ("aarch64", "arm64"),
                )
            }
            parent = json_bytes(
                {
                    "manifests": [
                        payloads["x86_64"]["descriptor"],
                        payloads["aarch64"]["descriptor"],
                    ],
                    "mediaType": "application/vnd.oci.image.index.v1+json",
                    "schemaVersion": 2,
                }
            )
            reference = "registry.example/{}@{}".format(image, sha256_digest(parent))
            self.references[image] = reference
            platforms = {}
            for target, oci_architecture in (("x86_64", "amd64"), ("aarch64", "arm64")):
                archive = self.external / "{}-{}.oci.tar".format(stem, target)
                self.archives[image][target] = archive
                archive_record = write_oci_archive(
                    archive,
                    payloads[target],
                    parent,
                    reference,
                    preserve_parent=target == "x86_64",
                )
                platforms[target] = {
                    "os": "linux",
                    "architecture": oci_architecture,
                    "manifest": archive_record["manifest"],
                    "archive": {
                        "filename": "{}-{}.oci.tar".format(stem, target),
                        **archive_record["archive"],
                    },
                }
            images[image] = {"reference": reference, "platforms": platforms}
        self.ubuntu_reference = self.references["ubuntu"]
        self.nativelink_reference = self.references["nativelink"]
        self.ubuntu_archive = self.archives["ubuntu"]["x86_64"]
        self.nativelink_archive = self.archives["nativelink"]["x86_64"]
        (sdme / "offline-oci-archives.json").write_text(
            json.dumps({"schema_version": 1, "images": images}, indent=2) + "\n",
            encoding="utf-8",
        )
        (nativelink / "deployment.json").write_text(
            json.dumps(
                {
                    "image": {
                        "version": "v1.6.6",
                        "reference": self.nativelink_reference,
                    }
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (scripts / "preflight-worker.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        (scripts / "preflight-worker.sh").chmod(0o755)
        (scripts / "preflight_worker.py").write_text("# probe\n", encoding="utf-8")
        (tools / "_isolation.py").write_text("# isolation\n", encoding="utf-8")
        (tools / "_rpm.py").write_text("# rpm\n", encoding="utf-8")
        (tools / "nativelink_config.py").write_text("# validator\n", encoding="utf-8")
        self.script = scripts / SCRIPT.name
        script = self.script.read_text(encoding="utf-8")
        self.script.write_text(
            script.replace(UBUNTU_REFERENCE, self.ubuntu_reference).replace(
                NATIVELINK_REFERENCE, self.nativelink_reference
            ),
            encoding="utf-8",
        )
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

    def safe_runtime_data_root(self) -> Path:
        temporary = tempfile.TemporaryDirectory(
            prefix="buckos-sdme-test-", dir="/var/lib"
        )
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name) / "data"

    def install_fake_runtime_tools(self, architecture: str) -> Path:
        fake_bin = self.external / "fake-bin"
        fake_bin.mkdir(mode=0o755)
        log = self.external / "commands.log"
        state = self.external / "fake-sdme-state"
        state.mkdir(mode=0o700)
        quoted_log = shlex.quote(str(log))
        quoted_state = shlex.quote(str(state))
        quoted_ubuntu = shlex.quote(str(self.archives["ubuntu"][architecture]))
        quoted_nativelink = shlex.quote(str(self.archives["nativelink"][architecture]))
        (fake_bin / "sdme").write_text(
            """#!/bin/sh
set -eu
printf 'sdme %s\\n' "$*" >> {log}
if [ "$1" = fs ] && [ "$2" = ls ]; then
  first=1
  printf '['
  for record in {state}/*.fs; do
    [ -e "$record" ] || continue
    name=$(basename "$record" .fs)
    if [ "$first" -eq 0 ]; then printf ','; fi
    printf '{{"name":"%s"}}' "$name"
    first=0
  done
  printf ']\\n'
elif [ "$1" = fs ] && [ "$2" = import ]; then
  name=
  shift 2
  while [ "$#" -gt 0 ]; do
    if [ "$1" = --name ]; then
      shift
      name=$1
    fi
    shift
  done
  : > {state}/"$name".fs
elif [ "$1" = fs ] && [ "$2" = build ]; then
  name=$3
  : > {state}/"$name".fs
  printf '%s\\n' \\
    'ubuntu_image={ubuntu_reference}' \\
    'nativelink_image={nativelink_reference}' \\
    'architecture={architecture}' > {state}/"$name".runtime-images
elif [ "$1" = cp ]; then
  source=$2
  destination=$3
  case "$source" in
    fs:*)
      remote=$(printf '%s' "$source" | cut -c4-)
      fs=$(printf '%s' "$remote" | cut -d: -f1)
      path=$(printf '%s' "$remote" | cut -d: -f2-)
      base=$(basename "$path")
      cp {state}/"$fs.$base" "$destination/$base"
      ;;
    *)
      remote=$(printf '%s' "$destination" | cut -c4-)
      fs=$(printf '%s' "$remote" | cut -d: -f1)
      path=$(printf '%s' "$remote" | cut -d: -f2-)
      base=$(basename "$path")
      cp "$source" {state}/"$fs.$base"
      ;;
  esac
fi
""".format(
                log=quoted_log,
                state=quoted_state,
                architecture=architecture,
                ubuntu_reference=self.ubuntu_reference,
                nativelink_reference=self.nativelink_reference,
            ),
            encoding="utf-8",
        )
        (fake_bin / "podman").write_text(
            """#!/bin/sh
set -eu
printf 'podman %s\\n' "$*" >> {log}
output=
image=
while [ "$#" -gt 0 ]; do
  if [ "$1" = --output ]; then
    shift
    output=$1
  else
    image=$1
  fi
  shift
done
if [ -n "$output" ]; then
  case "$image" in
    */ubuntu@*) cp {ubuntu} "$output" ;;
    */nativelink@*) cp {nativelink} "$output" ;;
    *) exit 2 ;;
  esac
fi
""".format(
                log=quoted_log,
                ubuntu=quoted_ubuntu,
                nativelink=quoted_nativelink,
            ),
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
        self.assertIn(self.ubuntu_reference, result.stdout)
        self.assertIn(self.nativelink_reference, result.stdout)
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
        data_root = self.safe_runtime_data_root()

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

    def test_prepare_runtime_accepts_offline_archives_without_podman(self) -> None:
        architecture = {"amd64": "x86_64", "arm64": "aarch64"}.get(
            platform.machine(), platform.machine()
        )
        if architecture not in ("x86_64", "aarch64"):
            self.skipTest("unsupported test architecture")
        log = self.install_fake_runtime_tools(architecture)

        result = self.run_script(
            "prepare-runtime",
            "worker",
            "--arch",
            architecture,
            "--data-root",
            str(self.safe_runtime_data_root()),
            "--ubuntu-oci-archive",
            str(self.archives["ubuntu"][architecture]),
            "--nativelink-oci-archive",
            str(self.archives["nativelink"][architecture]),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        commands = log.read_text(encoding="utf-8")
        self.assertNotIn("podman ", commands)
        self.assertIn("sdme fs import", commands)
        self.assertIn("sdme fs build {}".format(RUNTIME_FS), commands)

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
        self.assertIn("fs:buckos-re-nativelink-5c2e6eca51c6:/bin/nativelink", rootfs)
        self.assertNotIn("/oci/apps/", rootfs)
        self.assertLess(
            rootfs.index("rm -rf /nix /opt/buckos-re/image-bin"),
            rootfs.index('test "$(/usr/bin/nativelink --version)"'),
        )
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

    def test_offline_plan_validates_both_archives_without_podman(self) -> None:
        result = self.run_script(
            "plan",
            "control",
            "--arch",
            "x86_64",
            "--data-root",
            str(self.external / "data"),
            "--ubuntu-oci-archive",
            str(self.ubuntu_archive),
            "--nativelink-oci-archive",
            str(self.nativelink_archive),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("podman pull", result.stdout)
        self.assertNotIn("podman save", result.stdout)
        self.assertIn(str(self.ubuntu_archive), result.stdout)
        self.assertIn(str(self.nativelink_archive), result.stdout)
        self.assertIn("buckos-re-image-provenance.json", result.stdout)
        self.assertFalse((self.external / "data").exists())

    def test_worker_plan_propagates_offline_archives_to_bootstrap_steps(self) -> None:
        result = self.run_script(
            *self.worker_arguments(),
            "--ubuntu-oci-archive",
            str(self.ubuntu_archive),
            "--nativelink-oci-archive",
            str(self.nativelink_archive),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        prepare_line = next(
            line for line in result.stdout.splitlines() if "prepare-runtime worker" in line
        )
        apply_line = next(
            line for line in result.stdout.splitlines() if "apply worker" in line
        )
        for line in (prepare_line, apply_line):
            self.assertIn("--ubuntu-oci-archive", line)
            self.assertIn(str(self.ubuntu_archive), line)
            self.assertIn("--nativelink-oci-archive", line)
            self.assertIn(str(self.nativelink_archive), line)

    def test_offline_plan_rejects_wrong_architecture(self) -> None:
        result = self.run_script(
            "plan",
            "control",
            "--arch",
            "aarch64",
            "--data-root",
            str(self.external / "data"),
            "--ubuntu-oci-archive",
            str(self.ubuntu_archive),
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("archive filename mismatch", result.stderr)

    def test_offline_plan_rejects_unsafe_source_path(self) -> None:
        unsafe = self.external / "unsafe"
        unsafe.mkdir(mode=0o777)
        unsafe.chmod(0o777)
        archive = unsafe / self.ubuntu_archive.name
        shutil.copy2(self.ubuntu_archive, archive)
        result = self.run_script(
            "plan",
            "control",
            "--arch",
            "x86_64",
            "--data-root",
            str(self.external / "data"),
            "--ubuntu-oci-archive",
            str(archive),
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("group/world-writable path component", result.stderr)

    def test_plan_rejects_incomplete_managed_cache(self) -> None:
        images = self.external / "data/images"
        images.mkdir(parents=True)
        shutil.copy2(self.ubuntu_archive, images / self.ubuntu_archive.name)
        result = self.run_script(
            "plan",
            "control",
            "--arch",
            "x86_64",
            "--data-root",
            str(self.external / "data"),
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("incomplete cached OCI archive pair", result.stderr)

    def test_plan_rejects_dangling_cached_archive_symlink(self) -> None:
        images = self.external / "data/images"
        images.mkdir(parents=True)
        (images / self.ubuntu_archive.name).symlink_to("missing-archive")
        result = self.run_script(
            "plan",
            "control",
            "--arch",
            "x86_64",
            "--data-root",
            str(self.external / "data"),
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("unsafe or legacy cached OCI archive state", result.stderr)

    def test_offline_plan_reuses_valid_managed_cache(self) -> None:
        images = self.external / "data/images"
        images.mkdir(parents=True)
        cached = images / self.ubuntu_archive.name
        shutil.copy2(self.ubuntu_archive, cached)
        provenance = oci_archive.verify_archive(
            self.repo / "infra/remote-execution/sdme/offline-oci-archives.json",
            "ubuntu",
            "x86_64",
            self.ubuntu_reference,
            cached,
            "offline",
        )
        cached.with_name(cached.name + ".provenance.json").write_text(
            json.dumps(provenance, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        result = self.run_script(
            "plan",
            "control",
            "--arch",
            "x86_64",
            "--data-root",
            str(self.external / "data"),
            "--ubuntu-oci-archive",
            str(self.ubuntu_archive),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Reuse validated ubuntu OCI archive", result.stdout)
        self.assertNotIn(self.ubuntu_reference, result.stdout)

    def test_rejects_duplicate_offline_archive_option(self) -> None:
        result = self.run_script(
            "plan",
            "control",
            "--arch",
            "x86_64",
            "--data-root",
            str(self.external / "data"),
            "--ubuntu-oci-archive",
            str(self.ubuntu_archive),
            "--ubuntu-oci-archive",
            str(self.ubuntu_archive),
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("may be supplied only once", result.stderr)

    def test_rejects_empty_offline_archive_option(self) -> None:
        result = self.run_script(
            "plan",
            "control",
            "--arch",
            "x86_64",
            "--data-root",
            str(self.external / "data"),
            "--ubuntu-oci-archive",
            "",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("must not be empty", result.stderr)


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
