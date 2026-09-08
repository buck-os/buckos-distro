#!/usr/bin/env python3

import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import deb_generate
from _lockfile import dump_lockfile, load_lockfile
from lockfile import (
    discover,
    identity_errors,
    in_directory,
    json_pointer,
    replace_record,
    select,
    select_one,
    validate,
)


def add_lock(root, flavor, release, architecture, compression="gzip"):
    directory = Path(root) / "flavors" / flavor / "lock"
    directory.mkdir(parents=True, exist_ok=True)
    suffix = ".lock.json.gz" if compression == "gzip" else ".lock.json"
    path = directory / "{}-{}-{}{}".format(
        flavor, release, architecture, suffix,
    )
    identity_key = "distro" if flavor in ("debian", "ubuntu") else "flavor"
    dump_lockfile({
        identity_key: flavor,
        "packages": {},
        "release": release,
        "schema": 2,
        "target_cpu": architecture,
    }, str(path))
    return path


class TestDiscoveryAndSelectors(unittest.TestCase):
    def test_selects_by_flavor_release_identity_and_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            add_lock(root, "fedora", "44", "x86_64")
            second = add_lock(root, "fedora", "45", "aarch64", "none")
            add_lock(root, "debian", "13", "x86_64")
            records = discover(root)

            self.assertEqual(3, len(records))
            self.assertEqual(2, len(select(records, ["fedora"])))
            self.assertEqual(
                "fedora:45:aarch64",
                select_one(records, "fedora:45:aarch64").selector,
            )
            self.assertEqual(
                "fedora:45:aarch64",
                select_one(records, str(second.relative_to(root))).selector,
            )
            self.assertEqual(
                "fedora:45:aarch64",
                select_one(records, str(second)).selector,
            )

    def test_duplicate_encodings_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            add_lock(root, "fedora", "45", "x86_64", "gzip")
            add_lock(root, "fedora", "45", "x86_64", "none")
            with self.assertRaisesRegex(ValueError, "exists in both"):
                discover(root)

    def test_broad_selector_is_rejected_when_one_is_required(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            add_lock(root, "fedora", "44", "x86_64")
            add_lock(root, "fedora", "45", "x86_64")
            with self.assertRaisesRegex(ValueError, "matched 2"):
                select_one(discover(root), "fedora")


class TestValidation(unittest.TestCase):
    def test_validates_identity_and_canonical_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            add_lock(root, "fedora", "45", "x86_64")
            record = discover(root)[0]
            self.assertEqual([], validate(record, check_generated=False))

            lock = json.loads(gzip.decompress(record.path.read_bytes()))
            lock["release"] = "44"
            dump_lockfile(lock, str(record.path))
            self.assertRegex(identity_errors(record, lock)[0], "fedora:44")

    def test_rejects_a_nondeterministic_gzip_header(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = add_lock(root, "fedora", "45", "x86_64")
            value = json.loads(gzip.decompress(path.read_bytes()))
            with open(path, "wb") as raw:
                with gzip.GzipFile(
                        filename="recorded-name",
                        mode="wb",
                        fileobj=raw,
                        mtime=123) as stream:
                    stream.write((json.dumps(value) + "\n").encode())
            errors = validate(discover(root)[0], check_generated=False)
            self.assertTrue(any("timestamp" in error for error in errors))
            self.assertTrue(any("source filename" in error for error in errors))
            self.assertTrue(any("not canonical" in error for error in errors))

    def test_rejects_a_lockfile_at_the_repository_size_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = add_lock(root, "fedora", "45", "x86_64")
            record = discover(root)[0]
            with mock.patch(
                    "lockfile.configured_max_tracked_file_size",
                    return_value=path.stat().st_size):
                errors = validate(record, check_generated=False)
            self.assertTrue(any("repository limit" in error for error in errors))

    def test_rejects_a_stale_generated_source_shard(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".buckroot").touch()
            lock_dir = root / "flavors" / "ubuntu" / "lock"
            generated_dir = root / "flavors" / "ubuntu" / "generated"
            lock_dir.mkdir(parents=True)
            generated_dir.mkdir(parents=True)
            path = lock_dir / "ubuntu-1-x86_64.lock.json.gz"
            dump_lockfile({
                "architecture": "amd64",
                "base_debs": [],
                "codename": "test",
                "distro": "ubuntu",
                "image_sets": {},
                "release": "1",
                "schema": 2,
                "source_policy": {},
                "sources": [{"name": "hello"}],
                "target_cpu": "x86_64",
            }, str(path))
            output = generated_dir / "ubuntu-1-x86_64.bzl"
            with in_directory(root):
                deb_generate.main([
                    path.relative_to(root).as_posix(),
                    "--output",
                    str(output),
                ])
            record = discover(root)[0]
            self.assertEqual([], validate(record))

            stale = generated_dir / "ubuntu-1-x86_64-sources-999.bzl"
            stale.write_text("SOURCES = []\n", encoding="utf-8")
            errors = validate(record)
            self.assertTrue(any("stale source shard" in error for error in errors))

    def test_guarded_replacement_preserves_encoding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = add_lock(root, "fedora", "45", "x86_64")
            record = discover(root)[0]
            replacement = load_lockfile(str(path))
            replacement["note"] = "reviewed change"
            replace_record(record, replacement, regenerate=False)
            self.assertTrue(path.is_file())
            self.assertEqual(replacement, load_lockfile(str(path)))

    def test_guarded_replacement_rejects_an_identity_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = add_lock(root, "fedora", "45", "x86_64")
            record = discover(root)[0]
            replacement = load_lockfile(str(path))
            replacement["target_cpu"] = "aarch64"
            with self.assertRaisesRegex(ValueError, "path identifies"):
                replace_record(record, replacement, regenerate=False)


class TestJsonPointer(unittest.TestCase):
    def test_reads_objects_lists_and_escaped_keys(self):
        value = {"solve": {"build": ["bash"]}, "a/b": {"~key": 7}}
        self.assertEqual("bash", json_pointer(value, "/solve/build/0"))
        self.assertEqual(7, json_pointer(value, "/a~1b/~0key"))

    def test_reports_missing_components(self):
        with self.assertRaisesRegex(ValueError, "does not exist"):
            json_pointer({"present": True}, "/missing")


if __name__ == "__main__":
    unittest.main()
