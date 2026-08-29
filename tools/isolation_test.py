#!/usr/bin/env python3

import subprocess
import unittest
from unittest.mock import MagicMock, call, patch

import _isolation


class TestProcMount(unittest.TestCase):
    def test_unshare_reuses_the_visible_procfs(self):
        script = _isolation._chroot_script("/work", "/work", "/root")
        self.assertIn('mount --rbind /proc "$ROOT/proc"', script)
        self.assertNotIn("mount -t proc", script)

    @patch("_isolation.run")
    @patch("_isolation._mapped_user_namespace")
    @patch("_isolation.require_tool", return_value="/usr/bin/bwrap")
    def test_bwrap_reuses_the_visible_procfs(
        self,
        _require_tool,
        mapped_user_namespace,
        run,
    ):
        mapped_user_namespace.return_value.__enter__.return_value = 19
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


class TestSubordinateMapping(unittest.TestCase):
    def test_maps_uid_before_gid_with_the_existing_two_entry_policy(self):
        with (
            patch("_isolation.os.getuid", return_value=1000),
            patch("_isolation.os.getgid", return_value=100),
            patch(
                "_isolation._subid_range",
                side_effect=[(100000, 65536), (200000, 65536)],
            ),
            patch(
                "_isolation.require_tool",
                side_effect=lambda name: "/usr/bin/{}".format(name),
            ),
            patch("_isolation.run") as run,
        ):
            _isolation._map_subordinate_ids(4242)

        self.assertEqual(
            run.call_args_list,
            [
                call([
                    "/usr/bin/newuidmap", "4242",
                    "0", "1000", "1",
                    "1", "100000", "65536",
                ]),
                call([
                    "/usr/bin/newgidmap", "4242",
                    "0", "100", "1",
                    "1", "200000", "65536",
                ]),
            ],
        )


