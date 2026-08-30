#!/usr/bin/env python3

import hashlib
import os
import stat
import tempfile
import unittest
from unittest import mock

from _deb import (
    clear_signed_payload,
    compatible_binary_version,
    dsc_files,
    ensure_base_files,
    parse_control,
    source_identity,
)
from deb_extract import select_deb
from dsc_unpack import archive_source_tree, validate_sources
from deb_generate import bzl_literal, validate_lock
from dpkgbuild_replay import (
    build_environment,
    build_option,
    copy_source,
    select_installroot_debs,
)
from deb_lock import (
    apt_build_dep_command,
    apt_options,
    apt_source_command,
    apt_uri_lines,
    dependency_overlay,
    parse_source_exception,
    source_files_from_metadata,
    source_record,
    source_requests,
)


DSC = """-----BEGIN PGP SIGNED MESSAGE-----
Hash: SHA512

Format: 3.0 (quilt)
Source: hello
Version: 2.10-5
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
    def test_archives_source_nodes_buck_cannot_represent_directly(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            destination = os.path.join(tmp, "destination")
            archive = os.path.join(tmp, "source.tar")
            os.makedirs(os.path.join(source, "debian"))
            with open(os.path.join(source, "debian", "rules"), "w", encoding="utf-8") as stream:
                stream.write("#!/usr/bin/make -f\n")
            with open(os.path.join(source, "regular"), "w", encoding="utf-8") as stream:
                stream.write("payload")
            os.mkfifo(os.path.join(source, "fixture.fifo"))
            with open(os.path.join(source, "literal\\slash"), "w", encoding="utf-8") as stream:
                stream.write("unit")

            archive_source_tree(source, archive, "1700000000")
            copy_source(archive, destination)

            self.assertTrue(os.path.isfile(os.path.join(destination, "regular")))
            self.assertTrue(stat.S_ISFIFO(os.lstat(os.path.join(destination, "fixture.fifo")).st_mode))
            self.assertTrue(os.path.isfile(os.path.join(destination, "literal\\slash")))

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

    def test_rejects_dsc_with_wrong_exact_source_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "hello.orig.tar.gz")
            with open(source, "wb") as stream:
                stream.write(b"hello")
            dsc = os.path.join(tmp, "hello.dsc")
            with open(dsc, "w", encoding="utf-8") as stream:
                stream.write(DSC.format(digest=hashlib.sha256(b"hello").hexdigest()))

            with self.assertRaisesRegex(ValueError, "source name mismatch"):
                validate_sources(dsc, [source], "hello-from", "2.10-5")
            with self.assertRaisesRegex(ValueError, "source version mismatch"):
                validate_sources(dsc, [source], "hello", "2.10-6")


class TestBuildrootSkeleton(unittest.TestCase):
    def test_enables_multiarch_dpkg_info_filenames(self):
        with tempfile.TemporaryDirectory() as root:
            ensure_base_files(root)
            path = os.path.join(root, "var", "lib", "dpkg", "info", "format")
            with open(path, encoding="utf-8") as stream:
                self.assertEqual("1\n", stream.read())

    def test_restores_multilib_links_created_by_libc_maintainer_scripts(self):
        with tempfile.TemporaryDirectory() as root:
            lib32 = os.path.join(root, "usr", "lib32")
            os.makedirs(lib32)
            open(os.path.join(lib32, "ld-linux.so.2"), "wb").close()

            ensure_base_files(root)

            self.assertEqual("usr/lib32", os.readlink(os.path.join(root, "lib32")))
            self.assertEqual(
                "../lib32/ld-linux.so.2",
                os.readlink(os.path.join(root, "usr", "lib", "ld-linux.so.2")),
            )

    def test_restores_accounts_from_base_passwd_master_files(self):
        with tempfile.TemporaryDirectory() as root:
            shared = os.path.join(root, "usr", "share", "base-passwd")
            os.makedirs(shared)
            with open(os.path.join(shared, "passwd.master"), "w", encoding="utf-8") as stream:
                stream.write("root:*:0:0:root:/root:/bin/bash\n")
                stream.write("_apt:*:42:65534::/nonexistent:/usr/sbin/nologin\n")
            with open(os.path.join(shared, "group.master"), "w", encoding="utf-8") as stream:
                stream.write("root:*:0:\n")
                stream.write("shadow:*:42:\n")

            ensure_base_files(root)

            with open(os.path.join(root, "etc", "passwd"), encoding="utf-8") as stream:
                passwd = stream.read()
            with open(os.path.join(root, "etc", "group"), encoding="utf-8") as stream:
                group = stream.read()
            self.assertIn("_apt:*:42:65534:", passwd)
            self.assertIn("shadow:*:42:", group)
            self.assertEqual(1, sum(line.startswith("root:") for line in passwd.splitlines()))
            self.assertEqual(1, sum(line.startswith("root:") for line in group.splitlines()))

    def test_resolves_localhost_without_a_hosts_package(self):
        with tempfile.TemporaryDirectory() as root:
            ensure_base_files(root)

            with open(os.path.join(root, "etc", "hosts"), encoding="utf-8") as stream:
                hosts = stream.read()
            self.assertIn("127.0.0.1 localhost", hosts)
            self.assertIn("::1 localhost", hosts)

            with open(os.path.join(root, "etc", "nsswitch.conf"), encoding="utf-8") as stream:
                nsswitch = stream.read()
            self.assertEqual("hosts: files\n", nsswitch)
            self.assertNotIn("dns", nsswitch)

    def test_keeps_a_packaged_hosts_database(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "etc"))
            with open(os.path.join(root, "etc", "hosts"), "w", encoding="utf-8") as stream:
                stream.write("127.0.0.1 packaged\n")
            with open(os.path.join(root, "etc", "nsswitch.conf"), "w", encoding="utf-8") as stream:
                stream.write("hosts: files myhostname\n")

            ensure_base_files(root)

            with open(os.path.join(root, "etc", "hosts"), encoding="utf-8") as stream:
                self.assertEqual("127.0.0.1 packaged\n", stream.read())
            with open(os.path.join(root, "etc", "nsswitch.conf"), encoding="utf-8") as stream:
                self.assertEqual("hosts: files myhostname\n", stream.read())

    def test_selects_the_packaged_dpkg_vendor(self):
        with tempfile.TemporaryDirectory() as root:
            origins = os.path.join(root, "etc", "dpkg", "origins")
            os.makedirs(origins)
            open(os.path.join(origins, "debian"), "wb").close()

            ensure_base_files(root)

            self.assertEqual("debian", os.readlink(os.path.join(origins, "default")))

    def test_links_the_mingw_compiler_alternative(self):
        with tempfile.TemporaryDirectory() as root:
            bindir = os.path.join(root, "usr", "bin")
            os.makedirs(bindir)
            open(os.path.join(bindir, "i686-w64-mingw32-gcc-win32"), "wb").close()
            open(os.path.join(bindir, "x86_64-w64-mingw32-gcc-win32"), "wb").close()
            # Present and not an alternative: binutils ships the plain name.
            open(os.path.join(bindir, "i686-w64-mingw32-ar"), "wb").close()

            ensure_base_files(root)

            self.assertEqual(
                "i686-w64-mingw32-gcc-win32",
                os.readlink(os.path.join(bindir, "i686-w64-mingw32-gcc")),
            )
            self.assertEqual(
                "x86_64-w64-mingw32-gcc-win32",
                os.readlink(os.path.join(bindir, "x86_64-w64-mingw32-gcc")),
            )
            self.assertFalse(os.path.islink(os.path.join(bindir, "i686-w64-mingw32-ar")))

    def test_links_the_blas_and_lapack_alternatives(self):
        with tempfile.TemporaryDirectory() as root:
            triplet = os.path.join(root, "usr", "lib", "x86_64-linux-gnu")
            os.makedirs(os.path.join(triplet, "blas"))
            os.makedirs(os.path.join(triplet, "lapack"))
            open(os.path.join(triplet, "blas", "libblas.so.3"), "wb").close()
            open(os.path.join(triplet, "lapack", "liblapack.so.3"), "wb").close()

            ensure_base_files(root)

            self.assertEqual(
                "blas/libblas.so.3",
                os.readlink(os.path.join(triplet, "libblas.so.3")),
            )
            self.assertEqual(
                "lapack/liblapack.so.3",
                os.readlink(os.path.join(triplet, "liblapack.so.3")),
            )

    def test_leaves_the_loader_name_alone_without_an_implementation(self):
        with tempfile.TemporaryDirectory() as root:
            triplet = os.path.join(root, "usr", "lib", "x86_64-linux-gnu")
            # The directory exists and holds a different implementation, so
            # the scan had somewhere real to look and declined rather than
            # finding nothing.
            os.makedirs(os.path.join(triplet, "blas"))
            open(os.path.join(triplet, "blas", "libblas.so.3.12.1"), "wb").close()

            ensure_base_files(root)

            self.assertTrue(
                os.path.isfile(os.path.join(triplet, "blas", "libblas.so.3.12.1"))
            )
            self.assertFalse(os.path.lexists(os.path.join(triplet, "libblas.so.3")))

    def test_links_the_versioned_imagemagick_convert(self):
        with tempfile.TemporaryDirectory() as root:
            bindir = os.path.join(root, "usr", "bin")
            os.makedirs(bindir)
            open(os.path.join(bindir, "convert-im7.q16"), "wb").close()

            ensure_base_files(root)

            self.assertEqual(
                "convert-im7.q16",
                os.readlink(os.path.join(bindir, "convert")),
            )

    def test_refuses_to_choose_between_two_convert_candidates(self):
        with tempfile.TemporaryDirectory() as root:
            bindir = os.path.join(root, "usr", "bin")
            os.makedirs(bindir)
            open(os.path.join(bindir, "convert-im6.q16"), "wb").close()
            open(os.path.join(bindir, "convert-im7.q16"), "wb").close()

            ensure_base_files(root)

            # A real negative: the glob matched two candidates, so the scan
            # had something to find and declined rather than found nothing.
            self.assertTrue(os.path.exists(os.path.join(bindir, "convert-im6.q16")))
            self.assertTrue(os.path.exists(os.path.join(bindir, "convert-im7.q16")))
            self.assertFalse(os.path.lexists(os.path.join(bindir, "convert")))

    def test_restores_package_alternative_links_after_overlay(self):
        with tempfile.TemporaryDirectory() as root:
            bindir = os.path.join(root, "usr", "bin")
            os.makedirs(bindir)
            open(os.path.join(bindir, "mawk"), "wb").close()
            open(os.path.join(bindir, "gcc"), "wb").close()
            open(os.path.join(bindir, "bison.yacc"), "wb").close()
            open(os.path.join(bindir, "which.debianutils"), "wb").close()
            open(os.path.join(bindir, "lua5.1"), "wb").close()
            open(os.path.join(bindir, "luac5.1"), "wb").close()
            open(os.path.join(bindir, "openjade-1.4devel"), "wb").close()
            open(os.path.join(bindir, "osgmlnorm"), "wb").close()
            ensure_base_files(root)
            self.assertEqual("mawk", os.readlink(os.path.join(bindir, "awk")))
            self.assertEqual("mawk", os.readlink(os.path.join(bindir, "nawk")))
            self.assertEqual("gcc", os.readlink(os.path.join(bindir, "cc")))
            self.assertEqual("bison.yacc", os.readlink(os.path.join(bindir, "yacc")))
            self.assertEqual(
                "which.debianutils",
                os.readlink(os.path.join(bindir, "which")),
            )
            self.assertEqual("lua5.1", os.readlink(os.path.join(bindir, "lua")))
            self.assertEqual("luac5.1", os.readlink(os.path.join(bindir, "luac")))
            self.assertEqual(
                "openjade-1.4devel",
                os.readlink(os.path.join(bindir, "openjade")),
            )
            self.assertEqual("osgmlnorm", os.readlink(os.path.join(bindir, "sgmlnorm")))

    def test_registers_payload_xml_catalogs(self):
        with tempfile.TemporaryDirectory() as root:
            for name, filename in (("a", "catalog.xml"), ("b", "catalog-docbook5.xml")):
                directory = os.path.join(root, "usr", "share", "xml", name)
                os.makedirs(directory)
                open(os.path.join(directory, filename), "wb").close()

            ensure_base_files(root)

            catalog = os.path.join(root, "etc", "xml", "catalog")
            with open(catalog, encoding="utf-8") as stream:
                contents = stream.read()
            self.assertIn('catalog="/usr/share/xml/a/catalog.xml"', contents)
            self.assertIn('catalog="/usr/share/xml/b/catalog-docbook5.xml"', contents)

    def test_registers_payload_sgml_catalogs(self):
        with tempfile.TemporaryDirectory() as root:
            catalog_dir = os.path.join(root, "etc", "sgml")
            os.makedirs(catalog_dir)
            open(os.path.join(catalog_dir, "docbook.cat"), "wb").close()
            open(os.path.join(catalog_dir, "openjade.cat"), "wb").close()

            ensure_base_files(root)

            catalog = os.path.join(catalog_dir, "catalog")
            self.assertEqual(
                "/var/lib/sgml-base/supercatalog",
                os.readlink(catalog),
            )
            with open(
                os.path.join(root, "var", "lib", "sgml-base", "supercatalog"),
                encoding="utf-8",
            ) as stream:
                contents = stream.read()
            self.assertIn("CATALOG /etc/sgml/docbook.cat\n", contents)
            self.assertIn("CATALOG /etc/sgml/openjade.cat\n", contents)

    def test_restores_default_java_alternatives(self):
        with tempfile.TemporaryDirectory() as root:
            java_bindir = os.path.join(root, "usr", "lib", "jvm", "default-java", "bin")
            os.makedirs(java_bindir)
            java = os.path.join(java_bindir, "java")
            open(java, "wb").close()
            os.chmod(java, 0o755)

            ensure_base_files(root)

            self.assertEqual(
                "../lib/jvm/default-java/bin/java",
                os.readlink(os.path.join(root, "usr", "bin", "java")),
            )

    def test_assembles_payload_ca_certificate_bundle(self):
        with tempfile.TemporaryDirectory() as root:
            certificates = os.path.join(root, "usr", "share", "ca-certificates", "test")
            os.makedirs(certificates)
            with open(os.path.join(certificates, "a.crt"), "wb") as stream:
                stream.write(b"certificate-a")
            with open(os.path.join(certificates, "b.crt"), "wb") as stream:
                stream.write(b"certificate-b\n")

            ensure_base_files(root)

            bundle = os.path.join(root, "etc", "ssl", "certs", "ca-certificates.crt")
            with open(bundle, "rb") as stream:
                self.assertEqual(b"certificate-a\ncertificate-b\n", stream.read())

    def test_maps_requested_binary_kind_to_dpkg_buildpackage_option(self):
        self.assertEqual("-b", build_option("binary"))
        self.assertEqual("-B", build_option("arch"))
        self.assertEqual("-A", build_option("indep"))

    def test_prepares_root_compatible_build_environment(self):
        env = build_environment("1700000000", ["parallel=1", "parallel=1"])
        self.assertEqual("1", env["FAKEROOTDONTTRYCHOWN"])
        self.assertEqual("1", env["FORCE_UNSAFE_CONFIGURE"])
        self.assertEqual("parallel=1", env["DEB_BUILD_OPTIONS"])

    def test_pins_the_gnulib_getcwd_probe_that_times_out_under_load(self):
        # gnulib's getcwd probe allows itself five seconds and is killed by
        # SIGALRM on a busy machine, so configure records "no" for a test
        # that never reached a verdict and compiles in a replacement.  The
        # result is that build-farm load decides what `find` contains.
        # Pinning the cache variable restores the answer the probe gives
        # when it is allowed to finish, which is also what the archive
        # ships.
        env = build_environment("1700000000")
        self.assertEqual("yes", env["gl_cv_func_getcwd_path_max"])

    def test_aggregate_installroot_contains_only_declared_packages(self):
        paths = ["/tmp/one.deb", "/tmp/unrelated.deb"]
        fields = {
            "/tmp/one.deb": {"Package": "one"},
            "/tmp/unrelated.deb": {"Package": "unrelated"},
        }
        with mock.patch(
            "dpkgbuild_replay.deb_fields",
            side_effect=lambda path: fields[path],
        ):
            self.assertEqual(
                ["/tmp/one.deb"],
                select_installroot_debs(paths, ["one"]),
            )

    def test_aggregate_installroot_rejects_missing_declared_package(self):
        with mock.patch(
            "dpkgbuild_replay.deb_fields",
            return_value={"Package": "other"},
        ):
            with self.assertRaisesRegex(ValueError, "did not produce"):
                select_installroot_debs(["/tmp/other.deb"], ["one"])


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

    def test_uses_explicit_source_selectors_for_apt_operations(self):
        self.assertEqual(
            [
                "apt-get", "source", "--print-uris", "--download-only",
                "src:coreutils=9.7-3ubuntu2",
            ],
            apt_source_command("coreutils", "9.7-3ubuntu2"),
        )
        self.assertEqual(
            [
                "apt-get", "source", "--print-uris", "--download-only",
                "src:coreutils",
            ],
            apt_source_command("coreutils"),
        )
        self.assertEqual(
            [
                "apt-get", "-Pnocheck", "build-dep",
                "src:coreutils=9.7-3ubuntu2",
            ],
            apt_build_dep_command("coreutils", "9.7-3ubuntu2"),
        )

    def test_source_record_ignores_same_named_binary_source_collision(self):
        digest = "a" * 64
        commands = []

        def fake_apt_output(command):
            commands.append(command)
            if command[:2] == ["apt-get", "source"]:
                if command[-1] != "src:coreutils=9.7-3ubuntu2":
                    return (
                        "'http://archive.example/coreutils-from_1.dsc' "
                        "coreutils-from_1.dsc 1 SHA256:{}\n".format(digest)
                    )
                return (
                    "'http://archive.example/coreutils_9.7-3ubuntu2.dsc' "
                    "coreutils_9.7-3ubuntu2.dsc 1 SHA512:{}\n".format("b" * 128)
                )
            return """Package: coreutils-from
