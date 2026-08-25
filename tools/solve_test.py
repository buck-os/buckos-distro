"""Tests for the image-set closure.

depgraph_test covers what a `Requires` means.  This covers the part that
sits above it: which closure a given override applies to.  That distinction
has no signature in the output -- a lockfile built with the wrong override
is still a well-formed lockfile, listing packages that all exist, and the
mistake only surfaces much later as rpm refusing a transaction.  So it gets
tested here rather than trusted to a re-solve.
"""

import unittest

from solve import build_universe, solve_image_sets


def binary(name, requires=(), provides=(), source="src-1.fc43.src.rpm"):
    return {
        "name": name,
        "requires": list(requires),
        "provides": list(provides),
        "sourcerpm": source,
        "version": "1",
        "release": "1.fc43",
        "epoch": "0",
        "arch": "x86_64",
        "location": name + ".rpm",
        "checksum": "0" * 64,
        "checksum_type": "sha256",
    }


def universe_of(*pkgs):
    return build_universe(list(pkgs), [])


class TestImageSets(unittest.TestCase):
    def test_closes_over_runtime_requires(self):
        universe = universe_of(
            binary("shell", requires=["libc"]),
            binary("glibc", provides=["libc"]),
        )
        sets, problems = solve_image_sets(universe, {"live": ["shell"]})
        self.assertEqual(problems, [])
        self.assertEqual(sets["live"], ["glibc", "shell"])

    def test_reports_a_root_that_does_not_exist(self):
        universe = universe_of(binary("shell"))
        sets, problems = solve_image_sets(universe, {"live": ["shell", "nope"]})
        kinds = [kind for kind, _, _ in problems]
        self.assertEqual(kinds, ["missing-binary"])
        # The rest of the set still solves: one retired package name should
        # not cost the report on the other thirty.
        self.assertEqual(sets["live"], ["shell"])

    def test_attributes_problems_to_the_image_not_the_package(self):
        universe = universe_of(binary("shell", requires=["missing-cap"]))
        _, problems = solve_image_sets(universe, {"live": ["shell"]})
        self.assertEqual(len(problems), 1)
        self.assertIn("image:live", problems[0][2])

    def test_each_set_is_closed_independently(self):
        universe = universe_of(
            binary("shell"),
            binary("kernel"),
        )
        sets, problems = solve_image_sets(
            universe, {"live": ["shell"], "netinst": ["kernel"]}
        )
        self.assertEqual(problems, [])
        self.assertEqual(sets["live"], ["shell"])
        self.assertEqual(sets["netinst"], ["kernel"])


class TestScopedOverrides(unittest.TestCase):
    """The systemd-sysusers case, reduced.

    Two packages provide one capability.  The right answer differs between
    a buildroot (the standalone binary, which is smaller) and an image (the
    full package, which is installed anyway and owns the same file), so the
    global override has to be overridable per set.
    """

    def universe(self):
        return universe_of(
            binary("systemd", provides=["/usr/bin/systemd-sysusers"]),
            binary("systemd-standalone-sysusers",
                   provides=["/usr/bin/systemd-sysusers"]),
            binary("setup", requires=["/usr/bin/systemd-sysusers"]),
        )

    def test_global_override_applies_by_default(self):
        sets, problems = solve_image_sets(
            self.universe(), {"live": ["setup"]},
            overrides={"/usr/bin/systemd-sysusers":
                       "systemd-standalone-sysusers"},
        )
        self.assertEqual(problems, [])
        self.assertIn("systemd-standalone-sysusers", sets["live"])

    def test_image_override_wins_over_the_global_one(self):
        sets, problems = solve_image_sets(
            self.universe(), {"live": ["setup"]},
            overrides={"/usr/bin/systemd-sysusers":
                       "systemd-standalone-sysusers"},
            image_overrides={"live": {"/usr/bin/systemd-sysusers": "systemd"}},
        )
        self.assertEqual(problems, [])
        self.assertIn("systemd", sets["live"])
        self.assertNotIn("systemd-standalone-sysusers", sets["live"])

    def test_an_override_scoped_to_one_set_leaves_the_others_alone(self):
        sets, _ = solve_image_sets(
            self.universe(),
            {"live": ["setup"], "minimal": ["setup"]},
            overrides={"/usr/bin/systemd-sysusers":
                       "systemd-standalone-sysusers"},
            image_overrides={"live": {"/usr/bin/systemd-sysusers": "systemd"}},
        )
        self.assertIn("systemd", sets["live"])
        self.assertIn("systemd-standalone-sysusers", sets["minimal"])

    def test_the_global_overrides_are_not_mutated(self):
        # Layering by dict(a, **b) rather than a.update(b): the caller's
        # dict is also the build closure's, and one leaked key there would
        # silently rebuild every buildroot.
        overrides = {"/usr/bin/systemd-sysusers":
                     "systemd-standalone-sysusers"}
        solve_image_sets(
            self.universe(), {"live": ["setup"]}, overrides,
            image_overrides={"live": {"other-cap": "whatever"}},
        )
        self.assertEqual(
            overrides,
            {"/usr/bin/systemd-sysusers": "systemd-standalone-sysusers"},
        )


if __name__ == "__main__":
    unittest.main()
