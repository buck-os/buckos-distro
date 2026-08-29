#!/usr/bin/env python3

import hashlib
import os
import tempfile
import unittest
from unittest import mock

from _deb import (
    clear_signed_payload,
    compatible_binary_version,
    dsc_files,
    parse_control,
    source_identity,
)
from deb_buildroot_assemble import ensure_base_files
from deb_extract import select_deb
from dsc_unpack import validate_sources
from deb_generate import bzl_literal, validate_lock
from deb_lock import (
    apt_options,
    apt_uri_lines,
    dependency_overlay,
    parse_source_exception,
    source_requests,
)


DSC = """-----BEGIN PGP SIGNED MESSAGE-----
Hash: SHA512

Format: 3.0 (quilt)
Source: hello
Checksums-Sha256:
 {digest} 5 hello.orig.tar.gz
-----BEGIN PGP SIGNATURE-----
ignored
-----END PGP SIGNATURE-----
"""


class TestControlParsing(unittest.TestCase):
    def test_clearsigned_payload_is_unwrapped(self):
        payload = clear_signed_payload(DSC.format(digest="0" * 64))
        self.assertTrue(payload.startswith("Format: 3.0 (quilt)\n"))
        self.assertNotIn("PGP SIGNATURE", payload)

    def test_multiline_checksums_ignore_the_initial_empty_value(self):
        fields = parse_control(DSC.format(digest="0" * 64))
        self.assertEqual(
            {"hello.orig.tar.gz": ("0" * 64, 5)},
            dsc_files(fields),
        )

    def test_preserves_explicit_source_version_for_bin_nmu(self):
        self.assertEqual(
            ("bash", "5.2.37-2"),
            source_identity({
                "Package": "bash",
                "Source": "bash (5.2.37-2)",
                "Version": "5.2.37-2+b9",
            }),
        )

    def test_infers_source_version_without_source_field(self):
        self.assertEqual(
            ("hello", "2.10-5"),
            source_identity({"Package": "hello", "Version": "2.10-5"}),
        )

    def test_binary_nmu_version_is_compatible_with_its_source(self):
        self.assertTrue(compatible_binary_version("5.2.37-2+b9", "5.2.37-2"))
        self.assertFalse(compatible_binary_version("5.2.37-3", "5.2.37-2"))


class TestSourceValidation(unittest.TestCase):
    def test_rejects_a_digest_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "hello.orig.tar.gz")
            with open(source, "wb") as stream:
                stream.write(b"hello")
            dsc = os.path.join(tmp, "hello.dsc")
            with open(dsc, "w", encoding="utf-8") as stream:
                stream.write(DSC.format(digest="0" * 64))

            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                validate_sources(dsc, [source])

    def test_accepts_the_manifested_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            content = b"hello"
            source = os.path.join(tmp, "hello.orig.tar.gz")
            with open(source, "wb") as stream:
                stream.write(content)
            dsc = os.path.join(tmp, "hello.dsc")
            with open(dsc, "w", encoding="utf-8") as stream:
                stream.write(DSC.format(digest=hashlib.sha256(content).hexdigest()))

            self.assertEqual(
                {"hello.orig.tar.gz": source},
                validate_sources(dsc, [source]),
            )


class TestBuildrootSkeleton(unittest.TestCase):
    def test_enables_multiarch_dpkg_info_filenames(self):
        with tempfile.TemporaryDirectory() as root:
            ensure_base_files(root)
            path = os.path.join(root, "var", "lib", "dpkg", "info", "format")
            with open(path, encoding="utf-8") as stream:
                self.assertEqual("1\n", stream.read())


class TestAptMetadata(unittest.TestCase):
    def test_uses_private_status_and_archive_cache(self):
        options = apt_options("/tmp/status", "/tmp/archives")
        self.assertIn("Dir::State::status=/tmp/status", options)
        self.assertIn("Dir::Cache::archives=/tmp/archives", options)

    def test_parses_sha256_download_uri(self):
        digest = "a" * 64
        self.assertEqual(
            [{
                "digest": digest,
                "digest_kind": "sha256",
                "filename": "hello_2.10+dfsg_amd64.deb",
                "size": 42,
                "url": "http://archive.example/hello_2.10%2bdfsg_amd64.deb",
            }],
            apt_uri_lines(
                "'http://archive.example/hello_2.10%2bdfsg_amd64.deb' "
                "hello_2.10%2Bdfsg_amd64.deb 42 SHA256:{}\n".format(digest)
            ),
        )

    def test_groups_live_binaries_by_exact_source_identity(self):
        live = [
            {
                "architecture": "amd64",
                "package": "libexample1",
                "source": "example@1.0-1",
                "source_name": "example",
                "source_version": "1.0-1",
                "target": "libexample1",
                "version": "1.0-1+b2",
            },
            {
                "architecture": "amd64",
                "package": "example-bin",
                "source": "example@1.0-1",
                "source_name": "example",
                "source_version": "1.0-1",
                "target": "example-bin",
                "version": "1.0-1+b2",
            },
        ]
        requests, selected = source_requests({"live": live}, ["live"], [])
        self.assertEqual({("example", "1.0-1")}, set(requests))
        self.assertEqual(2, len(requests[("example", "1.0-1")]))
        self.assertEqual(("example", "1.0-1"), selected["libexample1"])

    def test_source_exception_is_explicit_and_must_be_used(self):
        live = [{
            "package": "firmware",
            "source": "firmware@1",
            "source_name": "firmware",
            "source_version": "1",
        }]
        requests, selected = source_requests(
            {"live": live},
            ["live"],
            [{"package": "firmware", "reason": "signed artifact"}],
        )
        self.assertEqual({}, requests)
        self.assertIn("firmware", selected)
        with self.assertRaisesRegex(ValueError, "do not match"):
            source_requests(
                {"live": live},
                ["live"],
                [{"package": "other", "reason": "signed artifact"}],
            )

    def test_dependency_overlay_excludes_only_common_base_targets(self):
        base = {"deb-make": {"target": "deb-make"}}
        overlay = dependency_overlay(base, {
            "deb-make": {"target": "deb-make"},
            "deb-texinfo": {"target": "deb-texinfo"},
        })
        self.assertEqual([{"target": "deb-texinfo"}], overlay)

    def test_parses_machine_readable_source_exception(self):
        self.assertEqual(
            {
                "kind": "firmware",
                "package": "firmware-linux",
                "reason": "Firmware payload.",
                "source": "firmware-nonfree@1",
            },
            parse_source_exception(
                '{"package":"firmware-linux","source":"firmware-nonfree@1",'
                '"kind":"firmware","reason":"Firmware payload."}'
            ),
        )


