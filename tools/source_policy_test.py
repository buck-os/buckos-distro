#!/usr/bin/env python3

import unittest

from source_policy import (
    SourcePolicyError,
    build_source_policy,
    validate_source_policy,
)


class TestSourcePolicy(unittest.TestCase):
    def test_counts_source_and_explicit_pinned_payloads(self):
        policy = build_source_policy(
            {
                "live": [
                    {"package": "bash", "source": "bash@5.3"},
                    {"package": "linux-firmware", "source": "linux-firmware@1"},
                ],
                "image-tools": [
                    {"package": "xorriso", "source": "libisoburn@1.5"},
                ],
            },
            {"bash@5.3"},
            [{
                "kind": "firmware",
                "package": "linux-firmware",
                "reason": "Firmware payload is distributed as signed binary data.",
                "source": "linux-firmware@1",
            }],
        )

        self.assertEqual(["live"], policy["image_sets"])
        self.assertEqual(
            {"pinned": 1, "source": 1, "total": 2},
            policy["summary"]["live"],
        )

    def test_rejects_an_uncovered_payload(self):
        with self.assertRaisesRegex(
            SourcePolicyError, "no source producer or approved exception: kernel"
        ):
            build_source_policy(
                {"live": [{"package": "kernel", "source": "linux@1"}]},
                set(),
                [],
            )

    def test_rejects_an_unapproved_exception_kind(self):
        with self.assertRaisesRegex(SourcePolicyError, "unsupported kind"):
            build_source_policy(
                {"live": [{"package": "broken", "source": "broken@1"}]},
                set(),
                [{
                    "kind": "ordinary-build-failure",
                    "package": "broken",
                    "reason": "It does not compile.",
                }],
            )

    def test_rejects_a_stale_exception_for_a_source_built_payload(self):
        with self.assertRaisesRegex(SourcePolicyError, "exception.*is stale"):
            build_source_policy(
                {"live": [{"package": "bash", "source": "bash@5.3"}]},
                {"bash@5.3"},
                [{
                    "kind": "signed-artifact",
                    "package": "bash",
                    "reason": "Stale test exception.",
                }],
            )

    def test_rejects_an_exception_for_an_unselected_payload(self):
        with self.assertRaisesRegex(
            SourcePolicyError, "do not cover a selected pinned payload"
        ):
            build_source_policy(
                {"live": [{"package": "bash", "source": "bash@5.3"}]},
                {"bash@5.3"},
                [{
                    "kind": "firmware",
                    "package": "unused",
                    "reason": "Not present in the selected image.",
                }],
            )

    def test_validation_recomputes_counts(self):
        policy = build_source_policy(
            {"live": [{"package": "bash", "source": "bash@5.3"}]},
            {"bash@5.3"},
            [],
        )
        policy["summary"]["live"]["source"] = 0

        with self.assertRaisesRegex(SourcePolicyError, "does not match"):
            validate_source_policy(
                policy,
                {"live": [{"package": "bash", "source": "bash@5.3"}]},
                {"bash@5.3"},
            )


if __name__ == "__main__":
    unittest.main()
