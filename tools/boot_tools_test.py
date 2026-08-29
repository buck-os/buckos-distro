#!/usr/bin/env python3

import os
import tarfile
import tempfile
import unittest
from unittest import mock

from _deb import fakeroot_command
from deb_rootfs_install import (
    bootstrap_script,
    cleanup_script,
    normalize_merged_usr_script,
    transaction_script,
)
from iso_build import _bios_script, _write_md5sums, _xorriso_script
from iso_boot_test import parse_marker, qemu_command, validate
from initramfs_build import _install_image_tool_script, stage_image_tool
from rootfs_overlay import append_file, parse_file
from squashfs_build import _build_mksquashfs_script, _mksquashfs_script, write_pseudo


ISO_MATRIX = (
    ("fedora", "44", "rpm", True),
    ("fedora", "45", "rpm", True),
    ("centos", "9", "rpm", True),
    ("centos", "10", "rpm", True),
    ("centos-hyperscale", "9", "rpm", True),
    ("centos-hyperscale", "10", "rpm", True),
    ("debian", "13", "debian", False),
    ("ubuntu", "26.04", "ubuntu", False),
)
ARCHITECTURES = ("x86_64", "aarch64")
IMAGE_VARIANTS = (None, "prebuilt")


def repo_root():
    for start in (os.getcwd(), os.path.dirname(os.path.abspath(__file__))):
        path = start
        while True:
            if os.path.isfile(os.path.join(path, ".buckroot")):
                return path
            parent = os.path.dirname(path)
            if parent == path:
                break
            path = parent
    raise AssertionError("cannot locate repository root")


def load_iso_test_graph():
    rules = {
        "iso_boot_test": [],
        "iso_image": [],
        "rootfs_overlay": [],
        "squashfs": [],
    }

    def recorder(kind):
        def record(**attributes):
            rules[kind].append(attributes)

        return record

    namespace = {
        "execution_compatible_with": lambda architecture: [architecture],
        "iso_boot_test": recorder("iso_boot_test"),
        "iso_image": recorder("iso_image"),
        "load": lambda *_args, **_kwargs: None,
        "rootfs_overlay": recorder("rootfs_overlay"),
        "squashfs": recorder("squashfs"),
        "target_platform": lambda flavor, release, architecture: (
            flavor,
            release,
            architecture,
        ),
    }
    path = os.path.join(repo_root(), "defs", "iso_tests.bzl")
    with open(path, encoding="utf-8") as stream:
        source = stream.read()
    exec(compile(source, path, "exec"), namespace)
    namespace["all_live_iso_boot_tests"]()
    return rules


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
    def test_fakeroot_command_preserves_state_across_phases(self):
        runtime = {
            "fakeroot-sysv": "/work/fakeroot/fakeroot-sysv",
            "faked-sysv": "/work/fakeroot/faked-sysv",
            "library": "/work/fakeroot/libfakeroot-sysv.so",
            "state": "/work/fakeroot/state",
        }
        initial = fakeroot_command(runtime, ["dpkg-deb", "--extract"])
        resumed = fakeroot_command(runtime, ["tar", "--create"], load=True)
        self.assertNotIn("-i", initial)
        self.assertIn("-s", initial)
        self.assertIn("-i", resumed)
        self.assertEqual(resumed[-2:], ["tar", "--create"])

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


class TestInitramfsTools(unittest.TestCase):
    def test_stages_binary_seeded_construction_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            buildroot = os.path.join(tmp, "buildroot")
            work = os.path.join(tmp, "work")
            source = os.path.join(buildroot, "usr", "bin", "cp")
            os.makedirs(os.path.dirname(source))
            os.makedirs(work)
            with open(source, "wb") as stream:
                stream.write(b"binary-seeded-cp")
            os.chmod(source, 0o755)

            destination = stage_image_tool(buildroot, work, "cp")

            with open(destination, "rb") as stream:
                self.assertEqual(b"binary-seeded-cp", stream.read())
            self.assertTrue(os.access(destination, os.X_OK))

    def test_installs_tool_only_in_ephemeral_root(self):
        script = _install_image_tool_script(
            "/work/image-tools-bin/cp", "/work/root", "cp"
        )
        self.assertEqual(
            "/work/image-tools-bin/cp --preserve=mode,timestamps "
            "/work/image-tools-bin/cp /work/root/usr/bin/cp",
            script,
        )


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

    def test_reports_each_mismatched_marker_field(self):
        args = type("Args", (), {
            "expected_flavor": "fedora",
            "expected_version": "45",
            "architecture": "aarch64",
            "expect_selinux": True,
        })()
        valid = {
            "flavor": "fedora",
            "version": "45",
            "arch": "aarch64",
            "pid1": "systemd",
            "failed": "0",
            "selinux": "Enforcing",
            "avc": "0",
        }
        cases = (
            ("flavor", "ubuntu"),
            ("version", "44"),
            ("pid1", "busybox"),
            ("failed", "1"),
            ("selinux", "Permissive"),
            ("avc", "1"),
        )

        for field, wrong_value in cases:
            with self.subTest(field=field):
                fields = dict(valid)
                fields[field] = wrong_value
                self.assertEqual(
                    ["{}: expected {!r}, got {!r}".format(
                        field,
                        valid[field],
                        wrong_value,
                    )],
                    validate(args, fields),
                )


