#!/usr/bin/env python3
"""Tests for the generic RPM-family relock loop."""

import hashlib
import json
import os
import tempfile
import types
import unittest
from unittest import mock

import rpm_relock


def template():
    return {
        "flavor": "centos-hyperscale",
        "release": "10",
        "dist_tag": ".hs.el10",
    }


def recorded(**overrides):
    values = {
        "build": ["acl"],
        "explicit_build": [],
        "seed_packages": [],
        "overrides": [],
        "images": [],
        "image_overrides": [],
        "source_variants": [],
        "source_image_sets": ["live"],
        "prebuilt_sources": ["kernel"],
        "source_exceptions": [{
            "kind": "host-kernel-capability",
            "package": "kernel-core",
            "reason": "Host kernel interface is unavailable.",
            "source": "kernel",
        }],
        "seed_only": False,
        "stages": 3,
    }
    values.update(overrides)
    return values


def flags(argv, name):
    return [argv[index + 1]
            for index, value in enumerate(argv) if value == name]


class TestSolveArgv(unittest.TestCase):
    def test_flavor_probe_and_variants_are_preserved(self):
        argv = rpm_relock.solve_argv(
            template(),
            recorded(source_variants=["acl-compat=acl@2.3.2-6.el10:tar"]),
            [],
            "aarch64",
            "/tmp/out.json",
            probe="/tmp/results.probe.json",
        )
        self.assertEqual(flags(argv, "--flavor"), ["centos-hyperscale"])
        self.assertEqual(flags(argv, "--probe"), ["/tmp/results.probe.json"])
        self.assertEqual(
            flags(argv, "--source-variant"),
            ["acl-compat=acl@2.3.2-6.el10:tar"],
        )
        self.assertEqual(flags(argv, "--source-image"), ["live"])
        self.assertEqual(flags(argv, "--prebuilt-source"), ["kernel"])
        self.assertEqual(flags(argv, "--build"), [])
        self.assertEqual(
            [json.loads(value) for value in flags(argv, "--source-exception")],
            recorded()["source_exceptions"],
        )
        self.assertIn("--strict", argv)

    def test_probe_bootstrap_solve_is_not_strict(self):
        argv = rpm_relock.solve_argv(
            template(),
            recorded(),
            [],
            "x86_64",
            "/tmp/out.json",
            strict=False,
        )
        self.assertNotIn("--strict", argv)

    def test_buildroot_repo_is_replayed_with_its_kind(self):
        argv = rpm_relock.solve_argv(
            template(),
            recorded(),
            [{
                "kind": "buildroot",
                "name": "buildroot-koji",
                "base": "https://kojihub.stream.centos.org/kojifiles/repos/c10s-build/1/aarch64",
                "path": "/tmp/buildroot-primary.xml.gz",
            }],
            "aarch64",
            "/tmp/out.json",
        )
        self.assertEqual(flags(argv, "--buildroot-repo"),
                         ["buildroot-koji"])
        self.assertEqual(flags(argv, "--buildroot-primary"),
                         ["/tmp/buildroot-primary.xml.gz"])

    def test_buildroot_base_is_rewritten_for_the_target_architecture(self):
        self.assertEqual(
            rpm_relock.architecture_base(
                "https://kojihub.stream.centos.org/kojifiles/repos/"
                "c10s-build/824779/x86_64",
                "x86_64",
                "aarch64",
                "buildroot",
            ),
            "https://kojihub.stream.centos.org/kojifiles/repos/"
            "c10s-build/824779/aarch64",
        )

    def test_same_architecture_keeps_the_recorded_probe(self):
        result = rpm_relock.architecture_solve(
            {"probe": "fedora-44-x86_64.probe.json"},
            "x86_64",
            "x86_64",
        )
        self.assertEqual(result["probe"], "fedora-44-x86_64.probe.json")

    def test_cross_architecture_drops_the_recorded_probe(self):
        result = rpm_relock.architecture_solve(
            {"probe": "fedora-44-x86_64.probe.json"},
            "x86_64",
            "aarch64",
        )
        self.assertIsNone(result["probe"])


