#!/usr/bin/env python3
"""Validate pinned OCI archives and their managed-cache provenance."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import tarfile
from dataclasses import dataclass
from typing import Any, BinaryIO, Mapping, Sequence


LOG = logging.getLogger("oci-archive")
CHUNK_SIZE = 1024 * 1024
MAX_JSON_SIZE = 16 * 1024 * 1024
MAX_MEMBERS = 100_000
SHA256_PREFIX = "sha256:"
PLATFORMS = {
    "x86_64": ("linux", "amd64"),
    "aarch64": ("linux", "arm64"),
}
IMAGE_NAMES = frozenset({"ubuntu", "nativelink"})
MANIFEST_MEDIA_TYPES = frozenset(
    {
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    }
)
INDEX_MEDIA_TYPE = "application/vnd.oci.image.index.v1+json"
REFERENCE_ANNOTATION = "org.opencontainers.image.ref.name"


class ArchiveValidationError(ValueError):
    """The metadata, archive, or provenance failed admission."""


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ArchiveValidationError(message)


@dataclass(frozen=True)
class Descriptor:
    digest: str
    size: int
    media_type: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "media_type": self.media_type,
            "size": self.size,
        }


@dataclass(frozen=True)
class ArchiveContract:
    image: str
    reference: str
    target_architecture: str
    os: str
    oci_architecture: str
    variant: str | None
    manifest: Descriptor | None
    archive_filename: str | None
    archive_sha256: str | None
    archive_size: int | None
    enforce_variant: bool


@dataclass(frozen=True)
class ArchiveIdentity:
    sha256: str
    size: int


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ArchiveValidationError("duplicate JSON key: {!r}".format(key))
        result[key] = value
    return result


def _decode_json(data: bytes, label: str) -> Any:
    try:
        return json.loads(data, object_pairs_hook=_object_without_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ArchiveValidationError("{} is not valid JSON: {}".format(label, error)) from error


def _read_json(path: Path, label: str) -> Any:
    try:
        data = path.read_bytes()
    except OSError as error:
        raise ArchiveValidationError("cannot read {}: {}".format(label, error)) from error
    return _decode_json(data, label)


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ArchiveValidationError("{} must be an object".format(label))
    return value


def _exact_keys(
    value: Mapping[str, Any], required: set[str], optional: set[str], label: str
) -> None:
    missing = required - value.keys()
    unknown = value.keys() - required - optional
    if missing:
        raise ArchiveValidationError(
            "{} is missing keys: {}".format(label, ", ".join(sorted(missing)))
        )
    if unknown:
        raise ArchiveValidationError(
            "{} has unknown keys: {}".format(label, ", ".join(sorted(unknown)))
        )


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ArchiveValidationError("{} must be a non-empty string".format(label))
    return value


def _size(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ArchiveValidationError("{} must be a nonnegative integer".format(label))
    return value


def _positive_size(value: Any, label: str) -> int:
    result = _size(value, label)
    if result == 0:
        raise ArchiveValidationError("{} must be positive".format(label))
    return result


def _sha256(value: Any, label: str, *, prefixed: bool) -> str:
    text = _string(value, label)
    digest = text.removeprefix(SHA256_PREFIX) if prefixed else text
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        expected = (
            "sha256:<64 lowercase hexadecimal characters>"
            if prefixed
            else "64 lowercase hexadecimal characters"
        )
        raise ArchiveValidationError("{} must be {}".format(label, expected))
    if prefixed and not text.startswith(SHA256_PREFIX):
        raise ArchiveValidationError("{} must use sha256".format(label))
    return text


def _descriptor(value: Any, label: str) -> Descriptor:
    record = _object(value, label)
    _exact_keys(record, {"digest", "media_type", "size"}, set(), label)
    return Descriptor(
        digest=_sha256(record["digest"], "{}.digest".format(label), prefixed=True),
        size=_positive_size(record["size"], "{}.size".format(label)),
        media_type=_string(record["media_type"], "{}.media_type".format(label)),
    )


def _validate_metadata_document(value: Any) -> Mapping[str, Any]:
    document = _object(value, "metadata")
    _exact_keys(document, {"schema_version", "images"}, set(), "metadata")
    if document["schema_version"] != 1:
        raise ArchiveValidationError("metadata schema_version must be 1")
    images = _object(document["images"], "metadata.images")
    if set(images) != IMAGE_NAMES:
        raise ArchiveValidationError(
            "metadata.images must contain exactly: {}".format(", ".join(sorted(IMAGE_NAMES)))
        )
    for image_name, image_value in images.items():
        image_label = "metadata.images.{}".format(image_name)
        image = _object(image_value, image_label)
        _exact_keys(image, {"reference", "platforms"}, set(), image_label)
        reference = _string(image["reference"], "{}.reference".format(image_label))
        if "@sha256:" not in reference:
            raise ArchiveValidationError("{}.reference must be digest-pinned".format(image_label))
        _sha256(
            reference.rsplit("@", 1)[1],
            "{}.reference digest".format(image_label),
            prefixed=True,
        )
        platforms = _object(image["platforms"], "{}.platforms".format(image_label))
        if not platforms or not set(platforms).issubset(PLATFORMS):
            raise ArchiveValidationError(
                "{}.platforms must contain known target architectures".format(image_label)
            )
        for target_architecture, platform_value in platforms.items():
            label = "{}.platforms.{}".format(image_label, target_architecture)
            platform = _object(platform_value, label)
            _exact_keys(
                platform,
                {"os", "architecture", "manifest", "archive"},
                {"variant"},
                label,
            )
            expected_os, expected_architecture = PLATFORMS[target_architecture]
            if platform["os"] != expected_os:
                raise ArchiveValidationError("{}.os must be {!r}".format(label, expected_os))
            if platform["architecture"] != expected_architecture:
                raise ArchiveValidationError(
                    "{}.architecture must be {!r}".format(label, expected_architecture)
                )
            variant = platform.get("variant")
            if variant is not None:
                _string(variant, "{}.variant".format(label))
            manifest = _descriptor(platform["manifest"], "{}.manifest".format(label))
            if manifest.media_type not in MANIFEST_MEDIA_TYPES:
                raise ArchiveValidationError("{}.manifest.media_type is unsupported".format(label))
            archive = _object(platform["archive"], "{}.archive".format(label))
            _exact_keys(archive, {"filename", "sha256", "size"}, set(), "{}.archive".format(label))
            filename = _string(archive["filename"], "{}.archive.filename".format(label))
            if Path(filename).name != filename or filename in (".", ".."):
                raise ArchiveValidationError("{}.archive.filename must be a basename".format(label))
            _sha256(archive["sha256"], "{}.archive.sha256".format(label), prefixed=False)
            _positive_size(archive["size"], "{}.archive.size".format(label))
    return document


def load_contract(
    metadata_path: Path,
    image_name: str,
    target_architecture: str,
    expected_reference: str,
    allow_missing: bool = False,
) -> ArchiveContract | None:
    document = _validate_metadata_document(_read_json(metadata_path, str(metadata_path)))
    if image_name not in IMAGE_NAMES:
        raise ArchiveValidationError("unsupported image name: {}".format(image_name))
    if target_architecture not in PLATFORMS:
        raise ArchiveValidationError(
            "unsupported target architecture: {}".format(target_architecture)
        )
    image = _object(document["images"][image_name], "metadata image")
    reference = _string(image["reference"], "metadata image reference")
    if reference != expected_reference:
        raise ArchiveValidationError(
            "{} image reference mismatch: expected {!r}, got {!r}".format(
                image_name, expected_reference, reference
            )
        )
    platforms = _object(image["platforms"], "metadata image platforms")
    if target_architecture not in platforms:
        if allow_missing:
            return None
        raise ArchiveValidationError(
            "trusted offline archive metadata is unavailable for {}/{}".format(
                image_name, target_architecture
            )
        )
    platform = _object(platforms[target_architecture], "metadata platform")
    archive = _object(platform["archive"], "metadata archive")
    return ArchiveContract(
        image=image_name,
        reference=reference,
        target_architecture=target_architecture,
        os=_string(platform["os"], "metadata platform os"),
        oci_architecture=_string(platform["architecture"], "metadata platform architecture"),
        variant=platform.get("variant"),
        manifest=_descriptor(platform["manifest"], "metadata platform manifest"),
        archive_filename=_string(archive["filename"], "metadata archive filename"),
        archive_sha256=_sha256(archive["sha256"], "metadata archive sha256", prefixed=False),
        archive_size=_positive_size(archive["size"], "metadata archive size"),
        enforce_variant=True,
    )


def validate_metadata(metadata_path: Path, expected_references: Sequence[str]) -> None:
    document = _validate_metadata_document(_read_json(metadata_path, str(metadata_path)))
    expected: dict[str, str] = {}
    for item in expected_references:
        try:
            name, reference = item.split("=", 1)
        except ValueError as error:
            raise ArchiveValidationError(
                "--expect must have the form IMAGE=REFERENCE"
            ) from error
        if name in expected:
            raise ArchiveValidationError("duplicate expected image: {}".format(name))
        expected[name] = reference
    if set(expected) != IMAGE_NAMES:
        raise ArchiveValidationError(
            "--expect must name exactly: {}".format(", ".join(sorted(IMAGE_NAMES)))
        )
    images = _object(document["images"], "metadata.images")
    for name, reference in expected.items():
        actual = _object(images[name], "metadata image")["reference"]
        if actual != reference:
            raise ArchiveValidationError(
                "{} image reference mismatch: expected {!r}, got {!r}".format(
                    name, reference, actual
                )
            )


def _normalize_member_name(name: str) -> str:
    while name.startswith("./"):
        name = name[2:]
    if not name or name.startswith("/") or "\\" in name:
        raise ArchiveValidationError("unsafe OCI archive member path: {!r}".format(name))
    if name.endswith("/"):
        name = name[:-1]
    parts = name.split("/")
    if not name or any(part in ("", ".", "..") for part in parts):
        raise ArchiveValidationError("unsafe OCI archive member path: {!r}".format(name))
    path = PurePosixPath(*parts)
    normalized = str(path)
    if normalized in ("oci-layout", "index.json", "blobs", "blobs/sha256"):
        return normalized
    parts = path.parts
    if (
        len(parts) == 3
        and parts[:2] == ("blobs", "sha256")
        and len(parts[2]) == 64
        and all(character in "0123456789abcdef" for character in parts[2])
    ):
        return normalized
    raise ArchiveValidationError("unexpected OCI archive member: {!r}".format(name))


def _read_member(archive: tarfile.TarFile, member: tarfile.TarInfo, label: str) -> bytes:
    if member.size > MAX_JSON_SIZE:
        raise ArchiveValidationError("{} exceeds the JSON size limit".format(label))
    stream = archive.extractfile(member)
    if stream is None:
        raise ArchiveValidationError("cannot read {}".format(label))
    data = stream.read(MAX_JSON_SIZE + 1)
    if len(data) != member.size:
        raise ArchiveValidationError("{} has a truncated payload".format(label))
    return data


def _hash_stream(stream: BinaryIO) -> ArchiveIdentity:
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = stream.read(CHUNK_SIZE)
        if not chunk:
            break
        digest.update(chunk)
        size += len(chunk)
    return ArchiveIdentity(digest.hexdigest(), size)


def _hash_member(archive: tarfile.TarFile, member: tarfile.TarInfo) -> ArchiveIdentity:
    stream = archive.extractfile(member)
    if stream is None:
        raise ArchiveValidationError("cannot read OCI blob {}".format(member.name))
    return _hash_stream(stream)


def _oci_descriptor(value: Any, label: str) -> Descriptor:
    record = _object(value, label)
    digest = _sha256(record.get("digest"), "{}.digest".format(label), prefixed=True)
    size = _positive_size(record.get("size"), "{}.size".format(label))
    media_type = _string(record.get("mediaType"), "{}.mediaType".format(label))
    return Descriptor(digest=digest, size=size, media_type=media_type)


def _blob_name(digest: str) -> str:
    return "blobs/sha256/{}".format(digest.removeprefix(SHA256_PREFIX))


def _verify_blob(
    archive: tarfile.TarFile,
    members: Mapping[str, tarfile.TarInfo],
    descriptor: Descriptor,
    verified: dict[str, ArchiveIdentity],
) -> tarfile.TarInfo:
    name = _blob_name(descriptor.digest)
    member = members.get(name)
    if member is None:
        raise ArchiveValidationError("OCI blob is missing: {}".format(descriptor.digest))
    if member.size != descriptor.size:
        raise ArchiveValidationError(
            "OCI blob size mismatch for {}: expected {}, got {}".format(
                descriptor.digest, descriptor.size, member.size
            )
        )
    identity = verified.get(name)
    if identity is None:
        identity = _hash_member(archive, member)
        verified[name] = identity
    expected_sha256 = descriptor.digest.removeprefix(SHA256_PREFIX)
    if identity.sha256 != expected_sha256:
        raise ArchiveValidationError(
            "OCI blob digest mismatch for {}: got sha256:{}".format(
                descriptor.digest, identity.sha256
            )
        )
    if identity.size != descriptor.size:
        raise ArchiveValidationError(
            "OCI blob payload size mismatch for {}: expected {}, got {}".format(
                descriptor.digest, descriptor.size, identity.size
            )
        )
    return member


def _platform_matches(value: Any, contract: ArchiveContract, label: str) -> None:
    platform = _object(value, label)
    if platform.get("os") != contract.os:
        raise ArchiveValidationError("{} has the wrong operating system".format(label))
    if platform.get("architecture") != contract.oci_architecture:
        raise ArchiveValidationError("{} has the wrong architecture".format(label))
    if contract.enforce_variant and platform.get("variant") != contract.variant:
        raise ArchiveValidationError("{} has the wrong variant".format(label))


def _read_parent_index(
    archive: tarfile.TarFile,
    members: Mapping[str, tarfile.TarInfo],
    descriptor: Descriptor,
    verified: dict[str, ArchiveIdentity],
) -> Mapping[str, Any]:
    parent_member = _verify_blob(archive, members, descriptor, verified)
    parent = _object(
        _decode_json(
            _read_member(archive, parent_member, "OCI parent image index"),
            "OCI parent image index",
        ),
        "OCI parent image index",
    )
    if parent.get("schemaVersion") != 2 or parent.get("mediaType") != INDEX_MEDIA_TYPE:
        raise ArchiveValidationError(
            "OCI parent image index has an invalid schema or mediaType"
        )
    return parent


def _select_parent_manifest(
    parent: Mapping[str, Any], contract: ArchiveContract
) -> tuple[int, Descriptor, Mapping[str, Any]]:
    child_values = parent.get("manifests")
    if not isinstance(child_values, list) or not child_values:
        raise ArchiveValidationError("OCI parent image index contains no manifests")
    matching: list[tuple[int, Descriptor, Mapping[str, Any]]] = []
    child_digests: set[str] = set()
    for child_index, child_value in enumerate(child_values):
        child_label = "OCI parent descriptor {}".format(child_index)
        child_record = _object(child_value, child_label)
        child_descriptor = _oci_descriptor(child_record, child_label)
        if child_descriptor.digest in child_digests:
            raise ArchiveValidationError(
                "OCI parent image index contains a duplicate manifest digest"
            )
        child_digests.add(child_descriptor.digest)
        platform = child_record.get("platform")
        if not isinstance(platform, dict):
            continue
        if (
            platform.get("os") == contract.os
            and platform.get("architecture") == contract.oci_architecture
            and (
                not contract.enforce_variant
                or platform.get("variant") == contract.variant
            )
        ):
            matching.append((child_index, child_descriptor, child_record))
    if len(matching) != 1:
        raise ArchiveValidationError(
            "OCI parent image index must select exactly one trusted platform"
        )
    return matching[0]


def _inspect_archive(
    path: Path,
    contract: ArchiveContract,
    expected_identity: ArchiveIdentity | None = None,
) -> tuple[ArchiveIdentity, Descriptor, dict[str, str]]:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ArchiveValidationError(
            "cannot open OCI archive {}: {}".format(path, error)
        ) from error
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ArchiveValidationError("OCI archive is not a regular file: {}".format(path))
        archive_identity = _hash_stream(stream)
        if expected_identity is not None:
            if archive_identity.sha256 != expected_identity.sha256:
                raise ArchiveValidationError(
                    "offline archive SHA-256 mismatch: expected sha256:{}, got sha256:{}".format(
                        expected_identity.sha256, archive_identity.sha256
                    )
                )
            if archive_identity.size != expected_identity.size:
                raise ArchiveValidationError(
                    "offline archive size mismatch: expected {}, got {}".format(
                        expected_identity.size, archive_identity.size
                    )
                )
        stream.seek(0)
        try:
            archive = tarfile.open(fileobj=stream, mode="r:*")
        except tarfile.TarError as error:
            raise ArchiveValidationError("cannot read OCI archive {}: {}".format(path, error)) from error
        with archive:
            members: dict[str, tarfile.TarInfo] = {}
            for count, member in enumerate(archive, start=1):
                if count > MAX_MEMBERS:
                    raise ArchiveValidationError("OCI archive has too many members")
                name = _normalize_member_name(member.name)
                if name in members:
                    raise ArchiveValidationError("duplicate OCI archive member: {}".format(name))
                if name in ("blobs", "blobs/sha256"):
                    if not member.isdir():
                        raise ArchiveValidationError(
                            "OCI archive namespace member is not a directory: {}".format(
                                name
                            )
                        )
                elif not member.isfile():
                    raise ArchiveValidationError(
                        "OCI archive member is not a regular file: {}".format(name)
                    )
                members[name] = member

            layout_member = members.get("oci-layout")
            index_member = members.get("index.json")
            if layout_member is None or not layout_member.isfile():
                raise ArchiveValidationError("OCI archive lacks a regular oci-layout file")
            if index_member is None or not index_member.isfile():
                raise ArchiveValidationError("OCI archive lacks a regular index.json file")
            layout = _object(
                _decode_json(_read_member(archive, layout_member, "oci-layout"), "oci-layout"),
                "oci-layout",
            )
            if layout.get("imageLayoutVersion") != "1.0.0":
                raise ArchiveValidationError("OCI archive imageLayoutVersion must be 1.0.0")
            index = _object(
                _decode_json(_read_member(archive, index_member, "index.json"), "index.json"),
                "index.json",
            )
            if index.get("schemaVersion") != 2 or index.get("mediaType") != INDEX_MEDIA_TYPE:
                raise ArchiveValidationError("OCI archive index.json has an invalid schema or mediaType")
            manifests = index.get("manifests")
            if not isinstance(manifests, list) or len(manifests) != 1:
                raise ArchiveValidationError(
                    "OCI archive index.json must contain exactly one parent image index"
                )
            root_record = _object(manifests[0], "index.json image")
            root_descriptor = _oci_descriptor(root_record, "index.json image")
            parent_digest = contract.reference.rsplit("@", 1)[1]
            verified: dict[str, ArchiveIdentity] = {}
            if root_descriptor.digest == parent_digest:
                if root_descriptor.media_type != INDEX_MEDIA_TYPE:
                    raise ArchiveValidationError("OCI archive parent is not an OCI image index")
                annotations = _object(root_record.get("annotations"), "index.json annotations")
                if annotations.get(REFERENCE_ANNOTATION) != contract.reference:
                    raise ArchiveValidationError(
                        "OCI archive parent annotation does not match the pinned image reference"
                    )
                parent = _read_parent_index(
                    archive, members, root_descriptor, verified
                )
                selected_index, manifest_descriptor, manifest_record = (
                    _select_parent_manifest(parent, contract)
                )
                if selected_index != 0:
                    raise ArchiveValidationError(
                        "selected platform must be first for SDME 0.18 compatibility"
                    )
            else:
                manifest_descriptor = root_descriptor
                manifest_record = root_record
                parent_name = _blob_name(parent_digest)
                parent_member = members.get(parent_name)
                if parent_member is not None:
                    parent_descriptor = Descriptor(
                        digest=parent_digest,
                        size=parent_member.size,
                        media_type=INDEX_MEDIA_TYPE,
                    )
                    parent = _read_parent_index(
                        archive, members, parent_descriptor, verified
                    )
                    _, parent_manifest, _ = _select_parent_manifest(parent, contract)
                    if parent_manifest != root_descriptor:
                        raise ArchiveValidationError(
                            "direct OCI archive parent does not bind the selected manifest"
                        )
                elif contract.manifest is not None:
                    raise ArchiveValidationError(
                        "direct OCI archive lacks the pinned parent image index"
                    )
            if manifest_descriptor.media_type not in MANIFEST_MEDIA_TYPES:
                raise ArchiveValidationError(
                    "selected OCI image manifest mediaType is unsupported"
                )
            if contract.manifest is not None and manifest_descriptor != contract.manifest:
                raise ArchiveValidationError(
                    "OCI archive root does not resolve to the trusted platform manifest"
                )
            if "platform" in manifest_record:
                _platform_matches(manifest_record["platform"], contract, "selected platform")

            manifest_member = _verify_blob(
                archive, members, manifest_descriptor, verified
            )
            manifest = _object(
                _decode_json(
                    _read_member(archive, manifest_member, "OCI image manifest"),
                    "OCI image manifest",
                ),
                "OCI image manifest",
            )
            if manifest.get("schemaVersion") != 2:
                raise ArchiveValidationError("OCI image manifest schemaVersion must be 2")
            body_media_type = manifest.get("mediaType")
            if body_media_type is not None and body_media_type != manifest_descriptor.media_type:
                raise ArchiveValidationError("OCI image manifest mediaType mismatch")
            config_descriptor = _oci_descriptor(manifest.get("config"), "OCI image config")
            layer_values = manifest.get("layers")
            if not isinstance(layer_values, list) or not layer_values:
                raise ArchiveValidationError("OCI image manifest must contain at least one layer")
            layer_descriptors = [
                _oci_descriptor(value, "OCI image layer {}".format(index_value))
                for index_value, value in enumerate(layer_values)
            ]
            config_member = _verify_blob(archive, members, config_descriptor, verified)
            for layer_descriptor in layer_descriptors:
                _verify_blob(archive, members, layer_descriptor, verified)
            config = _object(
                _decode_json(
                    _read_member(archive, config_member, "OCI image config"),
                    "OCI image config",
                ),
                "OCI image config",
            )
            _platform_matches(config, contract, "OCI image config")
            descriptor_platform = manifest_record.get("platform") or {}
            descriptor_variant = descriptor_platform.get("variant")
            config_variant = config.get("variant")
            if (
                descriptor_variant is not None
                and config_variant is not None
                and descriptor_variant != config_variant
            ):
                raise ArchiveValidationError(
                    "selected descriptor and OCI image config variants disagree"
                )
            selected_platform = {
                "architecture": contract.oci_architecture,
                "os": contract.os,
            }
            selected_variant = descriptor_variant or config_variant
            if selected_variant is not None:
                selected_platform["variant"] = selected_variant
            included_blobs = {
                name for name, member in members.items() if name.startswith("blobs/") and member.isfile()
            }
            if included_blobs != set(verified):
                extra = sorted(included_blobs - set(verified))
                missing = sorted(set(verified) - included_blobs)
                raise ArchiveValidationError(
                    "OCI archive blob closure mismatch: extra={}, missing={}".format(
                        extra, missing
                    )
                )

        after = os.fstat(stream.fileno())
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ArchiveValidationError("OCI archive changed during validation")
    return archive_identity, manifest_descriptor, selected_platform


def verify_archive(
    metadata_path: Path,
    image_name: str,
    target_architecture: str,
    expected_reference: str,
    archive_path: Path,
    acquisition: str,
    require_filename: bool = False,
    recorded_filename: str | None = None,
) -> dict[str, Any]:
    if acquisition not in ("offline", "registry"):
        raise ArchiveValidationError("unsupported acquisition mode: {}".format(acquisition))
    if recorded_filename is not None:
        if not recorded_filename or Path(recorded_filename).name != recorded_filename:
            raise ArchiveValidationError("recorded archive filename must be a basename")
    contract = load_contract(
        metadata_path,
        image_name,
        target_architecture,
        expected_reference,
        allow_missing=acquisition == "registry",
    )
    if contract is None:
        expected_os, expected_architecture = PLATFORMS[target_architecture]
        contract = ArchiveContract(
            image=image_name,
            reference=expected_reference,
            target_architecture=target_architecture,
            os=expected_os,
            oci_architecture=expected_architecture,
            variant=None,
            manifest=None,
            archive_filename=recorded_filename or archive_path.name,
            archive_sha256=None,
            archive_size=None,
            enforce_variant=False,
        )
    if require_filename and archive_path.name != contract.archive_filename:
        raise ArchiveValidationError(
            "archive filename mismatch: expected {!r}, got {!r}".format(
                contract.archive_filename, archive_path.name
            )
        )
    expected_identity = None
    if acquisition == "offline":
        if contract.archive_sha256 is None or contract.archive_size is None:
            raise ArchiveValidationError("offline archive metadata is incomplete")
        expected_identity = ArchiveIdentity(
            contract.archive_sha256, contract.archive_size
        )
    identity, manifest, platform = _inspect_archive(
        archive_path, contract, expected_identity
    )
    return {
        "acquisition": acquisition,
        "archive": {
            "filename": recorded_filename or contract.archive_filename or archive_path.name,
            "sha256": identity.sha256,
            "size": identity.size,
        },
        "image": image_name,
        "manifest": manifest.as_dict(),
        "platform": platform,
        "reference": contract.reference,
        "schema_version": 1,
        "target_architecture": contract.target_architecture,
    }


def write_provenance(value: Mapping[str, Any]) -> None:
    json.dump(value, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def verify_cache(
    metadata_path: Path,
    image_name: str,
    target_architecture: str,
    expected_reference: str,
    archive_path: Path,
    provenance_path: Path,
    required_acquisition: str,
) -> None:
    provenance = _object(
        _read_json(provenance_path, str(provenance_path)), "cached provenance"
    )
    provenance_archive = _object(provenance.get("archive"), "cached provenance archive")
    if provenance_archive.get("filename") != archive_path.name:
        raise ArchiveValidationError("cached provenance archive filename mismatch")
    actual_acquisition = provenance.get("acquisition")
    if actual_acquisition != required_acquisition:
        raise ArchiveValidationError(
            "cached acquisition mode mismatch: expected {!r}, got {!r}".format(
                required_acquisition, actual_acquisition
            )
        )
    expected = verify_archive(
        metadata_path,
        image_name,
        target_architecture,
        expected_reference,
        archive_path,
        required_acquisition,
        require_filename=True,
        recorded_filename=archive_path.name,
    )
    if provenance != expected:
        raise ArchiveValidationError("cached OCI provenance does not match the archive")


def runtime_provenance(
    target_architecture: str,
    build_definition: Path,
    ubuntu_provenance_path: Path,
    nativelink_provenance_path: Path,
) -> dict[str, Any]:
    if target_architecture not in PLATFORMS:
        raise ArchiveValidationError(
            "unsupported target architecture: {}".format(target_architecture)
        )
    images: dict[str, Mapping[str, Any]] = {}
    for name, path in (
        ("ubuntu", ubuntu_provenance_path),
        ("nativelink", nativelink_provenance_path),
    ):
        provenance = _object(_read_json(path, str(path)), "{} provenance".format(name))
        if provenance.get("schema_version") != 1:
            raise ArchiveValidationError("{} provenance schema is unsupported".format(name))
        if provenance.get("image") != name:
            raise ArchiveValidationError("{} provenance names the wrong image".format(name))
        if provenance.get("target_architecture") != target_architecture:
            raise ArchiveValidationError(
                "{} provenance names the wrong target architecture".format(name)
            )
        images[name] = provenance
    try:
        build_bytes = build_definition.read_bytes()
    except OSError as error:
        raise ArchiveValidationError(
            "cannot read build definition {}: {}".format(build_definition, error)
        ) from error
    return {
        "build_definition_sha256": hashlib.sha256(build_bytes).hexdigest(),
        "images": images,
        "schema_version": 1,
        "target_architecture": target_architecture,
    }


def _parser() -> ArgumentParser:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    metadata = subparsers.add_parser("metadata", help="validate trusted metadata")
    metadata.add_argument("metadata", type=Path)
    metadata.add_argument("--expect", action="append", default=[], metavar="IMAGE=REFERENCE")

    verify = subparsers.add_parser("verify", help="validate an OCI archive")
    verify.add_argument("metadata", type=Path)
    verify.add_argument("image", choices=sorted(IMAGE_NAMES))
    verify.add_argument("architecture", choices=sorted(PLATFORMS))
    verify.add_argument("reference")
    verify.add_argument("archive", type=Path)
    verify.add_argument("--acquisition", choices=("offline", "registry"), required=True)
    verify.add_argument("--require-filename", action="store_true")
    verify.add_argument("--record-filename")

    cache = subparsers.add_parser("cache", help="validate a managed archive and provenance")
    cache.add_argument("metadata", type=Path)
    cache.add_argument("image", choices=sorted(IMAGE_NAMES))
    cache.add_argument("architecture", choices=sorted(PLATFORMS))
    cache.add_argument("reference")
    cache.add_argument("archive", type=Path)
    cache.add_argument("provenance", type=Path)
    cache.add_argument("--acquisition", choices=("offline", "registry"), required=True)

    runtime = subparsers.add_parser(
        "runtime", help="compose runtime provenance from admitted image inputs"
    )
    runtime.add_argument("architecture", choices=sorted(PLATFORMS))
    runtime.add_argument("build_definition", type=Path)
    runtime.add_argument("ubuntu_provenance", type=Path)
    runtime.add_argument("nativelink_provenance", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        logging.basicConfig(
            level=logging.DEBUG if args.verbose else logging.WARNING,
            format="oci-archive: %(levelname)s: %(message)s",
        )
        if args.command == "metadata":
            validate_metadata(args.metadata, args.expect)
        elif args.command == "verify":
            provenance = verify_archive(
                args.metadata,
                args.image,
                args.architecture,
                args.reference,
                args.archive,
                args.acquisition,
                args.require_filename,
                args.record_filename,
            )
            write_provenance(provenance)
        elif args.command == "cache":
            verify_cache(
                args.metadata,
                args.image,
                args.architecture,
                args.reference,
                args.archive,
                args.provenance,
                args.acquisition,
            )
        else:
            write_provenance(
                runtime_provenance(
                    args.architecture,
                    args.build_definition,
                    args.ubuntu_provenance,
                    args.nativelink_provenance,
                )
            )
        return 0
    except (ArchiveValidationError, OSError, tarfile.TarError) as error:
        print("oci-archive: error: {}".format(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
