#!/usr/bin/env python3
"""Tests for the generic RPM-family relock loop."""

import unittest

import rpm_relock


def template():
    return {
        "flavor": "centos-hyperscale",
        "release": "10",
        "dist_tag": ".hs.el10",
    }


def recorded(**overrides):
    values = {
        "build": ["acl"],
        "explicit_build": [],
        "seed_packages": [],
        "overrides": [],
        "images": [],
        "image_overrides": [],
        "source_variants": [],
        "source_image_sets": ["live"],
        "prebuilt_sources": ["kernel"],
        "seed_only": False,
        "stages": 3,
    }
    values.update(overrides)
    return values


def flags(argv, name):
    return [argv[index + 1]
            for index, value in enumerate(argv) if value == name]


class TestSolveArgv(unittest.TestCase):
    def test_flavor_probe_and_variants_are_preserved(self):
        argv = rpm_relock.solve_argv(
            template(),
            recorded(source_variants=["acl-compat=acl@2.3.2-6.el10:tar"]),
            [],
            "aarch64",
            "/tmp/out.json",
            probe="/tmp/results.probe.json",
        )
        self.assertEqual(flags(argv, "--flavor"), ["centos-hyperscale"])
        self.assertEqual(flags(argv, "--probe"), ["/tmp/results.probe.json"])
        self.assertEqual(
            flags(argv, "--source-variant"),
            ["acl-compat=acl@2.3.2-6.el10:tar"],
        )
        self.assertEqual(flags(argv, "--source-image"), ["live"])
        self.assertEqual(flags(argv, "--prebuilt-source"), ["kernel"])
        self.assertEqual(flags(argv, "--build"), [])


class TestConvergence(unittest.TestCase):
    def test_complete_lock_is_converged(self):
        state = rpm_relock.convergence_state({"packages": {}, "problems": []})
        self.assertTrue(rpm_relock.converged(state))

    def test_every_incomplete_state_is_exposed(self):
        state = rpm_relock.convergence_state({
            "problems": [{"kind": "unresolved"}],
            "packages": {
                "needs-probe": {
                    "dynamic_buildrequires": {
                        "suspected": ["rust-packaging"],
                        "unmet": False,
                    },
                },
                "needs-another-pass": {
                    "dynamic_buildrequires": {
                        "suspected": [],
                        "unmet": True,
                    },
                },
            },
        })
        self.assertFalse(rpm_relock.converged(state))
        self.assertEqual(sorted(state["unprobed"]), ["needs-probe"])
        self.assertEqual(sorted(state["unmet"]), ["needs-another-pass"])
        self.assertEqual(
            rpm_relock.describe_state(state),
            "1 unresolved problem(s), 1 unprobed package(s), 1 unmet probe(s)",
        )


if __name__ == "__main__":
    unittest.main()
