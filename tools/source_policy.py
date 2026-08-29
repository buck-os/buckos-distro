#!/usr/bin/env python3
"""Build and validate source coverage policy for image payloads."""

from collections.abc import Iterable, Mapping, Sequence, Set
from typing import Any


SOURCE_POLICY_SCHEMA = 1
SOURCE_EXCEPTION_KINDS = frozenset({
    "fedora-45-tar-incompatibility",
    "firmware",
    "host-kernel-capability",
    "signed-artifact",
})


class SourcePolicyError(ValueError):
    """The requested source policy does not cover its selected payloads."""


def _require_text(record: Mapping[str, Any], field: str, subject: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value.strip():
        raise SourcePolicyError("{} has no non-empty {}".format(subject, field))
    return value


def _exception_index(
    exceptions: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, str]]:
    indexed = {}
    for raw in exceptions:
        package = _require_text(raw, "package", "source exception")
        if package in indexed:
            raise SourcePolicyError(
                "duplicate source exception for {}".format(package)
            )
        kind = _require_text(raw, "kind", "source exception {}".format(package))
        if kind not in SOURCE_EXCEPTION_KINDS:
            raise SourcePolicyError(
                "source exception {} has unsupported kind {!r}; expected one of {}".format(
                    package,
                    kind,
                    ", ".join(sorted(SOURCE_EXCEPTION_KINDS)),
                )
            )
        reason = _require_text(
            raw, "reason", "source exception {}".format(package)
        )
        entry = {"kind": kind, "package": package, "reason": reason}
        source = raw.get("source")
        if source is not None:
            if not isinstance(source, str) or not source.strip():
                raise SourcePolicyError(
                    "source exception {} has an invalid source".format(package)
                )
            entry["source"] = source
        indexed[package] = entry
    return indexed


def build_source_policy(
    image_sets: Mapping[str, Iterable[Mapping[str, Any]]],
    producers: Set[str],
    exceptions: Sequence[Mapping[str, Any]],
    selected_image_sets: Sequence[str] = ("live",),
) -> dict[str, Any]:
    """Return a deterministic policy after proving selected payload coverage.

    Each image entry supplies a binary ``package`` and its canonical ``source``
    producer key. Callers choose the key format, so Debian-family locks can use
    an exact ``name@version`` identity while RPM-family locks can use the source
    identity their solver records.
    """
    selected = sorted(set(selected_image_sets))
    if not selected:
        raise SourcePolicyError("source policy selects no image sets")

    exception_by_package = _exception_index(exceptions)
    used_exceptions = set()
    summary = {}

    for image_name in selected:
        if image_name not in image_sets:
            raise SourcePolicyError(
                "source policy selects missing image set {!r}".format(image_name)
            )

        seen = set()
        source_count = 0
        pinned_count = 0
        missing = []
        for payload in image_sets[image_name]:
            package = _require_text(
                payload, "package", "{} image payload".format(image_name)
            )
            if package in seen:
                raise SourcePolicyError(
                    "{} image set contains duplicate package {}".format(
                        image_name, package
                    )
                )
            seen.add(package)

            source = payload.get("source")
            if isinstance(source, str) and source in producers:
                if package in exception_by_package:
                    raise SourcePolicyError(
                        "source exception for {} is stale: producer {} is selected".format(
                            package, source
                        )
                    )
                source_count += 1
                continue

            exception = exception_by_package.get(package)
            if exception is None:
                missing.append(package)
                continue
            if exception.get("source") is not None and exception["source"] != source:
                raise SourcePolicyError(
                    "source exception for {} names {}, payload names {}".format(
                        package, exception["source"], source
                    )
                )
            used_exceptions.add(package)
            pinned_count += 1

        if missing:
            raise SourcePolicyError(
                "{} image payloads have no source producer or approved exception: {}".format(
                    image_name, ", ".join(sorted(missing))
                )
            )
        summary[image_name] = {
            "pinned": pinned_count,
            "source": source_count,
            "total": source_count + pinned_count,
        }

    stale = sorted(set(exception_by_package) - used_exceptions)
    if stale:
        raise SourcePolicyError(
            "source exceptions do not cover a selected pinned payload: {}".format(
                ", ".join(stale)
            )
        )

    return {
        "exceptions": [exception_by_package[name] for name in sorted(used_exceptions)],
        "image_sets": selected,
        "schema": SOURCE_POLICY_SCHEMA,
        "summary": summary,
    }


def validate_source_policy(
    recorded: Mapping[str, Any],
    image_sets: Mapping[str, Iterable[Mapping[str, Any]]],
    producers: Set[str],
) -> None:
    """Fail when recorded policy differs from current lock contents."""
    if recorded.get("schema") != SOURCE_POLICY_SCHEMA:
        raise SourcePolicyError(
            "unsupported source policy schema: {}".format(recorded.get("schema"))
        )
    expected = build_source_policy(
        image_sets,
        producers,
        recorded.get("exceptions", ()),
        recorded.get("image_sets", ()),
    )
    if recorded != expected:
        raise SourcePolicyError(
            "recorded source policy does not match computed payload coverage"
        )
