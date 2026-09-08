#!/usr/bin/env python3

import os
import tempfile
import unittest

from _lockfile import (
    configured_compression,
    dump_lockfile,
    find_lockfile,
    load_lockfile,
    lockfile_name,
    lockfile_releases,
    strip_lockfile_suffix,
)
from lockfile_convert import convert


def write(path, text):
    with open(path, "w", encoding="utf-8") as stream:
        stream.write(text)


class TestLockfileCodec(unittest.TestCase):
    def test_plain_and_gzip_round_trip_the_same_data(self):
        value = {"packages": ["zlib", "bash"] * 100, "schema": 2}
        with tempfile.TemporaryDirectory() as directory:
            plain = os.path.join(directory, "test.lock.json")
            compressed = plain + ".gz"
            dump_lockfile(value, plain)
            dump_lockfile(value, compressed)
            self.assertEqual(value, load_lockfile(plain))
            self.assertEqual(value, load_lockfile(compressed))
            self.assertLess(os.path.getsize(compressed), os.path.getsize(plain))

    def test_gzip_output_is_byte_for_byte_deterministic(self):
        value = {"payload": ["repeat-me"] * 100}
        with tempfile.TemporaryDirectory() as directory:
            first = os.path.join(directory, "first.lock.json.gz")
            second = os.path.join(directory, "second.lock.json.gz")
            dump_lockfile(value, first)
            dump_lockfile(value, second)
            with open(first, "rb") as stream:
                first_bytes = stream.read()
            with open(second, "rb") as stream:
                second_bytes = stream.read()
            self.assertEqual(first_bytes, second_bytes)
            self.assertEqual(b"\x00\x00\x00\x00", first_bytes[4:8])

    def test_supported_suffixes_strip_without_leaving_gz(self):
        self.assertEqual("fedora-45-x86_64", strip_lockfile_suffix(
            "fedora-45-x86_64.lock.json.gz"
        ))
        self.assertEqual("fedora-45-x86_64", strip_lockfile_suffix(
            "fedora-45-x86_64.lock.json"
        ))


class TestLockfileConfiguration(unittest.TestCase):
    def test_local_buck_config_overrides_the_checked_in_default(self):
        with tempfile.TemporaryDirectory() as root:
            write(os.path.join(root, ".buckroot"), "")
            write(
                os.path.join(root, ".buckconfig"),
                "[buckos.lockfiles]\ncompression = gzip\n",
            )
            write(
                os.path.join(root, ".buckconfig.local"),
                "[buckos.lockfiles]\ncompression = none\n",
            )
            self.assertEqual("none", configured_compression(root))
            self.assertEqual(
                "fedora-45-x86_64.lock.json",
                lockfile_name("fedora", "45", "x86_64", root=root),
            )

    def test_invalid_compression_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            write(os.path.join(root, ".buckroot"), "")
            write(
                os.path.join(root, ".buckconfig"),
                "[buckos.lockfiles]\ncompression = zstd\n",
            )
            with self.assertRaisesRegex(ValueError, "must be one of"):
                configured_compression(root)

    def test_converter_moves_between_both_supported_formats(self):
        with tempfile.TemporaryDirectory() as directory:
            plain = os.path.join(directory, "fedora-45-x86_64.lock.json")
            value = {"schema": 2}
            dump_lockfile(value, plain)
            compressed = convert(plain, "gzip")
            self.assertFalse(os.path.exists(plain))
            self.assertEqual(value, load_lockfile(compressed))
            restored = convert(compressed, "none")
            self.assertFalse(os.path.exists(compressed))
            self.assertEqual(value, load_lockfile(restored))

    def test_converter_rejects_unknown_compression(self):
        with self.assertRaisesRegex(ValueError, "unsupported"):
            convert("unused.lock.json", "zstd")


class TestLockfileDiscovery(unittest.TestCase):
    def test_discovers_plain_and_compressed_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            write(os.path.join(directory, "centos-9-x86_64.lock.json"), "{}")
            write(os.path.join(directory, "centos-10-x86_64.lock.json.gz"), "")
            write(os.path.join(directory, "fedora-45-x86_64.lock.json.gz"), "")
            self.assertEqual(
                ["10", "9"],
                lockfile_releases(directory, "centos", "x86_64"),
            )
            self.assertTrue(find_lockfile(
                directory, "centos", "10", "x86_64"
            ).endswith(".gz"))

    def test_duplicate_encodings_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            plain = os.path.join(directory, "fedora-45-x86_64.lock.json")
            write(plain, "{}")
            write(plain + ".gz", "")
            with self.assertRaisesRegex(ValueError, "both plain and compressed"):
                find_lockfile(directory, "fedora", "45", "x86_64")
            with self.assertRaisesRegex(ValueError, "has both"):
                lockfile_releases(directory, "fedora", "x86_64")


if __name__ == "__main__":
    unittest.main()
