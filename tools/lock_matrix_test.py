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

    def test_rpm_live_variants_keep_normal_producers(self):
        root = repo_root()
        rpm_matrix = {
            "fedora": ("44", "45"),
            "centos": ("9", "10"),
            "centos-hyperscale": ("9", "10"),
        }
        overlaps = []
        for flavor, releases in rpm_matrix.items():
            for release in releases:
                for architecture in ARCHITECTURES:
                    path = os.path.join(
                        root,
                        "flavors",
                        flavor,
                        "lock",
                        "{}-{}-{}.lock.json".format(
                            flavor, release, architecture
                        ),
                    )
                    with open(path, encoding="utf-8") as stream:
                        lock = json.load(stream)
                    normal = {}
                    variants = {}
                    for recipe_name, recipe in lock["packages"].items():
                        target = variants if recipe.get("variant_of") else normal
                        for binary in recipe["subpackages"]:
                            target[binary] = recipe_name
                    for entry in lock["image_sets"]["live"]:
                        binary = entry["name"]
                        variant = variants.get(binary)
                        if variant is None:
                            continue
                        self.assertIn(binary, normal)
                        self.assertEqual(entry["source"], normal[binary])
                        overlaps.append(
                            (flavor, release, architecture, binary, variant)
                        )
        self.assertEqual(
            overlaps,
            [
                ("fedora", "44", "x86_64", "libacl", "acl-compat"),
                ("fedora", "44", "aarch64", "libacl", "acl-compat"),
            ],
        )

    def test_fedora_44_tar_keeps_acl_compat_build_dependencies(self):
        root = repo_root()
        for architecture in ARCHITECTURES:
            with self.subTest(architecture=architecture):
                path = os.path.join(
                    root,
                    "flavors",
                    "fedora",
                    "lock",
                    "fedora-44-{}.lock.json".format(architecture),
                )
                with open(path, encoding="utf-8") as stream:
                    lock = json.load(stream)
                live_libacl = next(
                    entry
                    for entry in lock["image_sets"]["live"]
                    if entry["name"] == "libacl"
                )
                self.assertEqual(live_libacl["source"], "acl")
                self.assertEqual(live_libacl["evr"], "2.4.0-1.fc44")
                self.assertEqual(lock["packages"]["acl-compat"]["variant_of"], "acl")
                tar_deps = {
                    entry["name"]: entry
                    for entry in lock["packages"]["tar"]["deps_built"]
                }
                for binary in ("libacl", "libacl-devel"):
                    self.assertEqual(tar_deps[binary]["variant"], "acl-compat")
                    self.assertEqual(tar_deps[binary]["evr"], "2.3.2-6.fc44")


# The source packages CentOS Hyperscale replaces with its own newer builds,
# per release.  This is the whole reason the flavor exists: Hyperscale is
# CentOS Stream plus the SIG's `main` repository, and `main` wins wherever it
# offers a higher EVR.  Recorded explicitly rather than derived from the lock,
# because a list derived from the lock would agree with the lock no matter
# what the lock said.
HYPERSCALE_REPO = "hyperscale-main"
HYPERSCALE_SOURCES = {
    "9": {
        "dbus-broker": 1,
        "dracut": 5,
        "e2fsprogs": 4,
        "elfutils": 3,
        "grep": 1,
        "libbpf": 1,
        "selinux-policy": 2,
        "squashfs-tools": 1,
        "systemd": 6,
        "tar": 1,
        "vim": 2,
    },
    "10": {
        "chkconfig": 1,
        "coreutils": 2,
        "dbus-broker": 1,
        "dracut": 5,
        "elfutils": 3,
        "selinux-policy": 2,
        "systemd": 6,
    },
}


