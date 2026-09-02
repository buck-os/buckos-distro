#!/usr/bin/env python3

import io
import os
import tarfile
import tempfile
import unittest
from types import SimpleNamespace

from _kernel import certificate_der, read_kernel_release, write_certificate_pem
from kernel_modules_normalize import normalize_modules
from kernel_rootfs import compose_rootfs
from linux_kernel_build import build_kernel, set_config_values


def _write(path, data, mode="w"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, mode) as stream:
        stream.write(data)


class TestKernelMetadata(unittest.TestCase):
    def test_release_is_one_safe_uname_value(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = os.path.join(temporary, "version")
            _write(path, "6.18.0-buckos\n")
            self.assertEqual("6.18.0-buckos", read_kernel_release(path))
            _write(path, "../../escape\n")
            with self.assertRaisesRegex(ValueError, "invalid kernel release"):
                read_kernel_release(path)

    def test_certificate_normalization_is_encoding_independent(self):
        with tempfile.TemporaryDirectory() as temporary:
            der = os.path.join(temporary, "cert.der")
            pem = os.path.join(temporary, "cert.pem")
            _write(der, b"\x30\x03\x02\x01\x01", "wb")
            write_certificate_pem(der, pem)
            self.assertEqual(certificate_der(der), certificate_der(pem))


class TestKernelModules(unittest.TestCase):
    def test_normalizes_a_version_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = os.path.join(temporary, "source")
            version = os.path.join(temporary, "version")
            output = os.path.join(temporary, "output")
            _write(os.path.join(source, "kernel", "demo.ko"), b"module", "wb")
            _write(os.path.join(source, "modules.dep"), "kernel/demo.ko:\n")
            _write(version, "6.18.0-test")

            normalize_modules(source, version, output, "version")

            self.assertTrue(os.path.isfile(os.path.join(
                output, "usr", "lib", "modules", "6.18.0-test",
                "kernel", "demo.ko",
            )))

    def test_normalizes_a_rootfs_shaped_tar(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = os.path.join(temporary, "modules.tar")
            version = os.path.join(temporary, "version")
            output = os.path.join(temporary, "output")
            payload = b"module"
            with tarfile.open(source, "w") as archive:
                member = tarfile.TarInfo("./lib/modules/6.18.0-test/demo.ko")
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))
            _write(version, "6.18.0-test")

            normalize_modules(source, version, output, "auto")

            with open(os.path.join(
                output, "usr", "lib", "modules", "6.18.0-test", "demo.ko"
            ), "rb") as stream:
                self.assertEqual(payload, stream.read())

    def test_rejects_a_directory_symlink_outside_the_module_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = os.path.join(temporary, "source")
            version = os.path.join(temporary, "version")
            output = os.path.join(temporary, "output")
            os.makedirs(source)
            os.symlink("/usr/lib/modules", os.path.join(source, "escape"))
            _write(version, "6.18.0-test")

            with self.assertRaisesRegex(ValueError, "absolute module symlink"):
                normalize_modules(source, version, output, "version")


class TestKernelRootfs(unittest.TestCase):
    def test_composes_multiple_kernels_and_checks_the_ima_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            rootfs = os.path.join(temporary, "rootfs.tar")
            with tarfile.open(rootfs, "w") as archive:
                payload = b"base\n"
                member = tarfile.TarInfo("etc/os-release")
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))
            certificate = os.path.join(temporary, "ima.der")
            _write(certificate, b"\x30\x03\x02\x01\x01", "wb")

            entries = []
            for release in ("6.18.0-stable", "6.18.0-debug"):
                version = os.path.join(temporary, release + ".version")
                image = os.path.join(temporary, release + ".image")
                modules = os.path.join(temporary, release + ".modules")
                _write(version, release)
                _write(image, release.encode(), "wb")
                _write(os.path.join(
                    modules, "usr", "lib", "modules", release, "modules.dep"
                ), "")
                entries.append((image, version, modules, None, None, certificate))

            output = os.path.join(temporary, "composed.tar")
            releases = compose_rootfs(
                rootfs, entries, output, expected_ima_certificate=certificate
            )

            self.assertEqual(["6.18.0-stable", "6.18.0-debug"], releases)
            with tarfile.open(output) as archive:
                members = {member.name: member for member in archive.getmembers()}
                for release in releases:
                    path = "usr/lib/modules/{}/vmlinuz".format(release)
                    self.assertEqual(release.encode(), archive.extractfile(members[path]).read())

    def test_rejects_a_kernel_with_another_ima_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            rootfs = os.path.join(temporary, "rootfs.tar")
            with tarfile.open(rootfs, "w"):
                pass
            version = os.path.join(temporary, "version")
            image = os.path.join(temporary, "image")
            trusted = os.path.join(temporary, "trusted.der")
            expected = os.path.join(temporary, "expected.der")
            _write(version, "6.18.0-test")
            _write(image, b"kernel", "wb")
            _write(trusted, b"\x30\x03\x02\x01\x01", "wb")
            _write(expected, b"\x30\x03\x02\x01\x02", "wb")
            with self.assertRaisesRegex(ValueError, "different IMA certificate"):
                compose_rootfs(
                    rootfs,
                    [(image, version, None, None, None, trusted)],
                    os.path.join(temporary, "output.tar"),
                    expected_ima_certificate=expected,
                )


