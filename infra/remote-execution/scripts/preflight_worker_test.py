#!/usr/bin/env python3

import io
import json
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import preflight_worker


class ReporterTest(unittest.TestCase):
    def test_records_have_stable_levels_and_failures(self):
        output = io.StringIO()
        reporter = preflight_worker.Reporter()
        with redirect_stdout(output):
            reporter.passed("one", "line one\nline two")
            reporter.warned("two", "warning")
            reporter.failed("three", "failure")

        self.assertEqual(
            output.getvalue().splitlines(),
            [
                "PASS one line one line two",
                "WARN two warning",
                "FAIL three failure",
            ],
        )
        self.assertEqual(reporter.failures, 1)

    def test_bad_arguments_return_two(self):
        output = io.StringIO()
        with redirect_stdout(output):
            status = preflight_worker.main([])

        self.assertEqual(status, 2)
        self.assertTrue(output.getvalue().startswith("FAIL arguments "))

    def test_accepts_zero_minimum_for_dynamic_inode_filesystems(self):
        args = preflight_worker._parser().parse_args([
            "--worker-user", "worker",
            "--arch", "x86_64",
            "--scratch-root", "/scratch",
            "--probe-sysroot", "/probe",
            "--min-scratch-bytes", "1",
            "--min-scratch-inodes", "0",
        ])

        self.assertEqual(args.min_scratch_inodes, 0)


class ExitStatusTest(unittest.TestCase):
    def _args(self):
        return SimpleNamespace(
            worker_user="worker",
            scratch_root=Path("/scratch"),
            min_scratch_bytes=1,
            min_scratch_inodes=1,
            arch="x86_64",
            emulated_aarch64=False,
            probe_sysroot=Path("/probe"),
        )

    def test_all_required_checks_return_zero(self):
        ranges = preflight_worker.SubordinateRanges(1, 65536, 1, 65536)
        probe = preflight_worker.ProbeSysroot(Path("/probe"), "/usr/bin/python3")
        with (
            patch("preflight_worker.check_identity", return_value=True),
            patch("preflight_worker.check_user_namespace_policy", return_value=True),
            patch("preflight_worker.check_tools", return_value=True),
            patch("preflight_worker.check_subordinate_ranges", return_value=ranges),
            patch("preflight_worker.check_scratch", return_value=Path("/scratch")),
            patch("preflight_worker.check_architecture", return_value=True),
            patch("preflight_worker.check_probe_sysroot", return_value=probe),
            patch("preflight_worker.run_sandbox_probe") as sandbox,
            redirect_stdout(io.StringIO()),
        ):
            status = preflight_worker.run_preflight(self._args(), MagicMock())

        self.assertEqual(status, 0)
        sandbox.assert_called_once()

    def test_failed_prerequisite_returns_one_without_sandbox(self):
        with (
            patch("preflight_worker.check_identity", return_value=False),
            patch("preflight_worker.check_user_namespace_policy", return_value=True),
            patch("preflight_worker.check_tools", return_value=True),
            patch("preflight_worker.check_subordinate_ranges", return_value=None),
            patch("preflight_worker.check_scratch", return_value=Path("/scratch")),
            patch("preflight_worker.check_architecture", return_value=True),
            patch("preflight_worker.check_probe_sysroot", return_value=None),
            patch("preflight_worker.run_sandbox_probe") as sandbox,
            redirect_stdout(io.StringIO()),
        ):
            status = preflight_worker.run_preflight(self._args(), MagicMock())

        self.assertEqual(status, 1)
        sandbox.assert_not_called()


