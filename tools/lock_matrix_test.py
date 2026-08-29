#!/usr/bin/env python3
"""Validate the checked-in release and architecture lock matrix."""

import json
import os
import unittest


MATRIX = {
    "fedora": ("44", "45"),
    "centos": ("9", "10"),
    "centos-hyperscale": ("9", "10"),
    "debian": ("13",),
    "ubuntu": ("26.04",),
}
ARCHITECTURES = ("x86_64", "aarch64")
DEB_ARCH = {"x86_64": "amd64", "aarch64": "arm64"}


def repo_root():
    for start in (os.getcwd(), os.path.dirname(os.path.abspath(__file__))):
        path = start
        while True:
            if os.path.isfile(os.path.join(path, ".buckroot")):
                return path
            parent = os.path.dirname(path)
            if parent == path:
                break
            path = parent
    raise AssertionError("cannot locate repository root")


class TestLockMatrix(unittest.TestCase):
    def test_every_supported_release_has_both_architectures(self):
        root = repo_root()
        for flavor, releases in MATRIX.items():
            for release in releases:
                for architecture in ARCHITECTURES:
                    with self.subTest(flavor=flavor, release=release, architecture=architecture):
                        path = os.path.join(
                            root,
                            "flavors",
                            flavor,
                            "lock",
                            "{}-{}-{}.lock.json".format(flavor, release, architecture),
                        )
                        with open(path, encoding="utf-8") as stream:
                            lock = json.load(stream)
                        self.assertEqual(architecture, lock["target_cpu"])
                        if flavor in ("debian", "ubuntu"):
                            self.assertEqual(DEB_ARCH[architecture], lock["architecture"])
                            for image in lock["image_sets"].values():
                                self.assertIn("dpkg", {item["package"] for item in image})
                                for item in image:
                                    self.assertIn(item["architecture"], (DEB_ARCH[architecture], "all"))
                        else:
                            for image in lock["image_sets"].values():
                                for item in image:
                                    self.assertIn(item["arch"], (architecture, "noarch"))

    def test_fedora_43_is_not_supported(self):
        lock_dir = os.path.join(repo_root(), "flavors", "fedora", "lock")
        self.assertFalse(any(name.startswith("fedora-43") for name in os.listdir(lock_dir)))


if __name__ == "__main__":
    unittest.main()
