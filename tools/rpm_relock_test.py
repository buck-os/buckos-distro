#!/usr/bin/env python3
"""Tests for the generic RPM-family relock loop."""

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
