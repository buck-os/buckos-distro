#!/usr/bin/env python3

import os
import re
import subprocess
import tempfile
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


def _bwrap_argv(run):
    """The argv run_isolated handed to bwrap, from a patched run()."""
    return run.call_args.args[0]


def _bwrap_kernel_mounts(argv):
    """Host paths bwrap exposes inside the sandbox, keyed by target."""
    mounts = {}
    index = 0
    while index < len(argv):
        flag = argv[index]
        if flag in ("--bind", "--ro-bind"):
            mounts[argv[index + 2]] = flag
            index += 3
        elif flag == "--dev":
            mounts[argv[index + 1]] = flag
            index += 2
        elif flag == "--tmpfs":
            mounts[argv[index + 1]] = flag
            index += 2
        else:
            index += 1
    return mounts


def _unshare_kernel_mounts(script):
    """The same, read out of the chroot script the unshare path runs."""
    mounts = {}
    for match in re.finditer(r'mount --rbind (\S+) "\$ROOT([^"]+)"', script):
        mounts[match.group(2)] = match.group(1)
    for match in re.finditer(r'mount -t (\S+) \S+ "\$ROOT([^"]+)"', script):
        mounts[match.group(2)] = match.group(1)
    return mounts


# The filesystems a build reaches for that only the running kernel can
# answer for.  Anything a spec reads here is invisible to the buildroot's
# own fabricated skeleton.
KERNEL_FILESYSTEMS = frozenset(("/proc", "/sys", "/dev", "/tmp"))


class TestKernelFilesystems(unittest.TestCase):
    """Both sandboxes must show a build the same kernel filesystems.

    SPEC.md and README.md both state the two isolation modes are
    equivalent in hermeticity.  A mount present in one and absent from
    the other makes that false in the way that is hardest to notice: the
    build succeeds under one mode and fails inside %build under the
    other, naming a path rather than a sandbox.
    """

    @patch("_isolation.run")
    @patch("_isolation._mapped_user_namespace")
    @patch("_isolation.require_tool", return_value="/usr/bin/bwrap")
    def test_bwrap_exposes_sysfs_read_only(
        self,
        _require_tool,
        mapped_user_namespace,
        run,
    ):
        mapped_user_namespace.return_value.__enter__.return_value = 19
        _isolation.run_isolated(["true"], "bwrap", "/work", "/work", "/root")

        argv = _bwrap_argv(run)
        self.assertIn(["--ro-bind", "/sys", "/sys"], [
            argv[i:i + 3] for i in range(len(argv))
        ])

    def test_unshare_exposes_sysfs(self):
        script = _isolation._chroot_script("/work", "/work", "/root")
        self.assertIn('mount --rbind /sys "$ROOT/sys"', script)

    @patch("_isolation.run")
    @patch("_isolation._mapped_user_namespace")
    @patch("_isolation.require_tool", return_value="/usr/bin/bwrap")
    def test_both_modes_expose_the_same_kernel_filesystems(
        self,
        _require_tool,
        mapped_user_namespace,
        run,
    ):
        mapped_user_namespace.return_value.__enter__.return_value = 19
        _isolation.run_isolated(["true"], "bwrap", "/work", "/work", "/root")

        bwrap = set(_bwrap_kernel_mounts(_bwrap_argv(run)))
        unshare = set(_unshare_kernel_mounts(
            _isolation._chroot_script("/work", "/work", "/root")))

        # Compared against each other rather than against a literal, so
        # dropping a mount from either mode fails here instead of quietly
        # redefining what both are expected to carry.
        self.assertEqual(
            bwrap & KERNEL_FILESYSTEMS,
            unshare & KERNEL_FILESYSTEMS,
        )
        self.assertEqual(bwrap & KERNEL_FILESYSTEMS, KERNEL_FILESYSTEMS)


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
                "--bind", "/work", "/build",
                "--ro-bind", "/proc", "/proc",
                "--ro-bind", "/sys", "/sys",
                "--dev", "/dev",
                "--tmpfs", "/tmp",
                "--setenv", "HOME", "/builddir",
                "--chdir", "/build/source",
                "/bin/true",
            ],
        )
        self.assertEqual(events[2][2]["pass_fds"], (14,))
        # TMPDIR and friends are added by run_isolated, not by the caller:
        # /tmp in here is the tmpfs two lines above, so a tool falling back
        # to it would charge its intermediates to memory.  Asserted exactly
        # rather than loosened, because this is the assertion that noticed
        # the environment had changed at all -- twice now, the second time
        # when the bind moved to /build and these three moved with it.
        #
        # Every path in this argv and this environment is now either a
        # constant or the caller's own sysroot.  "/work" appears once, as
        # the *source* of the bind, which is the only place the host's
        # scratch name is allowed to survive.
        self.assertEqual(
            events[2][2]["env"],
            {
                "PATH": "/usr/bin",
                "TMPDIR": "/build/tmp",
                "TMP": "/build/tmp",
                "TEMP": "/build/tmp",
            },
        )
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


