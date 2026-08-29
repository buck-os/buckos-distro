#!/usr/bin/env python3
"""Reproduce the trusted offline OCI archives from digest-pinned registries."""

from __future__ import annotations

import argparse
from contextlib import AbstractContextManager
from dataclasses import dataclass
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import re
import sys
import tarfile
import tempfile
from typing import Any, BinaryIO, Mapping, Protocol, Sequence
import urllib.error
import urllib.parse
import urllib.request

from oci_archive import ArchiveValidationError, load_contract, verify_archive


LOG = logging.getLogger("oci-acquire")
LOG.setLevel(logging.WARNING)
CHUNK_SIZE = 1024 * 1024
MAX_JSON_SIZE = 16 * 1024 * 1024
MAX_TOKEN_SIZE = 1024 * 1024
INDEX_MEDIA_TYPE = "application/vnd.oci.image.index.v1+json"
MANIFEST_MEDIA_TYPES = frozenset(
    {
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    }
)
MANIFEST_ACCEPT = ", ".join((INDEX_MEDIA_TYPE, *sorted(MANIFEST_MEDIA_TYPES)))
REFERENCE_PATTERN = re.compile(
    r"^(?P<registry>[a-z0-9.-]+(?::[0-9]+)?)/"
    r"(?P<repository>[a-z0-9._-]+(?:/[a-z0-9._-]+)*)@"
    r"(?P<digest>sha256:[0-9a-f]{64})$"
)
IMAGE_NAMES = ("ubuntu", "nativelink")
TARGET_ARCHITECTURES = ("aarch64", "x86_64")


class AcquisitionError(ValueError):
    """Registry content cannot reproduce the tracked archive."""


@dataclass(frozen=True)
class Descriptor:
    digest: str
    size: int
    media_type: str


class DescriptorContract(Protocol):
    digest: str
    size: int
    media_type: str


class ArchiveContract(Protocol):
    image: str
    reference: str
    target_architecture: str
    os: str
    oci_architecture: str
    variant: str | None
    manifest: DescriptorContract | None
    archive_filename: str | None
    archive_sha256: str | None
    archive_size: int | None


@dataclass(frozen=True)
class RegistryReference:
    value: str
    api_registry: str
    repository: str
    digest: str

    def endpoint(self, kind: str, digest: str) -> str:
        quoted_repository = urllib.parse.quote(self.repository, safe="/")
        quoted_digest = urllib.parse.quote(digest, safe=":")
        return "https://{}/v2/{}/{}/{}".format(
            self.api_registry, quoted_repository, kind, quoted_digest
        )


class RegistryTransport(Protocol):
    def open_manifest(
        self, reference: RegistryReference, digest: str
    ) -> AbstractContextManager[BinaryIO]: ...

    def open_blob(
        self, reference: RegistryReference, digest: str
    ) -> AbstractContextManager[BinaryIO]: ...


