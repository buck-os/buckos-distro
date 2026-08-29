"""Tests for the refresh loop's argv reconstruction.

Only `solve_argv` is covered, and it is the part worth covering: a refresh
re-derives solve's command line from the lockfile's own record of it, so
any solve input the lockfile stores but this function forgets is an input
that silently stops applying the first time anyone refreshes.  The rest of
relock is network and file movement.
"""

import json
import os
import tempfile
import unittest

import probe
import relock


def lock(**solve):
    recorded = {
        "build": [],
        "explicit_build": [],
        "overrides": [],
        "images": [],
        "image_overrides": [],
        "source_exceptions": [],
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
                explicit_build=["acl", "zlib-ng"],
                overrides=["crate(syn/derive)=rust-syn+derive-devel"],
                images=["live=bash,coreutils"],
                image_overrides=["live:/usr/bin/systemd-sysusers=systemd"],
                source_image_sets=["live"],
                prebuilt_sources=["kernel"],
                source_exceptions=[{
                    "kind": "host-kernel-capability",
                    "package": "kernel-core",
                    "reason": "Host kernel interface is unavailable.",
                    "source": "kernel",
                }],
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
        self.assertEqual(flags(argv, "--source-image"), ["live"])
        self.assertEqual(flags(argv, "--prebuilt-source"), ["kernel"])
        self.assertEqual(
            [json.loads(value) for value in flags(argv, "--source-exception")],
            [{
                "kind": "host-kernel-capability",
                "package": "kernel-core",
                "reason": "Host kernel interface is unavailable.",
                "source": "kernel",
            }],
        )

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
            lock(seed_only=True, build=["acl"], explicit_build=["acl"]),
            repos=[],
            out="/o",
        )
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
        del stale["solve"]["explicit_build"]
        del stale["solve"]["source_exceptions"]
        argv = relock.solve_argv(stale, repos=[], out="/o")
        self.assertEqual(flags(argv, "--build"), ["acl"])
        self.assertNotIn("--seed-only", argv)
        self.assertEqual(flags(argv, "--seed-package"), [])

    def test_an_image_derived_lock_does_not_replay_the_effective_build_list(self):
        stale = lock(
            build=["bash", "kernel"],
            source_image_sets=["live"],
        )
        del stale["solve"]["explicit_build"]
        argv = relock.solve_argv(stale, repos=[], out="/o")
        self.assertEqual(flags(argv, "--build"), [])
        self.assertEqual(flags(argv, "--source-image"), ["live"])

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


class TestRpmPaths(unittest.TestCase):
    def test_lockfile_release_discovery_is_flavor_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in (
                "centos-9-x86_64.lock.json",
                "centos-10-x86_64.lock.json",
                "fedora-44-x86_64.lock.json",
                "centos-9-aarch64.lock.json",
            ):
                with open(os.path.join(directory, name), "w"):
                    pass
            self.assertEqual(
                relock.lockfile_releases(
                    directory, "x86_64", flavor="centos"),
                ["10", "9"],
            )

    def test_probe_paths_include_the_flavor(self):
        self.assertEqual(
            probe.probe_path(
                "/locks/centos-hyperscale-10-aarch64.lock.json"),
            "/locks/centos-hyperscale-10-aarch64.probe.json",
        )

    def test_probe_targets_use_the_lock_flavor(self):
        got = probe.probe_targets(
            {
                "flavor": "centos",
                "release": "10",
                "target_cpu": "aarch64",
                "solve": {"build": ["zlib", "acl"]},
            },
        )
        self.assertEqual(got, [
            "//flavors/centos:probe-acl-10-aarch64",
            "//flavors/centos:probe-zlib-10-aarch64",
        ])


if __name__ == "__main__":
    unittest.main()
