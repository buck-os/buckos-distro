from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import json
from pathlib import Path
import stat
import tarfile
import tempfile
import unittest

import oci_acquire


CONFIG_MEDIA_TYPE = "application/vnd.oci.image.config.v1+json"
LAYER_MEDIA_TYPE = "application/vnd.oci.image.layer.v1.tar+gzip"


def encoded_json(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def descriptor(value: bytes, media_type: str) -> dict[str, object]:
    return {
        "digest": digest(value),
        "mediaType": media_type,
        "size": len(value),
    }


def tar_info(name: str, *, size: int = 0, directory: bool = False) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE if directory else tarfile.REGTYPE
    info.size = 0 if directory else size
    info.mode = 0o755 if directory else 0o644
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    return info


def expected_archive(
    architecture: str,
    reference: str,
    parent: bytes,
    selected: dict[str, object],
    blobs: dict[str, bytes],
) -> bytes:
    if architecture == "x86_64":
        root: dict[str, object] = {
            "annotations": {
                "org.opencontainers.image.ref.name": reference,
            },
            "digest": digest(parent),
            "mediaType": oci_acquire.INDEX_MEDIA_TYPE,
            "size": len(parent),
        }
    else:
        root = selected
    index = encoded_json(
        {
            "manifests": [root],
            "mediaType": oci_acquire.INDEX_MEDIA_TYPE,
            "schemaVersion": 2,
        }
    )
    layout = b'{"imageLayoutVersion":"1.0.0"}\n'
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        archive.addfile(tar_info("blobs", directory=True))
        archive.addfile(tar_info("blobs/sha256", directory=True))
        for name, value in (("oci-layout", layout), ("index.json", index)):
            archive.addfile(tar_info(name, size=len(value)), io.BytesIO(value))
        for blob_digest in sorted(blobs):
            value = blobs[blob_digest]
            name = "blobs/sha256/" + blob_digest.removeprefix("sha256:")
            archive.addfile(tar_info(name, size=len(value)), io.BytesIO(value))
    return stream.getvalue()


@dataclass(frozen=True)
class ImageFixture:
    name: str
    architecture: str
    reference: str
    parent: bytes
    selected: dict[str, object]
    manifest: bytes
    config: bytes
    layer: bytes
    archive_name: str
    archive: bytes

    def metadata_record(self) -> dict[str, object]:
        return {
            "architecture": "amd64" if self.architecture == "x86_64" else "arm64",
            "archive": {
                "filename": self.archive_name,
                "sha256": hashlib.sha256(self.archive).hexdigest(),
                "size": len(self.archive),
            },
            "manifest": {
                "digest": digest(self.manifest),
                "media_type": "application/vnd.oci.image.manifest.v1+json",
                "size": len(self.manifest),
            },
            "os": "linux",
            **(
                {"variant": "v8"}
                if self.architecture == "aarch64" and self.name == "ubuntu"
                else {}
            ),
        }


def make_fixture(
    name: str, architecture: str, *, selected_first: bool | None = None
) -> ImageFixture:
    oci_architecture = "amd64" if architecture == "x86_64" else "arm64"
    platform = {"architecture": oci_architecture, "os": "linux"}
    if architecture == "aarch64" and name == "ubuntu":
        platform["variant"] = "v8"
    layer = ((name + "-" + architecture + "-layer\n").encode() * 131_073)
    config_value: dict[str, object] = {
        "architecture": oci_architecture,
        "os": "linux",
        "rootfs": {"diff_ids": [digest(layer)], "type": "layers"},
    }
    if "variant" in platform:
        config_value["variant"] = platform["variant"]
    config = encoded_json(config_value)
    manifest = encoded_json(
        {
            "config": descriptor(config, CONFIG_MEDIA_TYPE),
            "layers": [descriptor(layer, LAYER_MEDIA_TYPE)],
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "schemaVersion": 2,
        }
    )
    selected = {
        **descriptor(manifest, "application/vnd.oci.image.manifest.v1+json"),
        "platform": platform,
    }
    other_architecture = "arm64" if oci_architecture == "amd64" else "amd64"
    other = {
        "digest": "sha256:" + ("e" if name == "ubuntu" else "f") * 64,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "platform": {"architecture": other_architecture, "os": "linux"},
        "size": 123,
    }
    if selected_first is None:
        selected_first = architecture == "x86_64"
    parent = encoded_json(
        {
            "manifests": [selected, other] if selected_first else [other, selected],
            "mediaType": oci_acquire.INDEX_MEDIA_TYPE,
            "schemaVersion": 2,
        }
    )
    reference = "registry.example/{0}@{1}".format(name, digest(parent))
    blobs = {
        digest(parent): parent,
        digest(manifest): manifest,
        digest(config): config,
        digest(layer): layer,
    }
    archive_name = "{}-{}.oci.tar".format(name, architecture)
    archive = expected_archive(
        architecture, reference, parent, selected, blobs
    )
    return ImageFixture(
        name=name,
        architecture=architecture,
        reference=reference,
        parent=parent,
        selected=selected,
        manifest=manifest,
        config=config,
        layer=layer,
        archive_name=archive_name,
        archive=archive,
    )


class BoundedReader(io.BytesIO):
    def read(self, size: int = -1) -> bytes:
        if size < 0 or size > oci_acquire.CHUNK_SIZE:
            raise AssertionError("producer attempted an unbounded registry read")
        return super().read(min(size, 4093))


class FakeRegistryTransport:
    def __init__(self, fixtures: list[ImageFixture]) -> None:
        self.manifests: dict[tuple[str, str], bytes] = {}
        self.blobs: dict[tuple[str, str], bytes] = {}
        self.calls: list[tuple[str, str, str]] = []
        for fixture in fixtures:
            self.manifests[(fixture.reference, digest(fixture.parent))] = fixture.parent
            self.manifests[(fixture.reference, digest(fixture.manifest))] = fixture.manifest
            self.blobs[(fixture.reference, digest(fixture.config))] = fixture.config
            self.blobs[(fixture.reference, digest(fixture.layer))] = fixture.layer

    def open_manifest(
        self, reference: oci_acquire.RegistryReference, blob_digest: str
    ) -> BoundedReader:
        self.calls.append(("manifest", reference.value, blob_digest))
        return BoundedReader(self.manifests[(reference.value, blob_digest)])

    def open_blob(
        self, reference: oci_acquire.RegistryReference, blob_digest: str
    ) -> BoundedReader:
        self.calls.append(("blob", reference.value, blob_digest))
        return BoundedReader(self.blobs[(reference.value, blob_digest)])


def write_metadata(path: Path, fixtures: list[ImageFixture]) -> None:
    images: dict[str, object] = {}
    for fixture in fixtures:
        images[fixture.name] = {
            "platforms": {
                fixture.architecture: fixture.metadata_record(),
            },
            "reference": fixture.reference,
        }
    path.write_text(
        json.dumps(
            {"images": images, "schema_version": 1},
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


class OciAcquireTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.metadata = self.root / "metadata.json"
        self.output = self.root / "output"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def fixtures(
        self, architecture: str, *, selected_first: bool | None = None
    ) -> list[ImageFixture]:
        return [
            make_fixture("ubuntu", architecture, selected_first=selected_first),
            make_fixture("nativelink", architecture, selected_first=selected_first),
        ]

    def test_reproduces_exact_archive_for_each_architecture(self) -> None:
        for architecture in ("x86_64", "aarch64"):
            with self.subTest(architecture=architecture):
                output = self.root / architecture
                fixtures = self.fixtures(architecture)
                write_metadata(self.metadata, fixtures)
                transport = FakeRegistryTransport(fixtures)
                results = oci_acquire.acquire_architecture(
                    self.metadata, architecture, output, transport
                )
                self.assertEqual(
                    [result["filename"] for result in results],
                    [fixture.archive_name for fixture in fixtures],
                )
                for fixture in fixtures:
                    archive_path = output / fixture.archive_name
                    self.assertEqual(archive_path.read_bytes(), fixture.archive)
                    self.assertEqual(
                        stat.S_IMODE(archive_path.stat().st_mode), 0o600
                    )
                    with tarfile.open(archive_path, "r:") as archive:
                        index = json.load(archive.extractfile("index.json"))
                        root = index["manifests"][0]
                        expected_root = (
                            digest(fixture.parent)
                            if architecture == "x86_64"
                            else digest(fixture.manifest)
                        )
                        self.assertEqual(root["digest"], expected_root)
                        self.assertIn(
                            "blobs/sha256/"
                            + digest(fixture.parent).removeprefix("sha256:"),
                            archive.getnames(),
                        )

    def test_rejects_changed_registry_blob_without_publishing(self) -> None:
        fixtures = self.fixtures("aarch64")
        write_metadata(self.metadata, fixtures)
        transport = FakeRegistryTransport(fixtures)
        fixture = fixtures[1]
        changed = bytearray(fixture.layer)
        changed[-1] ^= 1
        transport.blobs[(fixture.reference, digest(fixture.layer))] = bytes(changed)
        with self.assertRaisesRegex(oci_acquire.AcquisitionError, "digest mismatch"):
            oci_acquire.acquire_architecture(
                self.metadata, "aarch64", self.output, transport
            )
        self.assertEqual(list(self.output.iterdir()), [])

    def test_rejects_archive_identity_drift_without_publishing(self) -> None:
        fixtures = self.fixtures("aarch64")
        write_metadata(self.metadata, fixtures)
        metadata = json.loads(self.metadata.read_text())
        metadata["images"]["ubuntu"]["platforms"]["aarch64"]["archive"][
            "sha256"
        ] = "0" * 64
        self.metadata.write_text(json.dumps(metadata))
        with self.assertRaisesRegex(
            oci_acquire.ArchiveValidationError,
            "archive SHA-256 mismatch",
        ):
            oci_acquire.acquire_architecture(
                self.metadata,
                "aarch64",
                self.output,
                FakeRegistryTransport(fixtures),
            )
        self.assertEqual(list(self.output.iterdir()), [])

    def test_rejects_x86_64_when_selected_descriptor_is_not_first(self) -> None:
        fixtures = self.fixtures("x86_64", selected_first=False)
        write_metadata(self.metadata, fixtures)
        with self.assertRaisesRegex(oci_acquire.AcquisitionError, "is not first"):
            oci_acquire.acquire_architecture(
                self.metadata,
                "x86_64",
                self.output,
                FakeRegistryTransport(fixtures),
            )
        self.assertEqual(list(self.output.iterdir()), [])

    def test_refuses_overwrite_before_contacting_registry(self) -> None:
        fixtures = self.fixtures("x86_64")
        write_metadata(self.metadata, fixtures)
        self.output.mkdir()
        (self.output / fixtures[0].archive_name).write_bytes(b"existing")
        transport = FakeRegistryTransport(fixtures)
        with self.assertRaisesRegex(oci_acquire.AcquisitionError, "refusing to overwrite"):
            oci_acquire.acquire_architecture(
                self.metadata, "x86_64", self.output, transport
            )
        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
