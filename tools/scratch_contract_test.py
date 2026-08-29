#!/usr/bin/env python3
"""A source lint: scratch trees for rpm payloads must live outside buck-out.

This guards a fix whose absence is expensive and whose cause is invisible.

An action that unpacks rpm payloads creates whatever filenames the packages
contain, and systemd-udev contains

    usr/lib/systemd/system/system-systemd\\x2dcryptsetup.slice

with a literal backslash.  That is a legal filename and a legal rpm, but it
is not expressible as a buck2 project-relative path, so if the tree lives
under buck-out the build dies with

    Error relativizing: `...system-systemd\\x2dcryptsetup.slice;6553f100`
      is not relative to project root

The reason this needs a test rather than a comment is what happens next.
The unrepresentable path is recorded in daemon state, so every subsequent
invocation fails instantly -- on a file that has already been deleted.  The
apparent fix is `buck2 kill`, which discards the action cache, which re-runs
the install, which recreates the file.  The failure regenerates its own
cause, and the symptom points at buck2 rather than at the line of Python
that chose the directory.

So `tempfile.mkdtemp()` with no `dir=` is banned in these scripts: it
inherits TMPDIR, buck2 sets TMPDIR inside buck-out, and the result is a
build that breaks days later for reasons no one will connect to this.  Use
scratch_dir() from _rpm.py, which picks a root outside the project.

Read as text rather than imported, for the same reason re_contract_test.py
is: the property is about what the source says, and a passing import proves
nothing about which directory gets used at runtime.
"""

import os
import re
import unittest

# The scripts that unpack rpm-authored trees.  Listed rather than globbed:
# a new script that needs this treatment should be an explicit decision,
# and a glob would silently exempt anything named unexpectedly while
# silently indicting tools that legitimately want a temp file.
GUARDED = (
    "deb_rootfs_install.py",
    "initramfs_build.py",
    "rootfs_install.py",
    "rpmbuild_replay.py",
)


def _repo_root():
    """Find the checked-in tree, which is not where this file runs from.

    Buck runs a python_test out of a link-tree containing only its own
    srcs, so a sibling lookup relative to __file__ finds nothing -- and
    finding nothing is the dangerous case for a lint, because "no files to
    check" reads exactly like "no violations".  Same upward search for
    .buckroot as re_contract_test.py, and for the same reason.
    """
    for start in (os.getcwd(), os.path.dirname(os.path.abspath(__file__))):
        path = start
        while True:
            if os.path.exists(os.path.join(path, ".buckroot")):
                return path
            parent = os.path.dirname(path)
            if parent == path:
                break
            path = parent
    raise AssertionError(
        "cannot find .buckroot from cwd {} or {}".format(
            os.getcwd(), os.path.abspath(__file__)
        )
    )


REPO_ROOT = _repo_root()
TOOLS_DIR = os.path.join(REPO_ROOT, "tools")

# tempfile.mkdtemp( ... ) with no dir= before the closing paren.  Deliberately
# narrow: mkstemp for a small file is fine, and so is mkdtemp with an
# explicit dir=, which is what scratch_dir itself uses.
BARE_MKDTEMP = re.compile(r"tempfile\.mkdtemp\((?![^)]*\bdir\s*=)[^)]*\)")


def source(name):
    path = os.path.join(TOOLS_DIR, name)
    if not os.path.exists(path):
        # A renamed or deleted script should fail loudly here rather than
        # quietly reducing this lint's coverage to nothing.
        raise AssertionError(
            "{} is in GUARDED but does not exist; update the list "
            "deliberately".format(name)
        )
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class TestScratchRootContract(unittest.TestCase):
    def test_no_bare_mkdtemp(self):
        for name in GUARDED:
            with self.subTest(script=name):
                found = BARE_MKDTEMP.findall(source(name))
                self.assertEqual(
                    found, [],
                    "{} calls tempfile.mkdtemp() without dir=, so its "
                    "scratch tree lands under buck2's TMPDIR inside "
                    "buck-out. Use scratch_dir() from _rpm.py -- see this "
                    "module's docstring for what breaks and why the "
                    "breakage outlives the file.".format(name),
                )

    def test_each_script_uses_scratch_dir(self):
        """The positive half: banning mkdtemp is not the same as fixing it.

        Without this, deleting the scratch directory entirely -- or
        hardcoding a path under buck-out -- would pass the ban and
        reintroduce the bug.
        """
        for name in GUARDED:
            with self.subTest(script=name):
                self.assertIn(
                    "scratch_dir(", source(name),
                    "{} does not call scratch_dir(); every script that "
                    "unpacks rpm payloads needs its tree outside the "
                    "project root".format(name),
                )


class TestScratchDirImplementation(unittest.TestCase):
    """The helper itself, since everything above delegates to it."""

    def test_default_root_is_outside_the_project(self):
        from _rpm import _DEFAULT_SCRATCH_ROOT

        self.assertTrue(
            os.path.isabs(_DEFAULT_SCRATCH_ROOT),
            "a relative scratch root would resolve against the action's "
            "cwd, which is the project root -- the exact thing being "
            "avoided",
        )
        # Not merely absolute: it must not be *inside* this repo, which an
        # absolute path pointing at buck-out would satisfy.
        self.assertFalse(
            os.path.realpath(_DEFAULT_SCRATCH_ROOT).startswith(
                os.path.realpath(REPO_ROOT) + os.sep
            ),
            "the default scratch root is inside the project tree",
        )

    def test_env_override_is_honoured(self):
        """So a host whose /var/tmp is a different device can fix staging.

        Hardlinking rpms into the scratch area depends on it sharing a
        device with buck-out; without an override that is unfixable
        locally and every package becomes a copy.
        """
        import tempfile

        from _rpm import SCRATCH_ROOT_ENV, scratch_dir

        with tempfile.TemporaryDirectory() as base:
            previous = os.environ.get(SCRATCH_ROOT_ENV)
            os.environ[SCRATCH_ROOT_ENV] = base
            try:
                made = scratch_dir("scratch-contract-test-")
            finally:
                if previous is None:
                    del os.environ[SCRATCH_ROOT_ENV]
                else:
                    os.environ[SCRATCH_ROOT_ENV] = previous
            self.assertEqual(os.path.dirname(made), base)
            self.assertTrue(os.path.isdir(made))


if __name__ == "__main__":
    unittest.main(verbosity=2)