class TestBubblewrapUserNamespace(unittest.TestCase):
    def _child(self, status=None):
        child = MagicMock()
        child.pid = 4242
        child.poll.return_value = status
        child.wait.return_value = status or 0
        return child

    def test_passes_mapped_namespace_fd_and_preserves_bwrap_shape(self):
        child = self._child()
        events = []

        def map_ids(pid):
            events.append(("map", pid))

        def open_namespace(path, flags):
            events.append(("open", path, flags))
            return 14

        def run_bwrap(argv, **kwargs):
            events.append(("run", argv, kwargs))
            return "result"

        with (
            patch("_isolation.subid_mapping_available", return_value=True),
            patch(
                "_isolation.require_tool",
                side_effect=lambda name: "/usr/bin/{}".format(name),
            ),
            patch("_isolation.os.pipe", side_effect=[(10, 11), (12, 13)]),
            patch("_isolation.os.read", return_value=b"r\n"),
            patch("_isolation.os.open", side_effect=open_namespace),
            patch("_isolation.os.close") as close,
            patch("_isolation._map_subordinate_ids", side_effect=map_ids),
            patch("_isolation.subprocess.Popen", return_value=child) as popen,
            patch("_isolation.run", side_effect=run_bwrap),
        ):
            result = _isolation.run_isolated(
                ["/bin/true"],
                "bwrap",
                "/work",
                "/work/source",
                "/root",
                env={"PATH": "/usr/bin"},
            )

        self.assertEqual(result, "result")
        holder_argv = popen.call_args.args[0]
        self.assertEqual(
            holder_argv[:5],
            ["/usr/bin/unshare", "--user", "--", "/bin/sh", "-c"],
        )
        self.assertIn("echo r >&11", holder_argv[5])
        self.assertIn("read _ <&12", holder_argv[5])
        self.assertEqual(popen.call_args.kwargs["pass_fds"], (11, 12))

        self.assertEqual(events[0], ("map", 4242))
        self.assertEqual(events[1][0:2], ("open", "/proc/4242/ns/user"))
        self.assertEqual(events[2][0], "run")
        bwrap_argv = events[2][1]
        self.assertEqual(
            bwrap_argv,
            [
                "/usr/bin/bwrap",
                "--userns", "14",
                "--uid", "0",
                "--gid", "0",
                "--unshare-net",
                "--unshare-pid",
                "--unshare-ipc",
                "--die-with-parent",
                "--bind", "/root", "/",
                "--bind", "/work", "/work",
                "--ro-bind", "/proc", "/proc",
                "--dev", "/dev",
                "--tmpfs", "/tmp",
                "--setenv", "HOME", "/builddir",
                "--chdir", "/work/source",
                "/bin/true",
            ],
        )
        self.assertEqual(events[2][2]["pass_fds"], (14,))
        self.assertEqual(events[2][2]["env"], {"PATH": "/usr/bin"})
        self.assertEqual(
            close.call_args_list,
            [call(11), call(12), call(10), call(14), call(13)],
        )
        child.terminate.assert_called_once_with()
        child.wait.assert_called_once_with(timeout=5)

    def test_missing_subordinate_mapping_fails_before_starting_holder(self):
        with (
            patch("_isolation.subid_mapping_available", return_value=False),
            patch("_isolation.require_tool", return_value="/usr/bin/bwrap"),
            patch("_isolation.subprocess.Popen") as popen,
            patch("_isolation.run") as run,
        ):
            with self.assertRaisesRegex(
                SystemExit,
                "isolation=bwrap requires subordinate UID and GID ranges",
            ):
                _isolation.run_isolated(
                    ["true"], "bwrap", "/work", "/work", "/root"
                )

        popen.assert_not_called()
        run.assert_not_called()

    def test_holder_failure_prevents_mapping_and_bwrap(self):
        child = self._child(status=17)
        with (
            patch("_isolation.subid_mapping_available", return_value=True),
            patch(
                "_isolation.require_tool",
                side_effect=lambda name: "/usr/bin/{}".format(name),
            ),
            patch("_isolation.os.pipe", side_effect=[(10, 11), (12, 13)]),
            patch("_isolation.os.read", return_value=b""),
            patch("_isolation.os.close") as close,
            patch("_isolation._map_subordinate_ids") as map_ids,
            patch("_isolation.os.open") as open_namespace,
            patch("_isolation.subprocess.Popen", return_value=child),
            patch("_isolation.run") as run,
        ):
            with self.assertRaises(subprocess.CalledProcessError) as raised:
                _isolation.run_isolated(
                    ["true"], "bwrap", "/work", "/work", "/root"
                )

        self.assertEqual(raised.exception.returncode, 17)
        map_ids.assert_not_called()
        open_namespace.assert_not_called()
        run.assert_not_called()
        self.assertEqual(
            close.call_args_list,
            [call(11), call(12), call(10), call(13)],
        )
        child.terminate.assert_not_called()
        self.assertEqual(child.wait.call_args_list, [call(), call(timeout=5)])

    def test_mapping_failure_closes_pipes_and_reaps_holder(self):
        child = self._child()
        with (
            patch("_isolation.subid_mapping_available", return_value=True),
            patch(
                "_isolation.require_tool",
                side_effect=lambda name: "/usr/bin/{}".format(name),
            ),
            patch("_isolation.os.pipe", side_effect=[(10, 11), (12, 13)]),
            patch("_isolation.os.read", return_value=b"r\n"),
            patch("_isolation.os.close") as close,
            patch(
                "_isolation._map_subordinate_ids",
                side_effect=RuntimeError("mapping failed"),
            ),
            patch("_isolation.os.open") as open_namespace,
            patch("_isolation.subprocess.Popen", return_value=child),
            patch("_isolation.run") as run,
        ):
            with self.assertRaisesRegex(RuntimeError, "mapping failed"):
                _isolation.run_isolated(
                    ["true"], "bwrap", "/work", "/work", "/root"
                )

        open_namespace.assert_not_called()
        run.assert_not_called()
        self.assertEqual(
            close.call_args_list,
            [call(11), call(12), call(10), call(13)],
        )
        child.terminate.assert_called_once_with()
        child.wait.assert_called_once_with(timeout=5)

    def test_bwrap_interruption_closes_namespace_and_reaps_holder(self):
        child = self._child()
        with (
            patch("_isolation.subid_mapping_available", return_value=True),
            patch(
                "_isolation.require_tool",
                side_effect=lambda name: "/usr/bin/{}".format(name),
            ),
            patch("_isolation.os.pipe", side_effect=[(10, 11), (12, 13)]),
            patch("_isolation.os.read", return_value=b"r\n"),
            patch("_isolation.os.open", return_value=14),
            patch("_isolation.os.close") as close,
            patch("_isolation._map_subordinate_ids"),
            patch("_isolation.subprocess.Popen", return_value=child),
            patch("_isolation.run", side_effect=KeyboardInterrupt),
        ):
            with self.assertRaises(KeyboardInterrupt):
                _isolation.run_isolated(
                    ["true"], "bwrap", "/work", "/work", "/root"
                )

        self.assertEqual(
            close.call_args_list,
            [call(11), call(12), call(10), call(14), call(13)],
        )
        child.terminate.assert_called_once_with()
        child.wait.assert_called_once_with(timeout=5)

    def test_bwrap_failure_closes_namespace_and_reaps_holder(self):
        child = self._child()
        failure = subprocess.CalledProcessError(23, ["bwrap"])
        with (
            patch("_isolation.subid_mapping_available", return_value=True),
            patch(
                "_isolation.require_tool",
                side_effect=lambda name: "/usr/bin/{}".format(name),
            ),
            patch("_isolation.os.pipe", side_effect=[(10, 11), (12, 13)]),
            patch("_isolation.os.read", return_value=b"r\n"),
            patch("_isolation.os.open", return_value=14),
            patch("_isolation.os.close") as close,
            patch("_isolation._map_subordinate_ids"),
            patch("_isolation.subprocess.Popen", return_value=child),
            patch("_isolation.run", side_effect=failure),
        ):
            with self.assertRaises(subprocess.CalledProcessError) as raised:
                _isolation.run_isolated(
                    ["true"], "bwrap", "/work", "/work", "/root"
                )

        self.assertIs(raised.exception, failure)
        self.assertEqual(
            close.call_args_list,
            [call(11), call(12), call(10), call(14), call(13)],
        )
        child.terminate.assert_called_once_with()
        child.wait.assert_called_once_with(timeout=5)

    def test_stuck_holder_is_killed_and_reaped(self):
        child = self._child()
        child.wait.side_effect = [subprocess.TimeoutExpired("holder", 5), 0]

        _isolation._reap_namespace_holder(child)

        child.terminate.assert_called_once_with()
        child.kill.assert_called_once_with()
        self.assertEqual(child.wait.call_args_list, [call(timeout=5), call()])


if __name__ == "__main__":
    unittest.main()