class TestActionTemporariesLandOnDisk(unittest.TestCase):
    """/tmp in the sandbox is a tmpfs, so a fallback there costs memory.

    Both modes mount it: --tmpfs /tmp under Bubblewrap, mount -t tmpfs
    under unshare.  Nothing in the tree was writing large temporaries
    there -- rpmbuild_replay had already pointed rpm at its own topdir and
    a full 86-recipe Debian ISO runs on tmpfs intermediates in 18 minutes
    -- but the drivers that set nothing were one large %install away from
    charging a build to RAM.
    """

    def test_temporary_variables_point_into_the_work_area(self):
        env = _isolation._with_action_tmpdir(
            {"PATH": "/usr/bin"}, "/work", "bwrap")
        for name in ("TMPDIR", "TMP", "TEMP"):
            with self.subTest(variable=name):
                self.assertEqual("/build/tmp", env[name])

    def test_the_value_does_not_depend_on_where_the_work_area_lives(self):
        """The point of the fixed bind, stated as an assertion.

        Two scratch directories that share nothing but their role produce
        the same variable.  Before the bind was fixed this returned each
        one's own name, and that name reached every tool that called
        mktemp -- and, through %_topdir, every DW_AT_comp_dir.
        """
        first = _isolation._with_action_tmpdir({}, "/scratch/a-9f3c1e", "bwrap")
        second = _isolation._with_action_tmpdir({}, "/var/tmp/b-0011ff", "bwrap")
        self.assertEqual(first["TMPDIR"], second["TMPDIR"])
        self.assertEqual("/build/tmp", first["TMPDIR"])

    def test_isolation_none_keeps_the_host_path(self):
        """No mount namespace means no bind, so there is nothing to translate.

        The pair to the test above: it would pass just as well if the
        function returned a constant unconditionally, and that would put a
        path into the environment of the one mode where it does not exist.
        """
        env = _isolation._with_action_tmpdir({}, "/scratch/a-9f3c1e", "none")
        self.assertEqual("/scratch/a-9f3c1e/tmp", env["TMPDIR"])

    def test_a_deliberate_caller_value_is_not_overridden(self):
        """rpmbuild_replay pairs its own with rpm's %_tmppath define.

        Overriding it would leave the two disagreeing, which is worse than
        either choice alone.  Safe to defer to the caller here precisely
        because nothing in this dict is inherited any more.
        """
        env = _isolation._with_action_tmpdir(
            {"TMPDIR": "/build/topdir/tmp"}, "/work", "bwrap")
        self.assertEqual("/build/topdir/tmp", env["TMPDIR"])

    def test_the_caller_dict_is_not_mutated(self):
        original = {"PATH": "/usr/bin"}
        _isolation._with_action_tmpdir(original, "/work", "bwrap")
        self.assertEqual({"PATH": "/usr/bin"}, original)

    def test_no_environment_stays_no_environment(self):
        """A caller passing none inherits the process env; leave that alone."""
        self.assertIsNone(_isolation._with_action_tmpdir(None, "/work", "bwrap"))

    def test_the_directory_is_created_before_the_sandbox_is_entered(self):
        """Nothing inside creates it, and a TMPDIR naming nothing is the
        state this replaces.

        Created at the host name even though the variable holds the
        sandbox one -- the bind needs a real directory on this side to
        point at.
        """
        with tempfile.TemporaryDirectory() as work:
            _isolation._with_action_tmpdir({}, work, "bwrap")
            self.assertTrue(os.path.isdir(os.path.join(work, "tmp")))


