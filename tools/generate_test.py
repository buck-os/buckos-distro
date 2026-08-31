#!/usr/bin/env python3
"""Tests for RPM lock-to-Starlark generation boundaries."""

import copy
import unittest

import generate
from source_policy import build_source_policy


def lock():
    value = {
        "dist_tag": ".fc44",
        "flavor": "fedora",
        "release": "44",
        "repos": [],
        "target_cpu": "x86_64",
        "solve": {
            "prebuilt_sources": ["kernel"],
            "source_image_sets": ["live"],
        },
        "image_sets": {
            "live": [
                {"name": "bash", "source": "bash"},
                {"name": "kernel-core", "source": "kernel"},
            ],
        },
        "packages": {
            "bash": {"source": {"name": "bash"}},
        },
    }
    value["source_policy"] = build_source_policy(
        {"live": [
            {"package": "bash", "source": "bash"},
            {"package": "kernel-core", "source": "kernel"},
        ]},
        {"bash"},
        [{
            "kind": "host-kernel-capability",
            "package": "kernel-core",
            "reason": "The build host lacks the required kernel interface.",
            "source": "kernel",
        }],
    )
    return value


class TestSourcePolicyValidation(unittest.TestCase):
    def test_accepts_the_recorded_policy(self):
        generate.validate_lock_source_policy(lock(), "lock.json")

    def test_rejects_a_tampered_policy(self):
        value = copy.deepcopy(lock())
        value["source_policy"]["summary"]["live"]["source"] = 0
        with self.assertRaisesRegex(SystemExit, "invalid source_policy"):
            generate.validate_lock_source_policy(value, "lock.json")

    def test_rejects_a_missing_policy_for_an_image_derived_lock(self):
        value = lock()
        del value["source_policy"]
        with self.assertRaisesRegex(SystemExit, "has no source_policy"):
            generate.validate_lock_source_policy(value, "lock.json")

    def test_render_emits_the_source_policy(self):
        value = lock()
        rendered = generate.render(
            value,
            "lock.json",
            [],
            [],
            {},
            [],
            [],
            [],
        )
        self.assertIn("SOURCE_POLICY = {", rendered)


if __name__ == "__main__":
    unittest.main()
