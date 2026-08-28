"""Tests for the refresh loop's argv reconstruction.

Only `solve_argv` is covered, and it is the part worth covering: a refresh
re-derives solve's command line from the lockfile's own record of it, so
any solve input the lockfile stores but this function forgets is an input
that silently stops applying the first time anyone refreshes.  The rest of
relock is network and file movement.
"""

import unittest

import relock


def lock(**solve):
    recorded = {
        "build": [],
        "overrides": [],
        "images": [],
        "image_overrides": [],
        "seed_packages": [],
        "seed_only": False,
        "stages": 3,
    }
    recorded.update(solve)
    return {
        "release": 43,
        "dist_tag": ".fc43",
        "target_cpu": "x86_64",
        "solve": recorded,
    }


def flags(argv, name):
    """Every value passed for a repeatable flag, in order."""
    return [argv[i + 1] for i, item in enumerate(argv) if item == name]


class TestSolveArgv(unittest.TestCase):
    def test_the_recorded_inputs_come_back(self):
        argv = relock.solve_argv(
            lock(
                build=["acl", "zlib-ng"],
                overrides=["crate(syn/derive)=rust-syn+derive-devel"],
                images=["live=bash,coreutils"],
                image_overrides=["live:/usr/bin/systemd-sysusers=systemd"],
            ),
            repos=[],
            out="/tmp/out.json",
        )
        self.assertEqual(flags(argv, "--build"), ["acl", "zlib-ng"])
        self.assertEqual(flags(argv, "--override"),
                         ["crate(syn/derive)=rust-syn+derive-devel"])
        self.assertEqual(flags(argv, "--image"), ["live=bash,coreutils"])
        self.assertEqual(flags(argv, "--image-override"),
                         ["live:/usr/bin/systemd-sysusers=systemd"])

    def test_a_seed_only_lockfile_can_be_refreshed(self):
        # The case this exists for.  A flavor that pins a buildroot and
        # builds nothing from source records an empty build list, and solve
        # refuses a run with no --build, --seed-only or --seed-package -- so
        # dropping the flag does not produce a subtly different solve, it
        # produces no solve at all.  CentOS Stream 10 is exactly this shape.
        argv = relock.solve_argv(lock(seed_only=True), repos=[], out="/o")
        self.assertIn("--seed-only", argv)
        self.assertEqual(flags(argv, "--build"), [])

    def test_seed_only_is_a_flag_not_a_value(self):
        # store_true on the other side, so emitting it with an argument
        # would feed solve a positional it has no parameter for.  Checked
        # against a lockfile that has something after it, since on its own
        # it lands last and "nothing follows" would pass either way.
        argv = relock.solve_argv(
            lock(seed_only=True, build=["acl"]), repos=[], out="/o")
        self.assertEqual(argv.count("--seed-only"), 1)
        self.assertEqual(argv[argv.index("--seed-only") + 1], "--build")

    def test_seed_only_false_emits_nothing(self):
        argv = relock.solve_argv(lock(seed_only=False), repos=[], out="/o")
        self.assertNotIn("--seed-only", argv)

    def test_extra_seed_roots_come_back(self):
        argv = relock.solve_argv(
            lock(build=["acl"], seed_packages=["gdb-minimal", "strace"]),
            repos=[], out="/o",
        )
        self.assertEqual(flags(argv, "--seed-package"),
                         ["gdb-minimal", "strace"])

    def test_a_lockfile_missing_the_newer_keys_still_replays(self):
        # Written before --seed-package existed.  Absent is not the same as
        # empty for `build`, which every lockfile has, but for these two it
        # is: a solve that did not record them did not use them.
        stale = lock(build=["acl"])
        del stale["solve"]["seed_packages"]
        del stale["solve"]["seed_only"]
        argv = relock.solve_argv(stale, repos=[], out="/o")
        self.assertEqual(flags(argv, "--build"), ["acl"])
        self.assertNotIn("--seed-only", argv)
        self.assertEqual(flags(argv, "--seed-package"), [])

    def test_repos_are_passed_by_kind(self):
        argv = relock.solve_argv(
            lock(build=["acl"]),
            repos=[{"kind": "binary", "name": "binary-releases",
                    "base": "https://example/os", "path": "/p/primary.xml"}],
            out="/o",
        )
        self.assertEqual(flags(argv, "--binary-repo"), ["binary-releases"])
        self.assertEqual(flags(argv, "--binary-base"), ["https://example/os"])
        self.assertEqual(flags(argv, "--binary-primary"), ["/p/primary.xml"])


if __name__ == "__main__":
    unittest.main()
