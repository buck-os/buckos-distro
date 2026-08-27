#!/usr/bin/env python3

import unittest
from unittest.mock import patch

import _isolation


class TestProcMount(unittest.TestCase):
    def test_unshare_reuses_the_visible_procfs(self):
        script = _isolation._chroot_script("/work", "/work", "/root")
        self.assertIn('mount --rbind /proc "$ROOT/proc"', script)
        self.assertNotIn("mount -t proc", script)

    @patch("_isolation.run")
    @patch("_isolation.require_tool", return_value="/usr/bin/bwrap")
    def test_bwrap_reuses_the_visible_procfs(self, _require_tool, run):
        _isolation.run_isolated(
            ["true"],
            "bwrap",
            "/work",
            "/work",
            "/root",
        )
        argv = run.call_args.args[0]
        index = argv.index("--ro-bind")
        self.assertEqual(argv[index:index + 3], ["--ro-bind", "/proc", "/proc"])


if __name__ == "__main__":
    unittest.main()
