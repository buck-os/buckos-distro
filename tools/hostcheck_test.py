#!/usr/bin/env python3

import contextlib
import io
import unittest
from unittest import mock

import hostcheck


def capability(name):
    return next(cap for cap in hostcheck.CAPABILITIES if cap["name"] == name)


class TestHostcheckOutput(unittest.TestCase):
    @mock.patch("hostcheck.run_checks", return_value=[])
    def test_success_describes_capabilities_not_all_packages(self, _run_checks):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = hostcheck.main([])

        self.assertEqual(status, 0)
        self.assertIn(
            "This host satisfies every probed build capability.",
            output.getvalue(),
        )
        self.assertNotIn("every package from source", output.getvalue())

    @mock.patch("hostcheck.run_checks")
    def test_bcond_gap_emits_only_valid_configuration(self, run_checks):
        run_checks.return_value = [
            (capability("netlink-crypto"), False, "not supported"),
        ]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = hostcheck.main(["--quiet"])

        self.assertEqual(status, 0)
        self.assertEqual(
            output.getvalue(),
            "[buckos.fedora]\n"
            "  without = gmp:fips, nettle:fipshmac\n",
        )
        self.assertNotIn("prebuilt", output.getvalue())

    @mock.patch("hostcheck.run_checks")
    def test_diagnostic_output_does_not_offer_prebuilt_sources(self, run_checks):
        run_checks.return_value = [
            (capability("netlink-crypto"), False, "not supported"),
        ]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = hostcheck.main([])

        self.assertEqual(status, 0)
        self.assertIn(
            "buildable with a feature disabled: gmp, nettle",
            output.getvalue(),
        )
        self.assertNotIn("prebuilt", output.getvalue())
        self.assertNotIn("pinned binary", output.getvalue())

    @mock.patch("hostcheck.run_checks")
    def test_required_gap_exits_nonzero_without_configuration(self, run_checks):
        run_checks.return_value = [
            (capability("af-alg"), False, "not supported"),
        ]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = hostcheck.main(["--quiet"])

        self.assertEqual(status, 1)
        self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
