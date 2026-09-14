#!/usr/bin/env python3
"""Contract tests over manifests emitted by real rootfs target families."""

import json
from pathlib import Path
import unittest


# Do not resolve the source symlink: Buck places resources beside it in the
# link tree, while resolving would jump back to the checkout without them.
TEST_ROOT = Path(__file__).absolute().parent


def manifest(name):
    with (TEST_ROOT / name).open(encoding="utf-8") as stream:
        return json.load(stream)


class TestRootfsManifest(unittest.TestCase):
    def assert_archive_contract(self, value):
        self.assertEqual(value["schema"], "buckos.rootfs.v1")
        self.assertEqual(value["archive"], {
            "compression": "none",
            "format": "tar",
            "layout": "complete-rootfs",
            "media_type": "application/x-tar",
            "root": "./",
        })
        self.assertEqual(value["filesystem"], {
            "acls": True,
            "numeric_ownership": True,
            "path_semantics": "posix",
            "xattrs": True,
        })

    def test_rpm_prebuilt_base_identity(self):
        value = manifest("fedora-prebuilt.json")
        self.assert_archive_contract(value)
        self.assertEqual(value["role"], "base")
        self.assertEqual(value["target"], {
            "architecture": "x86_64",
            "os": {"id": "fedora", "version_id": "44"},
            "package_manager": "rpm",
        })
        self.assertEqual(value["build"], {
            "buildroot_provenance": "binary-seed",
            "package_provenance": "upstream-binary",
            "transforms": [],
        })

    def test_debian_source_preferred_base_identity(self):
        value = manifest("debian-source.json")
        self.assert_archive_contract(value)
        self.assertEqual(value["role"], "base")
        self.assertEqual(value["target"], {
            "architecture": "x86_64",
            "os": {"id": "debian", "version_id": "13"},
            "package_manager": "dpkg",
        })
        self.assertEqual(value["build"], {
            "buildroot_provenance": "binary-seed",
            "package_provenance": "source-preferred",
            "transforms": [],
        })

    def test_transform_preserves_identity_and_records_itself(self):
        source = manifest("fedora-live-prebuilt.json")
        transformed = manifest("fedora-verify.json")
        self.assert_archive_contract(transformed)
        self.assertEqual(transformed["target"], source["target"])
        self.assertEqual(transformed["role"], source["role"])
        self.assertEqual(
            transformed["build"]["buildroot_provenance"],
            source["build"]["buildroot_provenance"],
        )
        self.assertEqual(
            transformed["build"]["package_provenance"],
            source["build"]["package_provenance"],
        )
        self.assertEqual(
            transformed["build"]["transforms"],
            ["verification-overlay"],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
