#!/usr/bin/python3
"""Tests for strict structured Buck2 ``what-ran`` validation."""

from __future__ import annotations

import io
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from what_ran_evidence import EvidenceError, validate_evidence


IDENTITY = "//infra/remote-execution:worker-architecture-x86_64"
DIGEST = "a" * 64 + ":42"
PINNED_REMOTE_RECORD = (
    '{"reason":"build","identity":"buckos'
    + IDENTITY
    + ' (buckos//platforms:linux-x86_64#fixture) (genrule)",'
    + '"reproducer":{"executor":"Re","details":{"digest":"'
    + DIGEST
    + '","platform_properties":{"platform.OSFamily":"linux",'
    + '"platform.arch":"x86_64"}}},"duration":"0.7s"}\n'
)


def record(executor: str, digest: str = DIGEST) -> str:
    return (
        '{"identity":"buckos'
        + IDENTITY
        + ' (genrule)","reproducer":{"executor":"'
        + executor
        + '","details":{"digest":"'
        + digest
        + '"}}}\n'
    )


class WhatRanEvidenceTest(unittest.TestCase):
    def test_accepts_exact_pinned_remote_executor(self) -> None:
        self.assertEqual(
            DIGEST,
            validate_evidence(io.StringIO(PINNED_REMOTE_RECORD), IDENTITY, "Re", True),
        )

    def test_rejects_old_uppercase_fixture(self) -> None:
        with self.assertRaisesRegex(EvidenceError, "got 'RE'"):
            validate_evidence(io.StringIO(record("RE")), IDENTITY, "Re", True)

    def test_rejects_local_execution(self) -> None:
        with self.assertRaisesRegex(EvidenceError, "got 'Local'"):
            validate_evidence(io.StringIO(record("Local")), IDENTITY, "Re", True)

    def test_rejects_case_insensitive_match(self) -> None:
        with self.assertRaisesRegex(EvidenceError, "got 're'"):
            validate_evidence(io.StringIO(record("re")), IDENTITY, "Re", True)

    def test_rejects_any_conflicting_matching_record(self) -> None:
        with self.assertRaisesRegex(EvidenceError, "got 'Local'"):
            validate_evidence(
                io.StringIO(record("Re") + record("Local")),
                IDENTITY,
                "Re",
                True,
            )

    def test_rejects_invalid_digest(self) -> None:
        with self.assertRaisesRegex(EvidenceError, "valid action digest"):
            validate_evidence(
                io.StringIO(record("Re", "not-a-digest")),
                IDENTITY,
                "Re",
                True,
            )

    def test_rejects_invalid_json(self) -> None:
        with self.assertRaisesRegex(EvidenceError, "not valid JSON"):
            validate_evidence(io.StringIO("{\n"), IDENTITY, "Re", True)

    def test_rejects_missing_identity(self) -> None:
        with self.assertRaisesRegex(EvidenceError, "no record matches"):
            validate_evidence(io.StringIO(record("Re")), "//missing:target", "Re")


if __name__ == "__main__":
    unittest.main()
