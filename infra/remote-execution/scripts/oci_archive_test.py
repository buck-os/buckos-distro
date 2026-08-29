#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

import oci_archive


MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
CONFIG_MEDIA_TYPE = "application/vnd.oci.image.config.v1+json"
LAYER_MEDIA_TYPE = "application/vnd.oci.image.layer.v1.tar"
INDEX_MEDIA_TYPE = "application/vnd.oci.image.index.v1+json"
REFERENCES = {
    "ubuntu": "docker.io/library/ubuntu@sha256:" + "1" * 64,
    "nativelink": "ghcr.io/tracemachina/nativelink@sha256:" + "2" * 64,
}


def encoded_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def tar_info(name: str, size: int, *, kind: bytes | None = None) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.mode = 0o644
    if kind is not None:
        info.type = kind
    return info


def add_file(archive: tarfile.TarFile, name: str, data: bytes) -> None:
    archive.addfile(tar_info(name, len(data)), io.BytesIO(data))


def write_archive(
    path: Path,
    *,
    oci_architecture: str = "amd64",
    layer: bytes = b"layer payload",
    declared_layer: bytes | None = None,
    index_manifest_digest: str | None = None,
    declared_layer_size: int | None = None,
    unsafe_member: str | None = None,
    duplicate_index: bool = False,
    symlink_member: bool = False,
    preserve_parent: bool = False,
    selected_first: bool = True,
    extra_blob: bool = False,
    namespace_file: bool = False,
    include_parent: bool = True,
) -> dict[str, object]:
    declared_layer = layer if declared_layer is None else declared_layer
    config = encoded_json(
        {
            "architecture": oci_architecture,
            "os": "linux",
            "rootfs": {"diff_ids": [digest(layer)], "type": "layers"},
        }
    )
    manifest = encoded_json(
        {
            "config": {
                "digest": digest(config),
                "mediaType": CONFIG_MEDIA_TYPE,
                "size": len(config),
            },
            "layers": [
                {
                    "digest": digest(declared_layer),
                    "mediaType": LAYER_MEDIA_TYPE,
                    "size": len(layer) if declared_layer_size is None else declared_layer_size,
                }
            ],
            "mediaType": MANIFEST_MEDIA_TYPE,
            "schemaVersion": 2,
        }
    )
    manifest_digest = digest(manifest)
    selected = {
        "digest": index_manifest_digest or manifest_digest,
        "mediaType": MANIFEST_MEDIA_TYPE,
        "platform": {"architecture": oci_architecture, "os": "linux"},
        "size": len(manifest),
    }
    other_architecture = "arm64" if oci_architecture == "amd64" else "amd64"
    other_platform = {"architecture": other_architecture, "os": "linux"}
    if other_architecture == "arm64":
        other_platform["variant"] = "v8"
    other = {
        "digest": "sha256:" + "e" * 64,
        "mediaType": MANIFEST_MEDIA_TYPE,
        "platform": other_platform,
        "size": 123,
    }
    parent = encoded_json(
        {
            "manifests": [selected, other] if selected_first else [other, selected],
            "mediaType": INDEX_MEDIA_TYPE,
            "schemaVersion": 2,
        }
    )
    reference = "registry.example/test@" + digest(parent)
    if preserve_parent:
        root = {
            "annotations": {"org.opencontainers.image.ref.name": reference},
            "digest": digest(parent),
            "mediaType": INDEX_MEDIA_TYPE,
            "size": len(parent),
        }
    else:
        root = selected
    index = encoded_json(
        {"manifests": [root], "mediaType": INDEX_MEDIA_TYPE, "schemaVersion": 2}
    )
    with tarfile.open(path, "w") as archive:
        add_file(archive, "oci-layout", encoded_json({"imageLayoutVersion": "1.0.0"}))
        add_file(archive, "index.json", index)
        if duplicate_index:
            add_file(archive, "./index.json", index)
        if include_parent:
            add_file(archive, "blobs/sha256/" + digest(parent).split(":", 1)[1], parent)
        add_file(archive, "blobs/sha256/" + digest(manifest).split(":", 1)[1], manifest)
        add_file(archive, "blobs/sha256/" + digest(config).split(":", 1)[1], config)
        add_file(archive, "blobs/sha256/" + digest(declared_layer).split(":", 1)[1], layer)
        if unsafe_member is not None:
            add_file(archive, unsafe_member, b"unsafe")
        if symlink_member:
            info = tar_info("unexpected-link", 0, kind=tarfile.SYMTYPE)
            info.linkname = "index.json"
            archive.addfile(info)
        if extra_blob:
            extra = b"unreferenced"
            add_file(archive, "blobs/sha256/" + digest(extra).split(":", 1)[1], extra)
        if namespace_file:
            add_file(archive, "blobs", b"not a directory")
    archive_bytes = path.read_bytes()
    return {
        "manifest": {
            "digest": manifest_digest,
            "media_type": MANIFEST_MEDIA_TYPE,
            "size": len(manifest),
        },
        "archive": {
            "filename": path.name,
            "sha256": hashlib.sha256(archive_bytes).hexdigest(),
            "size": len(archive_bytes),
        },
        "reference": reference,
    }