class TestRepositoryPrimary(unittest.TestCase):
    @staticmethod
    def write_primary(directory, contents=b"primary"):
        digest = hashlib.sha256(contents).hexdigest()
        name = "{}-primary.xml.zst".format(digest)
        path = os.path.join(directory, name)
        with open(path, "wb") as stream:
            stream.write(contents)
        return name, path

    def test_online_synchronizes_from_upstream(self):
        with mock.patch.object(
            rpm_relock.relock,
            "sync_repo",
            return_value="/cache/primary.xml.zst",
        ) as sync:
            self.assertEqual(
                rpm_relock.repository_primary(
                    "https://example/repo",
                    "/cache",
                    "binary",
                    "recorded-primary.xml.zst",
                ),
                "/cache/primary.xml.zst",
            )
        sync.assert_called_once_with(
            "https://example/repo", "/cache", "binary"
        )

    def test_offline_uses_exact_recorded_primary_and_ignores_other_files(self):
        with tempfile.TemporaryDirectory() as directory:
            name, path = self.write_primary(directory)
            for digest in ("0" * 64, "f" * 64):
                with open(
                    os.path.join(directory, digest + "-primary.xml.zst"),
                    "wb",
                ) as stream:
                    stream.write(b"other")
            with mock.patch.object(rpm_relock.relock, "sync_repo") as sync:
                self.assertEqual(
                    rpm_relock.repository_primary(
                        "https://example/repo",
                        directory,
                        "binary",
                        name,
                        offline=True,
                    ),
                    path,
                )
            sync.assert_not_called()

    def test_offline_requires_exact_recorded_primary(self):
        with tempfile.TemporaryDirectory() as directory:
            name = "{}-primary.xml.zst".format("1" * 64)
            self.write_primary(directory, b"different primary")
            with self.assertRaisesRegex(SystemExit, "recorded primary is missing"):
                rpm_relock.repository_primary(
                    "https://example/repo",
                    directory,
                    "binary",
                    name,
                    offline=True,
                )

    def test_offline_rejects_tampered_recorded_primary(self):
        with tempfile.TemporaryDirectory() as directory:
            name, path = self.write_primary(directory)
            with open(path, "wb") as stream:
                stream.write(b"tampered")
            with self.assertRaisesRegex(SystemExit, "sha256 mismatch"):
                rpm_relock.repository_primary(
                    "https://example/repo",
                    directory,
                    "binary",
                    name,
                    offline=True,
                )

    def test_offline_rejects_unsafe_or_untrusted_recorded_names(self):
        unsafe = (
            "/tmp/" + "1" * 64 + "-primary.xml.zst",
            "../" + "1" * 64 + "-primary.xml.zst",
            "nested/" + "1" * 64 + "-primary.xml.zst",
            "nested\\" + "1" * 64 + "-primary.xml.zst",
            "primary.xml.zst",
            "1" * 63 + "-primary.xml.zst",
        )
        with tempfile.TemporaryDirectory() as directory:
            for name in unsafe:
                with self.subTest(name=name):
                    with self.assertRaises(SystemExit):
                        rpm_relock.repository_primary(
                            "https://example/repo",
                            directory,
                            "binary",
                            name,
                            offline=True,
                        )

    def test_offline_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            name, path = self.write_primary(directory)
            target = path + ".target"
            os.replace(path, target)
            os.symlink(target, path)
            with self.assertRaisesRegex(SystemExit, "must not be a symlink"):
                rpm_relock.repository_primary(
                    "https://example/repo",
                    directory,
                    "binary",
                    name,
                    offline=True,
                )

    def test_offline_rejects_non_regular_file(self):
        with tempfile.TemporaryDirectory() as directory:
            name = "{}-primary.xml.zst".format("1" * 64)
            os.mkdir(os.path.join(directory, name))
            with self.assertRaisesRegex(SystemExit, "not a regular file"):
                rpm_relock.repository_primary(
                    "https://example/repo",
                    directory,
                    "binary",
                    name,
                    offline=True,
                )


