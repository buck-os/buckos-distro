#!/usr/bin/env python3

import base64
import io
import os
import tarfile
import tempfile
import unittest

from signing_helper import sign_pe, write_ima_manifest


def add_file(archive, name, payload, mode):
    member = tarfile.TarInfo(name)
    member.size = len(payload)
    member.mode = mode
    archive.addfile(member, io.BytesIO(payload))


class TestImaManifest(unittest.TestCase):
    def fake_evmctl(self, directory):
        path = os.path.join(directory, "evmctl")
        with open(path, "w", encoding="utf-8") as stream:
            stream.write("#!/bin/sh\n")
            stream.write("for last do :; done\n")
            # Model evmctl --sigfile: a useful sidecar can accompany the
            # non-zero status from its unprivileged xattr attempt.
            stream.write("printf '\\001\\002\\003' > \"$last.sig\"\n")
            stream.write("exit 1\n")
        os.chmod(path, 0o755)
        return path

    def test_executable_mode_signs_programs_and_elf_shared_objects(self):
        with tempfile.TemporaryDirectory() as temporary:
            rootfs = os.path.join(temporary, "rootfs.tar")
            output = os.path.join(temporary, "ima.pseudo")
            with tarfile.open(rootfs, "w") as archive:
                add_file(archive, "etc/config", b"plain data\n", 0o644)
                add_file(archive, "usr/bin/tool", b"#!/bin/sh\n", 0o755)
                add_file(archive, "usr/lib/libdemo.so", b"\x7fELFpayload", 0o644)

            count = write_ima_manifest(
                rootfs,
                output,
                "unused-private-key",
                "unused-certificate",
                self.fake_evmctl(temporary),
                "executables",
            )

            encoded = base64.b64encode(b"\x01\x02\x03").decode("ascii")
            self.assertEqual(2, count)
            with open(output, encoding="ascii") as stream:
                self.assertEqual(
                    stream.read(),
                    "usr/bin/tool x security.ima=0s{}\n"
                    "usr/lib/libdemo.so x security.ima=0s{}\n".format(
                        encoded, encoded
                    ),
                )

    def test_all_mode_signs_non_executable_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            rootfs = os.path.join(temporary, "rootfs.tar")
            output = os.path.join(temporary, "ima.pseudo")
            with tarfile.open(rootfs, "w") as archive:
                add_file(archive, "etc/config", b"plain data\n", 0o644)

            count = write_ima_manifest(
                rootfs,
                output,
                "unused-private-key",
                "unused-certificate",
                self.fake_evmctl(temporary),
                "all",
            )

            self.assertEqual(1, count)
            with open(output, encoding="ascii") as stream:
                self.assertIn("etc/config x security.ima=0s", stream.read())

    def test_fails_closed_for_an_unaddressable_signed_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            rootfs = os.path.join(temporary, "rootfs.tar")
            output = os.path.join(temporary, "ima.pseudo")
            with tarfile.open(rootfs, "w") as archive:
                add_file(archive, "usr/bin/not addressable", b"#!/bin/sh\n", 0o755)

            with self.assertRaisesRegex(ValueError, "cannot safely address"):
                write_ima_manifest(
                    rootfs,
                    output,
                    "unused-private-key",
                    "unused-certificate",
                    self.fake_evmctl(temporary),
                    "executables",
                )


class TestPeSigning(unittest.TestCase):
    def test_signs_and_verifies_a_pe_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = os.path.join(temporary, "input.efi")
            output = os.path.join(temporary, "output.efi")
            signer = os.path.join(temporary, "osslsigncode")
            with open(source, "wb") as stream:
                stream.write(b"MZunsigned")
            with open(signer, "w", encoding="utf-8") as stream:
                stream.write(
                    "#!/usr/bin/env python3\n"
                    "import shutil, sys\n"
                    "if sys.argv[1] == 'sign':\n"
                    "    source = sys.argv[sys.argv.index('-in') + 1]\n"
                    "    output = sys.argv[sys.argv.index('-out') + 1]\n"
                    "    shutil.copyfile(source, output)\n"
                    "    with open(output, 'ab') as result:\n"
                    "        result.write(b'-signed')\n"
                    "elif sys.argv[1] != 'verify':\n"
                    "    raise SystemExit(2)\n"
                )
            os.chmod(signer, 0o755)

            sign_pe(source, output, "private.key", "certificate.crt", signer)

            with open(output, "rb") as stream:
                self.assertEqual(b"MZunsigned-signed", stream.read())

    def test_rejects_non_pe_input_before_invoking_the_signer(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = os.path.join(temporary, "input")
            with open(source, "wb") as stream:
                stream.write(b"not a PE image")
            with self.assertRaisesRegex(RuntimeError, "not a PE/COFF image"):
                sign_pe(
                    source,
                    os.path.join(temporary, "output.efi"),
                    "private.key",
                    "certificate.crt",
                    os.path.join(temporary, "osslsigncode"),
                )


if __name__ == "__main__":
    unittest.main()
