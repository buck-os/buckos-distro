"""Tests for the RPM rootfs transaction wrapper."""

import types
import unittest

from rootfs_install import _transaction_script


class TestTransactionScript(unittest.TestCase):
    def args(self, *, keep_work=False, nodeps=False):
        return types.SimpleNamespace(
            keep_work=keep_work,
            nodeps=nodeps,
            source_date_epoch="1700000000",
        )

    def test_transaction_regenerates_the_excluded_udev_database(self):
        script = _transaction_script(
            self.args(),
            "/work/rpms",
            "/work/rootfs",
            "/work/rootfs.tar",
        )
        self.assertIn("--nodigest", script)
        self.assertIn("--excludepath /etc/udev/hwdb.bin", script)
        self.assertIn("systemd-hwdb --root=/work/rootfs update", script)

    def test_transaction_scrubs_the_content_that_records_this_build(self):
        # Measured across two runs of the Fedora live rootfs: three of 16,251
        # files differed, and squashfs compression turned them into 78 percent
        # of the ISO.  rpmdb.sqlite itself was identical, so it is kept.
        script = _transaction_script(
            self.args(),
            "/work/rpms",
            "/work/rootfs",
            "/work/rootfs.tar",
        )
        self.assertIn(": > /work/rootfs/etc/machine-id", script)
        self.assertIn("rm -f /work/rootfs/var/cache/ldconfig/aux-cache", script)
        self.assertIn("rpmdb.sqlite-shm", script)
        self.assertLess(script.index("machine-id"), script.index("tar --create"))

    def test_transaction_keeps_a_write_ahead_log_that_holds_frames(self):
        # -shm is shared memory and holds no database content.  A non-empty
        # -wal holds committed frames, so discarding it would corrupt the
        # database the image boots with; it is left in place instead.
        script = _transaction_script(
            self.args(),
            "/work/rpms",
            "/work/rootfs",
            "/work/rootfs.tar",
        )
        self.assertIn('[ ! -s "$rpmdb"/rpmdb.sqlite-wal ]', script)

    def test_keep_work_preserves_rootfs(self):
        script = _transaction_script(
            self.args(keep_work=True),
            "/work/rpms",
            "/work/rootfs",
            "/work/rootfs.tar",
        )
        self.assertNotIn("rm -rf /work/rootfs", script)

    def test_host_transaction_uses_the_same_payload_exclusion(self):
        script = _transaction_script(
            self.args(nodeps=True),
            "/work/rpms",
            "/work/rootfs",
            "/work/rootfs.tar",
        )
        self.assertNotIn("mount --bind /proc", script)
        self.assertIn("--excludepath /etc/udev/hwdb.bin", script)
        self.assertIn("--nodeps", script)

    def test_chroot_is_resolved_inside_the_buildroot(self):
        # semodule was resolved against both /usr/sbin and /usr/bin here and
        # chroot was not.  run() replaces the environment wholesale, so the
        # PATH inside the sandbox is the host's; EL10 keeps chroot in
        # /usr/sbin, which that PATH does not carry, and the step died with a
        # bare 127 before semodule ran.  Fedora 42 merged the two directories,
        # so only the EL buildroots hit it.
        script = _transaction_script(
            self.args(),
            "/work/rpms",
            "/work/rootfs",
            "/work/rootfs.tar",
            modules="/work/selinux-modules",
        )
        self.assertIn("/usr/sbin/chroot", script)
        self.assertIn('"$CHROOT" /work/rootfs "$SEMODULE"', script)
        self.assertNotIn(" chroot /work/rootfs", script)

    def test_installs_selinux_modules_before_archiving(self):
        script = _transaction_script(
            self.args(),
            "/work/rpms",
            "/work/rootfs",
            "/work/rootfs.tar",
            "/work/modules",
        )
        self.assertIn("semodule", script)
        self.assertIn("/usr/share/selinux/packages/buckos", script)
        self.assertIn("for module in /work/rootfs/usr/share/selinux", script)
        self.assertLess(script.index("semodule"), script.index("tar --create"))


if __name__ == "__main__":
    unittest.main()