class TestLinuxKernelBuild(unittest.TestCase):
    def test_rejects_a_source_symlink_outside_the_declared_tree(self):
        from linux_kernel_build import stage_source

        with tempfile.TemporaryDirectory() as temporary:
            source = os.path.join(temporary, "source")
            destination = os.path.join(temporary, "destination")
            os.makedirs(source)
            _write(os.path.join(source, "Makefile"), "all:\n\t@true\n")
            os.symlink("/usr/include", os.path.join(source, "host-headers"))

            with self.assertRaisesRegex(ValueError, "absolute kernel source symlink"):
                stage_source(source, destination)

    def test_generic_kbuild_produces_the_public_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = os.path.join(temporary, "source")
            os.makedirs(source)
            makefile = """\
.PHONY: olddefconfig bzImage vmlinux modules kernelrelease modules_install
olddefconfig:
\t@test -f $(O)/.config
bzImage:
\t@mkdir -p $(O)/arch/x86/boot
\t@printf kernel > $(O)/arch/x86/boot/bzImage
vmlinux:
\t@printf elf > $(O)/vmlinux
modules:
\t@printf map > $(O)/System.map
\t@printf symvers > $(O)/Module.symvers
kernelrelease:
\t@printf 6.18.0-test
modules_install:
\t@mkdir -p $(INSTALL_MOD_PATH)/lib/modules/6.18.0-test/kernel
\t@printf module > $(INSTALL_MOD_PATH)/lib/modules/6.18.0-test/kernel/demo.ko
\t@printf 'kernel/demo.ko:\\n' > $(INSTALL_MOD_PATH)/lib/modules/6.18.0-test/modules.dep
"""
            _write(os.path.join(source, "Makefile"), makefile)
            config = os.path.join(temporary, "config")
            _write(config, "CONFIG_MODULES=y\n")
            args = SimpleNamespace(
                architecture="x86_64",
                buildroot_tree=None,
                config=config,
                expected_release="6.18.0-test",
                image_path="",
                ima_certificate=None,
                isolation="none",
                jobs=1,
                localversion="",
                make="/usr/bin/make",
                make_arg=[],
                out_config=os.path.join(temporary, "out.config"),
                out_image=os.path.join(temporary, "image"),
                out_modules=os.path.join(temporary, "modules"),
                out_module_symvers=os.path.join(temporary, "Module.symvers"),
                out_system_map=os.path.join(temporary, "System.map"),
                out_version=os.path.join(temporary, "version"),
                out_vmlinux=os.path.join(temporary, "vmlinux"),
                source=source,
                source_date_epoch="1700000000",
                target_cpu="",
            )

            self.assertEqual("6.18.0-test", build_kernel(args))
            self.assertEqual("6.18.0-test", read_kernel_release(args.out_version))
            self.assertTrue(os.path.isfile(os.path.join(
                args.out_modules, "usr", "lib", "modules", "6.18.0-test",
                "kernel", "demo.ko",
            )))

    def test_ima_config_is_injected_without_private_key_material(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = os.path.join(temporary, "config")
            _write(config, "# CONFIG_IMA is not set\n")
            set_config_values(config, {
                "IMA": True,
                "IMA_X509_PATH": "/etc/keys/x509_ima.der",
            })
            with open(config, encoding="utf-8") as stream:
                result = stream.read()
            self.assertIn("CONFIG_IMA=y", result)
            self.assertIn('CONFIG_IMA_X509_PATH="/etc/keys/x509_ima.der"', result)
            self.assertNotIn("PRIVATE", result)


if __name__ == "__main__":
    unittest.main()
