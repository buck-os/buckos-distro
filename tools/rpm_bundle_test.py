#!/usr/bin/env python3
"""Exercise package collisions, relocation, exports and reproducible staging."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from rpm_bundle import assemble, digest, merge_payload, relocate_symlinks
from rpm_bundle_lock import select_packages
from verify_file import verify_copy


class BundleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_relocation_survives_moving_bundle(self):
        root = self.root / "payload"
        (root / "opt/vendor/lib").mkdir(parents=True)
        (root / "opt/vendor/lib/libvendor.a").write_bytes(b"!<arch>\n")
        (root / "opt/vendor/lib/libalias.a").symlink_to("/opt/vendor/lib/libvendor.a")
        relocate_symlinks(root)
        moved = self.root / "moved"
        root.rename(moved)
        self.assertEqual((moved / "opt/vendor/lib/libalias.a").read_bytes(), b"!<arch>\n")

    def test_symlink_escape_is_rejected(self):
        root = self.root / "payload"
        root.mkdir()
        (root / "escape").symlink_to("../outside")
        with self.assertRaisesRegex(ValueError, "escapes"):
            relocate_symlinks(root)

    def test_conflicts_rejected_but_identical_files_shared(self):
        left, right = self.root / "left", self.root / "right"
        left.mkdir()
        right.mkdir()
        (left / "lib.a").write_bytes(b"first")
        (right / "lib.a").write_bytes(b"first")
        merge_payload(left, right)
        (left / "lib.a").write_bytes(b"second")
        with self.assertRaisesRegex(ValueError, "conflicting"):
            merge_payload(left, right)

    def test_merge_cannot_write_through_directory_symlink(self):
        source, target = self.root / "source", self.root / "target"
        (source / "lib").mkdir(parents=True)
        target.mkdir()
        (target / "lib").symlink_to("../outside")
        with self.assertRaisesRegex(ValueError, "conflicting RPM directory"):
            merge_payload(source, target)

    def extract_fixture(self, rpm, destination):
        lib = Path(destination) / "opt/vendor/lib"
        lib.mkdir(parents=True)
        (lib / "libvendor.a").write_bytes(b"!<arch>\n")
        (lib / "libalias.a").symlink_to("/opt/vendor/lib/libvendor.a")

    def build(self, name, exports):
        rpm = self.root / "sdk.rpm"
        rpm.write_bytes(b"fixture")
        out, tar, manifest = (self.root / (name + suffix) for suffix in ("", ".tar", ".json"))
        with mock.patch("rpm_bundle.extract_rpm", side_effect=self.extract_fixture):
            with mock.patch("rpm_bundle.package_identity", return_value={"architecture": "aarch64"}):
                assemble([rpm], out, tar, manifest, "aarch64", exports)
        return out, tar, manifest

    def test_archives_reproducible_and_inventory_has_package_digest(self):
        _, tar1, manifest1 = self.build("one", {"library": "opt/vendor/lib/libvendor.a"})
        _, tar2, manifest2 = self.build("two", {"library": "opt/vendor/lib/libvendor.a"})
        self.assertEqual(digest(tar1), digest(tar2))
        self.assertEqual(manifest1.read_bytes(), manifest2.read_bytes())
        data = json.loads(manifest1.read_text())
        self.assertEqual(data["packages"][0]["sha256"], digest(self.root / "sdk.rpm"))
        self.assertEqual(data["runtime_compatibility"], "unverified")

    def test_missing_export_fails(self):
        with self.assertRaisesRegex(ValueError, "missing or escaping export"):
            self.build("missing", {"library": "opt/vendor/lib/absent.a"})

    def test_traversal_export_fails(self):
        with self.assertRaisesRegex(ValueError, "traversal"):
            self.build("traversal", {"library": "../outside.a"})

    def test_wrong_rpm_cpu_is_rejected(self):
        rpm = self.root / "wrong.rpm"
        rpm.write_bytes(b"fixture")
        with mock.patch("rpm_bundle.package_identity", return_value={"architecture": "x86_64"}):
            with self.assertRaisesRegex(ValueError, "bundle targets aarch64"):
                assemble([rpm], self.root / "wrong", self.root / "wrong.tar",
                         self.root / "wrong.json", "aarch64")

    def test_standalone_archive_export_dereferences_symlink(self):
        rpm = self.root / "sdk.rpm"
        rpm.write_bytes(b"fixture")
        exported = self.root / "export.a"
        with mock.patch("rpm_bundle.extract_rpm", side_effect=self.extract_fixture):
            with mock.patch("rpm_bundle.package_identity", return_value={"architecture": "aarch64"}):
                assemble([rpm], self.root / "export-payload", self.root / "export.tar",
                         self.root / "export.json", "aarch64",
                         {"lib": "opt/vendor/lib/libalias.a"}, export_outputs={"lib": exported})
        self.assertFalse(exported.is_symlink())
        self.assertEqual(exported.read_bytes(), b"!<arch>\n")

    def test_thin_static_archive_fails(self):
        def thin(rpm, destination):
            (Path(destination) / "lib.a").write_bytes(b"!<thin>\n")
        with mock.patch.object(self, "extract_fixture", side_effect=thin):
            with self.assertRaisesRegex(ValueError, "thin archives unsupported"):
                self.build("thin", {"library": "lib.a"})


class PinTest(unittest.TestCase):
    def package(self, version="1.0", arch="aarch64", checksum_type="sha256"):
        return {"name": "vendor-devel", "arch": arch, "epoch": "0",
                "version": version, "release": "1", "location": "vendor.rpm",
                "checksum": "a" * 64, "checksum_type": checksum_type}

    def test_version_order_and_cpu_filter(self):
        records = [self.package("9"), self.package("10"), self.package("11", "x86_64")]
        packages = select_packages(records, ["vendor-devel"], "aarch64", "https://example.org/repo/")
        self.assertEqual(packages[0]["version"], "10")
        self.assertEqual(packages[0]["url"], "https://example.org/repo/vendor.rpm")

    def test_reject_weak_checksum(self):
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            select_packages([self.package(checksum_type="sha1")], ["vendor-devel"],
                            "aarch64", "https://example.org/repo/")

    def test_missing_target_architecture_fails(self):
        with self.assertRaisesRegex(ValueError, "no aarch64"):
            select_packages([self.package(arch="x86_64")], ["vendor-devel"],
                            "aarch64", "https://example.org/repo/")

    def test_local_rpm_verifier_rejects_modified_bytes(self):
        with tempfile.TemporaryDirectory() as root:
            source, out = Path(root) / "source", Path(root) / "out"
            source.write_bytes(b"original")
            sha256 = digest(source)
            verify_copy(source, out, sha256)
            self.assertEqual(out.read_bytes(), b"original")
            out.unlink()
            source.write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                verify_copy(source, out, sha256)
            self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
