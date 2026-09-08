#!/usr/bin/env python3

import os
import tempfile
import unittest
from unittest import mock

from _repo import REPO_ROOT_ENV, find_repo_root


class TestRepoRoot(unittest.TestCase):
    def test_finds_marker_above_start(self):
        with tempfile.TemporaryDirectory() as root:
            marker = os.path.join(root, ".buckroot")
            child = os.path.join(root, "one", "two")
            open(marker, "w", encoding="utf-8").close()
            os.makedirs(child)
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(root, find_repo_root(child))

    def test_configured_root_wins_over_enclosing_repository(self):
        with tempfile.TemporaryDirectory() as outer:
            inner = os.path.join(outer, "imported")
            os.makedirs(inner)
            open(os.path.join(outer, ".buckroot"), "w", encoding="utf-8").close()
            open(os.path.join(inner, ".buckroot"), "w", encoding="utf-8").close()
            with mock.patch.dict(os.environ, {REPO_ROOT_ENV: inner}, clear=True):
                self.assertEqual(inner, find_repo_root(outer))

    def test_rejects_invalid_configured_root(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.dict(os.environ, {REPO_ROOT_ENV: root}, clear=True):
                with self.assertRaisesRegex(ValueError, REPO_ROOT_ENV):
                    find_repo_root()


if __name__ == "__main__":
    unittest.main()