class TestConvergence(unittest.TestCase):
    def test_complete_lock_is_converged(self):
        state = rpm_relock.convergence_state({"packages": {}, "problems": []})
        self.assertTrue(rpm_relock.converged(state))

    def test_missing_probe_forces_an_initial_pass(self):
        state = rpm_relock.convergence_state({"packages": {}, "problems": []})
        self.assertTrue(rpm_relock.probe_required(state, None))
        self.assertFalse(rpm_relock.probe_required(state, "/tmp/probe.json"))

    def test_every_incomplete_state_is_exposed(self):
        state = rpm_relock.convergence_state({
            "problems": [{"kind": "unresolved"}],
            "packages": {
                "needs-probe": {
                    "dynamic_buildrequires": {
                        "suspected": ["rust-packaging"],
                        "unmet": False,
                    },
                },
                "needs-another-pass": {
                    "dynamic_buildrequires": {
                        "suspected": [],
                        "unmet": True,
                    },
                },
            },
        })
        self.assertFalse(rpm_relock.converged(state))
        self.assertEqual(sorted(state["unprobed"]), ["needs-probe"])
        self.assertEqual(sorted(state["unmet"]), ["needs-another-pass"])
        self.assertEqual(
            rpm_relock.describe_state(state),
            "1 unresolved problem(s), 1 unprobed package(s), 1 unmet probe(s)",
        )


class TestProbeRelock(unittest.TestCase):
    def run_relock(self, states):
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            template_path = os.path.join(directory, "template.lock.json")
            output_path = os.path.join(directory, "output.lock.json")
            data = template()
            data.update({
                "target_cpu": "x86_64",
                "repos": [],
                "solve": recorded(probe=None),
            })
            with open(template_path, "w", encoding="utf-8") as stream:
                json.dump(data, stream)

            def fake_solve_and_generate(
                    template_data, recorded_data, repos, target_cpu, output,
                    probe_path=None, strict=True):
                del template_data, recorded_data, repos, target_cpu
                calls.append({"probe": probe_path, "strict": strict})
                state = states[min(len(calls) - 1, len(states) - 1)]
                with open(output, "w", encoding="utf-8") as stream:
                    json.dump(state, stream)

            probe_path = os.path.join(directory, "output.probe.json")
            fake_probe = types.SimpleNamespace(
                DEFAULT_CONFIG=[],
                probe_path=mock.Mock(return_value=probe_path),
                resolve_buck2=mock.Mock(return_value="buck2"),
                write_probe_file=mock.Mock(),
            )
            with (
                mock.patch.object(
                    rpm_relock,
                    "solve_and_generate",
                    side_effect=fake_solve_and_generate,
                ),
                mock.patch.object(rpm_relock.relock, "repo_root",
                                  return_value=directory),
                mock.patch.dict("sys.modules", {"probe": fake_probe}),
            ):
                rpm_relock.main([
                    "--template", template_path,
                    "--target-cpu", "x86_64",
                    "--output", output_path,
                    "--probe",
                ])
        return calls, fake_probe.write_probe_file

    def test_static_problems_are_allowed_until_the_final_strict_solve(self):
        calls, write_probe = self.run_relock([
            {"packages": {}, "problems": [{"kind": "unresolved"}]},
            {"packages": {}, "problems": []},
            {"packages": {}, "problems": []},
        ])
        self.assertEqual(
            [call["strict"] for call in calls],
            [False, False, True],
        )
        write_probe.assert_called_once()

    def test_missing_persisted_probe_forces_a_probe_of_a_clean_solve(self):
        calls, write_probe = self.run_relock([
            {"packages": {}, "problems": []},
            {"packages": {}, "problems": []},
            {"packages": {}, "problems": []},
        ])
        self.assertEqual(
            [call["strict"] for call in calls],
            [False, False, True],
        )
        self.assertIsNone(calls[0]["probe"])
        self.assertIsNotNone(calls[1]["probe"])
        write_probe.assert_called_once()


if __name__ == "__main__":
    unittest.main()
