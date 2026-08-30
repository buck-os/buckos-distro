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

from _rpm import reproducible_env


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
