#!/usr/bin/env python3
"""Tests for reading a rootfs tarball's index.

Worth direct coverage because the answer is not checkable downstream: pick
the wrong kernel version and dracut builds a perfectly valid initramfs for
a kernel the image does not boot, so the build stays green and the ISO
hangs. Nothing between here and a boot attempt can catch it.

Fixtures are built in memory rather than checked in -- a tar of empty
files is cheaper to construct than to review, and building it here means
the test states the layout it depends on.
"""

import io
import os
import tarfile
import tempfile
import unittest

from _image import extract_member, find_kernel, find_kernels


def make_tar(names, prefix="./", contents=None):
    """A tarball containing one regular file per name."""
    handle, path = tempfile.mkstemp(suffix=".tar")
    os.close(handle)
    with tarfile.open(path, "w") as tar:
        for name in names:
            payload = (contents or {}).get(name, b"")
            info = tarfile.TarInfo(prefix + name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return path


class ImageTarTest(unittest.TestCase):
    def tearDown(self):
        for path in getattr(self, "_paths", []):
            os.unlink(path)

    def tar(self, names, **kwargs):
        path = make_tar(names, **kwargs)
        self._paths = getattr(self, "_paths", []) + [path]
        return path


class TestFindKernels(ImageTarTest):
    def test_finds_the_kernel(self):
        path = self.tar([
            "usr/lib/modules/6.17.1-300.fc43.x86_64/vmlinuz",
            "usr/bin/bash",
        ])
        self.assertEqual(
            find_kernels(path),
            [("6.17.1-300.fc43.x86_64",
              "./usr/lib/modules/6.17.1-300.fc43.x86_64/vmlinuz")],
        )

    def test_handles_an_archive_without_the_dot_prefix(self):
        """rpm writes `./usr/...`; a hand-rolled tar may not."""
        path = self.tar(
            ["usr/lib/modules/6.17.1/vmlinuz"], prefix=""
        )
        self.assertEqual(
            [kver for kver, _ in find_kernels(path)], ["6.17.1"]
        )

    def test_finds_a_debian_family_kernel_under_boot(self):
        path = self.tar(["boot/vmlinuz-6.12.0-4-arm64"])
        self.assertEqual(
            find_kernels(path),
            [("6.12.0-4-arm64", "./boot/vmlinuz-6.12.0-4-arm64")],
        )

    def test_sorted_so_the_result_does_not_depend_on_tar_order(self):
        path = self.tar([
            "usr/lib/modules/6.19.0/vmlinuz",
            "usr/lib/modules/6.17.1/vmlinuz",
        ])
        self.assertEqual(
            [kver for kver, _ in find_kernels(path)], ["6.17.1", "6.19.0"]
        )

    def test_a_vmlinuz_elsewhere_is_not_a_kernel(self):
        """Depth is checked, so unversioned /boot/vmlinuz does not count.

        Otherwise a rescue image or a stray copy under a subdirectory of
        modules/ would be offered as a bootable kernel.
        """
        path = self.tar([
            "boot/vmlinuz",
            "usr/lib/modules/6.17.1/extra/vmlinuz",
            "usr/share/doc/vmlinuz",
        ])
        self.assertEqual(find_kernels(path), [])

    def test_a_kernelless_image_is_not_a_kernel(self):
        path = self.tar(["usr/bin/bash", "etc/fstab"])
        self.assertEqual(find_kernels(path), [])


class TestFindKernel(ImageTarTest):
    def test_single_kernel_needs_no_kver(self):
        path = self.tar(["usr/lib/modules/6.17.1/vmlinuz"])
        self.assertEqual(find_kernel(path)[0], "6.17.1")

    def test_no_kernel_says_why(self):
        path = self.tar(["usr/bin/bash"])
        with self.assertRaises(SystemExit) as caught:
            find_kernel(path)
        self.assertIn("no kernel found", str(caught.exception))

    def test_two_kernels_without_kver_is_an_error(self):
        """Not a silent pick: which one boots is the caller's decision."""
        path = self.tar([
            "usr/lib/modules/6.17.1/vmlinuz",
            "usr/lib/modules/6.19.0/vmlinuz",
        ])
        with self.assertRaises(SystemExit) as caught:
            find_kernel(path)
        message = str(caught.exception)
        self.assertIn("--kver", message)
        # Both candidates named, so the fix is in the error.
        self.assertIn("6.17.1", message)
        self.assertIn("6.19.0", message)

    def test_kver_selects_among_several(self):
        path = self.tar([
            "usr/lib/modules/6.17.1/vmlinuz",
            "usr/lib/modules/6.19.0/vmlinuz",
        ])
        self.assertEqual(find_kernel(path, "6.19.0")[0], "6.19.0")

    def test_an_absent_kver_lists_what_is_there(self):
        path = self.tar(["usr/lib/modules/6.17.1/vmlinuz"])
        with self.assertRaises(SystemExit) as caught:
            find_kernel(path, "6.99.0")
        self.assertIn("6.17.1", str(caught.exception))


class TestExtractMember(ImageTarTest):
    def test_extracts_the_bytes(self):
        payload = b"\x1f\x8b not really a kernel"
        path = self.tar(
            ["usr/lib/modules/6.17.1/vmlinuz"],
            contents={"usr/lib/modules/6.17.1/vmlinuz": payload},
        )
        _kver, member = find_kernel(path)
        handle, out = tempfile.mkstemp()
        os.close(handle)
        try:
            extract_member(path, member, out)
            with open(out, "rb") as fh:
                self.assertEqual(fh.read(), payload)
        finally:
            os.unlink(out)

    def test_a_missing_member_is_an_error(self):
        path = self.tar(["usr/bin/bash"])
        with self.assertRaises(SystemExit):
            extract_member(path, "./nope", "/dev/null")


if __name__ == "__main__":
    unittest.main(verbosity=2)
