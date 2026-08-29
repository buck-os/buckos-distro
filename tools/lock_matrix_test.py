#!/usr/bin/env python3
"""Validate the checked-in release and architecture lock matrix."""

import json
import os
import unittest

from solve import rpm_source_policy_inputs
from source_policy import validate_source_policy


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
                            if lock.get("schema") == 3:
                                producers = {
                                    "{}@{}".format(source["name"], source["version_full"])
                                    for source in lock["sources"]
                                }
                                validate_source_policy(
                                    lock["source_policy"],
                                    lock["image_sets"],
                                    producers,
                                )
                                for image_name in lock["source_policy"]["image_sets"]:
                                    for item in lock["image_sets"][image_name]:
                                        self.assertEqual(
                                            item["source"],
                                            "{}@{}".format(
                                                item["source_name"],
                                                item["source_version"],
                                            ),
                                        )
                        else:
                            for image in lock["image_sets"].values():
                                for item in image:
                                    self.assertIn(item["arch"], (architecture, "noarch"))

    def test_fedora_43_is_not_supported(self):
        lock_dir = os.path.join(repo_root(), "flavors", "fedora", "lock")
        self.assertFalse(any(name.startswith("fedora-43") for name in os.listdir(lock_dir)))

    def test_fedora_source_policy_covers_both_architectures(self):
        root = repo_root()
        expected = {
            "44": {"pinned": 5, "source": 181, "total": 186},
            "45": {"pinned": 6, "source": 187, "total": 193},
        }
        for release in MATRIX["fedora"]:
            locks = {}
            for architecture in ARCHITECTURES:
                path = os.path.join(
                    root,
                    "flavors",
                    "fedora",
                    "lock",
                    "fedora-{}-{}.lock.json".format(release, architecture),
                )
                with open(path, encoding="utf-8") as stream:
                    lock = json.load(stream)
                images, producers = rpm_source_policy_inputs(lock)
                validate_source_policy(lock["source_policy"], images, producers)
                self.assertEqual(
                    lock["source_policy"]["summary"]["live"],
                    expected[release],
                )
                expected_build = {
                    entry["source"] for entry in lock["image_sets"]["live"]
                } - set(lock["solve"]["prebuilt_sources"])
                expected_build.update(
                    variant.split("=", 1)[0]
                    for variant in lock["solve"]["source_variants"]
                )
                self.assertEqual(set(lock["solve"]["build"]), expected_build)
                locks[architecture] = lock

            self.assertEqual(
                locks["x86_64"]["solve"]["build"],
                locks["aarch64"]["solve"]["build"],
            )
            self.assertEqual(
                locks["x86_64"]["source_policy"],
                locks["aarch64"]["source_policy"],
            )
            self.assertEqual(
                [repo["name"] for repo in locks["x86_64"]["repos"]],
                [repo["name"] for repo in locks["aarch64"]["repos"]],
            )


if __name__ == "__main__":
    unittest.main()