class TestSandboxPathTranslation(unittest.TestCase):
    """The one function every driver relies on to get the boundary right.

    Its failure mode is the reason it raises rather than returning
    something plausible: a path that resolves outside and not inside does
    not fail here, it fails part-way through an hour-long action, with a
    message about a missing file that plainly exists.
    """

    def test_the_work_area_itself_becomes_the_mount_point(self):
        self.assertEqual(
            "/build",
            _isolation.sandbox_path("/scratch/x-1234", "/scratch/x-1234", "bwrap"),
        )

    def test_a_path_under_the_work_area_keeps_its_tail(self):
        self.assertEqual(
            "/build/topdir/BUILD",
            _isolation.sandbox_path(
                "/scratch/x-1234/topdir/BUILD", "/scratch/x-1234", "bwrap"),
        )

    def test_two_different_work_areas_give_the_same_answer(self):
        """Restates the property the whole change exists for."""
        self.assertEqual(
            _isolation.sandbox_path("/scratch/a-01/topdir", "/scratch/a-01", "bwrap"),
            _isolation.sandbox_path(
                "/var/tmp/deeply/nested/b-99/topdir", "/var/tmp/deeply/nested/b-99",
                "bwrap"),
        )

    def test_isolation_none_is_the_identity(self):
        self.assertEqual(
            "/scratch/x-1234/topdir",
            _isolation.sandbox_path(
                "/scratch/x-1234/topdir", "/scratch/x-1234", "none"),
        )

    def test_a_path_outside_the_work_area_is_refused(self):
        """Silence here is what would ship a broken path into an action."""
        with self.assertRaises(ValueError) as caught:
            _isolation.sandbox_path("/etc/passwd", "/scratch/x-1234", "bwrap")
        self.assertIn("/etc/passwd", str(caught.exception))
        self.assertIn("/scratch/x-1234", str(caught.exception))

    def test_a_sibling_with_a_shared_prefix_is_refused(self):
        """The off-by-one a startswith() without the separator would allow."""
        with self.assertRaises(ValueError):
            _isolation.sandbox_path(
                "/scratch/x-1234-other/topdir", "/scratch/x-1234", "bwrap")

    def test_relative_paths_are_resolved_against_the_process_cwd(self):
        with tempfile.TemporaryDirectory() as work:
            os.makedirs(os.path.join(work, "topdir"))
            previous = os.getcwd()
            os.chdir(work)
            try:
                self.assertEqual(
                    "/build/topdir",
                    _isolation.sandbox_path("topdir", work, "bwrap"),
                )
            finally:
                os.chdir(previous)


