#!/usr/bin/env python3

import hashlib
import json
import os
import tempfile
import unittest
from unittest import mock

from _lockfile import dump_lockfile, load_lockfile, lockfile_digest
from upstream_generate import render, validate_overlay
from upstream_lock import (
    apt_repository_record,
    base_image_packages,
    change_summary,
    parse_named_repository,
    parse_ppa,
    resolve_rpm,
    validate_remote_url,
)


class TestBaseIdentity(unittest.TestCase):
    def test_digest_is_independent_of_encoding_and_whitespace(self):
        value = {"schema": 3, "distro": "ubuntu", "release": "26.04", "target_cpu": "x86_64"}
        with tempfile.TemporaryDirectory() as directory:
            plain = os.path.join(directory, "base.lock.json")
            compressed = plain + ".gz"
            dump_lockfile(value, plain)
            dump_lockfile(value, compressed)
            self.assertEqual(
                lockfile_digest(load_lockfile(plain)),
                lockfile_digest(load_lockfile(compressed)),
            )

    def test_debian_live_base_drops_kernel_payloads(self):
        lock = {
            "distro": "ubuntu",
            "image_sets": {"live": [
                {"package": "bash", "source": "bash@1"},
                {"package": "linux-image", "source": "linux-signed@1"},
            ]},
        }
        name, entries = base_image_packages(lock, "deb", None)
        self.assertEqual(name, "live")
        self.assertEqual([entry["package"] for entry in entries], ["bash"])

    def test_rpm_live_base_drops_kernel_payloads(self):
        lock = {"image_sets": {"live": [
            {"name": "bash"}, {"name": "kernel-core"},
        ]}}
        _name, entries = base_image_packages(lock, "rpm", None)
        self.assertEqual([entry["name"] for entry in entries], ["bash"])


class TestRepositories(unittest.TestCase):
    def test_named_rpm_repository_accepts_an_arbitrary_public_upstream(self):
        self.assertEqual(
            ("vendor", "https://packages.vendor.org/el/10/x86_64"),
            parse_named_repository("vendor=https://packages.vendor.org/el/10/x86_64/"),
        )

    def test_remote_url_rejects_local_addresses_and_credentials(self):
        for url in (
            "http://localhost/repo",
            "http://127.0.0.1/repo",
            "https://user:secret@example.org/repo",
            "https://packages.internal/repo",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_remote_url(url)

    def test_ppa_requires_and_records_the_key_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            key = os.path.join(directory, "vendor.gpg")
            with open(key, "wb") as stream:
                stream.write(b"public key")
            line, record = parse_ppa("project/stable=" + key, "resolute", "amd64")
            self.assertIn("https://ppa.launchpadcontent.net/project/stable/ubuntu", line)
            self.assertIn("signed-by=" + key, line)
            self.assertEqual(64, len(record["key_sha256"]))
            self.assertNotIn(key, json.dumps(record))

    def test_generic_apt_repository_requires_explicit_trust(self):
        with self.assertRaisesRegex(ValueError, "signed-by"):
            apt_repository_record("deb https://packages.example.org stable main")
        with self.assertRaisesRegex(ValueError, "trusted=yes"):
            apt_repository_record("deb [trusted=yes] https://packages.example.org stable main")

    def test_generic_apt_repository_does_not_publish_local_key_path(self):
        with tempfile.TemporaryDirectory() as directory:
            key = os.path.join(directory, "vendor.gpg")
            open(key, "wb").close()
            line = "deb [signed-by={}] https://packages.example.org stable main".format(key)
            record = apt_repository_record(line)
            self.assertNotIn(directory, record["source"])
            self.assertEqual("vendor.gpg", record["source"].split("signed-by=")[1].split("]")[0])


class TestOverlayData(unittest.TestCase):
    @staticmethod
    def rpm(name, repo, requires=()):
        return {
            "arch": "x86_64",
            "checksum": hashlib.sha256(name.encode()).hexdigest(),
            "checksum_type": "sha256",
            "epoch": "0",
            "location": "Packages/{}.rpm".format(name),
            "name": name,
            "provide_evr": [],
            "provides": [name],
            "release": "1.el10",
            "repo": repo,
            "require_ranges": [],
            "requires": list(requires),
            "sourcerpm": "{}-1-1.el10.src.rpm".format(name),
            "version": "1",
        }

    def test_rpm_overlay_closes_remote_package_over_the_base(self):
        base = {
            "repos": [{
                "base": "https://base.example.org/repo",
                "kind": "binary",
                "name": "base",
                "primary": "base-primary.xml.gz",
            }],
            "solve": {"overrides": []},
            "target_cpu": "x86_64",
        }
        parsed = {
            "/cache/base": [self.rpm("bash", "base")],
            "/cache/vendor": [self.rpm("podman", "vendor", requires=("bash",))],
        }
        with mock.patch(
            "upstream_lock.rpm_relock.repository_primary",
            side_effect=["/cache/base", "/cache/vendor"],
        ), mock.patch(
            "upstream_lock.solve.parse_primary",
            side_effect=lambda path, repo=None: parsed[path],
        ):
            packages, repositories = resolve_rpm(
                base,
                [{"name": "bash"}],
                ["podman"],
                [("vendor", "https://vendor.example.org/repo")],
                "/cache",
                False,
            )
        self.assertEqual([entry["name"] for entry in packages], ["bash", "podman"])
        self.assertEqual([repo["name"] for repo in repositories], ["base", "vendor"])

    def test_change_summary_records_additions_replacements_and_removals(self):
        base = [
            {"name": "bash", "arch": "x86_64", "evr": "1-1", "sha256": "a"},
            {"name": "old", "arch": "x86_64", "evr": "1-1", "sha256": "b"},
        ]
        resolved = [
            {"name": "bash", "arch": "x86_64", "evr": "2-1", "sha256": "c"},
            {"name": "podman", "arch": "x86_64", "evr": "5-1", "sha256": "d"},
        ]
        self.assertEqual(change_summary("rpm", base, resolved), {
            "additions": ["podman"],
            "removals": ["old"],
            "replacements": [{
                "architecture": "x86_64", "from": "1-1", "name": "bash", "to": "2-1",
            }],
        })

    def test_generated_data_contains_base_digest_and_packages(self):
        lock = {
            "schema": 1,
            "family": "rpm",
            "name": "podman",
            "base": {
                "architecture": "x86_64",
                "flavor": "centos",
                "image_set": "live",
                "release": "10",
                "sha256": "a" * 64,
            },
            "changes": {},
            "packages": [{"name": "podman"}],
            "repositories": [],
            "requested_packages": ["podman"],
        }
        validate_overlay(lock)
        content = render(lock, "podman.lock.json")
        self.assertIn("OVERLAY = struct(", content)
        self.assertIn('"sha256": "{}"'.format("a" * 64), content)
        self.assertIn('"podman"', content)


if __name__ == "__main__":
    unittest.main()
