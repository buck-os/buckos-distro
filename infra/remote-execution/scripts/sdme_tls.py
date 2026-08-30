#!/usr/bin/env python3
"""Validate and stage NativeLink mTLS credentials for SDME provisioning."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import ssl
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


LOG = logging.getLogger("sdme-tls")
MAX_CREDENTIAL_BYTES = 1024 * 1024
SOURCE_MODES = {0o400, 0o600}
EXCLUSIVE_TRUST_OPTIONS = ("-no-CAfile", "-no-CApath", "-no-CAstore")
# Parsed certificates are materialized here rather than under TMPDIR, which the
# caller controls. mkdtemp gives a 0700 directory owned by this process inside
# it, and the private key is never written to any of them.
PRIVATE_WORK_ROOT = "/var/tmp"
PEM_PATTERN = re.compile(
    rb"-----BEGIN ([A-Z0-9 ]+)-----\r?\n.*?-----END \1-----\r?\n?",
    re.DOTALL,
)
DNS_PATTERN = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)


class ValidationError(Exception):
    """A credential input failed admission."""


@dataclass(frozen=True)
class Credential:
    option: str
    source: Path
    destination: Optional[str]
    kind: str


def sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def run_openssl(
    openssl: Path,
    arguments: Sequence[str],
    *,
    input_bytes: Optional[bytes] = None,
) -> bytes:
    try:
        invocation = {
            "check": False,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "timeout": 10,
            "env": {"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
        }
        if input_bytes is None:
            invocation["stdin"] = subprocess.DEVNULL
        else:
            invocation["input"] = input_bytes
        result = subprocess.run([str(openssl), *arguments], **invocation)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValidationError("openssl invocation failed") from error
    if result.returncode != 0:
        diagnostic = result.stderr.decode("utf-8", errors="replace").strip()
        if diagnostic:
            diagnostic = diagnostic.splitlines()[-1]
            raise ValidationError("openssl rejected credential: {}".format(diagnostic))
        raise ValidationError("openssl rejected credential")
    return result.stdout


def validate_dns_name(value: str) -> None:
    if not DNS_PATTERN.fullmatch(value):
        raise ValidationError("--control-dns must be a canonical lowercase DNS name")


def resolve_exclusion_roots(values: Sequence[str]) -> List[Path]:
    roots: List[Path] = []
    for value in values:
        path = Path(value)
        if not path.is_absolute():
            raise ValidationError("--exclude-root must be an absolute path")
        # The root may not exist yet, so resolve without requiring it. Comparing
        # resolved roots against resolved sources keeps a symlinked checkout or
        # data root from hiding a credential inside it.
        roots.append(path.resolve())
    return roots


def validate_source_path(
    option: str,
    value: str,
    exclusion_roots: Sequence[Path] = (),
) -> Tuple[Path, bytes]:
    path = Path(value)
    if not path.is_absolute():
        raise ValidationError("{} must be an absolute path".format(option))
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ValidationError("{} does not exist".format(option)) from error
    if path != resolved:
        raise ValidationError("{} must not contain symlink components".format(option))
    for root in exclusion_roots:
        if resolved == root or root in resolved.parents:
            raise ValidationError(
                "{} must be outside the repository and managed data root".format(option)
            )

    current = Path("/")
    for component in resolved.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except OSError as error:
            raise ValidationError("cannot inspect {} ancestry".format(option)) from error
        if stat.S_ISLNK(metadata.st_mode):
            raise ValidationError("{} must not contain symlink components".format(option))
        if metadata.st_uid != 0 or metadata.st_gid != 0:
            raise ValidationError("{} path must be owned by root:root".format(option))
        if metadata.st_mode & 0o022:
            raise ValidationError(
                "{} path must not be group/world-writable".format(option)
            )

    try:
        descriptor = os.open(
            resolved,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as error:
        raise ValidationError("cannot open {}".format(option)) from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValidationError("{} must be a regular file".format(option))
        if metadata.st_uid != 0 or metadata.st_gid != 0:
            raise ValidationError("{} must be owned by root:root".format(option))
        if stat.S_IMODE(metadata.st_mode) not in SOURCE_MODES:
            raise ValidationError("{} must have mode 0400 or 0600".format(option))
        if metadata.st_nlink != 1:
            raise ValidationError("{} must have exactly one hard link".format(option))
        if metadata.st_size <= 0 or metadata.st_size > MAX_CREDENTIAL_BYTES:
            raise ValidationError("{} has an invalid size".format(option))
        chunks: List[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 65536))
            if not chunk:
                raise ValidationError("{} changed while being read".format(option))
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ValidationError("{} changed while being read".format(option))
        after = os.fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino, metadata.st_size) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
        ):
            raise ValidationError("{} changed while being read".format(option))
    finally:
        os.close(descriptor)
    return resolved, b"".join(chunks)


def split_pem(label: str, payload: bytes, allowed_types: Sequence[bytes]) -> List[bytes]:
    blocks: List[bytes] = []
    offset = 0
    for match in PEM_PATTERN.finditer(payload):
        if payload[offset : match.start()].strip():
            raise ValidationError("{} contains data outside PEM blocks".format(label))
        if match.group(1) not in allowed_types:
            raise ValidationError("{} contains an unexpected PEM block".format(label))
        blocks.append(match.group(0).rstrip() + b"\n")
        offset = match.end()
    if payload[offset:].strip() or not blocks:
        raise ValidationError("{} is not a complete PEM file".format(label))
    return blocks


def write_private_file(path: Path, payload: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short credential write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def cert_der(openssl: Path, certificate: Path) -> bytes:
    return run_openssl(
        openssl,
        ["x509", "-in", str(certificate), "-outform", "DER"],
    )


def cert_public_key(openssl: Path, certificate: Path) -> bytes:
    public_pem = run_openssl(
        openssl,
        ["x509", "-in", str(certificate), "-pubkey", "-noout"],
    )
    return run_openssl(
        openssl,
        ["pkey", "-pubin", "-outform", "DER"],
        input_bytes=public_pem,
    )


def private_public_key(openssl: Path, private_key_pem: bytes) -> bytes:
    """Derive the public key without the private key ever reaching a file."""
    return run_openssl(
        openssl,
        ["pkey", "-passin", "pass:", "-pubout", "-outform", "DER"],
        input_bytes=private_key_pem,
    )


def require_valid_now(openssl: Path, certificate: Path, label: str) -> None:
    """Reject a certificate whose validity has not started.

    `-checkend` reads only notAfter, so a certificate issued for the future
    passes every expiry check while no TLS stack will accept it.
    """
    output = run_openssl(
        openssl,
        ["x509", "-in", str(certificate), "-noout", "-startdate"],
    ).decode("utf-8", errors="replace").strip()
    prefix = "notBefore="
    if not output.startswith(prefix):
        raise ValidationError("cannot read {} validity start".format(label))
    try:
        start = ssl.cert_time_to_seconds(output[len(prefix):])
    except ValueError as error:
        raise ValidationError("cannot parse {} validity start".format(label)) from error
    if start > time.time():
        raise ValidationError("{} is not yet valid".format(label))


def require_leaf_properties(
    openssl: Path,
    leaf: Path,
    purpose: str,
    minimum_validity_seconds: int,
    control_dns: Optional[str],
) -> None:
    run_openssl(
        openssl,
        ["x509", "-in", str(leaf), "-noout", "-checkend", str(minimum_validity_seconds)],
    )
    require_valid_now(openssl, leaf, "leaf certificate")
    eku = run_openssl(
        openssl,
        ["x509", "-in", str(leaf), "-noout", "-ext", "extendedKeyUsage"],
    ).decode("utf-8", errors="replace")
    required_eku = (
        "TLS Web Server Authentication"
        if purpose == "sslserver"
        else "TLS Web Client Authentication"
    )
    if required_eku not in eku:
        raise ValidationError("leaf certificate lacks required {} EKU".format(required_eku))

    basic_constraints = run_openssl(
        openssl,
        ["x509", "-in", str(leaf), "-noout", "-ext", "basicConstraints"],
    ).decode("utf-8", errors="replace")
    if "CA:TRUE" in basic_constraints:
        raise ValidationError("leaf certificate must not be a CA")

    if control_dns is not None:
        try:
            decoded = ssl._ssl._test_decode_cert(str(leaf))  # type: ignore[attr-defined]
        except (OSError, ValueError, ssl.SSLError) as error:
            raise ValidationError("cannot decode control certificate SAN") from error
        dns_names = {
            value
            for name_type, value in decoded.get("subjectAltName", ())
            if name_type == "DNS"
        }
        if control_dns not in dns_names:
            raise ValidationError(
                "control certificate lacks exact DNS SAN {}".format(control_dns)
            )


def require_ca_properties(openssl: Path, certificate: Path) -> None:
    run_openssl(
        openssl,
        ["x509", "-in", str(certificate), "-noout", "-checkend", "0"],
    )
    require_valid_now(openssl, certificate, "CA certificate")
    basic_constraints = run_openssl(
        openssl,
        ["x509", "-in", str(certificate), "-noout", "-ext", "basicConstraints"],
    ).decode("utf-8", errors="replace")
    if "CA:TRUE" not in basic_constraints:
        raise ValidationError("CA bundle contains a certificate without CA:TRUE")
    key_usage = run_openssl(
        openssl,
        ["x509", "-in", str(certificate), "-noout", "-ext", "keyUsage"],
    ).decode("utf-8", errors="replace")
    if "Certificate Sign" not in key_usage:
        raise ValidationError("CA bundle contains a certificate without keyCertSign")


@contextlib.contextmanager
def private_work_directory():
    """A 0700 directory for parsed certificates, never taken from TMPDIR.

    `tempfile` resolves its base from the caller's environment, which is not
    something a privileged credential path should inherit. The base is checked
    the way `--stage-dir` is, so the helper is not trusting a directory it did
    not verify, and mkdtemp supplies the 0700 leaf.
    """
    base = Path(PRIVATE_WORK_ROOT)
    try:
        metadata = base.lstat()
    except OSError as error:
        raise ValidationError("credential work root does not exist") from error
    if base.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise ValidationError("credential work root must be a real directory")
    if metadata.st_uid != 0:
        raise ValidationError("credential work root must be owned by root")
    mode = stat.S_IMODE(metadata.st_mode)
    if mode & 0o022 and not mode & stat.S_ISVTX:
        raise ValidationError(
            "credential work root is group/world-writable without the sticky bit"
        )
    directory = Path(tempfile.mkdtemp(prefix="sdme-tls-", dir=str(base)))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def materialize_certificates(directory: Path, name: str, blocks: Sequence[bytes]) -> List[Path]:
    paths = []
    for index, block in enumerate(blocks):
        path = directory / "{}-{}.pem".format(name, index)
        write_private_file(path, block)
        paths.append(path)
    return paths


def require_exclusive_trust_options(openssl: Path) -> List[str]:
    """Return the flags that confine `openssl verify` to the supplied bundle.

    Without them the compiled-in default CA file, directory, or store can
    supplement the operator-supplied issuer and admit a chain the bundle alone
    does not trust.
    """
    try:
        result = subprocess.run(
            [str(openssl), "verify", "-help"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=10,
            env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValidationError("cannot inspect openssl verify options") from error
    text = result.stdout.decode("utf-8", errors="replace")
    missing = [flag for flag in EXCLUSIVE_TRUST_OPTIONS if flag not in text]
    if missing:
        raise ValidationError(
            "openssl verify cannot disable default trust: {}".format(", ".join(missing))
        )
    return list(EXCLUSIVE_TRUST_OPTIONS)


def verify_chain(
    openssl: Path,
    leaf: Path,
    intermediates: Sequence[Path],
    ca_bundle: Path,
    purpose: str,
    exclusive_trust_options: Sequence[str],
) -> None:
    arguments = [
        "verify",
        *exclusive_trust_options,
        "-CAfile",
        str(ca_bundle),
        "-purpose",
        purpose,
    ]
    if intermediates:
        untrusted = leaf.parent / "untrusted.pem"
        write_private_file(
            untrusted,
            b"".join(path.read_bytes() for path in intermediates),
        )
        arguments.extend(["-untrusted", str(untrusted)])
    arguments.append(str(leaf))
    run_openssl(openssl, arguments)


def validate_stage_directory(path: Path) -> None:
    if not path.is_absolute():
        raise ValidationError("--stage-dir must be absolute")
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ValidationError("--stage-dir does not exist") from error
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
        raise ValidationError("--stage-dir must be a real directory")
    if metadata.st_uid != 0 or metadata.st_gid != 0:
        raise ValidationError("--stage-dir must be owned by root:root")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise ValidationError("--stage-dir must have mode 0700")
    if any(path.iterdir()):
        raise ValidationError("--stage-dir must be empty")


def validate_installed_directory(
    path: Path,
    expected: Dict[str, bytes],
    service_gid: int,
) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ValidationError("installed TLS directory is missing") from error
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
        raise ValidationError("installed TLS path is not a real directory")
    if metadata.st_uid != 0 or metadata.st_gid != service_gid:
        raise ValidationError("installed TLS directory ownership is wrong")
    if stat.S_IMODE(metadata.st_mode) != 0o750:
        raise ValidationError("installed TLS directory mode is not 0750")

    actual_names = {entry.name for entry in path.iterdir()}
    if actual_names != set(expected):
        raise ValidationError("installed TLS file set does not match the selected role")
    for name, payload in expected.items():
        installed = path / name
        file_metadata = installed.lstat()
        if not stat.S_ISREG(file_metadata.st_mode) or installed.is_symlink():
            raise ValidationError("installed TLS entry is not a regular file")
        if file_metadata.st_uid != 0 or file_metadata.st_gid != service_gid:
            raise ValidationError("installed TLS file ownership is wrong")
        if stat.S_IMODE(file_metadata.st_mode) != 0o440:
            raise ValidationError("installed TLS file mode is not 0440")
        if file_metadata.st_nlink != 1:
            raise ValidationError("installed TLS file has multiple hard links")
        try:
            installed_payload = installed.read_bytes()
        except OSError as error:
            raise ValidationError("cannot read installed TLS file") from error
        if installed_payload != payload:
            raise ValidationError("installed TLS credential bytes do not match")


def validate_credentials(args: argparse.Namespace) -> Tuple[Dict[str, object], List[Credential]]:
    openssl = Path(args.openssl)
    if not openssl.is_absolute() or not openssl.is_file() or not os.access(openssl, os.X_OK):
        raise ValidationError("--openssl must name an absolute executable file")
    validate_dns_name(args.control_dns)

    if args.role == "control":
        credentials = [
            Credential("--tls-control-chain", Path(args.tls_control_chain), "control-chain.pem", "chain"),
            Credential("--tls-control-key", Path(args.tls_control_key), "control-key.pem", "key"),
            Credential("--tls-control-ca", Path(args.tls_control_ca), None, "ca"),
            Credential("--tls-reapi-client-ca", Path(args.tls_reapi_client_ca), "reapi-client-ca.pem", "ca"),
            Credential("--tls-worker-client-ca", Path(args.tls_worker_client_ca), "worker-client-ca.pem", "ca"),
        ]
        chain_option = "--tls-control-chain"
        key_option = "--tls-control-key"
        issuer_option = "--tls-control-ca"
        purpose = "sslserver"
    else:
        credentials = [
            Credential("--tls-control-ca", Path(args.tls_control_ca), "control-ca.pem", "ca"),
            Credential("--tls-worker-chain", Path(args.tls_worker_chain), "worker-chain.pem", "chain"),
            Credential("--tls-worker-key", Path(args.tls_worker_key), "worker-key.pem", "key"),
            Credential("--tls-worker-issuer-ca", Path(args.tls_worker_issuer_ca), None, "ca"),
        ]
        chain_option = "--tls-worker-chain"
        key_option = "--tls-worker-key"
        issuer_option = "--tls-worker-issuer-ca"
        purpose = "sslclient"

    exclusion_roots = resolve_exclusion_roots(args.exclude_root or ())
    payloads: Dict[str, bytes] = {}
    admitted: List[Credential] = []
    for credential in credentials:
        source, payload = validate_source_path(
            credential.option, str(credential.source), exclusion_roots
        )
        payloads[credential.option] = payload
        admitted.append(
            Credential(credential.option, source, credential.destination, credential.kind)
        )

    exclusive_trust_options = require_exclusive_trust_options(openssl)

    with private_work_directory() as temporary_root:
        chain_blocks = split_pem(chain_option, payloads[chain_option], (b"CERTIFICATE",))
        key_blocks = split_pem(
            key_option,
            payloads[key_option],
            (b"PRIVATE KEY", b"RSA PRIVATE KEY", b"EC PRIVATE KEY"),
        )
        if len(key_blocks) != 1:
            raise ValidationError("{} must contain exactly one private key".format(key_option))
        issuer_blocks = split_pem(issuer_option, payloads[issuer_option], (b"CERTIFICATE",))

        chain_paths = materialize_certificates(temporary_root, "chain", chain_blocks)
        issuer_paths = materialize_certificates(temporary_root, "issuer", issuer_blocks)
        issuer_bundle = temporary_root / "issuer-bundle.pem"
        write_private_file(issuer_bundle, b"".join(issuer_blocks))

        leaf = chain_paths[0]
        require_leaf_properties(
            openssl,
            leaf,
            purpose,
            args.minimum_validity_seconds,
            args.control_dns if args.role == "control" else None,
        )
        for certificate in issuer_paths:
            require_ca_properties(openssl, certificate)
        verify_chain(
            openssl,
            leaf,
            chain_paths[1:],
            issuer_bundle,
            purpose,
            exclusive_trust_options,
        )

        certificate_public_key = cert_public_key(openssl, leaf)
        if certificate_public_key != private_public_key(openssl, key_blocks[0]):
            raise ValidationError("certificate and private key do not match")

        # The issuer bundle is validation-only for both roles and is never
        # installed, so only its certificate identities can bind it to the
        # deployment. Record canonical DER fingerprints, sorted so that bundle
        # reordering is not drift while a changed certificate set always is.
        manifest: Dict[str, object] = {
            "control_dns": args.control_dns,
            "files": {},
            "leaf_certificate_sha256": sha256_hex(cert_der(openssl, leaf)),
            "leaf_public_key_sha256": sha256_hex(certificate_public_key),
            "role": args.role,
            "schema_version": 2,
            "validation_only_ca": {
                issuer_option.lstrip("-"): sorted(
                    sha256_hex(cert_der(openssl, path)) for path in issuer_paths
                )
            },
        }
        manifest_files = manifest["files"]
        assert isinstance(manifest_files, dict)
        for credential in admitted:
            if credential.destination is not None and credential.kind != "key":
                manifest_files[credential.destination] = sha256_hex(payloads[credential.option])

        if args.role == "control":
            reapi_blocks = split_pem(
                "--tls-reapi-client-ca",
                payloads["--tls-reapi-client-ca"],
                (b"CERTIFICATE",),
            )
            worker_blocks = split_pem(
                "--tls-worker-client-ca",
                payloads["--tls-worker-client-ca"],
                (b"CERTIFICATE",),
            )
            reapi_paths = materialize_certificates(temporary_root, "reapi", reapi_blocks)
            worker_paths = materialize_certificates(temporary_root, "worker", worker_blocks)
            for certificate in [*reapi_paths, *worker_paths]:
                require_ca_properties(openssl, certificate)
            reapi_fingerprints = {sha256_hex(cert_der(openssl, path)) for path in reapi_paths}
            worker_fingerprints = {sha256_hex(cert_der(openssl, path)) for path in worker_paths}
            if len(reapi_fingerprints) != len(reapi_paths) or len(worker_fingerprints) != len(worker_paths):
                raise ValidationError("client CA bundle contains a duplicate certificate")
            if not worker_fingerprints < reapi_fingerprints:
                raise ValidationError(
                    "REAPI client CA bundle must strictly include the worker CA bundle"
                )
        else:
            # The worker installs this anchor as control-ca.pem and trusts it to
            # identify the control plane, so it needs the same admission the
            # control role gives its CAs. Only the issuer option is covered
            # above, and for the worker that is a different file.
            control_anchor_blocks = split_pem(
                "--tls-control-ca",
                payloads["--tls-control-ca"],
                (b"CERTIFICATE",),
            )
            for certificate in materialize_certificates(
                temporary_root, "control-anchor", control_anchor_blocks
            ):
                require_ca_properties(openssl, certificate)

    installed_payloads = {
        credential.destination: payloads[credential.option]
        for credential in admitted
        if credential.destination is not None
    }

    if args.installed_dir:
        if args.service_gid is None or args.service_gid <= 0:
            raise ValidationError("--installed-dir requires a positive --service-gid")
        validate_installed_directory(
            Path(args.installed_dir), installed_payloads, args.service_gid
        )

    if args.stage_dir:
        stage_dir = Path(args.stage_dir)
        validate_stage_directory(stage_dir)
        for name, payload in installed_payloads.items():
            write_private_file(stage_dir / name, payload)
        descriptor = os.open(
            stage_dir,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    return manifest, admitted


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--openssl", required=True)
    result.add_argument("--role", choices=("control", "worker"), required=True)
    result.add_argument("--control-dns", required=True)
    result.add_argument("--minimum-validity-seconds", type=int, default=86400)
    result.add_argument("--exclude-root", action="append")
    result.add_argument("--stage-dir")
    result.add_argument("--installed-dir")
    result.add_argument("--service-gid", type=int)
    result.add_argument("--tls-control-chain")
    result.add_argument("--tls-control-key")
    result.add_argument("--tls-control-ca", required=True)
    result.add_argument("--tls-reapi-client-ca")
    result.add_argument("--tls-worker-client-ca")
    result.add_argument("--tls-worker-chain")
    result.add_argument("--tls-worker-key")
    result.add_argument("--tls-worker-issuer-ca")
    result.add_argument("-v", "--verbose", action="store_true")
    return result


def require_role_options(args: argparse.Namespace) -> None:
    control_options = (
        "tls_control_chain",
        "tls_control_key",
        "tls_reapi_client_ca",
        "tls_worker_client_ca",
    )
    worker_options = (
        "tls_worker_chain",
        "tls_worker_key",
        "tls_worker_issuer_ca",
    )
    required = control_options if args.role == "control" else worker_options
    forbidden = worker_options if args.role == "control" else control_options
    missing = ["--" + name.replace("_", "-") for name in required if not getattr(args, name)]
    supplied_forbidden = [
        "--" + name.replace("_", "-") for name in forbidden if getattr(args, name)
    ]
    if missing:
        raise ValidationError("missing required options: {}".format(", ".join(missing)))
    if supplied_forbidden:
        raise ValidationError(
            "options are invalid for {} role: {}".format(
                args.role, ", ".join(supplied_forbidden)
            )
        )
    if args.minimum_validity_seconds < 86400:
        raise ValidationError("minimum validity must be at least 86400 seconds")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="sdme-tls: %(message)s",
    )
    try:
        require_role_options(args)
        manifest, _ = validate_credentials(args)
    except ValidationError as error:
        LOG.error("%s", error)
        return 2
    LOG.debug("validated %s mTLS credential set", args.role)
    json.dump(manifest, sys.stdout, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