class TestDebProjection(unittest.TestCase):
    def _fields(self, entries):
        def field(path, name):
            return entries[os.path.basename(path)].get(name, "")
        return field

    def test_selects_exact_binary_architecture_and_source_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "libexample1_1.0-1+b2_amd64.deb")
            open(path, "wb").close()
            entries = {os.path.basename(path): {
                "Architecture": "amd64",
                "Package": "libexample1",
                "Source": "example (1.0-1)",
                "Version": "1.0-1+b2",
            }}
            with mock.patch("deb_extract.deb_field", side_effect=self._fields(entries)):
                self.assertEqual(
                    path,
                    select_deb(tmp, "libexample1", "amd64", "example", "1.0-1"),
                )

    def test_rejects_wrong_architecture(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "example_1_arm64.deb")
            open(path, "wb").close()
            entries = {os.path.basename(path): {
                "Architecture": "arm64",
                "Package": "example",
                "Source": "example",
                "Version": "1",
            }}
            with mock.patch("deb_extract.deb_field", side_effect=self._fields(entries)):
                with self.assertRaisesRegex(ValueError, "wrong architecture"):
                    select_deb(tmp, "example", "amd64", "example", "1")

    def test_rejects_incompatible_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "example_2_amd64.deb")
            open(path, "wb").close()
            entries = {os.path.basename(path): {
                "Architecture": "amd64",
                "Package": "example",
                "Source": "example",
                "Version": "2",
            }}
            with mock.patch("deb_extract.deb_field", side_effect=self._fields(entries)):
                with self.assertRaisesRegex(ValueError, "incompatible version"):
                    select_deb(tmp, "example", "amd64", "example", "1")

    def test_rejects_ambiguous_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            entries = {}
            for filename in ("example_1_amd64.deb", "example_1_amd64.ddeb"):
                path = os.path.join(tmp, filename)
                open(path, "wb").close()
                entries[filename] = {
                    "Architecture": "amd64",
                    "Package": "example",
                    "Source": "example",
                    "Version": "1",
                }
            with mock.patch("deb_extract.deb_field", side_effect=self._fields(entries)):
                with self.assertRaisesRegex(ValueError, "ambiguous deb"):
                    select_deb(tmp, "example", "amd64", "example", "1")

    def test_rejects_missing_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "no deb"):
                select_deb(tmp, "missing", "amd64", "missing", "1")

    def test_accepts_debian_binary_uri_without_digest(self):
        self.assertEqual(
            [{
                "digest": "",
                "digest_kind": "",
                "filename": "libsmartcols1_2.41.5-0+deb13u1_amd64.deb",
                "size": 143216,
                "url": "http://security.example/libsmartcols1.deb",
            }],
            apt_uri_lines(
                "'http://security.example/libsmartcols1.deb' "
                "libsmartcols1_2.41.5-0+deb13u1_amd64.deb 143216\n"
            ),
        )


class TestStarlarkGeneration(unittest.TestCase):
    def test_renders_python_only_literals_as_starlark(self):
        self.assertEqual(
            '{\n    "enabled": True,\n    "missing": None,\n}',
            bzl_literal({"missing": None, "enabled": True}),
        )

    def test_rejects_tampered_source_policy(self):
        lock = {
            "architecture": "amd64",
            "distro": "debian",
            "image_sets": {
                "live": [{"package": "hello", "source": "hello@2.10-5"}],
            },
            "schema": 3,
            "source_policy": {
                "exceptions": [],
                "image_sets": ["live"],
                "schema": 1,
                "summary": {"live": {"pinned": 0, "source": 0, "total": 1}},
            },
            "sources": [{"name": "hello", "version_full": "2.10-5"}],
            "target_cpu": "x86_64",
        }
        with self.assertRaisesRegex(ValueError, "does not match"):
            validate_lock(lock)


if __name__ == "__main__":
    unittest.main()
