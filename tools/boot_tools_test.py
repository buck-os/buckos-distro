#!/usr/bin/env python3

import os
import tarfile
import tempfile
import unittest
from unittest import mock

from deb_rootfs_install import (
    bootstrap_script,
    cleanup_script,
    normalize_merged_usr_script,
    transaction_script,
)
from iso_build import _bios_script, _write_md5sums, _xorriso_script
from iso_boot_test import parse_marker, qemu_command, validate
from rootfs_overlay import append_file, parse_file
from squashfs_build import _build_mksquashfs_script, _mksquashfs_script, write_pseudo


class TestRootfsOverlay(unittest.TestCase):
    def test_appends_deterministic_root_owned_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            archive_path = os.path.join(tmp, "rootfs.tar")
            with open(source, "wb") as stream:
                stream.write(b"payload\n")
            with tarfile.open(archive_path, "w"):
                pass
            with tarfile.open(archive_path, "a") as archive:
                append_file(archive, "usr/bin/check", 0o755, source, 123)
            with tarfile.open(archive_path) as archive:
                member = archive.getmember("usr/bin/check")
                self.assertEqual(0o755, member.mode)
                self.assertEqual(0, member.uid)
                self.assertEqual(0, member.gid)
                self.assertEqual(123, member.mtime)
                self.assertEqual(b"payload\n", archive.extractfile(member).read())

    def test_rejects_parent_traversal(self):
        with self.assertRaisesRegex(Exception, "inside the rootfs"):
            parse_file("../escape:0644:file")


class TestDebRootfsTransaction(unittest.TestCase):
    def test_bootstrap_extracts_before_running_maintainer_scripts(self):
        script = bootstrap_script("/target", "/debs")
        self.assertLess(script.index("dpkg-deb --extract"), script.index("/usr/bin/dpkg"))

    def test_transaction_uses_target_tmp_and_configures_everything(self):
        script = transaction_script("/debs")
        self.assertIn("export TMPDIR=/tmp", script)
        self.assertIn("dpkg --force-confnew --configure -a", script)
        self.assertLess(
            script.index("update-initramfs.buckos-real"),
            script.index("dpkg --force-confnew --configure -a"),
        )
        self.assertLess(
            script.index("dpkg --force-confnew --configure -a"),
            script.rindex("update-initramfs.buckos-real"),
        )

    def test_normalizes_merged_usr_before_target_execution(self):
        script = bootstrap_script("/target", "/debs")
        self.assertIn('cp -a "$path/." "$usr/"', script)
        self.assertIn('ln -s "usr/$directory" "$path"', script)
        self.assertLess(script.index("dpkg-deb --extract"), script.index("cp -a"))
        self.assertLess(script.index("cp -a"), script.index("test -x /target/bin/sh"))

    def test_normalization_is_scoped_to_the_requested_root(self):
        script = normalize_merged_usr_script("/target root")
        self.assertIn("root='/target root'", script)
        self.assertIn('path="${root%/}/$directory"', script)

    def test_cleanup_runs_inside_owning_namespace(self):
        self.assertEqual("set -e\nrm -rf /target", cleanup_script("/target"))


class TestSquashfsPseudoFile(unittest.TestCase):
    def test_skips_archive_root_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            contexts = os.path.join(tmp, "contexts")
            pseudo = os.path.join(tmp, "pseudo")
            with open(contexts, "w", encoding="utf-8") as stream:
                stream.write("/.\tsystem_u:object_r:root_t:s0\n")
                stream.write("/usr\tsystem_u:object_r:usr_t:s0\n")
                stream.write("/afs\tsystem_u:object_r:afs_t:s0\n")

            written, skipped = write_pseudo(contexts, pseudo)

            self.assertEqual((written, skipped), (2, 0))
            with open(pseudo, encoding="utf-8") as stream:
                self.assertEqual(
                    stream.read(),
                    "usr x security.selinux=system_u:object_r:usr_t:s0\n"
                    "afs x security.selinux=system_u:object_r:afs_t:s0\n",
                )

    def test_builds_xattr_capable_tool_without_overriding_makefile_flags(self):
        script = _build_mksquashfs_script(
            "/work/source.tar", "/work", "/work/mksquashfs"
        )
        self.assertIn("CONFIG=1", script)
        self.assertIn("ZSTD_SUPPORT=1", script)
        self.assertIn("XATTR_SUPPORT=1", script)
        self.assertIn("EXTRA_CFLAGS=", script)
        self.assertNotIn(" CFLAGS=", script)

    def test_uses_explicit_mksquashfs_when_provided(self):
        args = type("Args", (), {
            "block_size": "",
            "compressor": "zstd",
            "exclude": [],
            "processors": "",
        })()
        script = _mksquashfs_script(
            args, "/root", "/image", "/pseudo", "/work/mksquashfs"
        )
        self.assertIn("MKSQUASHFS=/work/mksquashfs", script)
        self.assertNotIn("for _c in", script)