Version: 9.7-3ubuntu2
Binary: coreutils
Checksums-Sha256:
 {digest} 1 coreutils_9.7-3ubuntu2.dsc

Package: coreutils
Version: 9.7-3ubuntu2
Binary: coreutils
Checksums-Sha256:
 {digest} 1 coreutils_9.7-3ubuntu2.dsc
""".format(digest=digest)

        with mock.patch("deb_lock.apt_output", side_effect=fake_apt_output):
            record = source_record("coreutils", "9.7-3ubuntu2")

        self.assertEqual("coreutils", record["name"])
        self.assertEqual("9.7-3ubuntu2", record["version_full"])
        self.assertEqual(digest, record["files"][0]["sha256"])
        self.assertEqual("src:coreutils=9.7-3ubuntu2", commands[0][-1])

    def test_source_uri_uses_metadata_sha256_when_apt_reports_sha512(self):
        sha256 = "a" * 64
        entries = apt_uri_lines(
            "'http://archive.example/pkg_1.0%2borig.tar.xz' "
            "pkg_1.0%2Borig.tar.xz 42 SHA512:{}\n".format("b" * 128)
        )
        files = source_files_from_metadata(entries, {
            "Checksums-Sha256": "{} 42 pkg_1.0+orig.tar.xz".format(sha256),
        })
        self.assertEqual("pkg_1.0+orig.tar.xz", files[0]["filename"])
        self.assertEqual(sha256, files[0]["sha256"])

    def test_source_uri_without_digest_uses_metadata_sha256(self):
        sha256 = "a" * 64
        entries = apt_uri_lines(
            "'http://archive.example/pkg_1.dsc' pkg_1.dsc 42\n"
        )
        files = source_files_from_metadata(entries, {
            "Checksums-Sha256": "{} 42 pkg_1.dsc".format(sha256),
        })
        self.assertEqual(sha256, files[0]["sha256"])

    def test_source_uri_rejects_missing_metadata_sha256(self):
        entries = apt_uri_lines(
            "'http://archive.example/pkg_1.dsc' pkg_1.dsc 42 SHA512:{}\n".format(
                "b" * 128
            )
        )
        with self.assertRaisesRegex(ValueError, "no Checksums-Sha256"):
            source_files_from_metadata(entries, {})

    def test_source_uri_rejects_size_mismatch(self):
        entries = apt_uri_lines(
            "'http://archive.example/pkg_1.dsc' pkg_1.dsc 41 SHA512:{}\n".format(
                "b" * 128
            )
        )
        with self.assertRaisesRegex(ValueError, "source size mismatch"):
            source_files_from_metadata(entries, {
                "Checksums-Sha256": "{} 42 pkg_1.dsc".format("a" * 64),
            })

    def test_source_uri_rejects_conflicting_sha256(self):
        entries = apt_uri_lines(
            "'http://archive.example/pkg_1.dsc' pkg_1.dsc 42 SHA256:{}\n".format(
                "b" * 64
            )
        )
        with self.assertRaisesRegex(ValueError, "source SHA-256 mismatch"):
            source_files_from_metadata(entries, {
                "Checksums-Sha256": "{} 42 pkg_1.dsc".format("a" * 64),
            })

    def test_source_uri_rejects_filename_missing_from_metadata(self):
        entries = apt_uri_lines(
            "'http://archive.example/other_1.dsc' other_1.dsc 42 SHA512:{}\n".format(
                "b" * 128
            )
        )
        with self.assertRaisesRegex(ValueError, "missing from source"):
            source_files_from_metadata(entries, {
                "Checksums-Sha256": "{} 42 pkg_1.dsc".format("a" * 64),
            })

    def test_source_uri_rejects_metadata_file_without_uri(self):
        entries = apt_uri_lines(
            "'http://archive.example/pkg_1.dsc' pkg_1.dsc 42 SHA512:{}\n".format(
                "b" * 128
            )
        )
        with self.assertRaisesRegex(ValueError, "no URI.*pkg_1.orig.tar.xz"):
            source_files_from_metadata(entries, {
                "Checksums-Sha256": (
                    "{} 42 pkg_1.dsc\n{} 99 pkg_1.orig.tar.xz".format(
                        "a" * 64, "c" * 64,
                    )
                ),
            })

    def test_source_metadata_rejects_duplicate_and_invalid_sha256(self):
        entries = apt_uri_lines(
            "'http://archive.example/pkg_1.dsc' pkg_1.dsc 42 SHA512:{}\n".format(
                "b" * 128
            )
        )
        with self.assertRaisesRegex(ValueError, "duplicate source filename"):
            source_files_from_metadata(entries, {
                "Checksums-Sha256": (
                    "{} 42 pkg_1.dsc\n{} 42 pkg_1.dsc".format(
                        "a" * 64, "c" * 64,
                    )
                ),
            })
        with self.assertRaisesRegex(ValueError, "invalid SHA-256"):
            source_files_from_metadata(entries, {
                "Checksums-Sha256": "not-a-digest 42 pkg_1.dsc",
            })

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
            with mock.patch("deb_extract.deb_fields", side_effect=lambda path: entries[os.path.basename(path)]):
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
            with mock.patch("deb_extract.deb_fields", side_effect=lambda path: entries[os.path.basename(path)]):
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
            with mock.patch("deb_extract.deb_fields", side_effect=lambda path: entries[os.path.basename(path)]):
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
            with mock.patch("deb_extract.deb_fields", side_effect=lambda path: entries[os.path.basename(path)]):
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