class HTTPRegistryTransport:
    """Anonymous OCI Distribution transport with Bearer challenge support."""

    def __init__(self) -> None:
        self._tokens: dict[tuple[str, str], str] = {}

    @staticmethod
    def _request(
        url: str, *, token: str | None = None, accept: str | None = None
    ) -> urllib.request.Request:
        headers = {
            "Accept-Encoding": "identity",
            "User-Agent": "buckos-offline-oci-acquire/1",
        }
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        if accept is not None:
            headers["Accept"] = accept
        return urllib.request.Request(url, headers=headers)

    @staticmethod
    def _challenge_parameters(challenge: str) -> Mapping[str, str]:
        try:
            scheme, parameters = challenge.split(" ", 1)
        except ValueError as error:
            raise AcquisitionError(
                "registry returned an invalid authentication challenge"
            ) from error
        if scheme.lower() != "bearer":
            raise AcquisitionError("registry requires unsupported authentication")
        values = urllib.request.parse_keqv_list(
            urllib.request.parse_http_list(parameters)
        )
        realm = values.get("realm")
        if not realm:
            raise AcquisitionError("registry Bearer challenge lacks a realm")
        parsed_realm = urllib.parse.urlsplit(realm)
        if parsed_realm.scheme != "https" or not parsed_realm.netloc:
            raise AcquisitionError("registry Bearer realm must use HTTPS")
        return values

    def _fetch_token(
        self, reference: RegistryReference, challenge: str
    ) -> str:
        values = self._challenge_parameters(challenge)
        realm = values["realm"]
        query = urllib.parse.parse_qsl(urllib.parse.urlsplit(realm).query)
        for key in ("service", "scope"):
            if values.get(key):
                query.append((key, values[key]))
        parsed = urllib.parse.urlsplit(realm)
        url = urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), "")
        )
        LOG.debug("requesting public registry token for %s", reference.repository)
        with urllib.request.urlopen(self._request(url), timeout=60) as response:
            payload = _read_bounded(response, MAX_TOKEN_SIZE, "registry token response")
        value = _decode_json(payload, "registry token response")
        if not isinstance(value, dict):
            raise AcquisitionError("registry token response must be an object")
        token = value.get("token") or value.get("access_token")
        if not isinstance(token, str) or not token:
            raise AcquisitionError("registry token response lacks a token")
        return token

    def _open(
        self, reference: RegistryReference, url: str, accept: str | None
    ) -> AbstractContextManager[BinaryIO]:
        key = (reference.api_registry, reference.repository)
        token = self._tokens.get(key)
        try:
            return urllib.request.urlopen(
                self._request(url, token=token, accept=accept), timeout=120
            )
        except urllib.error.HTTPError as error:
            if error.code != 401:
                raise
            challenge = error.headers.get("WWW-Authenticate")
            error.close()
            if not challenge:
                raise AcquisitionError(
                    "registry denied access without an authentication challenge"
                ) from error
            token = self._fetch_token(reference, challenge)
            self._tokens[key] = token
            return urllib.request.urlopen(
                self._request(url, token=token, accept=accept), timeout=120
            )

    def open_manifest(
        self, reference: RegistryReference, digest: str
    ) -> AbstractContextManager[BinaryIO]:
        return self._open(
            reference, reference.endpoint("manifests", digest), MANIFEST_ACCEPT
        )

    def open_blob(
        self, reference: RegistryReference, digest: str
    ) -> AbstractContextManager[BinaryIO]:
        return self._open(reference, reference.endpoint("blobs", digest), None)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AcquisitionError("duplicate JSON key: {!r}".format(key))
        result[key] = value
    return result