class TestHyperscalePrecedence(unittest.TestCase):
    """Prove the live image actually got the Hyperscale builds.

    Every other gate this flavor has is satisfied by CentOS Stream wearing a
    Hyperscale label.  The image boots, systemd is PID 1, SELinux is
    enforcing, there are no AVC denials -- and none of that changes if a
    repository-precedence regression quietly resolved `systemd` to Stream's
    252 instead of Hyperscale's 260.  The thing that makes the flavor what it
    is has no assertion anywhere, so it goes here.

    Checked against the recorded live image set rather than a booted image:
    it is the manifest the rootfs transaction installs from, it names the
    repository and the exact EVR of every payload package, and it is
    available without building anything.
    """

    def live_set(self, release, architecture):
        path = os.path.join(
            repo_root(), "flavors", "centos-hyperscale", "lock",
            "centos-hyperscale-{}-{}.lock.json".format(release, architecture),
        )
        with open(path, encoding="utf-8") as stream:
            return json.load(stream)["image_sets"]["live"]

    def test_every_replaced_source_comes_from_hyperscale(self):
        """The displacement check: a Stream build where Hyperscale is expected."""
        for release, expected in HYPERSCALE_SOURCES.items():
            for architecture in ARCHITECTURES:
                live = self.live_set(release, architecture)
                by_source = {}
                for item in live:
                    by_source.setdefault(item["source"], []).append(item)
                for source, count in expected.items():
                    with self.subTest(release=release, arch=architecture, source=source):
                        items = by_source.get(source)
                        self.assertIsNotNone(
                            items,
                            "{} is not in the release {} live image at all; if "
                            "Hyperscale stopped shipping it, update "
                            "HYPERSCALE_SOURCES deliberately".format(source, release),
                        )
                        self.assertEqual(
                            count, len(items),
                            "{} produces {} live binaries on release {}, not {}"
                            .format(source, len(items), release, count),
                        )
                        for item in items:
                            self.assertEqual(
                                HYPERSCALE_REPO, item["repo"],
                                "{} came from {} instead of {}: a Stream build "
                                "has displaced the Hyperscale one".format(
                                    item["name"], item["repo"], HYPERSCALE_REPO,
                                ),
                            )

    def test_replaced_builds_carry_the_release_dist_tag(self):
        """A second, independent signal, so neither one alone can be spoofed.

        The repository field says where the solver took the package from; the
        dist tag is baked into the EVR by whoever built it.  Requiring both to
        agree also catches a lock cross-contaminated from the other release.
        """
        for release, expected in HYPERSCALE_SOURCES.items():
            tag = ".hs.el{}".format(release)
            for architecture in ARCHITECTURES:
                for item in self.live_set(release, architecture):
                    if item["source"] not in expected:
                        continue
                    with self.subTest(release=release, arch=architecture, package=item["name"]):
                        self.assertIn(
                            tag, item["evr"],
                            "{} is {}, which does not carry {}".format(
                                item["name"], item["evr"], tag,
                            ),
                        )

    def test_the_two_signals_agree_across_the_whole_image(self):
        """Nothing outside the registry may come from Hyperscale, or wear its tag.

        This is the half that keeps the registry honest.  Without it a new
        Hyperscale replacement could appear, or an existing one silently move
        to Stream while some other package took its place in the count, and
        the checks above would still pass.
        """
        for release, expected in HYPERSCALE_SOURCES.items():
            tag = ".hs.el{}".format(release)
            for architecture in ARCHITECTURES:
                live = self.live_set(release, architecture)
                from_repo = {i["name"] for i in live if i["repo"] == HYPERSCALE_REPO}
                tagged = {i["name"] for i in live if tag in i["evr"]}
                registered = {
                    i["name"] for i in live if i["source"] in expected
                }
                with self.subTest(release=release, arch=architecture):
                    self.assertEqual(
                        registered, from_repo,
                        "the set of packages from {} does not match "
                        "HYPERSCALE_SOURCES; update it deliberately".format(
                            HYPERSCALE_REPO,
                        ),
                    )
                    self.assertEqual(
                        from_repo, tagged,
                        "repository and dist tag disagree about which packages "
                        "are Hyperscale builds",
                    )

    def test_both_architectures_select_the_same_builds(self):
        """An arch-specific precedence regression would otherwise pass.

        The two architectures resolve independently from their own repository
        tables, so nothing but this makes them agree.
        """
        for release in HYPERSCALE_SOURCES:
            selected = {}
            for architecture in ARCHITECTURES:
                selected[architecture] = {
                    item["name"]: item["evr"]
                    for item in self.live_set(release, architecture)
                    if item["repo"] == HYPERSCALE_REPO
                }
            with self.subTest(release=release):
                self.assertEqual(*selected.values())

    def test_the_registry_is_not_empty(self):
        """Guards every check above from passing by having nothing to check."""
        for release, expected in HYPERSCALE_SOURCES.items():
            with self.subTest(release=release):
                self.assertTrue(expected)
                self.assertTrue(all(count > 0 for count in expected.values()))


if __name__ == "__main__":
    unittest.main()