class TestIsoBootMatrix(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules = load_iso_test_graph()

    def test_complete_matrix_shape_and_wiring(self):
        rootfs_rules = {
            rule["name"]: rule for rule in self.rules["rootfs_overlay"]
        }
        squashfs_rules = {
            rule["name"]: rule for rule in self.rules["squashfs"]
        }
        iso_rules = {rule["name"]: rule for rule in self.rules["iso_image"]}
        boot_rules = {
            rule["name"]: rule for rule in self.rules["iso_boot_test"]
        }

        self.assertEqual(32, len(rootfs_rules))
        self.assertEqual(32, len(squashfs_rules))
        self.assertEqual(32, len(iso_rules))
        self.assertEqual(48, len(boot_rules))
        self.assertEqual(len(rootfs_rules), len(self.rules["rootfs_overlay"]))
        self.assertEqual(len(squashfs_rules), len(self.rules["squashfs"]))
        self.assertEqual(len(iso_rules), len(self.rules["iso_image"]))
        self.assertEqual(len(boot_rules), len(self.rules["iso_boot_test"]))

        production_isos = set()
        expected_boot_names = set()
        for flavor, release, layout, expect_selinux in ISO_MATRIX:
            for architecture in ARCHITECTURES:
                for variant in IMAGE_VARIANTS:
                    variant_suffix = "-" + variant if variant else ""
                    image_suffix = "{}-{}-{}".format(
                        variant_suffix,
                        release,
                        architecture,
                    )
                    test_suffix = "{}{}-{}-{}".format(
                        flavor,
                        variant_suffix,
                        release.replace(".", "_"),
                        architecture,
                    )
                    rootfs_name = "rootfs-verify-" + test_suffix
                    squashfs_name = "squashfs-verify-" + test_suffix
                    iso_name = "iso-verify-" + test_suffix
                    production_rootfs = "//flavors/{}:rootfs-live{}".format(
                        flavor,
                        image_suffix,
                    )

                    self.assertEqual(
                        production_rootfs,
                        rootfs_rules[rootfs_name]["rootfs"],
                    )
                    self.assertEqual(
                        ":" + rootfs_name,
                        squashfs_rules[squashfs_name]["rootfs"],
                    )
                    self.assertEqual(
                        "//flavors/{}:kernel-live{}".format(flavor, image_suffix),
                        iso_rules[iso_name]["kernel"],
                    )
                    self.assertEqual(
                        "//flavors/{}:initramfs-live{}".format(flavor, image_suffix),
                        iso_rules[iso_name]["initramfs"],
                    )
                    self.assertEqual(
                        ":" + squashfs_name,
                        iso_rules[iso_name]["squashfs"],
                    )
                    self.assertEqual(layout, iso_rules[iso_name]["layout"])
                    self.assertEqual(architecture, iso_rules[iso_name]["target_cpu"])
                    self.assertEqual(
                        "hybrid" if architecture == "x86_64" else "uefi",
                        iso_rules[iso_name]["boot_mode"],
                    )

                    production_isos.add(
                        production_rootfs.replace(":rootfs-live", ":iso-live")
                    )
                    firmwares = (
                        ("bios", "uefi")
                        if architecture == "x86_64"
                        else ("uefi",)
                    )
                    for firmware in firmwares:
                        boot_name = "boot-{}-{}".format(test_suffix, firmware)
                        expected_boot_names.add(boot_name)
                        boot = boot_rules[boot_name]
                        self.assertEqual(":" + iso_name, boot["iso"])
                        self.assertEqual(architecture, boot["architecture"])
                        self.assertEqual(firmware, boot["firmware"])
                        self.assertEqual(flavor, boot["expected_flavor"])
                        self.assertEqual(release, boot["expected_version"])
                        self.assertEqual(expect_selinux, boot["expect_selinux"])

        expected_production_isos = {
            "//flavors/{}:iso-live{}-{}-{}".format(
                flavor,
                "-" + variant if variant else "",
                release,
                architecture,
            )
            for flavor, release, _layout, _expect_selinux in ISO_MATRIX
            for architecture in ARCHITECTURES
            for variant in IMAGE_VARIANTS
        }
        self.assertEqual(32, len(expected_production_isos))
        self.assertEqual(expected_production_isos, production_isos)
        self.assertEqual(expected_boot_names, set(boot_rules))

    def test_debian_prebuilt_boot_set_is_complete(self):
        self.assertEqual(
            {
                "boot-debian-prebuilt-13-x86_64-bios",
                "boot-debian-prebuilt-13-x86_64-uefi",
                "boot-debian-prebuilt-13-aarch64-uefi",
            },
            {
                rule["name"]
                for rule in self.rules["iso_boot_test"]
                if rule["name"].startswith("boot-debian-prebuilt-")
            },
        )

    def test_ubuntu_prebuilt_boot_set_is_complete(self):
        self.assertEqual(
            {
                "boot-ubuntu-prebuilt-26_04-x86_64-bios",
                "boot-ubuntu-prebuilt-26_04-x86_64-uefi",
                "boot-ubuntu-prebuilt-26_04-aarch64-uefi",
            },
            {
                rule["name"]
                for rule in self.rules["iso_boot_test"]
                if rule["name"].startswith("boot-ubuntu-prebuilt-")
            },
        )


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
