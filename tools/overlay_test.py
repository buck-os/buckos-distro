#!/usr/bin/env python3
"""overlay_tree: a later layer replaces the one below it, path for path.

Every case here is one that shutil.copytree(dirs_exist_ok=True) gets wrong,
and each is reachable the moment a package is built from source instead of
pinned.  A bootstrap stage exists precisely to rebuild something the seed
already ships, so its installroot collides with the seed on every path it
owns -- and copytree only tolerates a collision when both sides are
directories.  The first soname symlink it meets raises EEXIST and the build
dies partway through composing its own buildroot.
"""

import os
import stat
import tempfile
import types
import unittest
from unittest import mock

from _rpm import overlay_tree, rpm_package_filename


def write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)
    if mode is not None:
        os.chmod(path, mode)


def link(path, target):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.symlink(target, path)


class OverlayTreeTest(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="overlay-test-")
        self.lower = os.path.join(self.base, "lower")
        self.upper = os.path.join(self.base, "upper")
        self.dest = os.path.join(self.base, "dest")
        for path in (self.lower, self.upper, self.dest):
            os.makedirs(path)

    def tearDown(self):
        import shutil

        # The read-only-directory case leaves a tree the walker cannot
        # descend as-is.
        for root, dirnames, _ in os.walk(self.base):
            for name in dirnames:
                try:
                    os.chmod(os.path.join(root, name), 0o700)
                except OSError:
                    pass
        shutil.rmtree(self.base, ignore_errors=True)

    def compose(self):
        overlay_tree(self.lower, self.dest)
        overlay_tree(self.upper, self.dest)

    def test_symlink_replaces_symlink(self):
        """The case that broke stage 2: two layers own the same soname link.

        The seed pins xz and the stage-1 build rebuilds it, so
        usr/lib64/liblzma.so.5 exists in both -- pointing at different
        versions.  os.symlink onto an existing path is EEXIST, which is
        why this needs unlinking rather than tolerating.
        """
        link(os.path.join(self.lower, "usr/lib64/liblzma.so.5"),
             "liblzma.so.5.8.0")
        link(os.path.join(self.upper, "usr/lib64/liblzma.so.5"),
             "liblzma.so.5.8.1")

        self.compose()

        out = os.path.join(self.dest, "usr/lib64/liblzma.so.5")
        self.assertTrue(os.path.islink(out))
        self.assertEqual(os.readlink(out), "liblzma.so.5.8.1")

    def test_file_replaces_file(self):
        write(os.path.join(self.lower, "usr/bin/gzip"), "pinned")
        write(os.path.join(self.upper, "usr/bin/gzip"), "rebuilt")

        self.compose()

        with open(os.path.join(self.dest, "usr/bin/gzip")) as fh:
            self.assertEqual(fh.read(), "rebuilt")

    def test_file_replaces_readonly_file(self):
        """rpm ships mode 0444 files; replacing one must not need write bits.

        Removing and recreating is what makes this work -- copy2 onto an
        existing read-only file is EACCES for an unprivileged builder.
        """
        write(os.path.join(self.lower, "usr/share/x"), "pinned", 0o444)
        write(os.path.join(self.upper, "usr/share/x"), "rebuilt", 0o444)

        self.compose()

        with open(os.path.join(self.dest, "usr/share/x")) as fh:
            self.assertEqual(fh.read(), "rebuilt")

    def test_directories_merge(self):
        """Replacement is per path, not per directory.

        A package that owns one file in /usr/bin must not take the
        directory with it.
        """
        write(os.path.join(self.lower, "usr/bin/gzip"), "pinned")
        write(os.path.join(self.lower, "usr/bin/tar"), "seed-only")
        write(os.path.join(self.upper, "usr/bin/gzip"), "rebuilt")

        self.compose()

        self.assertTrue(os.path.exists(os.path.join(self.dest, "usr/bin/tar")))
        with open(os.path.join(self.dest, "usr/bin/gzip")) as fh:
            self.assertEqual(fh.read(), "rebuilt")

    def test_writes_into_readonly_directory(self):
        """The filesystem package owns /usr/lib as dr-xr-xr-x.

        Once the seed layer has laid that down, an unprivileged process
        cannot create anything inside it, so the next layer fails with a
        wall of EACCES.  Directories are forced owner-writable for the
        same reason make_dirs_writable does it.
        """
        os.makedirs(os.path.join(self.lower, "usr/lib"))
        os.chmod(os.path.join(self.lower, "usr/lib"), 0o555)
        write(os.path.join(self.upper, "usr/lib/libz.so.1"), "rebuilt")

        self.compose()

        out = os.path.join(self.dest, "usr/lib/libz.so.1")
        self.assertTrue(os.path.isfile(out))
        mode = stat.S_IMODE(os.stat(os.path.dirname(out)).st_mode)
        self.assertTrue(mode & stat.S_IWUSR, oct(mode))

    def test_symlink_replaces_directory(self):
        """A rebuild is allowed to change a path's type.

        The merged-usr direction: /lib was a real directory and becomes a
        link to usr/lib.  Left as a directory, the two fork silently.
        """
        write(os.path.join(self.lower, "lib/libc.so.6"), "pinned")
        link(os.path.join(self.upper, "lib"), "usr/lib")

        self.compose()

        out = os.path.join(self.dest, "lib")
        self.assertTrue(os.path.islink(out))
        self.assertEqual(os.readlink(out), "usr/lib")

    def test_directory_replaces_dangling_symlink(self):
        """And the reverse: a link below, a real tree above.

        The link dangles -- ../../var/debug is provided by no layer here --
        so there is nothing to follow and replacing it is the only option.
        Contrast test_upper_directory_follows_a_merged_usr_symlink, where
        the link resolves and the answer is the other way round.
        """
        link(os.path.join(self.lower, "usr/lib/debug"), "../../var/debug")
        write(os.path.join(self.upper, "usr/lib/debug/gzip.debug"), "rebuilt")

        self.compose()

        out = os.path.join(self.dest, "usr/lib/debug")
        self.assertFalse(os.path.islink(out))
        self.assertTrue(os.path.isfile(os.path.join(out, "gzip.debug")))

    def test_upper_directory_follows_a_merged_usr_symlink(self):
        """libtirpc, and the reason 26 probes reported a missing rpm.

        `filesystem` ships /lib64 as a symlink to usr/lib64, and libtirpc
        declares its library under /lib64.  On a real system that path
        resolves through the link and the file lands in /usr/lib64.

        Replacing the link with a real directory forks them, and what
        breaks is not libtirpc: /usr/lib64 keeps ld-linux-x86-64.so.2,
        which every binary names absolutely as /lib64/ld-linux-x86-64.so.2,
        so exec fails on the interpreter.  The shell calls that 127 and
        prints nothing, for a binary sitting right there in /usr/bin.
        """
        os.makedirs(os.path.join(self.lower, "usr/lib64"))
        write(os.path.join(self.lower, "usr/lib64/ld-linux-x86-64.so.2"), "loader")
        link(os.path.join(self.lower, "lib64"), "usr/lib64")
        write(os.path.join(self.upper, "lib64/libtirpc.so.3"), "tirpc")

        self.compose()

        out = os.path.join(self.dest, "lib64")
        self.assertTrue(os.path.islink(out), "/lib64 must stay a symlink")
        self.assertEqual(os.readlink(out), "usr/lib64")
        # Both files reachable by both spellings -- the point of the merge.
        for path in ("lib64/libtirpc.so.3", "usr/lib64/libtirpc.so.3",
                     "lib64/ld-linux-x86-64.so.2",
                     "usr/lib64/ld-linux-x86-64.so.2"):
            self.assertTrue(os.path.isfile(os.path.join(self.dest, path)), path)

    def test_symlinked_directory_stays_a_symlink(self):
        """os.walk lists a symlink-to-directory in dirnames and does not
        descend it.  Handled as a directory it becomes a real one, which on
        a merged-usr layout forks /lib away from /usr/lib."""
        os.makedirs(os.path.join(self.lower, "usr/lib"))
        write(os.path.join(self.lower, "usr/lib/libc.so.6"), "pinned")
        link(os.path.join(self.lower, "lib"), "usr/lib")

        overlay_tree(self.lower, self.dest)

        out = os.path.join(self.dest, "lib")
        self.assertTrue(os.path.islink(out))
        self.assertEqual(os.readlink(out), "usr/lib")

    def test_dangling_symlink_survives(self):
        """Copied by link text, never by target.

        rpm ships links whose target is provided by another package, so a
        layer in isolation is full of dangling ones; resolving them would
        both fail and silently deep-copy the target when it did not.
        """
        link(os.path.join(self.lower, "usr/bin/ex"), "vim")

        overlay_tree(self.lower, self.dest)

        out = os.path.join(self.dest, "usr/bin/ex")
        self.assertTrue(os.path.islink(out))
        self.assertEqual(os.readlink(out), "vim")

    def test_lower_layer_is_not_mutated(self):
        """The seed is a Buck input shared by every concurrent action.

        Composing must read it and nothing else; a write here corrupts
        other packages' builds and poisons the cache.
        """
        write(os.path.join(self.lower, "usr/bin/gzip"), "pinned")
        write(os.path.join(self.upper, "usr/bin/gzip"), "rebuilt")

        self.compose()

        with open(os.path.join(self.lower, "usr/bin/gzip")) as fh:
            self.assertEqual(fh.read(), "pinned")

    def test_file_modes_are_preserved(self):
        write(os.path.join(self.upper, "usr/bin/gzip"), "rebuilt", 0o755)
        write(os.path.join(self.upper, "usr/share/x"), "data", 0o644)

        overlay_tree(self.upper, self.dest)

        self.assertEqual(
            stat.S_IMODE(os.stat(os.path.join(self.dest, "usr/bin/gzip")).st_mode),
            0o755,
        )
        self.assertEqual(
            stat.S_IMODE(os.stat(os.path.join(self.dest, "usr/share/x")).st_mode),
            0o644,
        )


class RpmFilenameTest(unittest.TestCase):
    @mock.patch("_rpm.require_tool", return_value="/usr/bin/rpm")
    @mock.patch("_rpm.run")
    def test_uses_header_identity_instead_of_artifact_name(self, run, _tool):
        run.return_value = types.SimpleNamespace(
            stdout="gzip-1.14-4.fc45.x86_64.rpm"
        )

        self.assertEqual(
            rpm_package_filename("buck-out/some-target/gzip.rpm"),
            "gzip-1.14-4.fc45.x86_64.rpm",
        )

    @mock.patch("_rpm.require_tool", return_value="/usr/bin/rpm")
    @mock.patch("_rpm.run")
    def test_rejects_a_filename_with_a_path_component(self, run, _tool):
        run.return_value = types.SimpleNamespace(stdout="../escape.rpm")

        with self.assertRaises(SystemExit):
            rpm_package_filename("package.rpm")


if __name__ == "__main__":
    unittest.main(verbosity=2)
