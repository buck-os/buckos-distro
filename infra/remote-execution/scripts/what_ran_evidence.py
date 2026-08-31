#!/usr/bin/python3
"""Validate one target's structured Buck2 ``what-ran`` evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Optional


ACTION_DIGEST = re.compile(r"^[0-9a-f]{64}:[0-9]+$")


class EvidenceError(ValueError):
    """Raised when what-ran evidence is absent, malformed, or unexpected."""


def _records(lines: Iterable[str]) -> list[dict[str, Any]]:
    records = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise EvidenceError(
                f"line {line_number} is not valid JSON: {error.msg}"
            ) from error
        if not isinstance(value, dict):
            raise EvidenceError(f"line {line_number} is not a JSON object")
        records.append(value)
    return records


def validate_evidence(
    lines: Iterable[str],
    identity: str,
    executor: str,
    require_action_digest: bool = False,
) -> Optional[str]:
    """Require all matching records to use the exact requested executor."""
    matches = []
    for record in _records(lines):
        actual_identity = record.get("identity")
        if isinstance(actual_identity, str) and identity in actual_identity:
            matches.append(record)
    if not matches:
        raise EvidenceError(f"no record matches identity {identity!r}")

    digests = []
    for record in matches:
        reproducer = record.get("reproducer")
        if not isinstance(reproducer, dict):
            raise EvidenceError("matching record has no reproducer object")
        actual_executor = reproducer.get("executor")
        if actual_executor != executor:
            raise EvidenceError(
                f"expected executor {executor!r}, got {actual_executor!r}"
            )
        if not require_action_digest:
            continue
        details = reproducer.get("details")
        if not isinstance(details, dict):
            raise EvidenceError("matching record has no reproducer details")
        digest = details.get("digest")
        if not isinstance(digest, str) or not ACTION_DIGEST.fullmatch(digest):
            raise EvidenceError("matching record has no valid action digest")
        digests.append(digest)

    if not require_action_digest:
        return None
    if not digests:
        raise EvidenceError("matching evidence has no action digest")
    return digests[0]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--executor", required=True)
    parser.add_argument("--require-action-digest", action="store_true")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        with arguments.input.open(encoding="utf-8") as stream:
            digest = validate_evidence(
                stream,
                arguments.identity,
                arguments.executor,
                arguments.require_action_digest,
            )
    except (EvidenceError, OSError) as error:
        print(f"what-ran evidence rejected: {error}", file=sys.stderr)
        return 1
    if digest is not None:
        print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
