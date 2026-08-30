#!/usr/bin/env python3
"""Every configured live ISO label must be legal and unambiguous.

ISO9660 caps the volume identifier at 32 characters, and xorriso refuses a
longer one rather than truncating it, so an over-long label is a build
failure that surfaces only when someone builds that image. Two Hyperscale
prebuilt labels were 33 and 34 characters and nobody found out until the
first Hyperscale image build was attempted.

Length is only half of it. The label is also the live-root kernel argument,
so two images sharing one would give the live root of whichever disc was
found first. Uniqueness is the property that actually matters, and trimming
toward the limit is exactly the operation that could destroy it.
"""

import os
import re
import unittest


VOLUME_LABEL_MAX = 32

# The configured matrix, matching tools/boot_tools_test.py. Kept as data
# rather than read from Buck, because a test that shells out to buck2 from
# inside a buck2 test is a worse trade than restating eight rows.
FLAVOR_RELEASES = (
    ("fedora", "44"),
    ("fedora", "45"),
    ("centos", "9"),
    ("centos", "10"),
    ("centos-hyperscale", "9"),
    ("centos-hyperscale", "10"),
    ("debian", "13"),
    ("ubuntu", "26.04"),
)
IMAGE_VARIANTS = ("", "-prebuilt")


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


def trim():
    """The Starlark helper's limit, read from the source that defines it."""
    path = os.path.join(repo_root(), "defs", "releases.bzl")
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    match = re.search(r"_ISO_VOLUME_LABEL_MAX\s*=\s*(\d+)", source)
    if not match:
        raise AssertionError("defs/releases.bzl no longer defines the limit")
    return int(match.group(1))


def label(flavor, release, variant):
    """The same construction defs/rpm_family.bzl and defs/deb_family.bzl use."""
    text = "{}-{}-LIVE{}".format(flavor.upper(), release, variant.upper())
    return text[:VOLUME_LABEL_MAX]


def configured():
    return {
        "{}-{}{}".format(flavor, release, variant): label(flavor, release, variant)
        for flavor, release in FLAVOR_RELEASES
        for variant in IMAGE_VARIANTS
    }


class TestIsoVolumeLabels(unittest.TestCase):
    def test_the_starlark_helper_uses_the_iso9660_limit(self):
        self.assertEqual(VOLUME_LABEL_MAX, trim())

    def test_every_label_is_within_the_iso9660_limit(self):
        for image, text in sorted(configured().items()):
            self.assertLessEqual(
                len(text),
                VOLUME_LABEL_MAX,
                "{} has a {}-character label {!r}".format(image, len(text), text),
            )

    def test_no_two_images_claim_the_same_label(self):
        # The one that matters. Trimming toward the limit is what could make
        # two images collide, and a collision means the live root comes from
        # whichever disc was found first.
        seen = {}
        for image, text in sorted(configured().items()):
            seen.setdefault(text, []).append(image)
        collisions = {t: i for t, i in seen.items() if len(i) > 1}
        self.assertEqual({}, collisions, "labels claimed by more than one image")

    def test_the_two_labels_that_actually_needed_trimming_still_differ(self):
        # CENTOS-HYPERSCALE is the only flavor long enough to reach the
        # limit, and its prebuilt labels are the ones that were rejected.
        for release in ("9", "10"):
            source = label("centos-hyperscale", release, "")
            prebuilt = label("centos-hyperscale", release, "-prebuilt")
            self.assertEqual(VOLUME_LABEL_MAX, len(prebuilt))
            self.assertNotEqual(source, prebuilt)
            self.assertTrue(prebuilt.startswith(source))

    def test_an_untrimmed_label_is_left_exactly_as_written(self):
        self.assertEqual("FEDORA-44-LIVE", label("fedora", "44", ""))
        self.assertEqual(
            "FEDORA-44-LIVE-PREBUILT", label("fedora", "44", "-prebuilt")
        )


if __name__ == "__main__":
    unittest.main()