def _decode_json(data: bytes, label: str) -> Any:
    try:
        return json.loads(data, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AcquisitionError("{} is not valid JSON: {}".format(label, error)) from error


def _read_bounded(stream: BinaryIO, limit: int, label: str) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while chunk := stream.read(min(CHUNK_SIZE, limit + 1 - size)):
        chunks.append(chunk)
        size += len(chunk)
        if size > limit:
            raise AcquisitionError("{} exceeds {} bytes".format(label, limit))
    return b"".join(chunks)


def _content_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise AcquisitionError(
            "{} must be sha256:<64 lowercase hexadecimal characters>".format(label)
        )
    return value


def _positive_size(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise AcquisitionError("{} must be a positive integer".format(label))
    return value


def _descriptor(value: Any, label: str) -> Descriptor:
    if not isinstance(value, dict):
        raise AcquisitionError("{} must be an object".format(label))
    digest = _digest(value.get("digest"), "{}.digest".format(label))
    size = _positive_size(value.get("size"), "{}.size".format(label))
    media_type = value.get("mediaType")
    if not isinstance(media_type, str) or not media_type:
        raise AcquisitionError("{}.mediaType must be a non-empty string".format(label))
    return Descriptor(digest=digest, size=size, media_type=media_type)


def _parse_reference(value: str) -> RegistryReference:
    match = REFERENCE_PATTERN.fullmatch(value)
    if match is None:
        raise AcquisitionError(
            "image reference must be REGISTRY/REPOSITORY@sha256:DIGEST"
        )
    registry = match.group("registry")
    api_registry = "registry-1.docker.io" if registry == "docker.io" else registry
    return RegistryReference(
        value=value,
        api_registry=api_registry,
        repository=match.group("repository"),
        digest=match.group("digest"),
    )


def _metadata_references(metadata_path: Path) -> dict[str, str]:
    try:
        document = _decode_json(metadata_path.read_bytes(), str(metadata_path))
    except OSError as error:
        raise AcquisitionError(
            "cannot read metadata {}: {}".format(metadata_path, error)
        ) from error
    if not isinstance(document, dict) or not isinstance(document.get("images"), dict):
        raise AcquisitionError("metadata must contain an images object")
    references: dict[str, str] = {}
    for image_name in IMAGE_NAMES:
        image = document["images"].get(image_name)
        if not isinstance(image, dict) or not isinstance(image.get("reference"), str):
            raise AcquisitionError(
                "metadata image {} lacks a reference".format(image_name)
            )
        references[image_name] = image["reference"]
    return references


def _expected_platform(contract: ArchiveContract) -> dict[str, str]:
    platform = {
        "os": contract.os,
        "architecture": contract.oci_architecture,
    }
    if contract.variant is not None:
        platform["variant"] = contract.variant
    return platform


def _platform(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise AcquisitionError("{} must be an object".format(label))
    result = {
        key: value[key]
        for key in ("os", "architecture", "variant")
        if key in value
    }
    if any(not isinstance(item, str) or not item for item in result.values()):
        raise AcquisitionError("{} values must be non-empty strings".format(label))
    return result


def _fetch_document(
    opener: AbstractContextManager[BinaryIO],
    expected_digest: str,
    expected_size: int | None,
    label: str,
) -> bytes:
    limit = MAX_JSON_SIZE if expected_size is None else min(MAX_JSON_SIZE, expected_size)
    with opener as stream:
        data = _read_bounded(stream, limit, label)
    if expected_size is not None and len(data) != expected_size:
        raise AcquisitionError(
            "{} size mismatch: expected {}, got {}".format(
                label, expected_size, len(data)
            )
        )
    actual_digest = _content_digest(data)
    if actual_digest != expected_digest:
        raise AcquisitionError(
            "{} digest mismatch: expected {}, got {}".format(
                label, expected_digest, actual_digest
            )
        )
    return data


def _download_blob(
    transport: RegistryTransport,
    reference: RegistryReference,
    descriptor: Descriptor,
    destination: Path,
    label: str,
) -> None:
    digest = hashlib.sha256()
    size = 0
    temporary = destination.with_name(destination.name + ".tmp")
    try:
        with transport.open_blob(reference, descriptor.digest) as stream, temporary.open(
            "xb"
        ) as output:
            while chunk := stream.read(CHUNK_SIZE):
                size += len(chunk)
                if size > descriptor.size:
                    raise AcquisitionError(
                        "{} exceeds its declared size {}".format(label, descriptor.size)
                    )
                output.write(chunk)
                digest.update(chunk)
            output.flush()
            os.fsync(output.fileno())
        if size != descriptor.size:
            raise AcquisitionError(
                "{} size mismatch: expected {}, got {}".format(
                    label, descriptor.size, size
                )
            )
        actual_digest = "sha256:" + digest.hexdigest()
        if actual_digest != descriptor.digest:
            raise AcquisitionError(
                "{} digest mismatch: expected {}, got {}".format(
                    label, descriptor.digest, actual_digest
                )
            )
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _tar_info(name: str, *, size: int = 0, directory: bool = False) -> tarfile.TarInfo:
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


def _add_bytes(archive: tarfile.TarFile, name: str, data: bytes) -> None:
    archive.addfile(_tar_info(name, size=len(data)), io.BytesIO(data))


def _add_path(archive: tarfile.TarFile, name: str, path: Path) -> None:
    with path.open("rb") as stream:
        archive.addfile(_tar_info(name, size=path.stat().st_size), stream)


def _write_archive(
    path: Path,
    contract: ArchiveContract,
    selected_record: Mapping[str, Any],
    parent: bytes,
    blobs: Mapping[str, bytes | Path],
) -> None:
    parent_digest = contract.reference.rsplit("@", 1)[1]
    if contract.target_architecture == "x86_64":
        root_descriptor: Mapping[str, Any] = {
            "annotations": {
                "org.opencontainers.image.ref.name": contract.reference,
            },
            "digest": parent_digest,
            "mediaType": INDEX_MEDIA_TYPE,
            "size": len(parent),
        }
    else:
        root_descriptor = selected_record
    index = (
        json.dumps(
            {
                "manifests": [root_descriptor],
                "mediaType": INDEX_MEDIA_TYPE,
                "schemaVersion": 2,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    layout = b'{"imageLayoutVersion":"1.0.0"}\n'
    with path.open("xb") as raw:
        with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            archive.addfile(_tar_info("blobs", directory=True))
            archive.addfile(_tar_info("blobs/sha256", directory=True))
            _add_bytes(archive, "oci-layout", layout)
            _add_bytes(archive, "index.json", index)
            for digest in sorted(blobs):
                name = "blobs/sha256/" + digest.removeprefix("sha256:")
                value = blobs[digest]
                if isinstance(value, bytes):
                    _add_bytes(archive, name, value)
                else:
                    _add_path(archive, name, value)
        raw.flush()
        os.fsync(raw.fileno())
    os.chmod(path, 0o600)


def _acquire_image(
    metadata_path: Path,
    contract: ArchiveContract,
    staging_dir: Path,
    transport: RegistryTransport,
) -> Path:
    if contract.manifest is None or contract.archive_filename is None:
        raise AcquisitionError("offline archive contract is incomplete")
    tracked_manifest = Descriptor(
        digest=contract.manifest.digest,
        size=contract.manifest.size,
        media_type=contract.manifest.media_type,
    )
    reference = _parse_reference(contract.reference)
    LOG.info("%s: fetching pinned parent %s", contract.image, reference.digest)
    parent = _fetch_document(
        transport.open_manifest(reference, reference.digest),
        reference.digest,
        None,
        "{} parent index".format(contract.image),
    )
    parent_value = _decode_json(parent, "{} parent index".format(contract.image))
    if not isinstance(parent_value, dict):
        raise AcquisitionError("{} parent index must be an object".format(contract.image))
    if (
        parent_value.get("schemaVersion") != 2
        or parent_value.get("mediaType") != INDEX_MEDIA_TYPE
    ):
        raise AcquisitionError("{} parent is not an OCI image index".format(contract.image))
    manifests = parent_value.get("manifests")
    if not isinstance(manifests, list) or not manifests:
        raise AcquisitionError("{} parent index has no manifests".format(contract.image))
    expected_platform = _expected_platform(contract)
    matching: list[tuple[int, Mapping[str, Any], Descriptor]] = []
    child_digests: set[str] = set()
    for index, value in enumerate(manifests):
        if not isinstance(value, dict):
            raise AcquisitionError(
                "{} parent descriptor {} must be an object".format(contract.image, index)
            )
        descriptor = _descriptor(
            value, "{} parent descriptor {}".format(contract.image, index)
        )
        if descriptor.digest in child_digests:
            raise AcquisitionError(
                "{} parent contains a duplicate manifest digest".format(contract.image)
            )
        child_digests.add(descriptor.digest)
        platform_value = value.get("platform")
        if platform_value is not None and _platform(
            platform_value, "{} parent platform {}".format(contract.image, index)
        ) == expected_platform:
            matching.append((index, value, descriptor))
    if len(matching) != 1:
        raise AcquisitionError(
            "{} parent must select exactly one tracked platform".format(contract.image)
        )
    selected_index, selected_record, selected_descriptor = matching[0]
    if selected_descriptor != tracked_manifest:
        raise AcquisitionError(
            "{} selected descriptor does not match tracked metadata".format(contract.image)
        )
    if (
        contract.target_architecture == "x86_64"
        and selected_index != 0
    ):
        raise AcquisitionError(
            "{} selected x86_64 descriptor is not first for SDME 0.18".format(
                contract.image
            )
        )

    LOG.info("%s: fetching selected manifest %s", contract.image, selected_descriptor.digest)
    manifest = _fetch_document(
        transport.open_manifest(reference, selected_descriptor.digest),
        selected_descriptor.digest,
        selected_descriptor.size,
        "{} selected manifest".format(contract.image),
    )
    manifest_value = _decode_json(
        manifest, "{} selected manifest".format(contract.image)
    )
    if not isinstance(manifest_value, dict) or manifest_value.get("schemaVersion") != 2:
        raise AcquisitionError("{} selected manifest is invalid".format(contract.image))
    if manifest_value.get("mediaType") != selected_descriptor.media_type:
        raise AcquisitionError(
            "{} selected manifest mediaType mismatch".format(contract.image)
        )
    config_descriptor = _descriptor(
        manifest_value.get("config"), "{} config descriptor".format(contract.image)
    )
    layer_values = manifest_value.get("layers")
    if not isinstance(layer_values, list) or not layer_values:
        raise AcquisitionError("{} selected manifest has no layers".format(contract.image))
    layer_descriptors = [
        _descriptor(value, "{} layer descriptor {}".format(contract.image, index))
        for index, value in enumerate(layer_values)
    ]

    blob_dir = staging_dir / ("." + contract.image + "-blobs")
    blob_dir.mkdir(mode=0o700)
    blobs: dict[str, bytes | Path] = {
        reference.digest: parent,
        selected_descriptor.digest: manifest,
    }
    for label, descriptor in [
        ("config", config_descriptor),
        *[("layer {}".format(index), value) for index, value in enumerate(layer_descriptors)],
    ]:
        existing = blobs.get(descriptor.digest)
        if existing is not None:
            if not isinstance(existing, Path):
                raise AcquisitionError(
                    "{} {} reuses an index or manifest digest".format(
                        contract.image, label
                    )
                )
            if existing.stat().st_size != descriptor.size:
                raise AcquisitionError(
                    "{} descriptors disagree about blob size".format(contract.image)
                )
            continue
        destination = blob_dir / descriptor.digest.removeprefix("sha256:")
        LOG.info(
            "%s: fetching %s %s (%d bytes)",
            contract.image,
            label,
            descriptor.digest,
            descriptor.size,
        )
        _download_blob(
            transport,
            reference,
            descriptor,
            destination,
            "{} {}".format(contract.image, label),
        )
        blobs[descriptor.digest] = destination

    config_path = blobs[config_descriptor.digest]
    if not isinstance(config_path, Path):
        raise AcquisitionError("{} config digest collision".format(contract.image))
    if config_descriptor.size > MAX_JSON_SIZE:
        raise AcquisitionError("{} config exceeds JSON size limit".format(contract.image))
    with config_path.open("rb") as stream:
        config_bytes = _read_bounded(
            stream, MAX_JSON_SIZE, "{} image config".format(contract.image)
        )
    config = _decode_json(
        config_bytes, "{} image config".format(contract.image)
    )
    if not isinstance(config, dict) or _platform(
        config, "{} image config".format(contract.image)
    ) != expected_platform:
        raise AcquisitionError("{} config platform mismatch".format(contract.image))

    archive_path = staging_dir / contract.archive_filename
    _write_archive(archive_path, contract, selected_record, parent, blobs)
    verify_archive(
        metadata_path,
        contract.image,
        contract.target_architecture,
        contract.reference,
        archive_path,
        "offline",
        require_filename=True,
    )
    return archive_path


def acquire_architecture(
    metadata_path: Path,
    architecture: str,
    output_dir: Path,
    transport: RegistryTransport,
) -> list[dict[str, Any]]:
    references = _metadata_references(metadata_path)
    contracts = []
    for image_name in IMAGE_NAMES:
        contract = load_contract(
            metadata_path, image_name, architecture, references[image_name]
        )
        if contract is None:
            raise AcquisitionError(
                "metadata lacks {}/{}".format(image_name, architecture)
            )
        contracts.append(contract)
    filenames = [contract.archive_filename for contract in contracts]
    if len(filenames) != len(set(filenames)):
        raise AcquisitionError("metadata reuses an archive filename")
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not output_dir.is_dir():
        raise AcquisitionError("output path is not a directory: {}".format(output_dir))
    destinations = [output_dir / str(filename) for filename in filenames]
    for destination in destinations:
        if destination.exists():
            raise AcquisitionError("refusing to overwrite {}".format(destination))

    with tempfile.TemporaryDirectory(prefix=".oci-acquire-", dir=output_dir) as value:
        staging_dir = Path(value)
        archives = [
            _acquire_image(metadata_path, contract, staging_dir, transport)
            for contract in contracts
        ]
        results = []
        for contract, archive_path, destination in zip(
            contracts, archives, destinations, strict=True
        ):
            os.replace(archive_path, destination)
            results.append(
                {
                    "filename": destination.name,
                    "sha256": contract.archive_sha256,
                    "size": contract.archive_size,
                }
            )
        directory = os.open(output_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    return results


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--architecture", choices=TARGET_ARCHITECTURES, required=True
    )
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        log_level = logging.DEBUG if args.verbose else logging.WARNING
        logging.basicConfig(
            level=log_level,
            format="oci-acquire: %(levelname)s: %(message)s",
        )
        LOG.setLevel(log_level)
        results = acquire_architecture(
            args.metadata,
            args.architecture,
            args.output_directory,
            HTTPRegistryTransport(),
        )
        json.dump(results, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0
    except (
        AcquisitionError,
        ArchiveValidationError,
        OSError,
        urllib.error.URLError,
    ) as error:
        print("oci-acquire: error: {}".format(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
