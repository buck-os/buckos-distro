#!/usr/bin/env python3
"""rpm's own rpmvercmp test corpus, run against our transcription.

The cases below are taken from rpm's tests/rpmvercmp.at. Using upstream's
corpus rather than cases invented here is the point: the value of this test
is that it encodes rpm's actual behaviour, including the parts that look
like bugs, and cases written from the documentation would agree with the
documentation instead.

The ones worth reading twice, because a plausible implementation gets them
backwards:

    10.0001 == 10.1      leading zeros are stripped before comparing
    1.0^git1 <  1.01     caret loses to a longer numeric segment
    xyz.4   <  8         a numeric segment outranks an alpha one
    a+      == a_        non-alphanumerics are separators, not content

Verified additionally against the host's real rpm bindings over this corpus
and over every version pair in the checked-in lockfiles; that cross-check
needs python3-rpm and so is not expressible as a portable test.
"""

import unittest

from rpmvercmp import compare_evr, package_is_newer, rpmvercmp

# (a, b, expected) exactly as upstream lists them.
CASES = [
    ("1.0", "1.0", 0),
    ("1.0", "2.0", -1),
    ("2.0", "1.0", 1),
    ("2.0.1", "2.0.1", 0),
    ("2.0", "2.0.1", -1),
    ("2.0.1", "2.0", 1),
    ("2.0.1a", "2.0.1a", 0),
    ("2.0.1a", "2.0.1", 1),
    ("2.0.1", "2.0.1a", -1),
    ("5.5p1", "5.5p1", 0),
    ("5.5p1", "5.5p2", -1),
    ("5.5p2", "5.5p1", 1),
    ("5.5p10", "5.5p10", 0),
    ("5.5p1", "5.5p10", -1),
    ("5.5p10", "5.5p1", 1),
    ("10xyz", "10.1xyz", -1),
    ("10.1xyz", "10xyz", 1),
    ("xyz10", "xyz10", 0),
    ("xyz10", "xyz10.1", -1),
    ("xyz10.1", "xyz10", 1),
    ("xyz.4", "xyz.4", 0),
    ("xyz.4", "8", -1),
    ("8", "xyz.4", 1),
    ("xyz.4", "2", -1),
    ("2", "xyz.4", 1),
    ("5.5p2", "5.6p1", -1),
    ("5.6p1", "5.5p2", 1),
    ("5.6p1", "6.5p1", -1),
    ("6.5p1", "5.6p1", 1),
    ("6.0.rc1", "6.0", 1),
    ("6.0", "6.0.rc1", -1),
    ("10b2", "10a1", 1),
    ("10a2", "10b2", -1),
    ("1.0aa", "1.0aa", 0),
    ("1.0a", "1.0aa", -1),
    ("1.0aa", "1.0a", 1),
    ("10.0001", "10.0001", 0),
    ("10.0001", "10.1", 0),
    ("10.1", "10.0001", 0),
    ("10.0001", "10.0039", -1),
    ("10.0039", "10.0001", 1),
    ("4.999.9", "5.0", -1),
    ("5.0", "4.999.9", 1),
    ("20101121", "20101121", 0),
    ("20101121", "20101122", -1),
    ("20101122", "20101121", 1),
    ("2_0", "2_0", 0),
    ("2.0", "2_0", 0),
    ("2_0", "2.0", 0),
    # RhBug:178798 -- separators are weightless, even trailing ones.
    ("a", "a", 0),
    ("a+", "a+", 0),
    ("a+", "a_", 0),
    ("a_", "a+", 0),
    ("+a", "+a", 0),
    ("+a", "_a", 0),
    ("_a", "+a", 0),
    ("+_", "+_", 0),
    ("_+", "+_", 0),
    ("_+", "_+", 0),
    ("+", "_", 0),
    ("_", "+", 0),
    # Tilde: sorts before everything, end of string included.
    ("1.0~rc1", "1.0~rc1", 0),
    ("1.0~rc1", "1.0", -1),
    ("1.0", "1.0~rc1", 1),
    ("1.0~rc1", "1.0~rc2", -1),
    ("1.0~rc2", "1.0~rc1", 1),
    ("1.0~rc1~git123", "1.0~rc1~git123", 0),
    ("1.0~rc1~git123", "1.0~rc1", -1),
    ("1.0~rc1", "1.0~rc1~git123", 1),
    # Caret: after end of string, before a new segment.
    ("1.0^", "1.0^", 0),
    ("1.0^", "1.0", 1),
    ("1.0", "1.0^", -1),
    ("1.0^git1", "1.0^git1", 0),
    ("1.0^git1", "1.0", 1),
    ("1.0", "1.0^git1", -1),
    ("1.0^git1", "1.0^git2", -1),
    ("1.0^git2", "1.0^git1", 1),
    ("1.0^git1", "1.01", -1),
    ("1.01", "1.0^git1", 1),
    ("1.0^20160101", "1.0^20160101", 0),
    ("1.0^20160101", "1.0.1", -1),
    ("1.0.1", "1.0^20160101", 1),
    ("1.0^20160101^git1", "1.0^20160101^git1", 0),
    ("1.0^20160102", "1.0^20160101^git1", 1),
    ("1.0^20160101^git1", "1.0^20160102", -1),
    # Tilde and caret together.
    ("1.0~rc1^git1", "1.0~rc1^git1", 0),
    ("1.0~rc1^git1", "1.0~rc1", 1),
    ("1.0~rc1", "1.0~rc1^git1", -1),
    ("1.0^git1~pre", "1.0^git1~pre", 0),
    ("1.0^git1", "1.0^git1~pre", 1),
    ("1.0^git1~pre", "1.0^git1", -1),
]