class ArchitectureTest(unittest.TestCase):
    def test_native_architecture_passes(self):
        output = io.StringIO()
        reporter = preflight_worker.Reporter()
        with redirect_stdout(output):
            ok = preflight_worker.check_architecture(
                "x86_64",
                False,
                reporter,
                machine="amd64",
            )

        self.assertTrue(ok)
        self.assertEqual(output.getvalue(), "PASS architecture native x86_64\n")

    def test_emulation_requires_enabled_persistent_handler(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            interpreter = root / "qemu-aarch64"
            interpreter.write_bytes(b"probe")
            interpreter.chmod(0o755)
            handler = root / "handler"
            handler.write_text(
                "enabled\ninterpreter {}\nflags: OCF\noffset 0\n".format(
                    interpreter
                ),
                encoding="utf-8",
            )
            output = io.StringIO()
            reporter = preflight_worker.Reporter()
            with redirect_stdout(output):
                ok = preflight_worker.check_architecture(
                    "aarch64",
                    True,
                    reporter,
                    machine="x86_64",
                    handler=handler,
                )

        self.assertTrue(ok)
        self.assertEqual(reporter.failures, 0)
        self.assertIn("PASS architecture-binfmt", output.getvalue())
        self.assertIn("PASS architecture emulated aarch64 on x86_64", output.getvalue())

    def test_emulation_rejects_nonpersistent_handler(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            interpreter = root / "qemu-aarch64"
            interpreter.write_bytes(b"probe")
            interpreter.chmod(0o755)
            handler = root / "handler"
            handler.write_text(
                "enabled\ninterpreter {}\nflags: OC\n".format(interpreter),
                encoding="utf-8",
            )
            reporter = preflight_worker.Reporter()
            with redirect_stdout(io.StringIO()):
                ok = preflight_worker.check_architecture(
                    "aarch64",
                    True,
                    reporter,
                    machine="x86_64",
                    handler=handler,
                )

        self.assertFalse(ok)
        self.assertEqual(reporter.failures, 1)


class UserNamespacePolicyTest(unittest.TestCase):
    def test_requires_enabled_namespace_switches(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            maximum = root / "max_user_namespaces"
            clone = root / "unprivileged_userns_clone"
            maximum.write_text("1024\n", encoding="utf-8")
            clone.write_text("1\n", encoding="utf-8")
            reporter = preflight_worker.Reporter()
            with redirect_stdout(io.StringIO()):
                ok = preflight_worker.check_user_namespace_policy(
                    reporter,
                    maximum_path=maximum,
                    clone_path=clone,
                )

        self.assertTrue(ok)
        self.assertEqual(reporter.failures, 0)


class ProbeSysrootTest(unittest.TestCase):
    def _elf(self, path, machine):
        header = bytearray(20)
        header[:4] = b"\x7fELF"
        header[5] = 1
        header[18:20] = machine.to_bytes(2, "little")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(header)
        path.chmod(0o755)

    def test_resolves_absolute_symlink_inside_sysroot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "opt/python/bin/python3"
            self._elf(target, 62)
            link = root / "usr/bin/python3"
            link.parent.mkdir(parents=True)
            link.symlink_to("/opt/python/bin/python3")

            resolved = preflight_worker._resolve_in_root(root, "/usr/bin/python3")

        self.assertEqual(resolved, target)

    def test_checks_probe_python_architecture(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("proc", "dev", "tmp"):
                (root / name).mkdir()
            self._elf(root / "usr/bin/python3", 183)
            reporter = preflight_worker.Reporter()
            with redirect_stdout(io.StringIO()):
                probe = preflight_worker.check_probe_sysroot(
                    root,
                    "aarch64",
                    reporter,
                )

        self.assertEqual(probe.python_path, "/usr/bin/python3")
        self.assertEqual(reporter.failures, 0)

    def test_rejects_wrong_probe_architecture(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("proc", "dev", "tmp"):
                (root / name).mkdir()
            self._elf(root / "usr/bin/python3", 62)
            reporter = preflight_worker.Reporter()
            with redirect_stdout(io.StringIO()):
                probe = preflight_worker.check_probe_sysroot(
                    root,
                    "aarch64",
                    reporter,
                )

        self.assertIsNone(probe)
        self.assertEqual(reporter.failures, 1)

    def test_rejects_scratch_nested_below_probe_root(self):
        reporter = preflight_worker.Reporter()
        probe = preflight_worker.ProbeSysroot(Path("/srv/probe"), "/usr/bin/python3")
        with redirect_stdout(io.StringIO()):
            ok = preflight_worker.check_probe_separation(
                probe,
                Path("/srv/probe/scratch"),
                reporter,
            )

        self.assertFalse(ok)
        self.assertEqual(reporter.failures, 1)


class ScratchTest(unittest.TestCase):
    def _filesystem(self, *, total_inodes=1000, available_inodes=200):
        return SimpleNamespace(
            f_flag=0,
            f_bavail=100,
            f_frsize=4096,
            f_files=total_inodes,
            f_favail=available_inodes,
        )

    def _check(self, temporary, filesystem, minimum_inodes):
        reporter = preflight_worker.Reporter()
        output = io.StringIO()
        with (
            patch("preflight_worker.os.statvfs", return_value=filesystem),
            redirect_stdout(output),
        ):
            scratch = preflight_worker.check_scratch(
                Path(temporary),
                1,
                minimum_inodes,
                reporter,
            )
        return scratch, reporter, output.getvalue()

    def test_enforces_available_bytes_and_inodes(self):
        filesystem = self._filesystem()
        with tempfile.TemporaryDirectory() as temporary:
            reporter = preflight_worker.Reporter()
            output = io.StringIO()
            with (
                patch("preflight_worker.os.statvfs", return_value=filesystem),
                redirect_stdout(output),
            ):
                scratch = preflight_worker.check_scratch(
                    Path(temporary),
                    100 * 4096,
                    200,
                    reporter,
                )
            leftovers = list(Path(temporary).iterdir())

        self.assertIsNotNone(scratch)
        self.assertEqual(reporter.failures, 0)
        self.assertEqual(leftovers, [])
        self.assertIn("PASS scratch-operations", output.getvalue())
        self.assertIn("PASS scratch-cleanup", output.getvalue())

    def test_rejects_insufficient_inode_capacity(self):
        filesystem = self._filesystem(available_inodes=199)
        with tempfile.TemporaryDirectory() as temporary:
            scratch, reporter, _output = self._check(temporary, filesystem, 200)

        self.assertIsNone(scratch)
        self.assertEqual(reporter.failures, 1)

    def test_accepts_dynamic_inode_filesystem_with_zero_minimum(self):
        filesystem = self._filesystem(total_inodes=0, available_inodes=0)
        with tempfile.TemporaryDirectory() as temporary:
            scratch, reporter, output = self._check(temporary, filesystem, 0)

        self.assertIsNotNone(scratch)
        self.assertEqual(reporter.failures, 0)
        self.assertIn("PASS scratch-inodes model=dynamic required=0", output)

    def test_rejects_nonzero_minimum_for_dynamic_inode_filesystem(self):
        filesystem = self._filesystem(total_inodes=0, available_inodes=0)
        with tempfile.TemporaryDirectory() as temporary:
            scratch, reporter, output = self._check(temporary, filesystem, 1)

        self.assertIsNone(scratch)
        self.assertEqual(reporter.failures, 1)
        self.assertIn("FAIL scratch-inodes model=dynamic required=1", output)

    def test_rejects_zero_minimum_for_fixed_inode_filesystem(self):
        filesystem = self._filesystem()
        with tempfile.TemporaryDirectory() as temporary:
            scratch, reporter, output = self._check(temporary, filesystem, 0)

        self.assertIsNone(scratch)
        self.assertEqual(reporter.failures, 1)
        self.assertIn("FAIL scratch-inodes model=fixed", output)

    def test_rejects_scratch_probe_create_failure(self):
        filesystem = self._filesystem()
        with tempfile.TemporaryDirectory() as temporary:
            with patch(
                "preflight_worker.tempfile.mkdtemp",
                side_effect=OSError("create failed"),
            ):
                scratch, reporter, output = self._check(temporary, filesystem, 1)

        self.assertIsNone(scratch)
        self.assertEqual(reporter.failures, 1)
        self.assertIn("FAIL scratch-operations create failed", output)

    def test_rejects_scratch_probe_write_failure(self):
        filesystem = self._filesystem()
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(
                Path,
                "open",
                side_effect=OSError("write failed"),
            ):
                scratch, reporter, output = self._check(temporary, filesystem, 1)

        self.assertIsNone(scratch)
        self.assertEqual(reporter.failures, 1)
        self.assertIn("FAIL scratch-operations write failed", output)
        self.assertIn("PASS scratch-cleanup", output)

    def test_rejects_scratch_probe_hardlink_failure(self):
        filesystem = self._filesystem()
        with tempfile.TemporaryDirectory() as temporary:
            with patch(
                "preflight_worker.os.link",
                side_effect=OSError("hardlink failed"),
            ):
                scratch, reporter, output = self._check(temporary, filesystem, 1)

        self.assertIsNone(scratch)
        self.assertEqual(reporter.failures, 1)
        self.assertIn("FAIL scratch-operations hardlink failed", output)
        self.assertIn("PASS scratch-cleanup", output)

    def test_rejects_scratch_probe_rename_failure(self):
        filesystem = self._filesystem()
        with tempfile.TemporaryDirectory() as temporary:
            with patch(
                "preflight_worker.os.rename",
                side_effect=OSError("rename failed"),
            ):
                scratch, reporter, output = self._check(temporary, filesystem, 1)

        self.assertIsNone(scratch)
        self.assertEqual(reporter.failures, 1)
        self.assertIn("FAIL scratch-operations rename failed", output)
        self.assertIn("PASS scratch-cleanup", output)

    def test_rejects_scratch_probe_cleanup_failure(self):
        filesystem = self._filesystem()
        with tempfile.TemporaryDirectory() as temporary:
            with patch(
                "preflight_worker.shutil.rmtree",
                side_effect=OSError("cleanup failed"),
            ):
                scratch, reporter, output = self._check(temporary, filesystem, 1)

        self.assertIsNone(scratch)
        self.assertEqual(reporter.failures, 1)
        self.assertIn("FAIL scratch-cleanup cleanup failed", output)


class SubordinateRangeTest(unittest.TestCase):
    def test_requires_full_ranges(self):
        isolation = SimpleNamespace(
            _UID_MAP="/etc/subuid",
            _GID_MAP="/etc/subgid",
            _subid_range=MagicMock(side_effect=[(100000, 65536), (200000, 12)]),
            subid_mapping_available=MagicMock(return_value=True),
        )
        reporter = preflight_worker.Reporter()
        with redirect_stdout(io.StringIO()):
            ranges = preflight_worker.check_subordinate_ranges(isolation, reporter)

        self.assertIsNone(ranges)
        self.assertEqual(reporter.failures, 1)


class SandboxProbeTest(unittest.TestCase):
    def _probe_root(self, parent):
        root = parent / "probe-root"
        for name in ("proc", "dev", "tmp", "usr/bin"):
            (root / name).mkdir(parents=True, exist_ok=True)
        return root

    def test_calls_production_launcher_and_removes_probe_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            scratch = base / "scratch"
            scratch.mkdir()
            probe = preflight_worker.ProbeSysroot(
                self._probe_root(base),
                "/usr/bin/python3",
            )
            ranges = preflight_worker.SubordinateRanges(
                100000,
                65536,
                200000,
                65536,
            )
            isolation = MagicMock()

            def run_isolated(command, mode, **kwargs):
                self.assertEqual(mode, "bwrap")
                self.assertEqual(kwargs["work"], kwargs["chdir"])
                self.assertTrue(Path(kwargs["sysroot"]).is_dir())
                result = Path(command[command.index("--result") + 1])
                result.write_text(
                    json.dumps([
                        {
                            "level": "PASS",
                            "name": "namespace-user",
                            "detail": "changed",
                        },
                    ]),
                    encoding="utf-8",
                )
                work = Path(kwargs["work"])
                (work / "owner-8-12").touch()
                (work / "owner-65534").touch()

            def remove_tree(path):
                shutil.rmtree(path)
                return True

            isolation.run_isolated.side_effect = run_isolated
            isolation.remove_tree.side_effect = remove_tree
            reporter = preflight_worker.Reporter()
            output = io.StringIO()
            with (
                patch(
                    "preflight_worker._check_external_owner",
                    return_value="mapped",
                ),
                redirect_stdout(output),
            ):
                preflight_worker.run_sandbox_probe(
                    isolation,
                    probe,
                    scratch,
                    ranges,
                    reporter,
                )

        self.assertEqual(reporter.failures, 0)
        self.assertIn(
            "PASS sandbox production Bubblewrap launcher exited 0",
            output.getvalue(),
        )
        self.assertIn("PASS cleanup scratch probe tree removed", output.getvalue())
        isolation.run_isolated.assert_called_once()
        isolation.remove_tree.assert_called_once()

    def test_launcher_failure_still_cleans_up(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            scratch = base / "scratch"
            scratch.mkdir()
            probe = preflight_worker.ProbeSysroot(
                self._probe_root(base),
                "/usr/bin/python3",
            )
            ranges = preflight_worker.SubordinateRanges(1, 65536, 1, 65536)
            isolation = MagicMock()
            isolation.run_isolated.side_effect = subprocess.CalledProcessError(
                7,
                ["bwrap"],
            )

            def remove_tree(path):
                shutil.rmtree(path)
                return True

            isolation.remove_tree.side_effect = remove_tree
            reporter = preflight_worker.Reporter()
            output = io.StringIO()
            with redirect_stdout(output):
                preflight_worker.run_sandbox_probe(
                    isolation,
                    probe,
                    scratch,
                    ranges,
                    reporter,
                )

        self.assertEqual(reporter.failures, 1)
        self.assertIn("FAIL sandbox production launcher exited 7", output.getvalue())
        self.assertIn("PASS cleanup scratch probe tree removed", output.getvalue())
        isolation.remove_tree.assert_called_once()

    def test_cleanup_failure_is_fatal_and_best_effort_removes_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            scratch = base / "scratch"
            scratch.mkdir()
            probe = preflight_worker.ProbeSysroot(
                self._probe_root(base),
                "/usr/bin/python3",
            )
            ranges = preflight_worker.SubordinateRanges(1, 65536, 1, 65536)
            isolation = MagicMock()

            def run_isolated(command, _mode, **kwargs):
                result = Path(command[command.index("--result") + 1])
                result.write_text("[]", encoding="utf-8")
                work = Path(kwargs["work"])
                (work / "owner-8-12").touch()
                (work / "owner-65534").touch()

            isolation.run_isolated.side_effect = run_isolated
            isolation.remove_tree.return_value = False
            reporter = preflight_worker.Reporter()
            output = io.StringIO()
            with (
                patch(
                    "preflight_worker._check_external_owner",
                    return_value="mapped",
                ),
                redirect_stdout(output),
            ):
                preflight_worker.run_sandbox_probe(
                    isolation,
                    probe,
                    scratch,
                    ranges,
                    reporter,
                )

            leftovers = list(scratch.iterdir())

        self.assertEqual(reporter.failures, 1)
        self.assertIn("FAIL cleanup scratch probe tree remains", output.getvalue())
        self.assertEqual(leftovers, [])


class SandboxResultTest(unittest.TestCase):
    def test_rejects_invalid_records(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = Path(temporary) / "result.json"
            result.write_text('{"level":"PASS"}', encoding="utf-8")
            with self.assertRaisesRegex(
                preflight_worker.CheckError,
                "not a list",
            ):
                preflight_worker._record_inside_results(
                    result,
                    preflight_worker.Reporter(),
                )


class InsideProbeTest(unittest.TestCase):
    def test_emits_every_required_sandbox_record(self):
        outer = {
            name: "outer-{}".format(name)
            for name in ("user", "net", "pid", "ipc", "mnt")
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = root / "result.json"
            with (
                patch("preflight_worker._mountinfo", return_value=[]),
                patch("preflight_worker._inside_namespace", return_value="changed"),
                patch("preflight_worker._inside_loopback", return_value="isolated"),
                patch("preflight_worker._inside_ownership", return_value="owned"),
                patch("preflight_worker._inside_proc", return_value="proc"),
                patch("preflight_worker._inside_dev", return_value="dev"),
                patch("preflight_worker._inside_tmp", return_value="tmp"),
                patch("preflight_worker._inside_exposure", return_value="private"),
            ):
                status = preflight_worker.inside_probe([
                    "--result", str(result),
                    "--work", str(root),
                    "--outer-namespaces", json.dumps(outer),
                ])
            records = json.loads(result.read_text(encoding="utf-8"))

        self.assertEqual(status, 0)
        self.assertEqual(
            [record["name"] for record in records],
            [
                "namespace-user",
                "namespace-net",
                "namespace-pid",
                "namespace-ipc",
                "namespace-mnt",
                "loopback",
                "ownership-8-12",
                "ownership-65534",
                "proc",
                "dev",
                "tmp",
                "exposure",
            ],
        )
        self.assertTrue(all(record["level"] == "PASS" for record in records))


class ExposureTest(unittest.TestCase):
    def test_accepts_only_expected_sandbox_mounts(self):
        mounts = [
            preflight_worker.MountInfo("/", frozenset(), "ext4", frozenset()),
            preflight_worker.MountInfo("/proc", frozenset(), "proc", frozenset()),
            preflight_worker.MountInfo("/dev", frozenset(), "tmpfs", frozenset()),
            preflight_worker.MountInfo("/dev/pts", frozenset(), "devpts", frozenset()),
            preflight_worker.MountInfo("/tmp", frozenset(), "tmpfs", frozenset()),
            preflight_worker.MountInfo(
                "/var/lib/worker/probe",
                frozenset(),
                "ext4",
                frozenset(),
            ),
        ]
        with patch("preflight_worker.os.path.lexists", return_value=False):
            detail = preflight_worker._inside_exposure(
                mounts,
                Path("/var/lib/worker/probe"),
            )

        self.assertEqual(detail, "unexpected_mounts=0 container_sockets=0")

    def test_rejects_developer_home_mount(self):
        mounts = [
            preflight_worker.MountInfo("/", frozenset(), "ext4", frozenset()),
            preflight_worker.MountInfo(
                "/home/developer",
                frozenset(),
                "ext4",
                frozenset(),
            ),
        ]
        with self.assertRaisesRegex(preflight_worker.CheckError, "unexpected mounts"):
            preflight_worker._inside_exposure(
                mounts,
                Path("/var/lib/worker/probe"),
            )


if __name__ == "__main__":
    unittest.main()
