#!/usr/bin/env python3
"""The environment a sandboxed action runs in has to be a declared thing.

`reproducible_env()` exists to pin the handful of variables that decide
whether two builds of the same inputs produce the same bytes.  It pinned
them with `setdefault`, which means an inherited value won: a daemon
started from a shell that exports TZ produced different output, and
nothing anywhere said so.

That is a quieter defect than the PATH one beside it.  A missing tool
fails loudly and gets fixed the same day.  A build that silently used the
launcher's timezone still passes every gate and its artifact is simply
wrong in a way no test looks for.
"""

import os
import unittest
from unittest import mock

from _rpm import SANDBOX_HOME, SANDBOX_PATH, reproducible_env


# The values the function exists to guarantee, and a hostile setting for
# each.  Hostile rather than merely different: each of these is a value a
# real login shell plausibly exports.
PINNED = {
    "SOURCE_DATE_EPOCH": ("1700000000", "1"),
    "LC_ALL": ("C.UTF-8", "en_US.UTF-8"),
    "LANG": ("C.UTF-8", "en_US.UTF-8"),
    "TZ": ("UTC", "America/New_York"),
    "RPM_BUILD_HOST": ("buckos-distro", "someone-elses-laptop"),
}


class TestPinsAreNotSuggestions(unittest.TestCase):
    def test_pins_survive_a_hostile_process_environment(self):
        """The regression this file was written for.

        Models the real case: the Buck daemon was started from a shell
        that exports these, so every sandboxed action inherits them.
        """
        hostile = {name: bad for name, (_good, bad) in PINNED.items()}
        with mock.patch.dict(os.environ, hostile, clear=False):
            env = reproducible_env()
        for name, (good, bad) in PINNED.items():
            with self.subTest(variable=name):
                self.assertEqual(
                    good, env[name],
                    "{} came through as the inherited {!r}; the pin is a "
                    "suggestion rather than a guarantee".format(name, bad),
                )

    def test_the_hostile_values_are_actually_different(self):
        """Guards the test above from passing because it asked for nothing."""
        for name, (good, bad) in PINNED.items():
            with self.subTest(variable=name):
                self.assertNotEqual(good, bad)

    def test_an_explicit_source_date_epoch_is_still_honoured(self):
        """The argument is a deliberate input and stays one.

        Only the *inherited* value is refused.  Callers pass their own
        epoch and that has to keep working, or every image rule changes
        behaviour.
        """
        with mock.patch.dict(os.environ, {"SOURCE_DATE_EPOCH": "1"}, clear=False):
            env = reproducible_env(source_date_epoch="1234567890")
        self.assertEqual("1234567890", env["SOURCE_DATE_EPOCH"])


class TestTheEnvironmentIsDeclaredNotInherited(unittest.TestCase):
    """Nothing reaches a sandboxed action that this function did not put there.

    The five pins above were the variables we knew mattered.  The rest of
    the environment came through untouched, so TMPDIR, MAKEFLAGS, LD_*, a
    proxy setting or anything else the launcher happened to export was an
    undeclared input to every build.  A survey of every os.environ read in
    the action drivers found nothing inside a sandbox needs one: every
    knob already arrives as an explicit argument.
    """

    def test_nothing_is_inherited_from_the_process_environment(self):
        marker = "BUCKOS_INHERITANCE_CANARY"
        with mock.patch.dict(os.environ, {marker: "leaked"}, clear=False):
            env = reproducible_env()
        self.assertNotIn(
            marker, env,
            "the process environment still reaches sandboxed actions, so "
            "the build depends on whoever started the daemon",
        )

    def test_the_keys_are_exactly_what_the_function_declares(self):
        """Adding an inherited variable later should fail here, not in a build."""
        with mock.patch.dict(os.environ, {"MAKEFLAGS": "-j99"}, clear=False):
            env = reproducible_env()
        self.assertEqual(
            {"PATH", "HOME", "SOURCE_DATE_EPOCH", "LC_ALL", "LANG", "TZ",
             "RPM_BUILD_HOST"},
            set(env),
        )

    def test_path_is_declared_and_covers_both_buildroot_layouts(self):
        """Fedora merged /usr/sbin into bin; EL did not, and both must work.

        Measured on the two image-tools buildroots: Fedora's /usr/sbin is a
        symlink to bin so nothing is unreachable, while EL10's is a real
        directory holding 136 binaries that are in no other directory.
        """
        env = reproducible_env()
        self.assertEqual(SANDBOX_PATH, env["PATH"])
        for entry in ("/usr/sbin", "/usr/bin", "/sbin", "/bin"):
            with self.subTest(entry=entry):
                self.assertIn(entry, SANDBOX_PATH.split(":"))

    def test_an_inherited_path_does_not_win(self):
        """The specific defect: the build inherited the launcher's PATH."""
        with mock.patch.dict(os.environ, {"PATH": "/nonsense"}, clear=False):
            env = reproducible_env()
        self.assertEqual(SANDBOX_PATH, env["PATH"])

    def test_a_caller_may_add_variables_but_not_redefine_a_pin(self):
        """Callers legitimately add HOME and friends; none may move a pin."""
        env = reproducible_env({"FAKEROOTDONTTRYCHOWN": "1"})
        self.assertEqual("1", env["FAKEROOTDONTTRYCHOWN"])
        self.assertEqual(SANDBOX_PATH, env["PATH"])
        for pin in ("TZ", "PATH", "HOME"):
            with self.subTest(pin=pin), self.assertRaises(ValueError):
                reproducible_env({pin: "somewhere-else"})

    def test_home_is_declared_for_both_isolation_modes(self):
        """Only Bubblewrap set it; the unshare path inherited the launcher's."""
        with mock.patch.dict(os.environ, {"HOME": "/home/whoever"}, clear=False):
            env = reproducible_env()
        self.assertEqual(SANDBOX_HOME, env["HOME"])


class TestTheSourceDoesNotReadTheEnvironment(unittest.TestCase):
    """A source lint, because the runtime tests only prove today's behaviour.

    Restoring `dict(os.environ)` would pass every check above that does not
    name the variable it reintroduced.  The property is about what the
    source says, so it is checked there, the same way re_contract_test and
    scratch_contract_test check theirs.
    """

    def test_reproducible_env_does_not_derive_from_os_environ(self):
        # Parsed rather than grepped.  A substring search matches the
        # docstring explaining why the environment is not inherited, which
        # is a lint that fails on its own justification; only a real
        # attribute access counts.
        import ast
        import inspect
        import textwrap

        import _rpm

        tree = ast.parse(textwrap.dedent(inspect.getsource(_rpm.reproducible_env)))
        reads = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and node.attr == "environ"
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
        ]
        self.assertEqual(
            [], reads,
            "reproducible_env reads the process environment again; the "
            "sandbox environment is meant to be declared here in full",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