class TestIsoBootMarker(unittest.TestCase):
    def test_parses_and_validates_marker(self):
        fields = parse_marker(
            "BUCKOS_VERIFY flavor=fedora version=45 arch=aarch64 "
            "pid1=systemd failed=0 selinux=Enforcing avc=0\n"
        )
        args = type("Args", (), {
            "expected_flavor": "fedora",
            "expected_version": "45",
            "architecture": "aarch64",
            "expect_selinux": True,
        })()
        self.assertEqual([], validate(args, fields))

    def test_reports_mismatched_architecture(self):
        args = type("Args", (), {
            "expected_flavor": "debian",
            "expected_version": "13",
            "architecture": "aarch64",
            "expect_selinux": False,
        })()
        errors = validate(args, {
            "flavor": "debian",
            "version": "13",
            "arch": "x86_64",
            "pid1": "systemd",
            "failed": "0",
            "avc": "0",
        })
        self.assertRegex("; ".join(errors), "arch")


class TestUbuntuIsoManifest(unittest.TestCase):
    def test_writes_sorted_checksums_without_hashing_itself(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "casper"))
            with open(os.path.join(tmp, "z-file"), "wb") as stream:
                stream.write(b"z")
            with open(os.path.join(tmp, "casper", "a-file"), "wb") as stream:
                stream.write(b"a")
            _write_md5sums(tmp)
            with open(os.path.join(tmp, "md5sum.txt"), encoding="utf-8") as stream:
                rows = stream.readlines()
        self.assertEqual(
            rows,
            [
                "0cc175b9c0f1b6a831c399e269772661  ./casper/a-file\n",
                "fbade9e36a3f36d3d676c1b808451dd7  ./z-file\n",
            ],
        )


class TestIsoBootCommand(unittest.TestCase):
    def x86_args(self, firmware):
        return type("Args", (), {
            "qemu": "/bin/true",
            "architecture": "x86_64",
            "firmware": firmware,
            "firmware_path": "",
            "firmware_vars": "",
        })()

    def arm_args(self, firmware):
        return type("Args", (), {
            "qemu": "/bin/true",
            "architecture": "aarch64",
            "firmware": "uefi",
            "firmware_path": firmware,
        })()

    def test_native_arm_uses_kvm_host_cpu(self):
        with tempfile.TemporaryDirectory() as tmp:
            firmware = os.path.join(tmp, "QEMU_EFI.fd")
            open(firmware, "wb").close()
            with mock.patch("iso_boot_test.platform.machine", return_value="aarch64"), \
                    mock.patch("iso_boot_test.os.access", return_value=True):
                command = qemu_command(self.arm_args(firmware), "/image.iso", tmp)
        self.assertIn("kvm", command)
        self.assertIn("-nographic", command)
        self.assertEqual("host", command[command.index("-cpu") + 1])
        self.assertEqual("/image.iso", command[command.index("-cdrom") + 1])

    def test_native_x86_uses_host_cpu(self):
        with mock.patch("iso_boot_test.platform.machine", return_value="x86_64"), \
                mock.patch("iso_boot_test.os.access", return_value=True):
            command = qemu_command(self.x86_args("bios"), "/image.iso", "/tmp")
        self.assertIn("kvm", command)
        self.assertEqual("host", command[command.index("-cpu") + 1])

    def test_cross_x86_uses_max_cpu(self):
        with mock.patch("iso_boot_test.platform.machine", return_value="aarch64"), \
                mock.patch("iso_boot_test.os.access", return_value=True):
            command = qemu_command(self.x86_args("bios"), "/image.iso", "/tmp")
        self.assertIn("tcg,thread=multi", command)
        self.assertEqual("max", command[command.index("-cpu") + 1])

    def test_cross_arm_uses_tcg_firmware_compatible_cpu(self):
        with tempfile.TemporaryDirectory() as tmp:
            firmware = os.path.join(tmp, "QEMU_EFI.fd")
            open(firmware, "wb").close()
            with mock.patch("iso_boot_test.platform.machine", return_value="x86_64"), \
                    mock.patch("iso_boot_test.os.access", return_value=True):
                command = qemu_command(self.arm_args(firmware), "/image.iso", tmp)
        self.assertIn("tcg,thread=multi", command)
        self.assertEqual("cortex-a57", command[command.index("-cpu") + 1])


class TestIsoBuildBootloaderPaths(unittest.TestCase):
    def test_bios_loader_accepts_rpm_and_debian_paths(self):
        script = _bios_script("/iso")
        self.assertIn("/usr/share/syslinux/isolinux.bin", script)
        self.assertIn("/usr/lib/ISOLINUX/isolinux.bin", script)
        self.assertIn("/usr/lib/syslinux/modules/bios/ldlinux.c32", script)

    def test_isohybrid_mbr_accepts_rpm_and_debian_paths(self):
        args = type("Args", (), {
            "boot_mode": "hybrid",
            "volume_label": "TEST",
        })()
        script = _xorriso_script(args, "/iso", "/out.iso", "2020010100000000")
        self.assertIn("/usr/share/syslinux/isohdpfx.bin", script)
        self.assertIn("/usr/lib/ISOLINUX/isohdpfx.bin", script)


if __name__ == "__main__":
    unittest.main()