def write_metadata(
    path: Path,
    platform_record: dict[str, object],
    references: dict[str, str] | None = None,
    architectures: tuple[tuple[str, str], ...] = (
        ("x86_64", "amd64"),
        ("aarch64", "arm64"),
    ),
) -> None:
    references = references or {
        **REFERENCES,
        "ubuntu": str(platform_record.get("reference", REFERENCES["ubuntu"])),
    }
    platforms = {}
    for target, oci_architecture in architectures:
        platforms[target] = {
            "os": "linux",
            "architecture": oci_architecture,
            "manifest": platform_record["manifest"],
            "archive": platform_record["archive"],
        }
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "images": {
                    image: {"reference": reference, "platforms": platforms}
                    for image, reference in references.items()
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


class OciArchiveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archive = self.root / "image.oci.tar"
        record = write_archive(self.archive)
        self.reference = str(record["reference"])
        self.metadata = self.root / "metadata.json"
        write_metadata(self.metadata, record)

    def verify(self, *, acquisition: str = "offline") -> dict[str, object]:
        return oci_archive.verify_archive(
            self.metadata,
            "ubuntu",
            "x86_64",
            self.reference,
            self.archive,
            acquisition,
        )

    def test_valid_archive_and_cache_replay(self) -> None:
        provenance = self.verify()
        self.assertEqual(
            provenance["archive"]["sha256"],
            hashlib.sha256(self.archive.read_bytes()).hexdigest(),
        )
        provenance_path = self.root / "provenance.json"
        provenance_path.write_text(
            json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        oci_archive.verify_cache(
            self.metadata,
            "ubuntu",
            "x86_64",
            self.reference,
            self.archive,
            provenance_path,
            "offline",
        )

    def test_rejects_wrong_reference(self) -> None:
        with self.assertRaisesRegex(oci_archive.ArchiveValidationError, "reference mismatch"):
            oci_archive.verify_archive(
                self.metadata,
                "ubuntu",
                "x86_64",
                REFERENCES["nativelink"],
                self.archive,
                "offline",
            )

    def test_registry_archive_allows_platform_without_offline_record(self) -> None:
        record = write_archive(self.archive, oci_architecture="arm64")
        write_metadata(
            self.metadata,
            record,
            architectures=(("x86_64", "amd64"),),
        )
        provenance = oci_archive.verify_archive(
            self.metadata,
            "ubuntu",
            "aarch64",
            str(record["reference"]),
            self.archive,
            "registry",
            recorded_filename="ubuntu-2604-aarch64.oci.tar",
        )
        self.assertEqual(provenance["platform"]["architecture"], "arm64")
        self.assertEqual(
            provenance["archive"]["filename"], "ubuntu-2604-aarch64.oci.tar"
        )

    def test_registry_missing_platform_still_rejects_wrong_reference(self) -> None:
        record = write_archive(self.archive, oci_architecture="arm64")
        references = {
            **REFERENCES,
            "ubuntu": "registry.example/changed@sha256:" + "3" * 64,
        }
        write_metadata(
            self.metadata,
            record,
            references,
            architectures=(("x86_64", "amd64"),),
        )
        with self.assertRaisesRegex(
            oci_archive.ArchiveValidationError, "reference mismatch"
        ):
            oci_archive.verify_archive(
                self.metadata,
                "ubuntu",
                "aarch64",
                str(record["reference"]),
                self.archive,
                "registry",
            )

    def test_offline_archive_rejects_platform_without_trusted_record(self) -> None:
        record = write_archive(self.archive, oci_architecture="arm64")
        write_metadata(
            self.metadata,
            record,
            architectures=(("x86_64", "amd64"),),
        )
        with self.assertRaisesRegex(
            oci_archive.ArchiveValidationError, "metadata is unavailable"
        ):
            oci_archive.verify_archive(
                self.metadata,
                "ubuntu",
                "aarch64",
                str(record["reference"]),
                self.archive,
                "offline",
            )

    def test_rejects_wrong_architecture(self) -> None:
        with self.assertRaisesRegex(
            oci_archive.ArchiveValidationError, "trusted platform|wrong architecture"
        ):
            oci_archive.verify_archive(
                self.metadata,
                "ubuntu",
                "aarch64",
                self.reference,
                self.archive,
                "registry",
            )

    def test_rejects_wrong_manifest(self) -> None:
        record = write_archive(self.archive)
        record["manifest"] = {
            **record["manifest"],
            "digest": "sha256:" + "f" * 64,
        }
        write_metadata(self.metadata, record)
        with self.assertRaisesRegex(oci_archive.ArchiveValidationError, "trusted platform manifest"):
            self.verify()

    def test_rejects_wrong_archive_checksum(self) -> None:
        record = write_archive(self.archive)
        record["archive"] = {**record["archive"], "sha256": "f" * 64}
        write_metadata(self.metadata, record)
        with self.assertRaisesRegex(oci_archive.ArchiveValidationError, "archive SHA-256 mismatch"):
            self.verify()

    def test_rejects_wrong_archive_size(self) -> None:
        record = write_archive(self.archive)
        record["archive"] = {**record["archive"], "size": record["archive"]["size"] + 1}
        write_metadata(self.metadata, record)
        with self.assertRaisesRegex(oci_archive.ArchiveValidationError, "archive size mismatch"):
            self.verify()

    def test_rejects_tampered_blob_even_with_updated_archive_checksum(self) -> None:
        record = write_archive(self.archive, layer=b"tampered", declared_layer=b"original")
        write_metadata(self.metadata, record)
        with self.assertRaisesRegex(oci_archive.ArchiveValidationError, "blob digest mismatch"):
            oci_archive.verify_archive(
                self.metadata,
                "ubuntu",
                "x86_64",
                str(record["reference"]),
                self.archive,
                "offline",
            )

    def test_rejects_wrong_blob_size(self) -> None:
        record = write_archive(self.archive, declared_layer_size=999)
        write_metadata(self.metadata, record)
        with self.assertRaisesRegex(oci_archive.ArchiveValidationError, "blob size mismatch"):
            oci_archive.verify_archive(
                self.metadata,
                "ubuntu",
                "x86_64",
                str(record["reference"]),
                self.archive,
                "offline",
            )

    def test_rejects_unsafe_member_path(self) -> None:
        record = write_archive(self.archive, unsafe_member="../escape")
        write_metadata(self.metadata, record)
        with self.assertRaisesRegex(oci_archive.ArchiveValidationError, "unsafe OCI archive member"):
            self.verify()

    def test_rejects_noncanonical_member_path(self) -> None:
        record = write_archive(
            self.archive,
            unsafe_member="blobs//sha256/" + "f" * 64,
        )
        write_metadata(self.metadata, record)
        with self.assertRaisesRegex(
            oci_archive.ArchiveValidationError, "unsafe OCI archive member"
        ):
            self.verify()

    def test_rejects_duplicate_normalized_member(self) -> None:
        record = write_archive(self.archive, duplicate_index=True)
        write_metadata(self.metadata, record)
        with self.assertRaisesRegex(oci_archive.ArchiveValidationError, "duplicate OCI archive member"):
            self.verify()

    def test_rejects_non_regular_member(self) -> None:
        record = write_archive(self.archive, symlink_member=True)
        write_metadata(self.metadata, record)
        with self.assertRaisesRegex(oci_archive.ArchiveValidationError, "unexpected OCI archive member"):
            self.verify()

    def test_rejects_regular_namespace_member(self) -> None:
        record = write_archive(self.archive, namespace_file=True)
        write_metadata(self.metadata, record)
        with self.assertRaisesRegex(
            oci_archive.ArchiveValidationError,
            "namespace member is not a directory",
        ):
            self.verify()

    def test_rejects_unreferenced_blob(self) -> None:
        record = write_archive(self.archive, extra_blob=True)
        write_metadata(self.metadata, record)
        with self.assertRaisesRegex(oci_archive.ArchiveValidationError, "blob closure mismatch"):
            self.verify()

    def test_accepts_parent_preserving_sparse_archive(self) -> None:
        record = write_archive(self.archive, preserve_parent=True)
        references = {**REFERENCES, "ubuntu": record["reference"]}
        write_metadata(self.metadata, record, references)
        provenance = oci_archive.verify_archive(
            self.metadata,
            "ubuntu",
            "x86_64",
            record["reference"],
            self.archive,
            "offline",
        )
        self.assertEqual(provenance["manifest"], record["manifest"])

    def test_accepts_direct_manifest_with_retained_parent(self) -> None:
        record = write_archive(
            self.archive,
            oci_architecture="arm64",
            selected_first=False,
        )
        write_metadata(
            self.metadata,
            record,
            architectures=(("aarch64", "arm64"),),
        )
        provenance = oci_archive.verify_archive(
            self.metadata,
            "ubuntu",
            "aarch64",
            str(record["reference"]),
            self.archive,
            "offline",
        )
        self.assertEqual(provenance["manifest"], record["manifest"])

    def test_rejects_direct_manifest_without_retained_parent(self) -> None:
        record = write_archive(self.archive, include_parent=False)
        write_metadata(self.metadata, record)
        with self.assertRaisesRegex(
            oci_archive.ArchiveValidationError, "lacks the pinned parent"
        ):
            oci_archive.verify_archive(
                self.metadata,
                "ubuntu",
                "x86_64",
                str(record["reference"]),
                self.archive,
                "offline",
            )

    def test_rejects_selected_platform_after_first_parent_entry(self) -> None:
        record = write_archive(
            self.archive, preserve_parent=True, selected_first=False
        )
        references = {**REFERENCES, "ubuntu": record["reference"]}
        write_metadata(self.metadata, record, references)
        with self.assertRaisesRegex(oci_archive.ArchiveValidationError, "must be first"):
            oci_archive.verify_archive(
                self.metadata,
                "ubuntu",
                "x86_64",
                record["reference"],
                self.archive,
                "offline",
            )

    def test_rejects_tampered_provenance(self) -> None:
        provenance = self.verify()
        provenance["reference"] = REFERENCES["nativelink"]
        provenance_path = self.root / "provenance.json"
        provenance_path.write_text(json.dumps(provenance), encoding="utf-8")
        with self.assertRaisesRegex(oci_archive.ArchiveValidationError, "provenance does not match"):
            oci_archive.verify_cache(
                self.metadata,
                "ubuntu",
                "x86_64",
                self.reference,
                self.archive,
                provenance_path,
                "offline",
            )

    def test_cache_rejects_trusted_filename_change(self) -> None:
        provenance = self.verify()
        provenance_path = self.root / "provenance.json"
        provenance_path.write_text(json.dumps(provenance), encoding="utf-8")
        metadata = json.loads(self.metadata.read_text(encoding="utf-8"))
        metadata["images"]["ubuntu"]["platforms"]["x86_64"]["archive"][
            "filename"
        ] = "renamed.oci.tar"
        self.metadata.write_text(json.dumps(metadata), encoding="utf-8")
        with self.assertRaisesRegex(
            oci_archive.ArchiveValidationError, "archive filename mismatch"
        ):
            oci_archive.verify_cache(
                self.metadata,
                "ubuntu",
                "x86_64",
                self.reference,
                self.archive,
                provenance_path,
                "offline",
            )

    def test_rejects_acquisition_mode_change(self) -> None:
        provenance = self.verify()
        provenance_path = self.root / "provenance.json"
        provenance_path.write_text(json.dumps(provenance), encoding="utf-8")
        with self.assertRaisesRegex(oci_archive.ArchiveValidationError, "acquisition mode mismatch"):
            oci_archive.verify_cache(
                self.metadata,
                "ubuntu",
                "x86_64",
                self.reference,
                self.archive,
                provenance_path,
                "registry",
            )

    def test_runtime_provenance_binds_both_inputs_and_build_definition(self) -> None:
        ubuntu = self.verify()
        nativelink = {**ubuntu, "image": "nativelink", "reference": REFERENCES["nativelink"]}
        ubuntu_path = self.root / "ubuntu.json"
        nativelink_path = self.root / "nativelink.json"
        build_path = self.root / "runtime.sdme"
        ubuntu_path.write_text(json.dumps(ubuntu), encoding="utf-8")
        nativelink_path.write_text(json.dumps(nativelink), encoding="utf-8")
        build_path.write_text("FROM fs:ubuntu\n", encoding="utf-8")

        result = oci_archive.runtime_provenance(
            "x86_64", build_path, ubuntu_path, nativelink_path
        )

        self.assertEqual(result["images"]["ubuntu"], ubuntu)
        self.assertEqual(result["images"]["nativelink"], nativelink)
        self.assertEqual(
            result["build_definition_sha256"],
            hashlib.sha256(build_path.read_bytes()).hexdigest(),
        )

    def test_runtime_provenance_rejects_wrong_input_architecture(self) -> None:
        provenance = self.verify()
        provenance_path = self.root / "provenance.json"
        build_path = self.root / "runtime.sdme"
        provenance_path.write_text(json.dumps(provenance), encoding="utf-8")
        build_path.write_text("FROM fs:ubuntu\n", encoding="utf-8")
        with self.assertRaisesRegex(oci_archive.ArchiveValidationError, "wrong target architecture"):
            oci_archive.runtime_provenance(
                "aarch64", build_path, provenance_path, provenance_path
            )


if __name__ == "__main__":
    unittest.main()