class TestRpmvercmpCorpus(unittest.TestCase):
    def test_upstream_corpus(self):
        for left, right, expected in CASES:
            with self.subTest(left=left, right=right):
                self.assertEqual(rpmvercmp(left, right), expected)

    def test_the_corpus_is_internally_antisymmetric(self):
        """Guards the corpus, not the code: a typo'd expectation is silent.

        A wrong `expected` would make this suite assert the wrong thing and
        still pass. Every case having a consistent mirror is a property the
        transcription cannot fake.
        """
        for left, right, expected in CASES:
            with self.subTest(left=left, right=right):
                self.assertEqual(rpmvercmp(right, left), -expected)


class TestCompareEvr(unittest.TestCase):
    def test_release_breaks_a_version_tie(self):
        # The commonest update shape there is.
        self.assertEqual(
            compare_evr(("0", "1.13", "4.fc43"), ("0", "1.13", "5.fc43")), -1
        )

    def test_epoch_overrules_a_newer_version(self):
        """Epoch exists for exactly this: a version that went backwards."""
        self.assertEqual(
            compare_evr(("1", "1.0", "1"), ("0", "9.0", "1")), 1
        )

    def test_absent_and_zero_epoch_are_the_same(self):
        for empty in (None, "", "(none)"):
            with self.subTest(epoch=empty):
                self.assertEqual(
                    compare_evr((empty, "1.0", "1"), ("0", "1.0", "1")), 0
                )

    def test_missing_version_or_release_does_not_raise(self):
        """Source repodata omits fields more often than one would like."""
        self.assertEqual(compare_evr(("0", None, None), ("0", "", "")), 0)
        self.assertEqual(compare_evr(("0", "1.0", None), ("0", None, None)), 1)


class TestPackageIsNewer(unittest.TestCase):
    def pkg(self, version, release, epoch="0"):
        return {"version": version, "release": release, "epoch": epoch}

    def test_newer_release_replaces(self):
        self.assertTrue(
            package_is_newer(self.pkg("1.13", "5.fc43"),
                             self.pkg("1.13", "4.fc43"))
        )

    def test_equal_does_not_replace(self):
        """Ties keep the incumbent, so a merge is order-independent.

        Two repos carrying the identical build is the normal case, not an
        edge one, and a tie that replaced would make the winner depend on
        which repo was passed first.
        """
        self.assertFalse(
            package_is_newer(self.pkg("1.13", "4.fc43"),
                             self.pkg("1.13", "4.fc43"))
        )

    def test_older_does_not_replace(self):
        self.assertFalse(
            package_is_newer(self.pkg("1.12", "9.fc43"),
                             self.pkg("1.13", "1.fc43"))
        )


if __name__ == "__main__":
    unittest.main()