class TestHostPathsCannotEnterTheSandbox(unittest.TestCase):
    """The static half of the translation, which is worth more than the boots.

    A sandbox path used host-side fails immediately with ENOENT.  A host
    path used inside fails an hour into an expensive action.  Only the
    second direction needs catching, and with a fixed bind it is exactly
    the detectable one: nothing crossing has any business naming the host
    work area.
    """

    WORK = "/scratch/buckos-distro-replay-9f3c1e00"

    def test_a_translated_script_passes(self):
        _isolation._assert_no_host_work_path(
            ["/bin/sh", "-c", "tar -xf /build/image.tar -C /build/root"],
            {"TMPDIR": "/build/tmp"},
            self.WORK,
        )

    def test_an_untranslated_script_stops_the_build(self):
        with self.assertRaises(SystemExit) as caught:
            _isolation._assert_no_host_work_path(
                ["/bin/sh", "-c",
                 "tar -xf {}/image.tar -C /build/root".format(self.WORK)],
                None,
                self.WORK,
            )
        message = str(caught.exception)
        self.assertIn(self.WORK, message)
        self.assertIn("/build", message)
        self.assertIn("argv[2]", message)

    def test_an_untranslated_argument_stops_the_build(self):
        """Not only interpolated scripts: bare argv crosses too."""
        with self.assertRaises(SystemExit) as caught:
            _isolation._assert_no_host_work_path(
                ["/usr/bin/rpmbuild", "--define",
                 "_topdir {}/topdir".format(self.WORK)],
                None,
                self.WORK,
            )
        self.assertIn("argv[2]", str(caught.exception))

    def test_an_untranslated_environment_value_stops_the_build(self):
        """The channel the call-site audit would not have caught."""
        with self.assertRaises(SystemExit) as caught:
            _isolation._assert_no_host_work_path(
                ["/bin/true"],
                {"TMPDIR": "{}/topdir/tmp".format(self.WORK)},
                self.WORK,
            )
        self.assertIn("$TMPDIR", str(caught.exception))

    def test_every_offender_is_named_not_only_the_first(self):
        """A driver that got it wrong once usually got it wrong twice."""
        with self.assertRaises(SystemExit) as caught:
            _isolation._assert_no_host_work_path(
                ["/bin/sh", "-c", "{}/a".format(self.WORK), "{}/b".format(self.WORK)],
                {"TMPDIR": "{}/tmp".format(self.WORK)},
                self.WORK,
            )
        message = str(caught.exception)
        self.assertIn("3 thing(s)", message)
        for where in ("argv[2]", "argv[3]", "$TMPDIR"):
            self.assertIn(where, message)

    def test_a_long_script_is_excerpted_around_the_offence(self):
        """A 4KB script has to name its own bug or nobody reads the message."""
        script = "# filler\n" * 400 + "cp {}/x /tmp/y".format(self.WORK)
        with self.assertRaises(SystemExit) as caught:
            _isolation._assert_no_host_work_path(
                ["/bin/sh", "-c", script], None, self.WORK)
        message = str(caught.exception)
        self.assertIn("cp {}/x".format(self.WORK), message)
        self.assertLess(len(message), len(script))

    def test_isolation_none_is_never_checked(self):
        """That mode has no bind, so the host path is the correct answer.

        Asserted through run_isolated rather than the helper, because the
        property is that the check is not reached at all -- a helper-level
        test would pass even if the call were placed above the early
        return, which is the mistake worth catching.
        """
        with patch("_isolation.run", return_value="ran") as ran:
            result = _isolation.run_isolated(
                ["/bin/sh", "-c", "cat {}/marker".format(self.WORK)],
                "none",
                self.WORK,
                self.WORK,
                None,
                env={"TMPDIR": "{}/tmp".format(self.WORK)},
            )
        self.assertEqual("ran", result)
        ran.assert_called_once()


class TestNoBuildrootShipsTheMountPoint(unittest.TestCase):
    """Mounting over a directory a package owns would hide it silently.

    No buildroot in the fleet ships one, checked across ten on both
    flavors and both architectures -- but that is a fact about today's
    package sets, not a guarantee, so it is asserted before every mount
    rather than written down once.
    """

    def test_a_tree_without_the_directory_passes(self):
        with tempfile.TemporaryDirectory() as sysroot:
            _isolation._assert_no_real_build_dir(sysroot)

    def test_an_empty_directory_passes(self):
        """What the sandbox itself leaves behind, so it cannot be fatal."""
        with tempfile.TemporaryDirectory() as sysroot:
            os.makedirs(os.path.join(sysroot, "build"))
            _isolation._assert_no_real_build_dir(sysroot)

    def test_a_populated_directory_stops_the_build(self):
        with tempfile.TemporaryDirectory() as sysroot:
            os.makedirs(os.path.join(sysroot, "build", "somepackage"))
            with self.assertRaises(SystemExit) as caught:
                _isolation._assert_no_real_build_dir(sysroot)
            self.assertIn("/build", str(caught.exception))

    def test_no_sysroot_is_not_an_error(self):
        """isolation=none passes none, and there is nothing to shadow."""
        _isolation._assert_no_real_build_dir(None)


if __name__ == "__main__":
    unittest.main()
