#!/usr/bin/env python3

import hashlib
import io
import json
import os
import platform
import secrets
import signal
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time
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

    def script_environment(self, overrides: dict[str, str] | None = None) -> dict[str, str]:
        environment = os.environ.copy()
        for name in (
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "no_proxy",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
        ):
            environment.pop(name, None)
        if overrides:
            environment.update(overrides)
        return environment

    def run_script(
        self, *arguments: str, environment: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.script), *arguments],
            check=False,
            env=self.script_environment(environment),
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

    def install_fake_runtime_tools(
        self,
        architecture: str,
        *,
        fail_archive_publish: bool = False,
        fail_image_provenance: bool = False,
        fail_runtime_provenance: bool = False,
        fail_runtime_build: bool = False,
        fail_after_runtime_provenance: bool = False,
        leak_after_runtime_provenance: bool = False,
    ) -> Path:
        fake_bin = self.external / "fake-bin"
        fake_bin.mkdir(mode=0o755)
        log = self.external / "commands.log"
        state = self.external / "fake-sdme-state"
        state.mkdir(mode=0o700)
        quoted_log = shlex.quote(str(log))
        quoted_state = shlex.quote(str(state))
        quoted_ubuntu = shlex.quote(str(self.archives["ubuntu"][architecture]))
        quoted_nativelink = shlex.quote(str(self.archives["nativelink"][architecture]))
        quoted_image_failure = shlex.quote(str(self.external / "image-provenance-failed"))
        quoted_runtime_failure = shlex.quote(str(self.external / "runtime-provenance-failed"))
        quoted_build_failure = shlex.quote(str(self.external / "runtime-build-failed"))
        quoted_post_provenance_failure = shlex.quote(
            str(self.external / "post-runtime-provenance-failed")
        )
        (fake_bin / "sdme").write_text(
            """#!/bin/sh
set -eu
printf 'sdme %s\\n' "$*" >> {log}
if [ -n "${{FAKE_SDME_BLOCK_READY:-}}" ] && [ ! -e "$FAKE_SDME_BLOCK_READY" ]; then
  : > "$FAKE_SDME_BLOCK_READY"
  while [ ! -e "$FAKE_SDME_BLOCK_RELEASE" ]; do /bin/sleep 0.05; done
fi
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
elif [ "$1" = fs ] && [ "$2" = export ]; then
  if [ "${{FAKE_INSPECTION_ERROR:-0}}" -eq 1 ]; then exit 1; fi
  name=${{3#fs:}}
  output=$4
  export_root={state}/export-root.$$
  /bin/rm -rf "$export_root"
  mkdir -p "$export_root/etc"
  if [ -e {state}/"$name".proxy-path ]; then
    cp {state}/"$name".proxy-path "$export_root/etc/buckos-re-build-proxy.env"
  fi
  if [ -e {state}/"$name".proxy-sentinel ]; then
    mkdir -p "$export_root/opt/unexpected"
    cp {state}/"$name".proxy-sentinel "$export_root/opt/unexpected/build-note"
  fi
  /bin/tar -cf "$output" -C "$export_root" .
  /bin/rm -rf "$export_root"
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
elif [ "$1" = fs ] && [ "$2" = rm ]; then
  name=
  for argument in "$@"; do name=$argument; done
  /bin/rm -f {state}/"$name".*
elif [ "$1" = fs ] && [ "$2" = build ]; then
  name=$3
  build_config=$4
  case " $* " in
    *" --no-cache "*) /bin/rm -f {state}/"$name".proxy-path {state}/"$name".proxy-sentinel ;;
  esac
  if [ -n "${{FAKE_EXPECT_PROXY_SECRET:-}}" ]; then
    proxy_source=$(awk '$1 == "COPY" && $3 == "/etc/buckos-re-build-proxy.env" {{ print $2 }}' "$build_config")
    [ -n "$proxy_source" ]
    case "$proxy_source" in */provision/transactions/*) ;; *) exit 2 ;; esac
    [ -f "$proxy_source" ] && [ ! -L "$proxy_source" ]
    [ "$(stat -c '%a' "$proxy_source")" = 600 ]
    [ "$(stat -c '%a' "$build_config")" = 600 ]
    [ "$(stat -c '%a' "$(dirname "$proxy_source")")" = 700 ]
    [ "$(stat -c '%u' "$proxy_source")" = 0 ]
    [ "$(stat -c '%u' "$build_config")" = 0 ]
    python3 - "$proxy_source" "$build_config" <<'PY'
import json
import os
import shlex
import sys

proxy_path, build_path = sys.argv[1:]
values = {{}}
with open(proxy_path, encoding="utf-8") as stream:
    for line in stream:
        if line.startswith("#"):
            continue
        item = shlex.split(line)[0]
        name, separator, value = item.partition("=")
        if not separator:
            raise SystemExit(2)
        values[name] = value
expected_name = os.environ.get("FAKE_EXPECT_PROXY_NAME")
expected_value = os.environ["FAKE_EXPECT_PROXY_SECRET"]
expected_names = set(filter(None, os.environ.get("FAKE_EXPECT_PROXY_NAMES", "").split(",")))
expected_values = os.environ.get("FAKE_EXPECT_PROXY_VALUES")
if expected_value not in values.values():
    raise SystemExit(2)
if expected_name and values.get(expected_name) != expected_value:
    raise SystemExit(2)
if expected_names and set(values) != expected_names:
    raise SystemExit(2)
if expected_values and values != json.loads(expected_values):
    raise SystemExit(2)
if os.environ.get("FAKE_UNRELATED_SECRET") in values.values():
    raise SystemExit(2)
if expected_value.encode() in open(build_path, "rb").read():
    raise SystemExit(2)
PY
    case "${{FAKE_BUILD_LEAK_PROXY:-}}" in
      known) cp "$proxy_source" {state}/"$name".proxy-path ;;
      unexpected) cp "$proxy_source" {state}/"$name".proxy-sentinel ;;
      value) printf '%s\\n' "$FAKE_EXPECT_PROXY_SECRET" > {state}/"$name".proxy-sentinel ;;
    esac
  fi
  if [ -n "${{FAKE_SDME_BLOCK_BUILD_READY:-}}" ] && \
     [ ! -e "$FAKE_SDME_BLOCK_BUILD_READY" ]; then
    : > "$FAKE_SDME_BLOCK_BUILD_READY"
    while [ ! -e "$FAKE_SDME_BLOCK_BUILD_RELEASE" ]; do /bin/sleep 0.05; done
  fi
  if [ {fail_runtime_build} -eq 1 ] && [ ! -e {build_failure} ]; then
    : > {build_failure}
    exit 1
  fi
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
      if [ "$base" = buckos-re-image-provenance.json ] && \
         [ {fail_image_provenance} -eq 1 ] && [ ! -e {image_failure} ]; then
        : > {image_failure}
        exit 1
      fi
      if [ "$base" = buckos-re-runtime-provenance.json ] && \
         [ {fail_runtime_provenance} -eq 1 ] && [ ! -e {runtime_failure} ]; then
        : > {runtime_failure}
        exit 1
      fi
      cp "$source" {state}/"$fs.$base"
      if [ "$base" = buckos-re-runtime-provenance.json ] && \
         [ {fail_after_runtime_provenance} -eq 1 ] && \
         [ ! -e {post_provenance_failure} ]; then
        : > {post_provenance_failure}
        exit 1
      fi
      if [ "$base" = buckos-re-runtime-provenance.json ] && \
         [ {leak_after_runtime_provenance} -eq 1 ] && \
         [ ! -e {post_provenance_failure} ]; then
        printf '%s\\n' buckos-sdme-proxy-transport-v1 > {state}/"$fs".proxy-sentinel
        : > {post_provenance_failure}
        exit 1
      fi
      ;;
  esac
fi
""".format(
                log=quoted_log,
                state=quoted_state,
                architecture=architecture,
                ubuntu_reference=self.ubuntu_reference,
                nativelink_reference=self.nativelink_reference,
                fail_image_provenance=int(fail_image_provenance),
                fail_runtime_provenance=int(fail_runtime_provenance),
                fail_runtime_build=int(fail_runtime_build),
                image_failure=quoted_image_failure,
                runtime_failure=quoted_runtime_failure,
                build_failure=quoted_build_failure,
                fail_after_runtime_provenance=int(fail_after_runtime_provenance),
                leak_after_runtime_provenance=int(leak_after_runtime_provenance),
                post_provenance_failure=quoted_post_provenance_failure,
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
        if fail_archive_publish:
            failure_marker = shlex.quote(str(self.external / "archive-mv-failed"))
            (fake_bin / "mv").write_text(
                """#!/bin/sh
set -eu
if [ "$1" = -- ]; then shift; fi
source=$1
if [ ! -e {marker} ]; then
  case "$source" in
    *.provenance.json.tmp.*)
      : > {marker}
      exit 1
      ;;
  esac
fi
exec /bin/mv -- "$@"
""".format(marker=failure_marker),
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

    def native_architecture(self) -> str:
        architecture = {"amd64": "x86_64", "arm64": "aarch64"}.get(
            platform.machine(), platform.machine()
        )
        if architecture not in ("x86_64", "aarch64"):
            self.skipTest("unsupported test architecture")
        return architecture

    def runtime_arguments(self, data_root: Path, architecture: str) -> list[str]:
        return [
            "prepare-runtime",
            "worker",
            "--arch",
            architecture,
            "--data-root",
            str(data_root),
            "--ubuntu-oci-archive",
            str(self.archives["ubuntu"][architecture]),
            "--nativelink-oci-archive",
            str(self.archives["nativelink"][architecture]),
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
        build_line = next(
            line
            for line in commands.splitlines()
            if "sdme fs build {}".format(RUNTIME_FS) in line
        )
        self.assertIn("infra/remote-execution/sdme/worker-rootfs.sdme", build_line)
        self.assertNotIn("--no-cache", build_line)

    def test_prepare_runtime_recovers_archive_publication_with_transaction(self) -> None:
        architecture = {"amd64": "x86_64", "arm64": "aarch64"}.get(
            platform.machine(), platform.machine()
        )
        if architecture not in ("x86_64", "aarch64"):
            self.skipTest("unsupported test architecture")
        self.install_fake_runtime_tools(architecture, fail_archive_publish=True)
        data_root = self.safe_runtime_data_root()
        arguments = (
            "prepare-runtime",
            "worker",
            "--arch",
            architecture,
            "--data-root",
            str(data_root),
            "--ubuntu-oci-archive",
            str(self.archives["ubuntu"][architecture]),
            "--nativelink-oci-archive",
            str(self.archives["nativelink"][architecture]),
        )

        first = self.run_script(*arguments)

        ubuntu_cache = data_root / "images/ubuntu-2604-{}.oci.tar".format(
            architecture
        )
        ubuntu_provenance = ubuntu_cache.with_name(
            ubuntu_cache.name + ".provenance.json"
        )
        self.assertNotEqual(first.returncode, 0)
        self.assertTrue(ubuntu_cache.is_file())
        self.assertFalse(ubuntu_provenance.exists())
        self.assertTrue(
            (
                data_root
                / "provision/transactions/archive-ubuntu.transaction"
            ).is_file()
        )
        transaction_text = (
            data_root / "provision/transactions/archive-ubuntu.transaction"
        ).read_text(encoding="utf-8")
        self.assertIn("object_sha256=", transaction_text)
        self.assertIn("phase=publishing", transaction_text)

        second = self.run_script(*arguments)

        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertTrue(ubuntu_cache.is_file())
        self.assertTrue(ubuntu_provenance.is_file())
        self.assertFalse(
            (
                data_root
                / "provision/transactions/archive-ubuntu.transaction"
            ).exists()
        )

    def test_prepare_runtime_rejects_archive_without_provenance_or_transaction(self) -> None:
        architecture = {"amd64": "x86_64", "arm64": "aarch64"}.get(
            platform.machine(), platform.machine()
        )
        if architecture not in ("x86_64", "aarch64"):
            self.skipTest("unsupported test architecture")
        self.install_fake_runtime_tools(architecture)
        data_root = self.safe_runtime_data_root()
        images = data_root / "images"
        images.mkdir(parents=True)
        ubuntu_cache = images / "ubuntu-2604-{}.oci.tar".format(architecture)
        shutil.copy2(self.archives["ubuntu"][architecture], ubuntu_cache)

        result = self.run_script(
            "prepare-runtime",
            "worker",
            "--arch",
            architecture,
            "--data-root",
            str(data_root),
            "--ubuntu-oci-archive",
            str(self.archives["ubuntu"][architecture]),
            "--nativelink-oci-archive",
            str(self.archives["nativelink"][architecture]),
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("without a matching transaction", result.stderr)
        self.assertFalse(
            ubuntu_cache.with_name(ubuntu_cache.name + ".provenance.json").exists()
        )

    def test_archive_recovery_rejects_invalid_transaction_records(self) -> None:
        architecture = self.native_architecture()
        self.install_fake_runtime_tools(architecture, fail_archive_publish=True)
        failure_marker = self.external / "archive-mv-failed"
        cases = (
            ("mismatched", "does not match"),
            ("malformed", "does not match"),
            ("symlinked", "is a symlink"),
            ("unsafe", "group/world-writable"),
        )
        for mutation, expected_error in cases:
            with self.subTest(mutation=mutation):
                failure_marker.unlink(missing_ok=True)
                data_root = self.safe_runtime_data_root()
                arguments = self.runtime_arguments(data_root, architecture)
                first = self.run_script(*arguments)
                self.assertNotEqual(first.returncode, 0)
                transaction = (
                    data_root
                    / "provision/transactions/archive-ubuntu.transaction"
                )
                self.assertTrue(transaction.is_file())
                if mutation == "mismatched":
                    transaction.write_text(
                        transaction.read_text(encoding="utf-8").replace(
                            "intent_sha256=", "intent_sha256=" + "0" * 64 + "#"
                        ),
                        encoding="utf-8",
                    )
                elif mutation == "malformed":
                    transaction.write_text("not-a-transaction\n", encoding="utf-8")
                elif mutation == "symlinked":
                    transaction.unlink()
                    target = self.external / "transaction-target"
                    target.write_text("not-a-transaction\n", encoding="utf-8")
                    transaction.symlink_to(target)
                else:
                    transaction.chmod(0o666)

                retry = self.run_script(*arguments)

                self.assertEqual(retry.returncode, 2)
                self.assertIn(expected_error, retry.stderr)

    def test_archive_recovery_rejects_both_acquisition_mode_changes(self) -> None:
        architecture = self.native_architecture()
        log = self.install_fake_runtime_tools(architecture, fail_archive_publish=True)
        failure_marker = self.external / "archive-mv-failed"

        offline_root = self.safe_runtime_data_root()
        offline_arguments = self.runtime_arguments(offline_root, architecture)
        first_offline = self.run_script(*offline_arguments)
        self.assertNotEqual(first_offline.returncode, 0)
        podman_before = (
            log.read_text(encoding="utf-8").count("podman ") if log.exists() else 0
        )
        offline_to_registry = self.run_script(
            "prepare-runtime",
            "worker",
            "--arch",
            architecture,
            "--data-root",
            str(offline_root),
            "--nativelink-oci-archive",
            str(self.archives["nativelink"][architecture]),
        )
        self.assertEqual(offline_to_registry.returncode, 2)
        self.assertIn("transaction record does not match", offline_to_registry.stderr)
        self.assertEqual(
            podman_before,
            log.read_text(encoding="utf-8").count("podman ") if log.exists() else 0,
        )

        failure_marker.unlink(missing_ok=True)
        registry_root = self.safe_runtime_data_root()
        registry_arguments = [
            "prepare-runtime",
            "worker",
            "--arch",
            architecture,
            "--data-root",
            str(registry_root),
            "--nativelink-oci-archive",
            str(self.archives["nativelink"][architecture]),
        ]
        first_registry = self.run_script(*registry_arguments)
        self.assertNotEqual(first_registry.returncode, 0)
        podman_before = (
            log.read_text(encoding="utf-8").count("podman ") if log.exists() else 0
        )
        registry_to_offline = self.run_script(
            *self.runtime_arguments(registry_root, architecture)
        )
        self.assertEqual(registry_to_offline.returncode, 2)
        self.assertIn("transaction record does not match", registry_to_offline.stderr)
        self.assertEqual(
            podman_before,
            log.read_text(encoding="utf-8").count("podman ") if log.exists() else 0,
        )

    def test_image_filesystem_publication_recovers_only_with_transaction(self) -> None:
        architecture = self.native_architecture()
        log = self.install_fake_runtime_tools(
            architecture, fail_image_provenance=True
        )
        data_root = self.safe_runtime_data_root()
        arguments = self.runtime_arguments(data_root, architecture)

        first = self.run_script(*arguments)

        transaction = (
            data_root
            / "provision/transactions/image-buckos-re-ubuntu-2260313b31c8.transaction"
        )
        self.assertNotEqual(first.returncode, 0)
        self.assertTrue(transaction.is_file())

        second = self.run_script(*arguments)

        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertFalse(transaction.exists())
        self.assertIn(
            "sdme fs rm -f buckos-re-ubuntu-2260313b31c8",
            log.read_text(encoding="utf-8"),
        )

    def test_image_filesystem_publication_rejects_missing_transaction(self) -> None:
        architecture = self.native_architecture()
        log = self.install_fake_runtime_tools(
            architecture, fail_image_provenance=True
        )
        data_root = self.safe_runtime_data_root()
        arguments = self.runtime_arguments(data_root, architecture)
        first = self.run_script(*arguments)
        self.assertNotEqual(first.returncode, 0)
        transaction = (
            data_root
            / "provision/transactions/image-buckos-re-ubuntu-2260313b31c8.transaction"
        )
        transaction.unlink()

        retry = self.run_script(*arguments)

        self.assertEqual(retry.returncode, 2)
        self.assertIn("rootfs lacks image provenance", retry.stderr)
        self.assertNotIn(
            "sdme fs rm -f buckos-re-ubuntu-2260313b31c8",
            log.read_text(encoding="utf-8"),
        )

    def test_image_filesystem_publication_rejects_mismatched_transaction(self) -> None:
        architecture = self.native_architecture()
        self.install_fake_runtime_tools(architecture, fail_image_provenance=True)
        data_root = self.safe_runtime_data_root()
        arguments = self.runtime_arguments(data_root, architecture)
        first = self.run_script(*arguments)
        self.assertNotEqual(first.returncode, 0)
        transaction = (
            data_root
            / "provision/transactions/image-buckos-re-ubuntu-2260313b31c8.transaction"
        )
        transaction.write_text(
            transaction.read_text(encoding="utf-8").replace(
                "intent_sha256=", "intent_sha256=" + "0" * 64 + "#"
            ),
            encoding="utf-8",
        )

        retry = self.run_script(*arguments)

        self.assertEqual(retry.returncode, 2)
        self.assertIn("transaction record does not match", retry.stderr)

    def test_runtime_publication_recovers_only_with_transaction(self) -> None:
        architecture = self.native_architecture()
        log = self.install_fake_runtime_tools(
            architecture, fail_runtime_provenance=True
        )
        data_root = self.safe_runtime_data_root()
        arguments = self.runtime_arguments(data_root, architecture)

        first = self.run_script(*arguments)

        transaction = (
            data_root
            / "provision/transactions/runtime-{}.transaction".format(RUNTIME_FS)
        )
        self.assertNotEqual(first.returncode, 0)
        self.assertTrue(transaction.is_file())

        second = self.run_script(*arguments)

        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertFalse(transaction.exists())
        commands = log.read_text(encoding="utf-8")
        self.assertIn("sdme fs rm -f {}".format(RUNTIME_FS), commands)
        self.assertIn("--no-cache", commands)

    def test_runtime_publication_rejects_missing_transaction(self) -> None:
        architecture = self.native_architecture()
        log = self.install_fake_runtime_tools(
            architecture, fail_runtime_provenance=True
        )
        data_root = self.safe_runtime_data_root()
        arguments = self.runtime_arguments(data_root, architecture)
        first = self.run_script(*arguments)
        self.assertNotEqual(first.returncode, 0)
        transaction = (
            data_root
            / "provision/transactions/runtime-{}.transaction".format(RUNTIME_FS)
        )
        transaction.unlink()

        retry = self.run_script(*arguments)

        self.assertEqual(retry.returncode, 2)
        self.assertIn("runtime rootfs lacks strict provenance", retry.stderr)
        self.assertNotIn(
            "sdme fs rm -f {}".format(RUNTIME_FS),
            log.read_text(encoding="utf-8"),
        )

    def test_runtime_publication_rejects_malformed_transaction(self) -> None:
        architecture = self.native_architecture()
        self.install_fake_runtime_tools(
            architecture, fail_runtime_provenance=True
        )
        data_root = self.safe_runtime_data_root()
        arguments = self.runtime_arguments(data_root, architecture)
        first = self.run_script(*arguments)
        self.assertNotEqual(first.returncode, 0)
        transaction = (
            data_root
            / "provision/transactions/runtime-{}.transaction".format(RUNTIME_FS)
        )
        transaction.write_text("not-a-transaction\n", encoding="utf-8")

        retry = self.run_script(*arguments)

        self.assertEqual(retry.returncode, 2)
        self.assertIn("transaction record does not match", retry.stderr)

    def test_proxy_runtime_build_contains_secrets_and_cleans_transaction(self) -> None:
        architecture = self.native_architecture()
        log = self.install_fake_runtime_tools(architecture)
        data_root = self.safe_runtime_data_root()
        secret = "http://builder:p'{}$()@proxy.example:8443".format(
            secrets.token_hex(12)
        )
        environment = {
            "HTTPS_PROXY": secret,
            "NO_PROXY": "localhost,127.0.0.1",
            "FAKE_EXPECT_PROXY_SECRET": secret,
            "FAKE_EXPECT_PROXY_NAME": "HTTPS_PROXY",
            "FAKE_EXPECT_PROXY_NAMES": "HTTPS_PROXY,NO_PROXY",
            "FAKE_UNRELATED_SECRET": "must-not-be-copied",
        }

        result = self.run_script(
            *self.runtime_arguments(data_root, architecture),
            "-v",
            environment=environment,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        commands = log.read_text(encoding="utf-8")
        self.assertIn("transactions/runtime-{}".format(RUNTIME_FS), commands)
        self.assertNotIn(secret, result.stdout + result.stderr + commands)
        provenance_copy = "sdme cp "
        provenance_destination = "fs:{}:/etc/buckos-re-runtime-provenance.json".format(
            RUNTIME_FS
        )
        provenance_position = next(
            index
            for index, line in enumerate(commands.splitlines())
            if provenance_copy in line and provenance_destination in line
        )
        inspection_positions = [
            index
            for index, line in enumerate(commands.splitlines())
            if "sdme fs export fs:{}".format(RUNTIME_FS) in line
        ]
        self.assertEqual(len(inspection_positions), 2)
        self.assertLess(inspection_positions[0], provenance_position)
        self.assertGreater(inspection_positions[1], provenance_position)
        transaction_dir = data_root / "provision/transactions"
        self.assertEqual(list(transaction_dir.iterdir()), [])
        state = self.external / "fake-sdme-state"
        for path in state.iterdir():
            self.assertNotIn(secret, path.read_text(encoding="utf-8"))

    def test_proxy_runtime_build_resumes_identical_failed_transaction(self) -> None:
        architecture = self.native_architecture()
        log = self.install_fake_runtime_tools(architecture, fail_runtime_build=True)
        data_root = self.safe_runtime_data_root()
        secret = "http://builder:{}@proxy.example:8080".format(
            secrets.token_hex(12)
        )
        environment = {
            "HTTP_PROXY": secret,
            "FAKE_EXPECT_PROXY_SECRET": secret,
            "FAKE_EXPECT_PROXY_NAME": "HTTP_PROXY",
            "FAKE_EXPECT_PROXY_NAMES": "HTTP_PROXY",
            "FAKE_BUILD_LEAK_PROXY": "known",
        }
        arguments = self.runtime_arguments(data_root, architecture)

        first = self.run_script(*arguments, environment=environment)

        transaction_dir = data_root / "provision/transactions"
        self.assertNotEqual(first.returncode, 0)
        self.assertTrue(
            (transaction_dir / "runtime-{}.transaction".format(RUNTIME_FS)).is_file()
        )
        self.assertFalse(
            (transaction_dir / "runtime-{}.proxy.env".format(RUNTIME_FS)).exists()
        )
        self.assertTrue(
            (self.external / "fake-sdme-state/{}.proxy-path".format(RUNTIME_FS)).is_file()
        )
        transaction = transaction_dir / "runtime-{}.transaction".format(RUNTIME_FS)
        build_definition = transaction_dir / "runtime-{}.build.sdme".format(
            RUNTIME_FS
        )
        self.assertFalse(build_definition.exists())
        self.assertEqual(transaction.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(secret, transaction.read_text(encoding="utf-8"))

        rotated_secret = "http://builder:{}@proxy.example:8080".format(
            secrets.token_hex(12)
        )
        second = self.run_script(
            *arguments,
            environment={
                "HTTP_PROXY": rotated_secret,
                "FAKE_EXPECT_PROXY_SECRET": rotated_secret,
                "FAKE_EXPECT_PROXY_NAME": "HTTP_PROXY",
                "FAKE_EXPECT_PROXY_NAMES": "HTTP_PROXY",
            },
        )

        self.assertEqual(second.returncode, 0, second.stderr)
        build_lines = [
            line
            for line in log.read_text(encoding="utf-8").splitlines()
            if "sdme fs build {}".format(RUNTIME_FS) in line
        ]
        self.assertEqual(len(build_lines), 2)
        self.assertIn("--no-cache", build_lines[0])
        self.assertIn("--no-cache", build_lines[1])
        self.assertNotIn(rotated_secret, second.stdout + second.stderr)
        self.assertFalse(
            (self.external / "fake-sdme-state/{}.proxy-path".format(RUNTIME_FS)).exists()
        )
        self.assertEqual(list(transaction_dir.iterdir()), [])

    def test_proxy_plan_never_prints_proxy_values(self) -> None:
        secret = secrets.token_hex(16)
        result = self.run_script(
            "plan",
            "control",
            "--arch",
            "x86_64",
            "--data-root",
            str(self.external / "data"),
            environment={
                "HTTPS_PROXY": "http://builder:{}@proxy.example:8443".format(secret)
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("private transaction files", result.stdout)
        self.assertIn("--no-cache", result.stdout)
        self.assertNotIn(secret, result.stdout + result.stderr)

    def test_all_proxy_variable_spellings_are_forwarded_exactly(self) -> None:
        architecture = self.native_architecture()
        self.install_fake_runtime_tools(architecture)
        state = self.external / "fake-sdme-state"
        for name in (
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "no_proxy",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
        ):
            with self.subTest(name=name):
                for path in state.iterdir():
                    if path.is_dir():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
                data_root = self.safe_runtime_data_root()
                value = "{}-exact-value".format(name)
                result = self.run_script(
                    *self.runtime_arguments(data_root, architecture),
                    environment={
                        name: value,
                        "FAKE_EXPECT_PROXY_SECRET": value,
                        "FAKE_EXPECT_PROXY_NAME": name,
                        "FAKE_EXPECT_PROXY_NAMES": name,
                    },
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn(value, result.stdout + result.stderr)

        for path in state.iterdir():
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        lower_value = "http://lower-proxy.example:8080"
        upper_value = "http://upper-proxy.example:8080"
        conflict_root = self.safe_runtime_data_root()
        conflict = self.run_script(
            *self.runtime_arguments(conflict_root, architecture),
            environment={
                "http_proxy": lower_value,
                "HTTP_PROXY": upper_value,
                "FAKE_EXPECT_PROXY_SECRET": lower_value,
                "FAKE_EXPECT_PROXY_NAMES": "http_proxy,HTTP_PROXY",
                "FAKE_EXPECT_PROXY_VALUES": json.dumps(
                    {"http_proxy": lower_value, "HTTP_PROXY": upper_value}
                ),
            },
        )
        self.assertEqual(conflict.returncode, 0, conflict.stderr)

    def test_no_proxy_build_and_retry_keep_the_original_sdme_command(self) -> None:
        architecture = self.native_architecture()
        log = self.install_fake_runtime_tools(architecture, fail_runtime_build=True)
        data_root = self.safe_runtime_data_root()
        arguments = self.runtime_arguments(data_root, architecture)

        first = self.run_script(*arguments)
        second = self.run_script(*arguments)

        self.assertNotEqual(first.returncode, 0)
        self.assertEqual(second.returncode, 0, second.stderr)
        build_lines = [
            line
            for line in log.read_text(encoding="utf-8").splitlines()
            if "sdme fs build {}".format(RUNTIME_FS) in line
        ]
        self.assertEqual(len(build_lines), 2)
        for line in build_lines:
            self.assertIn("infra/remote-execution/sdme/worker-rootfs.sdme", line)
            self.assertNotIn("transactions/runtime-", line)
            self.assertNotIn("--no-cache", line)

    def test_clean_runtime_reuse_with_proxy_creates_no_transport(self) -> None:
        architecture = self.native_architecture()
        log = self.install_fake_runtime_tools(architecture)
        data_root = self.safe_runtime_data_root()
        arguments = self.runtime_arguments(data_root, architecture)
        first = self.run_script(*arguments)
        self.assertEqual(first.returncode, 0, first.stderr)
        before = log.read_text(encoding="utf-8").count(
            "sdme fs build {}".format(RUNTIME_FS)
        )

        proxy_value = "http://reuse-proxy.example:8080"
        second = self.run_script(
            *arguments,
            environment={"HTTPS_PROXY": proxy_value},
        )

        self.assertEqual(second.returncode, 0, second.stderr)
        after = log.read_text(encoding="utf-8").count(
            "sdme fs build {}".format(RUNTIME_FS)
        )
        self.assertEqual(before, after)
        self.assertEqual(list((data_root / "provision/transactions").iterdir()), [])

    def test_proxy_and_direct_builds_publish_identical_runtime_provenance(self) -> None:
        architecture = self.native_architecture()
        self.install_fake_runtime_tools(architecture)
        state = self.external / "fake-sdme-state"
        direct_root = self.safe_runtime_data_root()
        direct = self.run_script(*self.runtime_arguments(direct_root, architecture))
        self.assertEqual(direct.returncode, 0, direct.stderr)
        provenance_path = state / "{}.buckos-re-runtime-provenance.json".format(
            RUNTIME_FS
        )
        direct_provenance = provenance_path.read_bytes()
        for path in state.iterdir():
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()

        proxy_root = self.safe_runtime_data_root()
        proxy_value = "http://provenance-proxy.example:8080"
        proxied = self.run_script(
            *self.runtime_arguments(proxy_root, architecture),
            environment={
                "HTTPS_PROXY": proxy_value,
                "FAKE_EXPECT_PROXY_SECRET": proxy_value,
                "FAKE_EXPECT_PROXY_NAME": "HTTPS_PROXY",
                "FAKE_EXPECT_PROXY_NAMES": "HTTPS_PROXY",
            },
        )

        self.assertEqual(proxied.returncode, 0, proxied.stderr)
        self.assertEqual(direct_provenance, provenance_path.read_bytes())

    def test_proxy_to_no_proxy_interrupted_transition_is_refused(self) -> None:
        architecture = self.native_architecture()
        log = self.install_fake_runtime_tools(architecture, fail_runtime_build=True)
        data_root = self.safe_runtime_data_root()
        arguments = self.runtime_arguments(data_root, architecture)
        proxy_value = "http://transition-proxy.example:8080"
        first = self.run_script(
            *arguments,
            environment={
                "all_proxy": proxy_value,
                "FAKE_EXPECT_PROXY_SECRET": proxy_value,
                "FAKE_EXPECT_PROXY_NAME": "all_proxy",
                "FAKE_EXPECT_PROXY_NAMES": "all_proxy",
                "FAKE_BUILD_LEAK_PROXY": "known",
            },
        )
        self.assertNotEqual(first.returncode, 0)

        retry = self.run_script(*arguments)

        self.assertEqual(retry.returncode, 2)
        self.assertIn("transaction record does not match", retry.stderr)
        build_lines = [
            line
            for line in log.read_text(encoding="utf-8").splitlines()
            if "sdme fs build {}".format(RUNTIME_FS) in line
        ]
        self.assertEqual(len(build_lines), 1)

    def test_proxy_retry_rejects_untrusted_transport_files(self) -> None:
        architecture = self.native_architecture()
        self.install_fake_runtime_tools(architecture, fail_runtime_build=True)
        failure_marker = self.external / "runtime-build-failed"
        for mutation, expected_error in (
            ("symlink", "is a symlink"),
            ("unsafe", "group/world-writable"),
        ):
            with self.subTest(mutation=mutation):
                failure_marker.unlink(missing_ok=True)
                data_root = self.safe_runtime_data_root()
                arguments = self.runtime_arguments(data_root, architecture)
                proxy_value = "http://transport-{}.example:8080".format(mutation)
                environment = {
                    "HTTPS_PROXY": proxy_value,
                    "FAKE_EXPECT_PROXY_SECRET": proxy_value,
                    "FAKE_EXPECT_PROXY_NAME": "HTTPS_PROXY",
                    "FAKE_EXPECT_PROXY_NAMES": "HTTPS_PROXY",
                }
                first = self.run_script(*arguments, environment=environment)
                self.assertNotEqual(first.returncode, 0)
                transport = (
                    data_root
                    / "provision/transactions/runtime-{}.proxy.env".format(RUNTIME_FS)
                )
                if mutation == "symlink":
                    transport.symlink_to(self.external / "missing-transport")
                else:
                    transport.write_text("unsafe\n", encoding="utf-8")
                    transport.chmod(0o666)

                retry = self.run_script(*arguments, environment=environment)

                self.assertEqual(retry.returncode, 2)
                self.assertIn(expected_error, retry.stderr)

    def test_proxy_transport_without_transaction_is_refused(self) -> None:
        architecture = self.native_architecture()
        self.install_fake_runtime_tools(architecture)
        data_root = self.safe_runtime_data_root()
        transaction_dir = data_root / "provision/transactions"
        transaction_dir.mkdir(parents=True, mode=0o700)
        transport = transaction_dir / "runtime-{}.proxy.env".format(RUNTIME_FS)
        transport.write_text("# buckos-sdme-proxy-transport-v1\n", encoding="utf-8")
        transport.chmod(0o600)

        result = self.run_script(*self.runtime_arguments(data_root, architecture))

        self.assertEqual(result.returncode, 2)
        self.assertIn("without a matching transaction record", result.stderr)
        self.assertTrue(transport.is_file())

    def test_proxy_transport_is_cleaned_after_sigterm_and_retry_is_fresh(self) -> None:
        architecture = self.native_architecture()
        log = self.install_fake_runtime_tools(architecture)
        data_root = self.safe_runtime_data_root()
        arguments = self.runtime_arguments(data_root, architecture)
        ready = self.external / "build-block-ready"
        release = self.external / "build-block-release"
        proxy_value = "http://signal-proxy.example:8080"
        environment = {
            "HTTPS_PROXY": proxy_value,
            "FAKE_EXPECT_PROXY_SECRET": proxy_value,
            "FAKE_EXPECT_PROXY_NAME": "HTTPS_PROXY",
            "FAKE_EXPECT_PROXY_NAMES": "HTTPS_PROXY",
            "FAKE_SDME_BLOCK_BUILD_READY": str(ready),
            "FAKE_SDME_BLOCK_BUILD_RELEASE": str(release),
        }
        first = subprocess.Popen(
            [str(self.script), *arguments],
            env=self.script_environment(environment),
            start_new_session=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(lambda: first.kill() if first.poll() is None else None)
        deadline = time.monotonic() + 20
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(ready.exists())

        os.killpg(first.pid, signal.SIGTERM)
        first.communicate(timeout=20)

        self.assertNotEqual(first.returncode, 0)
        transaction_dir = data_root / "provision/transactions"
        self.assertTrue(
            (transaction_dir / "runtime-{}.transaction".format(RUNTIME_FS)).is_file()
        )
        self.assertFalse(
            (transaction_dir / "runtime-{}.proxy.env".format(RUNTIME_FS)).exists()
        )
        self.assertFalse(
            (transaction_dir / "runtime-{}.build.sdme".format(RUNTIME_FS)).exists()
        )

        retry_environment = environment.copy()
        retry_environment.pop("FAKE_SDME_BLOCK_BUILD_READY")
        retry_environment.pop("FAKE_SDME_BLOCK_BUILD_RELEASE")
        retry = self.run_script(*arguments, environment=retry_environment)

        self.assertEqual(retry.returncode, 0, retry.stderr)
        build_lines = [
            line
            for line in log.read_text(encoding="utf-8").splitlines()
            if "sdme fs build {}".format(RUNTIME_FS) in line
        ]
        self.assertEqual(len(build_lines), 2)
        self.assertTrue(all("--no-cache" in line for line in build_lines))

    def test_runtime_proxy_scan_rejects_unexpected_content_before_provenance(self) -> None:
        architecture = self.native_architecture()
        self.install_fake_runtime_tools(architecture)
        data_root = self.safe_runtime_data_root()
        arguments = self.runtime_arguments(data_root, architecture)
        proxy_value = "http://leak-proxy.example:8080"
        result = self.run_script(
            *arguments,
            environment={
                "HTTPS_PROXY": proxy_value,
                "FAKE_EXPECT_PROXY_SECRET": proxy_value,
                "FAKE_EXPECT_PROXY_NAME": "HTTPS_PROXY",
                "FAKE_EXPECT_PROXY_NAMES": "HTTPS_PROXY",
                "FAKE_BUILD_LEAK_PROXY": "value",
            },
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("contains proxy transport material", result.stderr)
        state = self.external / "fake-sdme-state"
        self.assertFalse(
            (state / "{}.buckos-re-runtime-provenance.json".format(RUNTIME_FS)).exists()
        )
        transaction_dir = data_root / "provision/transactions"
        self.assertTrue(
            (transaction_dir / "runtime-{}.transaction".format(RUNTIME_FS)).is_file()
        )
        self.assertFalse(
            (transaction_dir / "runtime-{}.proxy.env".format(RUNTIME_FS)).exists()
        )

    def test_runtime_proxy_scan_rejects_reuse_leak_and_inspection_error(self) -> None:
        architecture = self.native_architecture()
        log = self.install_fake_runtime_tools(architecture)
        data_root = self.safe_runtime_data_root()
        arguments = self.runtime_arguments(data_root, architecture)
        first = self.run_script(*arguments)
        self.assertEqual(first.returncode, 0, first.stderr)
        state = self.external / "fake-sdme-state"
        leak = state / "{}.proxy-path".format(RUNTIME_FS)
        leak.write_text("# buckos-sdme-proxy-transport-v1\n", encoding="utf-8")

        contaminated = self.run_script(*arguments)

        self.assertEqual(contaminated.returncode, 2)
        self.assertIn("contains proxy transport material", contaminated.stderr)
        self.assertNotIn("sdme fs rm -f {}".format(RUNTIME_FS), log.read_text(encoding="utf-8"))
        leak.unlink()

        inspection_error = self.run_script(
            *arguments, environment={"FAKE_INSPECTION_ERROR": "1"}
        )

        self.assertEqual(inspection_error.returncode, 2)
        self.assertIn("runtime proxy inspection failed", inspection_error.stderr)
        self.assertNotIn("sdme fs rm -f {}".format(RUNTIME_FS), log.read_text(encoding="utf-8"))

    def test_fresh_runtime_inspection_error_prevents_provenance_publication(self) -> None:
        architecture = self.native_architecture()
        log = self.install_fake_runtime_tools(architecture)
        data_root = self.safe_runtime_data_root()
        arguments = self.runtime_arguments(data_root, architecture)

        first = self.run_script(
            *arguments, environment={"FAKE_INSPECTION_ERROR": "1"}
        )

        self.assertEqual(first.returncode, 2)
        self.assertIn("runtime proxy inspection failed", first.stderr)
        state = self.external / "fake-sdme-state"
        self.assertFalse(
            (state / "{}.buckos-re-runtime-provenance.json".format(RUNTIME_FS)).exists()
        )
        self.assertTrue(
            (
                data_root
                / "provision/transactions/runtime-{}.transaction".format(RUNTIME_FS)
            ).is_file()
        )

        retry = self.run_script(*arguments)

        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertIn(
            "sdme fs rm -f {}".format(RUNTIME_FS),
            log.read_text(encoding="utf-8"),
        )

    def test_post_provenance_proxy_leak_is_not_reused(self) -> None:
        architecture = self.native_architecture()
        log = self.install_fake_runtime_tools(
            architecture, leak_after_runtime_provenance=True
        )
        data_root = self.safe_runtime_data_root()
        arguments = self.runtime_arguments(data_root, architecture)
        proxy_value = "http://post-provenance.example:8080"
        environment = {
            "HTTPS_PROXY": proxy_value,
            "FAKE_EXPECT_PROXY_SECRET": proxy_value,
            "FAKE_EXPECT_PROXY_NAME": "HTTPS_PROXY",
            "FAKE_EXPECT_PROXY_NAMES": "HTTPS_PROXY",
        }

        first = self.run_script(*arguments, environment=environment)
        self.assertNotEqual(first.returncode, 0)
        state = self.external / "fake-sdme-state"
        self.assertTrue(
            (state / "{}.buckos-re-runtime-provenance.json".format(RUNTIME_FS)).is_file()
        )

        retry = self.run_script(*arguments, environment=environment)

        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertIn(
            "sdme fs rm -f {}".format(RUNTIME_FS),
            log.read_text(encoding="utf-8"),
        )

    def test_clean_post_provenance_interruption_completes_without_rebuild(self) -> None:
        architecture = self.native_architecture()
        log = self.install_fake_runtime_tools(
            architecture, fail_after_runtime_provenance=True
        )
        data_root = self.safe_runtime_data_root()
        arguments = self.runtime_arguments(data_root, architecture)
        proxy_value = "http://clean-post-provenance.example:8080"
        environment = {
            "HTTPS_PROXY": proxy_value,
            "FAKE_EXPECT_PROXY_SECRET": proxy_value,
            "FAKE_EXPECT_PROXY_NAME": "HTTPS_PROXY",
            "FAKE_EXPECT_PROXY_NAMES": "HTTPS_PROXY",
        }
        first = self.run_script(*arguments, environment=environment)
        self.assertNotEqual(first.returncode, 0)

        retry = self.run_script(*arguments, environment=environment)

        self.assertEqual(retry.returncode, 0, retry.stderr)
        build_lines = [
            line
            for line in log.read_text(encoding="utf-8").splitlines()
            if "sdme fs build {}".format(RUNTIME_FS) in line
        ]
        self.assertEqual(len(build_lines), 1)
        self.assertEqual(list((data_root / "provision/transactions").iterdir()), [])

    def test_prepare_runtime_refuses_concurrent_writer_and_releases_lock(self) -> None:
        architecture = self.native_architecture()
        self.install_fake_runtime_tools(architecture)
        data_root = self.safe_runtime_data_root()
        arguments = self.runtime_arguments(data_root, architecture)
        ready = self.external / "sdme-block-ready"
        release = self.external / "sdme-block-release"
        environment = {
            "FAKE_SDME_BLOCK_READY": str(ready),
            "FAKE_SDME_BLOCK_RELEASE": str(release),
        }
        first = subprocess.Popen(
            [str(self.script), *arguments],
            env=self.script_environment(environment),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(lambda: first.kill() if first.poll() is None else None)
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(ready.exists())

        concurrent = self.run_script(*arguments)

        self.assertEqual(concurrent.returncode, 2)
        self.assertIn("another provisioning operation holds", concurrent.stderr)
        release.touch()
        _, first_stderr = first.communicate(timeout=30)
        self.assertEqual(first.returncode, 0, first_stderr)

        later = self.run_script(*arguments)
        self.assertEqual(later.returncode, 0, later.stderr)

    def test_existing_filesystem_does_not_relax_incomplete_cache(self) -> None:
        architecture = {"amd64": "x86_64", "arm64": "aarch64"}.get(
            platform.machine(), platform.machine()
        )
        if architecture not in ("x86_64", "aarch64"):
            self.skipTest("unsupported test architecture")
        self.install_fake_runtime_tools(architecture)
        data_root = self.safe_runtime_data_root()
        images = data_root / "images"
        images.mkdir(parents=True)
        ubuntu_cache = images / "ubuntu-2604-{}.oci.tar".format(architecture)
        shutil.copy2(self.archives["ubuntu"][architecture], ubuntu_cache)
        state = self.external / "fake-sdme-state"
        (state / "buckos-re-ubuntu-2260313b31c8.fs").write_text(
            "", encoding="utf-8"
        )

        result = self.run_script(
            "prepare-runtime",
            "worker",
            "--arch",
            architecture,
            "--data-root",
            str(data_root),
            "--ubuntu-oci-archive",
            str(self.archives["ubuntu"][architecture]),
            "--nativelink-oci-archive",
            str(self.archives["nativelink"][architecture]),
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("incomplete cached OCI archive pair", result.stderr)
        self.assertFalse(
            ubuntu_cache.with_name(ubuntu_cache.name + ".provenance.json").exists()
        )

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
        self.assertIn("# PROVISIONER_PROXY_COPY", rootfs)
        self.assertIn(". /etc/buckos-re-build-proxy.env", rootfs)
        self.assertIn("rm -f /etc/buckos-re-build-proxy.env", rootfs)
        self.assertIn("test ! -e /etc/buckos-re-build-proxy.env", rootfs)
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
