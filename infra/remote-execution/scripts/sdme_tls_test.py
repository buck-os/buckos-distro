#!/usr/bin/env python3

import datetime
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest

from _skiploader import load_skips
from pathlib import Path
from typing import Dict, List, Optional

environmental_skip = load_skips().environmental_skip


TEST_ROOT = Path(__file__).resolve().parent
HELPER = TEST_ROOT / "sdme_tls.py"


class SdmeTlsTest(unittest.TestCase):
    def setUp(self) -> None:
        if os.geteuid() != 0:
            environmental_skip("credential ownership tests require root")
        openssl = shutil.which("openssl")
        if openssl is None:
            environmental_skip("openssl is unavailable")
        self.openssl = Path(openssl).resolve()
        self.temporary = tempfile.TemporaryDirectory(
            prefix="buckos-sdme-tls-test-", dir="/var/lib"
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.credentials = self.root / "credentials"
        self.credentials.mkdir(mode=0o700)
        self.serial = 1

        self.control_ca = self.make_ca("control-ca")
        self.worker_ca = self.make_ca("worker-ca")
        self.buck_ca = self.make_ca("buck-ca")
        self.other_ca = self.make_ca("other-ca")
        self.control_chain, self.control_key = self.make_leaf(
            "control",
            self.control_ca,
            purpose="serverAuth",
            san="DNS:control.internal",
        )
        self.worker_chain, self.worker_key = self.make_leaf(
            "worker",
            self.worker_ca,
            purpose="clientAuth",
        )
        self.other_worker_chain, _ = self.make_leaf(
            "other-worker",
            self.other_ca,
            purpose="clientAuth",
        )
        self.reapi_ca = self.credentials / "reapi-client-ca.pem"
        self.reapi_ca.write_bytes(self.buck_ca[0].read_bytes() + self.worker_ca[0].read_bytes())
        self.reapi_ca.chmod(0o600)

    def openssl_run(self, *arguments: str) -> None:
        subprocess.run(
            [str(self.openssl), *arguments],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def make_ca(self, name: str) -> tuple[Path, Path]:
        certificate = self.credentials / "{}.pem".format(name)
        key = self.credentials / "{}.key".format(name)
        self.openssl_run(
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
            "-days",
            "2",
            "-subj",
            "/CN={}".format(name),
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
        )
        certificate.chmod(0o600)
        key.chmod(0o600)
        return certificate, key

    @staticmethod
    def openssl_date(offset_days: int) -> str:
        moment = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
            days=offset_days
        )
        return moment.strftime("%Y%m%d%H%M%SZ")

    def make_future_ca(self, name: str) -> Path:
        """A CA whose notBefore has not arrived but whose notAfter is far out.

        An expired CA would prove nothing here: -checkend already rejects it.
        """
        certificate = self.credentials / "{}.pem".format(name)
        key = self.credentials / "{}.key".format(name)
        self.openssl_run(
            "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(certificate),
            "-not_before", self.openssl_date(30),
            "-not_after", self.openssl_date(400),
            "-subj", "/CN={}".format(name),
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign",
        )
        certificate.chmod(0o600)
        key.chmod(0o600)
        return certificate

    def make_expired_ca(self, name: str) -> Path:
        certificate = self.credentials / "{}.pem".format(name)
        key = self.credentials / "{}.key".format(name)
        self.openssl_run(
            "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(certificate),
            "-not_before", self.openssl_date(-40),
            "-not_after", self.openssl_date(-10),
            "-subj", "/CN={}".format(name),
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign",
        )
        certificate.chmod(0o600)
        key.chmod(0o600)
        return certificate

    def make_leaf(
        self,
        name: str,
        ca: tuple[Path, Path],
        *,
        purpose: str,
        san: Optional[str] = None,
        days: int = 2,
        not_before: Optional[str] = None,
        not_after: Optional[str] = None,
    ) -> tuple[Path, Path]:
        certificate = self.credentials / "{}.pem".format(name)
        key = self.credentials / "{}.key".format(name)
        request = self.credentials / "{}.csr".format(name)
        extensions = self.credentials / "{}.ext".format(name)
        extension_lines = [
            "basicConstraints=critical,CA:FALSE",
            "keyUsage=critical,digitalSignature",
            "extendedKeyUsage={}".format(purpose),
        ]
        if san is not None:
            extension_lines.append("subjectAltName={}".format(san))
        extensions.write_text("\n".join(extension_lines) + "\n", encoding="utf-8")
        self.openssl_run(
            "req",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(request),
            "-subj",
            "/CN={}".format(name),
        )
        validity = ["-days", str(days)]
        if not_before is not None or not_after is not None:
            validity = []
            if not_before is not None:
                validity += ["-not_before", not_before]
            if not_after is not None:
                validity += ["-not_after", not_after]
        self.openssl_run(
            "x509",
            "-req",
            "-in",
            str(request),
            "-CA",
            str(ca[0]),
            "-CAkey",
            str(ca[1]),
            "-set_serial",
            str(self.serial),
            *validity,
            "-out",
            str(certificate),
            "-extfile",
            str(extensions),
        )
        self.serial += 1
        certificate.chmod(0o600)
        key.chmod(0o600)
        request.unlink()
        extensions.unlink()
        return certificate, key

    def control_arguments(self) -> List[str]:
        return [
            "--openssl",
            str(self.openssl),
            "--role",
            "control",
            "--control-dns",
            "control.internal",
            "--tls-control-chain",
            str(self.control_chain),
            "--tls-control-key",
            str(self.control_key),
            "--tls-control-ca",
            str(self.control_ca[0]),
            "--tls-reapi-client-ca",
            str(self.reapi_ca),
            "--tls-worker-client-ca",
            str(self.worker_ca[0]),
        ]

    def worker_arguments(self) -> List[str]:
        return [
            "--openssl",
            str(self.openssl),
            "--role",
            "worker",
            "--control-dns",
            "control.internal",
            "--tls-control-ca",
            str(self.control_ca[0]),
            "--tls-worker-chain",
            str(self.worker_chain),
            "--tls-worker-key",
            str(self.worker_key),
            "--tls-worker-issuer-ca",
            str(self.worker_ca[0]),
        ]

    def run_helper(self, arguments: List[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(HELPER), *arguments],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def der_sha256(self, certificate: Path) -> str:
        result = subprocess.run(
            [str(self.openssl), "x509", "-in", str(certificate), "-outform", "DER"],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
        )
        return hashlib.sha256(result.stdout).hexdigest()

    def combined_bundle(self, name: str, *certificates: Path) -> Path:
        path = self.credentials / "{}.pem".format(name)
        path.write_bytes(b"".join(item.read_bytes() for item in certificates))
        path.chmod(0o600)
        return path

    def openssl_recorder(self) -> tuple[Path, Path]:
        """An openssl wrapper that records each invocation's exact argument vector."""
        log = self.root / "openssl-arguments.log"
        wrapper = self.root / "openssl-recorder"
        wrapper.write_text(
            "#!/bin/sh\n"
            "{{ printf '%s\\t' \"$@\"; printf '\\n'; }} >> {log}\n"
            "exec {openssl} \"$@\"\n".format(log=log, openssl=self.openssl),
            encoding="utf-8",
        )
        wrapper.chmod(0o700)
        return wrapper, log

    @staticmethod
    def verify_invocations(log: Path) -> List[List[str]]:
        invocations = []
        for line in log.read_text(encoding="utf-8").splitlines():
            fields = [field for field in line.split("\t") if field]
            if fields[:1] == ["verify"] and "-help" not in fields:
                invocations.append(fields)
        return invocations

    def excluded_root_arguments(self, *roots: Path) -> List[str]:
        arguments = []
        for root in roots:
            arguments.extend(["--exclude-root", str(root)])
        return arguments

    def credential_under(self, directory: Path, source: Path) -> Path:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        for parent in (directory, *directory.parents):
            if parent == Path("/"):
                break
            os.chown(parent, 0, 0)
            if parent.stat().st_mode & 0o022:
                parent.chmod(parent.stat().st_mode & ~0o022)
        copied = directory / source.name
        copied.write_bytes(source.read_bytes())
        copied.chmod(0o600)
        os.chown(copied, 0, 0)
        return copied

    def test_valid_control_credentials_stage_only_runtime_files(self) -> None:
        stage = self.root / "stage-control"
        stage.mkdir(mode=0o700)
        result = self.run_helper(self.control_arguments() + ["--stage-dir", str(stage)])

        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads(result.stdout)
        self.assertEqual(manifest["role"], "control")
        self.assertEqual(manifest["control_dns"], "control.internal")
        self.assertNotIn("control-key.pem", manifest["files"])
        self.assertNotIn(str(self.control_key), result.stdout)
        self.assertEqual(
            {path.name for path in stage.iterdir()},
            {
                "control-chain.pem",
                "control-key.pem",
                "reapi-client-ca.pem",
                "worker-client-ca.pem",
            },
        )
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in stage.iterdir()))

    def test_valid_worker_credentials_stage_only_runtime_files(self) -> None:
        stage = self.root / "stage-worker"
        stage.mkdir(mode=0o700)
        result = self.run_helper(self.worker_arguments() + ["--stage-dir", str(stage)])

        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads(result.stdout)
        self.assertEqual(manifest["role"], "worker")
        self.assertEqual(
            {path.name for path in stage.iterdir()},
            {"control-ca.pem", "worker-chain.pem", "worker-key.pem"},
        )

    def test_installed_directory_requires_exact_bytes_owner_group_and_mode(self) -> None:
        stage = self.root / "stage-installed"
        stage.mkdir(mode=0o700)
        initial = self.run_helper(self.worker_arguments() + ["--stage-dir", str(stage)])
        self.assertEqual(initial.returncode, 0, initial.stderr)
        os.chown(stage, 0, 12345)
        stage.chmod(0o750)
        for path in stage.iterdir():
            os.chown(path, 0, 12345)
            path.chmod(0o440)

        valid = self.run_helper(
            self.worker_arguments()
            + ["--installed-dir", str(stage), "--service-gid", "12345"]
        )
        self.assertEqual(valid.returncode, 0, valid.stderr)

        (stage / "worker-key.pem").chmod(0o640)
        invalid = self.run_helper(
            self.worker_arguments()
            + ["--installed-dir", str(stage), "--service-gid", "12345"]
        )
        self.assertEqual(invalid.returncode, 2)
        self.assertIn("mode is not 0440", invalid.stderr)

    def test_installed_directory_rejects_links_and_unexpected_entries(self) -> None:
        stage = self.root / "stage-entries"
        stage.mkdir(mode=0o700)
        initial = self.run_helper(self.worker_arguments() + ["--stage-dir", str(stage)])
        self.assertEqual(initial.returncode, 0, initial.stderr)
        os.chown(stage, 0, 12345)
        stage.chmod(0o750)
        for path in stage.iterdir():
            os.chown(path, 0, 12345)
            path.chmod(0o440)
        arguments = self.worker_arguments() + [
            "--installed-dir",
            str(stage),
            "--service-gid",
            "12345",
        ]
        self.assertEqual(self.run_helper(arguments).returncode, 0)

        outside = self.root / "worker-key-outside.pem"
        os.link(stage / "worker-key.pem", outside)
        hard_linked = self.run_helper(arguments)
        self.assertEqual(hard_linked.returncode, 2)
        self.assertIn("multiple hard links", hard_linked.stderr)
        outside.unlink()

        unexpected = stage / "unexpected.pem"
        unexpected.write_bytes(b"")
        os.chown(unexpected, 0, 12345)
        unexpected.chmod(0o440)
        extra_entry = self.run_helper(arguments)
        self.assertEqual(extra_entry.returncode, 2)
        self.assertIn("file set does not match", extra_entry.stderr)
        unexpected.unlink()

        replaced = stage / "control-ca.pem"
        original = replaced.read_bytes()
        replaced.unlink()
        replaced.symlink_to(stage / "worker-chain.pem")
        symlinked = self.run_helper(arguments)
        self.assertEqual(symlinked.returncode, 2)
        self.assertIn("not a regular file", symlinked.stderr)
        replaced.unlink()
        replaced.write_bytes(original)
        os.chown(replaced, 0, 12345)
        replaced.chmod(0o440)

        os.chown(stage / "worker-chain.pem", 0, 0)
        wrong_owner = self.run_helper(arguments)
        self.assertEqual(wrong_owner.returncode, 2)
        self.assertIn("ownership is wrong", wrong_owner.stderr)

    def test_rejects_missing_exact_control_dns_san(self) -> None:
        chain, key = self.make_leaf(
            "wrong-san",
            self.control_ca,
            purpose="serverAuth",
            san="DNS:other.internal",
        )
        arguments = self.control_arguments()
        arguments[arguments.index(str(self.control_chain))] = str(chain)
        arguments[arguments.index(str(self.control_key))] = str(key)

        result = self.run_helper(arguments)

        self.assertEqual(result.returncode, 2)
        self.assertIn("lacks exact DNS SAN", result.stderr)

    def test_rejects_wildcard_control_dns_san(self) -> None:
        chain, key = self.make_leaf(
            "wildcard-san",
            self.control_ca,
            purpose="serverAuth",
            san="DNS:*.internal",
        )
        arguments = self.control_arguments()
        arguments[arguments.index(str(self.control_chain))] = str(chain)
        arguments[arguments.index(str(self.control_key))] = str(key)

        result = self.run_helper(arguments)

        self.assertEqual(result.returncode, 2)
        self.assertIn("lacks exact DNS SAN", result.stderr)

    def test_rejects_mismatched_private_key(self) -> None:
        arguments = self.worker_arguments()
        arguments[arguments.index(str(self.worker_key))] = str(self.control_key)

        result = self.run_helper(arguments)

        self.assertEqual(result.returncode, 2)
        self.assertIn("do not match", result.stderr)

    def test_rejects_untrusted_worker_chain(self) -> None:
        arguments = self.worker_arguments()
        arguments[arguments.index(str(self.worker_chain))] = str(self.other_worker_chain)

        result = self.run_helper(arguments)

        self.assertEqual(result.returncode, 2)
        self.assertIn("openssl rejected credential", result.stderr)

    def test_rejects_incomplete_reapi_trust_bundle(self) -> None:
        arguments = self.control_arguments()
        arguments[arguments.index(str(self.reapi_ca))] = str(self.worker_ca[0])

        result = self.run_helper(arguments)

        self.assertEqual(result.returncode, 2)
        self.assertIn("strictly include", result.stderr)

    def test_rejects_credential_with_unsafe_mode(self) -> None:
        self.worker_key.chmod(0o640)

        result = self.run_helper(self.worker_arguments())

        self.assertEqual(result.returncode, 2)
        self.assertIn("mode 0400 or 0600", result.stderr)

    def test_rejects_writable_credential_ancestry(self) -> None:
        self.credentials.chmod(0o770)

        result = self.run_helper(self.worker_arguments())

        self.assertEqual(result.returncode, 2)
        self.assertIn("path must not be group/world-writable", result.stderr)

    def test_rejects_malformed_certificate_pem(self) -> None:
        malformed = self.credentials / "malformed.pem"
        malformed.write_text("not a certificate\n", encoding="utf-8")
        malformed.chmod(0o600)
        arguments = self.worker_arguments()
        arguments[arguments.index(str(self.worker_chain))] = str(malformed)

        result = self.run_helper(arguments)

        self.assertEqual(result.returncode, 2)
        self.assertIn("not a complete PEM file", result.stderr)

    def test_rejects_symlink_and_hard_link_inputs(self) -> None:
        symlink = self.credentials / "worker-link.key"
        symlink.symlink_to(self.worker_key)
        arguments = self.worker_arguments()
        arguments[arguments.index(str(self.worker_key))] = str(symlink)
        linked = self.run_helper(arguments)
        self.assertEqual(linked.returncode, 2)
        self.assertIn("symlink components", linked.stderr)

        hard_link = self.credentials / "worker-hardlink.key"
        os.link(self.worker_key, hard_link)
        arguments[arguments.index(str(symlink))] = str(self.worker_key)
        hard_linked = self.run_helper(arguments)
        self.assertEqual(hard_linked.returncode, 2)
        self.assertIn("exactly one hard link", hard_linked.stderr)

    def test_rejects_non_root_owned_input(self) -> None:
        os.chown(self.worker_key, 12345, 12345)

        result = self.run_helper(self.worker_arguments())

        self.assertEqual(result.returncode, 2)
        self.assertIn("owned by root:root", result.stderr)

    def test_rejects_encrypted_private_key(self) -> None:
        encrypted = self.credentials / "worker-encrypted.key"
        self.openssl_run(
            "pkey",
            "-in",
            str(self.worker_key),
            "-aes-256-cbc",
            "-passout",
            "pass:test-only",
            "-out",
            str(encrypted),
        )
        encrypted.chmod(0o600)
        arguments = self.worker_arguments()
        arguments[arguments.index(str(self.worker_key))] = str(encrypted)

        result = self.run_helper(arguments)

        self.assertEqual(result.returncode, 2)
        self.assertIn("unexpected PEM block", result.stderr)

    def test_rejects_wrong_extended_key_usage(self) -> None:
        chain, key = self.make_leaf(
            "wrong-eku",
            self.worker_ca,
            purpose="serverAuth",
        )
        arguments = self.worker_arguments()
        arguments[arguments.index(str(self.worker_chain))] = str(chain)
        arguments[arguments.index(str(self.worker_key))] = str(key)

        result = self.run_helper(arguments)

        self.assertEqual(result.returncode, 2)
        self.assertIn("lacks required TLS Web Client Authentication EKU", result.stderr)

    def test_rejects_non_ca_trust_input(self) -> None:
        arguments = self.worker_arguments()
        arguments[arguments.index(str(self.worker_ca[0]))] = str(self.worker_chain)

        result = self.run_helper(arguments)

        self.assertEqual(result.returncode, 2)
        self.assertIn("without CA:TRUE", result.stderr)

    def test_worker_validation_only_anchor_drift_changes_the_manifest(self) -> None:
        stage = self.root / "stage-anchor-worker"
        stage.mkdir(mode=0o700)
        baseline = self.run_helper(self.worker_arguments() + ["--stage-dir", str(stage)])
        self.assertEqual(baseline.returncode, 0, baseline.stderr)
        installed = {path.name: path.read_bytes() for path in stage.iterdir()}
        manifest = json.loads(baseline.stdout)
        self.assertEqual(
            manifest["validation_only_ca"],
            {"tls-worker-issuer-ca": [self.der_sha256(self.worker_ca[0])]},
        )

        widened = self.combined_bundle(
            "worker-issuer-widened", self.worker_ca[0], self.other_ca[0]
        )
        arguments = self.worker_arguments()
        arguments[arguments.index(str(self.worker_ca[0]))] = str(widened)
        drifted_stage = self.root / "stage-anchor-worker-drift"
        drifted_stage.mkdir(mode=0o700)

        drifted = self.run_helper(arguments + ["--stage-dir", str(drifted_stage)])

        self.assertEqual(drifted.returncode, 0, drifted.stderr)
        drifted_manifest = json.loads(drifted.stdout)
        self.assertEqual(
            {path.name: path.read_bytes() for path in drifted_stage.iterdir()},
            installed,
        )
        self.assertEqual(drifted_manifest["files"], manifest["files"])
        self.assertEqual(
            drifted_manifest["leaf_certificate_sha256"],
            manifest["leaf_certificate_sha256"],
        )
        self.assertEqual(
            drifted_manifest["validation_only_ca"],
            {
                "tls-worker-issuer-ca": sorted(
                    (
                        self.der_sha256(self.worker_ca[0]),
                        self.der_sha256(self.other_ca[0]),
                    )
                )
            },
        )
        self.assertNotEqual(drifted.stdout, baseline.stdout)

    def test_control_validation_only_anchor_drift_changes_the_manifest(self) -> None:
        baseline = self.run_helper(self.control_arguments())
        self.assertEqual(baseline.returncode, 0, baseline.stderr)
        manifest = json.loads(baseline.stdout)
        self.assertEqual(
            manifest["validation_only_ca"],
            {"tls-control-ca": [self.der_sha256(self.control_ca[0])]},
        )

        widened = self.combined_bundle(
            "control-anchor-widened", self.control_ca[0], self.other_ca[0]
        )
        arguments = self.control_arguments()
        arguments[arguments.index(str(self.control_ca[0]))] = str(widened)

        drifted = self.run_helper(arguments)

        self.assertEqual(drifted.returncode, 0, drifted.stderr)
        drifted_manifest = json.loads(drifted.stdout)
        self.assertEqual(drifted_manifest["files"], manifest["files"])
        self.assertNotEqual(
            drifted_manifest["validation_only_ca"], manifest["validation_only_ca"]
        )
        self.assertNotEqual(drifted.stdout, baseline.stdout)

    def test_validation_only_anchor_order_is_not_drift(self) -> None:
        forward = self.combined_bundle(
            "worker-issuer-forward", self.worker_ca[0], self.other_ca[0]
        )
        reversed_bundle = self.combined_bundle(
            "worker-issuer-reversed", self.other_ca[0], self.worker_ca[0]
        )
        first_arguments = self.worker_arguments()
        first_arguments[first_arguments.index(str(self.worker_ca[0]))] = str(forward)
        second_arguments = self.worker_arguments()
        second_arguments[second_arguments.index(str(self.worker_ca[0]))] = str(
            reversed_bundle
        )

        first = self.run_helper(first_arguments)
        second = self.run_helper(second_arguments)

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(
            json.loads(first.stdout)["validation_only_ca"],
            json.loads(second.stdout)["validation_only_ca"],
        )

    def test_rejects_credentials_inside_each_excluded_root(self) -> None:
        checkout = self.root / "checkout"
        data = self.root / "data"
        stage = self.root / "stage-excluded"
        stage.mkdir(mode=0o700)
        cases = (
            ("direct checkout", self.credential_under(checkout, self.worker_key)),
            (
                "nested checkout",
                self.credential_under(checkout / "secrets/deep", self.worker_key),
            ),
            ("direct data root", self.credential_under(data, self.worker_key)),
            (
                "nested data root",
                self.credential_under(data / "provision/tls", self.worker_key),
            ),
        )

        for label, credential in cases:
            with self.subTest(case=label):
                arguments = self.worker_arguments()
                arguments[arguments.index(str(self.worker_key))] = str(credential)
                arguments.extend(self.excluded_root_arguments(checkout, data))

                result = self.run_helper(arguments + ["--stage-dir", str(stage)])

                self.assertEqual(result.returncode, 2)
                self.assertIn(
                    "--tls-worker-key must be outside the repository and managed "
                    "data root",
                    result.stderr,
                )
                self.assertEqual(list(stage.iterdir()), [])

    def test_excluded_roots_admit_lexical_prefix_siblings(self) -> None:
        data = self.root / "data"
        data.mkdir(mode=0o700)
        sibling = self.credential_under(self.root / "data-extra", self.worker_key)
        arguments = self.worker_arguments()
        arguments[arguments.index(str(self.worker_key))] = str(sibling)
        arguments.extend(self.excluded_root_arguments(data))

        result = self.run_helper(arguments)

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_excluded_roots_survive_path_normalization_and_symlinked_roots(
        self,
    ) -> None:
        data = self.root / "data"
        credential = self.credential_under(data / "tls", self.worker_key)
        stage = self.root / "stage-normalization"
        stage.mkdir(mode=0o700)

        # An unnormalized spelling of an excluded credential never reaches the
        # exclusion comparison: canonicalization refuses it first. Either way no
        # credential inside the excluded root is admitted or staged.
        unnormalized = "{}/tls/./../tls/{}".format(data, credential.name)
        arguments = self.worker_arguments()
        arguments[arguments.index(str(self.worker_key))] = unnormalized
        arguments.extend(self.excluded_root_arguments(data))

        normalized = self.run_helper(arguments + ["--stage-dir", str(stage)])

        self.assertEqual(normalized.returncode, 2)
        self.assertIn("--tls-worker-key must not contain symlink", normalized.stderr)
        self.assertEqual(list(stage.iterdir()), [])

        # A symlink that points into the excluded root is refused for the same
        # reason, from a source path that is outside the root.
        linked = self.root / "worker-key-link.pem"
        linked.symlink_to(credential)
        link_arguments = self.worker_arguments()
        link_arguments[link_arguments.index(str(self.worker_key))] = str(linked)
        link_arguments.extend(self.excluded_root_arguments(data))

        through_link = self.run_helper(link_arguments + ["--stage-dir", str(stage)])

        self.assertEqual(through_link.returncode, 2)
        self.assertIn("--tls-worker-key must not contain symlink", through_link.stderr)
        self.assertEqual(list(stage.iterdir()), [])

        # A symlinked spelling of the root itself still excludes the canonical
        # credential path beneath it.
        alias = self.root / "data-alias"
        alias.symlink_to(data, target_is_directory=True)
        aliased_arguments = self.worker_arguments()
        aliased_arguments[aliased_arguments.index(str(self.worker_key))] = str(
            credential
        )
        aliased_arguments.extend(self.excluded_root_arguments(alias))

        aliased = self.run_helper(aliased_arguments + ["--stage-dir", str(stage)])

        self.assertEqual(aliased.returncode, 2)
        self.assertIn(
            "--tls-worker-key must be outside the repository and managed data root",
            aliased.stderr,
        )
        self.assertEqual(list(stage.iterdir()), [])

    def test_chain_verification_uses_only_the_supplied_bundle(self) -> None:
        recorder, log = self.openssl_recorder()
        arguments = self.control_arguments()
        arguments[arguments.index(str(self.openssl))] = str(recorder)

        result = self.run_helper(arguments)

        self.assertEqual(result.returncode, 0, result.stderr)
        invocations = self.verify_invocations(log)
        self.assertTrue(invocations)
        for fields in invocations:
            self.assertIn("-no-CAfile", fields)
            self.assertIn("-no-CApath", fields)
            self.assertIn("-no-CAstore", fields)
            self.assertNotIn("-CApath", fields)
            self.assertNotIn("-CAstore", fields)
            self.assertNotIn("-trusted", fields)
            self.assertEqual(fields.count("-CAfile"), 1)
            bundle = fields[fields.index("-CAfile") + 1]
            self.assertTrue(bundle.endswith("/issuer-bundle.pem"), bundle)

    def test_rejects_leaf_whose_issuer_is_absent_from_the_supplied_bundle(self) -> None:
        recorder, log = self.openssl_recorder()
        arguments = self.worker_arguments()
        arguments[arguments.index(str(self.openssl))] = str(recorder)
        arguments[arguments.index(str(self.worker_chain))] = str(
            self.other_worker_chain
        )

        result = self.run_helper(arguments)

        self.assertEqual(result.returncode, 2)
        self.assertIn("openssl rejected credential", result.stderr)
        invocations = self.verify_invocations(log)
        self.assertEqual(len(invocations), 1)
        for flag in ("-no-CAfile", "-no-CApath", "-no-CAstore"):
            self.assertIn(flag, invocations[0])

    def test_worker_control_trust_anchor_gets_full_admission(self) -> None:
        """The worker installs --tls-control-ca, so it must be validated.

        Every case substitutes only --tls-control-ca in an otherwise valid
        worker set. The suite previously exercised the validation-only issuer
        and the installed chain, and never this option, which is the input the
        worker uses to decide it is talking to the real control plane.
        """
        garbage = self.credentials / "not-a-certificate.pem"
        garbage.write_text("this is not a certificate at all\n", encoding="utf-8")
        garbage.chmod(0o600)
        cases = (
            ("non-CA leaf", self.worker_chain, "without CA:TRUE"),
            ("private key", self.worker_key, "unexpected PEM block"),
            ("garbage bytes", garbage, "not a complete PEM file"),
            (
                "expired CA",
                self.make_expired_ca("expired-control-anchor"),
                "openssl rejected credential",
            ),
            (
                "not-yet-valid CA",
                self.make_future_ca("future-control-anchor"),
                "CA certificate is not yet valid",
            ),
        )

        for label, substitute, expected in cases:
            with self.subTest(case=label):
                stage = self.root / "stage-anchor-{}".format(label.replace(" ", "-"))
                stage.mkdir(mode=0o700)
                arguments = self.worker_arguments()
                index = arguments.index("--tls-control-ca") + 1
                arguments[index] = str(substitute)

                result = self.run_helper(arguments + ["--stage-dir", str(stage)])

                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn(expected, result.stderr)
                self.assertEqual(list(stage.iterdir()), [], "refused after staging")

        # A correct worker set must still be admitted: a change that refused
        # everything would satisfy every case above.
        accepted = self.root / "stage-anchor-valid"
        accepted.mkdir(mode=0o700)
        valid = self.run_helper(
            self.worker_arguments() + ["--stage-dir", str(accepted)]
        )
        self.assertEqual(valid.returncode, 0, valid.stderr)
        self.assertEqual(
            {path.name for path in accepted.iterdir()},
            {"control-ca.pem", "worker-chain.pem", "worker-key.pem"},
        )

    def test_worker_control_anchor_keeps_its_raw_payload_hash(self) -> None:
        # Admission and identity are separate: the installed-byte comparison in
        # validate_installed_directory depends on this being the bytes on disk.
        result = self.run_helper(self.worker_arguments())

        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads(result.stdout)
        self.assertEqual(
            manifest["files"]["control-ca.pem"],
            hashlib.sha256(self.control_ca[0].read_bytes()).hexdigest(),
        )

    def test_rejects_a_not_yet_valid_leaf(self) -> None:
        # notAfter is far enough out that -checkend is satisfied, so only an
        # explicit notBefore comparison can reject this.
        chain, key = self.make_leaf(
            "future-worker",
            self.worker_ca,
            purpose="clientAuth",
            not_before=self.openssl_date(30),
            not_after=self.openssl_date(400),
        )
        arguments = self.worker_arguments()
        arguments[arguments.index(str(self.worker_chain))] = str(chain)
        arguments[arguments.index(str(self.worker_key))] = str(key)

        result = self.run_helper(arguments)

        self.assertEqual(result.returncode, 2)
        self.assertIn("leaf certificate is not yet valid", result.stderr)

    def test_not_yet_valid_leaf_would_pass_the_expiry_check_alone(self) -> None:
        # Guards the test above from becoming vacuous: if -checkend ever began
        # rejecting this certificate, the negative would stop proving anything.
        chain, _ = self.make_leaf(
            "future-probe",
            self.worker_ca,
            purpose="clientAuth",
            not_before=self.openssl_date(30),
            not_after=self.openssl_date(400),
        )
        probe = subprocess.run(
            [str(self.openssl), "x509", "-in", str(chain), "-noout",
             "-checkend", "86400"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.assertEqual(probe.returncode, 0)

    def test_rejects_a_not_yet_valid_issuer_ca(self) -> None:
        arguments = self.worker_arguments()
        index = arguments.index("--tls-worker-issuer-ca") + 1
        arguments[index] = str(self.make_future_ca("future-issuer"))

        result = self.run_helper(arguments)

        self.assertEqual(result.returncode, 2)
        self.assertIn("CA certificate is not yet valid", result.stderr)

    def test_nothing_is_materialized_under_a_caller_supplied_tmpdir(self) -> None:
        """TMPDIR must not be consulted at all.

        Asserting the sentinel is empty afterwards would prove nothing, because
        a temporary directory is created and removed again, and pointing TMPDIR
        at something unusable proves nothing either, because `tempfile` falls
        back to /tmp without erroring. Creating or removing an entry does change
        the sentinel's own mtime, so that is what is watched.
        """
        sentinel = self.root / "tmpdir-sentinel"
        sentinel.mkdir(mode=0o700)
        stage = self.root / "stage-tmpdir"
        stage.mkdir(mode=0o700)
        before_mtime = sentinel.stat().st_mtime_ns
        before_entries = {path.name for path in sentinel.iterdir()}
        time.sleep(0.02)
        environment = dict(os.environ)
        environment.update(
            TMPDIR=str(sentinel), TMP=str(sentinel), TEMP=str(sentinel)
        )

        result = subprocess.run(
            [str(HELPER), *self.worker_arguments(), "--stage-dir", str(stage)],
            check=False,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            {path.name for path in stage.iterdir()},
            {"control-ca.pem", "worker-chain.pem", "worker-key.pem"},
        )
        self.assertEqual({path.name for path in sentinel.iterdir()}, before_entries)
        self.assertEqual(
            sentinel.stat().st_mtime_ns,
            before_mtime,
            "the helper created something under the caller's TMPDIR",
        )

    def test_private_key_never_becomes_a_file(self) -> None:
        recorder, log = self.openssl_recorder()
        arguments = self.worker_arguments()
        arguments[arguments.index(str(self.openssl))] = str(recorder)

        result = self.run_helper(arguments)

        self.assertEqual(result.returncode, 0, result.stderr)
        invocations = [
            [field for field in line.split("\t") if field]
            for line in log.read_text(encoding="utf-8").splitlines()
        ]
        derivations = [
            fields for fields in invocations
            if fields[:1] == ["pkey"] and "-pubout" in fields and "-pubin" not in fields
        ]
        self.assertEqual(len(derivations), 1, invocations)
        # No -in means the key arrived on stdin and was never written anywhere.
        self.assertNotIn("-in", derivations[0])

    def test_leaves_no_work_directory_behind(self) -> None:
        root = Path("/var/tmp")
        before = {path.name for path in root.glob("sdme-tls-*")}

        result = self.run_helper(self.worker_arguments())

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({path.name for path in root.glob("sdme-tls-*")}, before)

    def test_rejects_leaf_with_less_than_24_hours_remaining(self) -> None:
        chain, key = self.make_leaf(
            "short-lived",
            self.worker_ca,
            purpose="clientAuth",
            days=1,
        )
        arguments = self.worker_arguments()
        arguments[arguments.index(str(self.worker_chain))] = str(chain)
        arguments[arguments.index(str(self.worker_key))] = str(key)

        result = self.run_helper(arguments)

        self.assertEqual(result.returncode, 2)
        self.assertIn("openssl rejected credential", result.stderr)


if __name__ == "__main__":
    unittest.main()
