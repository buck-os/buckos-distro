#!/usr/bin/env python3

import hashlib
import os
import tempfile
import unittest

from _deb import clear_signed_payload, dsc_files, parse_control
from deb_buildroot_assemble import ensure_base_files
from dsc_unpack import validate_sources
from deb_generate import bzl_literal
from deb_lock import apt_options, apt_uri_lines


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


if __name__ == "__main__":
    unittest.main()
