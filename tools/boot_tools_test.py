#!/usr/bin/env python3

import contextlib
import hashlib
import io
import os
import signal
import tarfile
import tempfile
import unittest
from unittest import mock

from _deb import fakeroot_command, stage_fakeroot_runtime
from _rpm import fabricated_mount_components
from deb_rootfs_install import (
    archive_script,
    bootstrap_script,
    cleanup_script,
    normalize_merged_usr_script,
    transaction_script,
)
from iso_build import (
    _bios_script,
    _efi_script,
    _fat_volume_id,
    _pin_tree_times,
    _write_md5sums,
    _xorriso_script,
)
from iso_boot_test import (
    BootCapture,
    BootFailure,
    capture_boot,
    complete_marker_from_output,
    main as iso_boot_main,
    parse_marker,
    qemu_command,
    validate,
    validate_production_capture,
    validate_verification_capture,
)
from initramfs_build import _install_image_tool_script, stage_image_tool
from rootfs_overlay import append_file, parse_file
from squashfs_build import _build_mksquashfs_script, _mksquashfs_script, write_pseudo


ISO_MATRIX = (
    ("fedora", "44", "rpm", True),
    ("fedora", "45", "rpm", True),
    ("centos", "9", "rpm", True),
    ("centos", "10", "rpm", True),
    ("centos-hyperscale", "9", "rpm", True),
    ("centos-hyperscale", "10", "rpm", True),
    ("debian", "13", "debian", False),
    ("ubuntu", "26.04", "ubuntu", False),
)
ARCHITECTURES = ("x86_64", "aarch64")
IMAGE_VARIANTS = (None, "prebuilt")


def repo_root():
    for start in (os.getcwd(), os.path.dirname(os.path.abspath(__file__))):
        path = start
        while True:
            if os.path.isfile(os.path.join(path, ".buckroot")):
                return path
            parent = os.path.dirname(path)
            if parent == path:
                break
            path = parent
    raise AssertionError("cannot locate repository root")


def load_iso_test_graph():
    rules = {
        "iso_boot_test": [],
        "iso_image": [],
        "rootfs_overlay": [],
        "squashfs": [],
    }

    def recorder(kind):
        def record(**attributes):
            rules[kind].append(attributes)

        return record

    namespace = {
        "execution_compatible_with": lambda architecture: [architecture],
        "iso_boot_test": recorder("iso_boot_test"),
        "iso_image": recorder("iso_image"),
        "load": lambda *_args, **_kwargs: None,
        "rootfs_overlay": recorder("rootfs_overlay"),
        "squashfs": recorder("squashfs"),
        "target_platform": lambda flavor, release, architecture: (
            flavor,
            release,
            architecture,
        ),
    }
    path = os.path.join(repo_root(), "defs", "iso_tests.bzl")
    with open(path, encoding="utf-8") as stream:
        source = stream.read()
    exec(compile(source, path, "exec"), namespace)
    namespace["all_live_iso_boot_tests"]()
    return rules


class TestRootfsOverlay(unittest.TestCase):
    def test_appends_deterministic_root_owned_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source")
            archive_path = os.path.join(tmp, "rootfs.tar")
            with open(source, "wb") as stream:
                stream.write(b"payload\n")
            with tarfile.open(archive_path, "w"):
                pass
            with tarfile.open(archive_path, "a") as archive:
                append_file(archive, "usr/bin/check", 0o755, source, 123)
            with tarfile.open(archive_path) as archive:
                member = archive.getmember("usr/bin/check")
                self.assertEqual(0o755, member.mode)
                self.assertEqual(0, member.uid)
                self.assertEqual(0, member.gid)
                self.assertEqual(123, member.mtime)
                self.assertEqual(b"payload\n", archive.extractfile(member).read())

    def test_rejects_parent_traversal(self):
        with self.assertRaisesRegex(Exception, "inside the rootfs"):
            parse_file("../escape:0644:file")


class TestDebRootfsTransaction(unittest.TestCase):
    def test_fakeroot_command_preserves_state_across_phases(self):
        runtime = {
            "fakeroot-sysv": "/work/fakeroot/fakeroot-sysv",
            "faked-sysv": "/work/fakeroot/faked-sysv",
            "library": "/work/fakeroot/libfakeroot-sysv.so",
            "state": "/work/fakeroot/state",
        }
        initial = fakeroot_command(runtime, ["dpkg-deb", "--extract"])
        resumed = fakeroot_command(runtime, ["tar", "--create"], load=True)
        self.assertNotIn("-i", initial)
        self.assertIn("-s", initial)
        self.assertIn("-i", resumed)
        self.assertEqual(resumed[-2:], ["tar", "--create"])

    def test_an_rpm_buildroot_runs_shared_image_actions_unwrapped(self):
        # fakeroot-sysv and Debian's multiarch libfakeroot layout do not
        # exist in an RPM buildroot, and squashfs and initramfs are shared by
        # every flavor.  Requiring it there took the whole RPM image pipeline
        # down, unnoticed, because no RPM image had been built since.
        with tempfile.TemporaryDirectory() as tmp:
            buildroot = os.path.join(tmp, "buildroot")
            os.makedirs(os.path.join(buildroot, "usr", "bin"))
            work = os.path.join(tmp, "work")
            os.makedirs(work)

            self.assertIsNone(
                stage_fakeroot_runtime(buildroot, work, required=False)
            )
            self.assertEqual(
                ["/bin/sh", "-c", "true"],
                fakeroot_command(None, ["/bin/sh", "-c", "true"]),
            )
            self.assertFalse(os.path.exists(os.path.join(work, "fakeroot")))

    def test_a_debian_buildroot_without_fakeroot_still_fails_closed(self):
        # The Debian tools that genuinely need it keep the hard requirement,
        # so tolerating absence in the shared tools cannot let a broken
        # Debian buildroot through unnoticed.
        with tempfile.TemporaryDirectory() as tmp:
            buildroot = os.path.join(tmp, "buildroot")
            os.makedirs(os.path.join(buildroot, "usr", "bin"))
            work = os.path.join(tmp, "work")
            os.makedirs(work)

            with self.assertRaisesRegex(SystemExit, "fakeroot-sysv"):
                stage_fakeroot_runtime(buildroot, work)

    def test_bootstrap_extracts_before_running_maintainer_scripts(self):
        script = bootstrap_script("/target", "/debs")
        self.assertLess(script.index("dpkg-deb --extract"), script.index("/usr/bin/dpkg"))

    def test_transaction_uses_target_tmp_and_configures_everything(self):
        script = transaction_script("/debs")
        self.assertIn("export TMPDIR=/tmp", script)
        self.assertIn("dpkg --force-confnew --configure -a", script)
        self.assertLess(
            script.index("update-initramfs.buckos-real"),
            script.index("dpkg --force-confnew --configure -a"),
        )
        self.assertLess(
            script.index("dpkg --force-confnew --configure -a"),
            script.rindex("update-initramfs.buckos-real"),
        )

    def test_transaction_scrubs_the_content_that_records_build_time(self):
        # Pinned mtimes do not reach file contents: dpkg and
        # update-alternatives write wall-clock stamps into their logs, and
        # ldconfig's cache records per-file inode data.  Measured against two
        # runs of the Debian live rootfs, these were three of the four
        # differences in an otherwise byte-identical 13,554-member archive.
        script = transaction_script("/debs")
        for scrubbed in (
            ": > /var/log/dpkg.log",
            ": > /var/log/alternatives.log",
            "rm -f /var/cache/ldconfig/aux-cache",
        ):
            self.assertIn(scrubbed, script)
        self.assertLess(
            script.index("dpkg --force-confnew --configure -a"),
            script.index(": > /var/log/dpkg.log"),
        )

    def test_archive_drops_the_scratch_mount_left_inside_the_payload(self):
        # The transaction runs against the target, so the sandbox creates the
        # work bind mount inside the image and leaves a directory named after
        # this build's scratch path.  It was the fourth difference.
        script = archive_script(
            "/work/rootfs", "/work/rootfs.tar", "1700000000", ["/work/rootfs/work"],
        )
        self.assertIn("rm -rf /work/rootfs/work", script)
        self.assertLess(script.index("rm -rf /work/rootfs/work"), script.index("tar --create"))

    def test_every_invented_component_is_pruned_not_only_the_leaf(self):
        # Bubblewrap creates every missing component of the bind path, not
        # just the last one.  The default scratch root hides this because the
        # image already ships /var/tmp, so exactly one is invented; a deeper
        # BUCKOS_SCRATCH_ROOT leaves the parents behind in the payload.
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "rootfs")
            os.makedirs(os.path.join(target, "var", "tmp"))

            self.assertEqual(
                [os.path.join(target, "var/tmp/buckos-x")],
                fabricated_mount_components(target, "/var/tmp/buckos-x"),
            )
            self.assertEqual(
                [
                    os.path.join(target, "var/tmp/alt/nested"),
                    os.path.join(target, "var/tmp/alt"),
                ],
                fabricated_mount_components(target, "/var/tmp/alt/nested"),
            )

    def test_a_directory_the_image_owns_is_never_pruned(self):
        # The list is what pruning acts on, so a shipped directory appearing
        # in it would delete /var/tmp out of the payload.
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "rootfs")
            os.makedirs(os.path.join(target, "var", "tmp"))

            fabricated = fabricated_mount_components(target, "/var/tmp/buckos-x")

            self.assertNotIn(os.path.join(target, "var"), fabricated)
            self.assertNotIn(os.path.join(target, "var/tmp"), fabricated)

    def test_archive_drops_the_timestamps_mtime_does_not_pin(self):
        # posix extended headers carry atime and ctime at nanosecond
        # precision.  --mtime pins neither, and nothing in the inputs
        # determines them, so leaving them in defeats every other measure.
        script = archive_script("/work/rootfs", "/work/rootfs.tar", "1700000000", "/work")
        self.assertIn("--pax-option=delete=atime,delete=ctime", script)

    def test_normalizes_merged_usr_before_target_execution(self):
        script = bootstrap_script("/target", "/debs")
        self.assertIn('cp -a "$path/." "$usr/"', script)
        self.assertIn('ln -s "usr/$directory" "$path"', script)
        self.assertLess(script.index("dpkg-deb --extract"), script.index("cp -a"))
        self.assertLess(script.index("cp -a"), script.index("test -x /target/bin/sh"))

    def test_normalization_is_scoped_to_the_requested_root(self):
        script = normalize_merged_usr_script("/target root")
        self.assertIn("root='/target root'", script)
        self.assertIn('path="${root%/}/$directory"', script)

    def test_cleanup_runs_inside_owning_namespace(self):
        self.assertEqual("set -e\nrm -rf /target", cleanup_script("/target"))


class TestInitramfsTools(unittest.TestCase):
    def test_stages_binary_seeded_construction_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            buildroot = os.path.join(tmp, "buildroot")
            work = os.path.join(tmp, "work")
            source = os.path.join(buildroot, "usr", "bin", "cp")
            os.makedirs(os.path.dirname(source))
            os.makedirs(work)
            with open(source, "wb") as stream:
                stream.write(b"binary-seeded-cp")
            os.chmod(source, 0o755)

            destination = stage_image_tool(buildroot, work, "cp")

            with open(destination, "rb") as stream:
                self.assertEqual(b"binary-seeded-cp", stream.read())
            self.assertTrue(os.access(destination, os.X_OK))

    def test_installs_tool_only_in_ephemeral_root(self):
        script = _install_image_tool_script(
            "/work/image-tools-bin/cp", "/work/root", "cp"
        )
        self.assertEqual(
            "/work/image-tools-bin/cp --preserve=mode,timestamps "
            "/work/image-tools-bin/cp /work/root/usr/bin/cp",
            script,
        )


class TestSquashfsScratchLeak(unittest.TestCase):
    def test_the_relabel_sandbox_root_is_pruned_of_its_own_mountpoint(self):
        # The relabel step is the only one whose sandbox root is the image
        # rather than a buildroot, so it is the only one where bubblewrap
        # invents the work bind mountpoint inside the payload.  Under Buck
        # the rule passes no --work, so the scratch path is random per run:
        # every RPM live image both differed from the last and shipped an
        # empty directory named after its build machine.
        with tempfile.TemporaryDirectory() as tmp:
            image = os.path.join(tmp, "root")
            os.makedirs(os.path.join(image, "var", "tmp"))
            work = "/var/tmp/buckos-distro-squashfs-abc123"

            fabricated = fabricated_mount_components(image, work)

            self.assertEqual([os.path.join(image, "var/tmp/buckos-distro-squashfs-abc123")], fabricated)
            self.assertNotIn(os.path.join(image, "var/tmp"), fabricated)
            self.assertNotIn(os.path.join(image, "var"), fabricated)

    def test_relabel_records_before_the_sandbox_and_prunes_after(self):
        # Ordering is the property. Recording has to happen before
        # bubblewrap invents the mountpoint, or every component looks
        # fabricated; pruning has to happen after, or there is nothing to
        # remove. Both live in _relabel, so read that function rather than
        # the file, which would compare definition order instead.
        source = os.path.join(repo_root(), "tools", "squashfs_build.py")
        with open(source, encoding="utf-8") as handle:
            text = handle.read()
        body = text[text.index("def _relabel("):]
        body = body[: body.index("\ndef ")]

        record = body.index("fabricated_mount_components(root, work)")
        sandbox = body.index("_matchpathcon_script(work)")
        prune = body.index("shutil.rmtree(path, ignore_errors=True)")

        self.assertLess(record, sandbox, "recorded after the sandbox ran")
        self.assertLess(sandbox, prune, "pruned before the sandbox ran")


class TestSquashfsPseudoFile(unittest.TestCase):
    def test_skips_archive_root_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            contexts = os.path.join(tmp, "contexts")
            pseudo = os.path.join(tmp, "pseudo")
            with open(contexts, "w", encoding="utf-8") as stream:
                stream.write("/.\tsystem_u:object_r:root_t:s0\n")
                stream.write("/usr\tsystem_u:object_r:usr_t:s0\n")
                stream.write("/afs\tsystem_u:object_r:afs_t:s0\n")

            written, skipped = write_pseudo(contexts, pseudo)

            self.assertEqual((written, skipped), (2, 0))
            with open(pseudo, encoding="utf-8") as stream:
                self.assertEqual(
                    stream.read(),
                    "usr x security.selinux=system_u:object_r:usr_t:s0\n"
                    "afs x security.selinux=system_u:object_r:afs_t:s0\n",
                )

    def test_builds_xattr_capable_tool_without_overriding_makefile_flags(self):
        script = _build_mksquashfs_script(
            "/work/source.tar", "/work", "/work/mksquashfs"
        )
        self.assertIn("CONFIG=1", script)
        self.assertIn("ZSTD_SUPPORT=1", script)
        self.assertIn("XATTR_SUPPORT=1", script)
        self.assertIn("EXTRA_CFLAGS=", script)
        self.assertNotIn(" CFLAGS=", script)

    def test_uses_explicit_mksquashfs_when_provided(self):
        args = type("Args", (), {
            "block_size": "",
            "compressor": "zstd",
            "exclude": [],
            "processors": "",
        })()
        script = _mksquashfs_script(
            args, "/root", "/image", "/pseudo", "/work/mksquashfs"
        )
        self.assertIn("MKSQUASHFS=/work/mksquashfs", script)
        self.assertNotIn("for _c in", script)


class TestIsoBootMarker(unittest.TestCase):
    def test_parses_and_validates_marker(self):
        fields = parse_marker(
            "BUCKOS_VERIFY flavor=fedora version=45 arch=aarch64 "
            "pid1=systemd failed=0 selinux=Enforcing avc=0\n"
        )
        args = type("Args", (), {
            "expected_flavor": "fedora",
            "expected_version": "45",
            "architecture": "aarch64",
            "expect_selinux": True,
        })()
        self.assertEqual([], validate(args, fields))

    def test_reports_mismatched_architecture(self):
        args = type("Args", (), {
            "expected_flavor": "debian",
            "expected_version": "13",
            "architecture": "aarch64",
            "expect_selinux": False,
        })()
        errors = validate(args, {
            "flavor": "debian",
            "version": "13",
            "arch": "x86_64",
            "pid1": "systemd",
            "failed": "0",
            "avc": "0",
        })
        self.assertRegex("; ".join(errors), "arch")

    def test_reports_each_mismatched_marker_field(self):
        args = type("Args", (), {
            "expected_flavor": "fedora",
            "expected_version": "45",
            "architecture": "aarch64",
            "expect_selinux": True,
        })()
        valid = {
            "flavor": "fedora",
            "version": "45",
            "arch": "aarch64",
            "pid1": "systemd",
            "failed": "0",
            "selinux": "Enforcing",
            "avc": "0",
        }
        cases = (
            ("flavor", "ubuntu"),
            ("version", "44"),
            ("pid1", "busybox"),
            ("failed", "1"),
            ("selinux", "Permissive"),
            ("avc", "1"),
        )

        for field, wrong_value in cases:
            with self.subTest(field=field):
                fields = dict(valid)
                fields[field] = wrong_value
                self.assertEqual(
                    ["{}: expected {!r}, got {!r}".format(
                        field,
                        valid[field],
                        wrong_value,
                    )],
                    validate(args, fields),
                )

    def test_waits_for_complete_marker_line(self):
        marker = (
            "BUCKOS_VERIFY flavor=debian version=13 arch=x86_64 "
            "pid1=systemd failed=0 selinux=not-installed avc=0"
        )
        self.assertFalse(complete_marker_from_output(marker))
        self.assertTrue(complete_marker_from_output(marker + "\n"))


class TestPairedIsoBoot(unittest.TestCase):
    @staticmethod
    def args(timeout=1):
        return type("Args", (), {
            "qemu": "/bin/true",
            "architecture": "x86_64",
            "firmware": "bios",
            "firmware_path": "",
            "firmware_vars": "",
            "timeout": timeout,
        })()

    def assert_process_gone(self, path):
        with open(path, encoding="utf-8") as stream:
            pid = int(stream.read())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_main_boots_distinct_isos_and_reports_production_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            production = os.path.join(tmp, "production.iso")
            verification = os.path.join(tmp, "verification.iso")
            with open(production, "wb") as stream:
                stream.write(b"exact production bytes")
            with open(verification, "wb") as stream:
                stream.write(b"different verification bytes")

            captures = (
                BootCapture("buckos login: ", False, -9),
                BootCapture(
                    "BUCKOS_VERIFY flavor=debian version=13 arch=x86_64 "
                    "pid1=systemd failed=0 selinux=not-installed avc=0\n",
                    False,
                    -9,
                ),
            )
            stdout = io.StringIO()
            with mock.patch(
                "iso_boot_test.capture_boot",
                side_effect=captures,
            ) as boot, contextlib.redirect_stdout(stdout):
                iso_boot_main([
                    "--production-iso", production,
                    "--verification-iso", verification,
                    "--arch", "x86_64",
                    "--firmware", "bios",
                    "--expected-flavor", "debian",
                    "--expected-version", "13",
                    "--qemu", "/bin/true",
                ])

            self.assertEqual(2, boot.call_count)
            self.assertEqual(os.path.realpath(production), boot.call_args_list[0].args[1])
            self.assertEqual("exact-media", boot.call_args_list[0].args[2])
            self.assertEqual(os.path.realpath(verification), boot.call_args_list[1].args[1])
            self.assertEqual("verification", boot.call_args_list[1].args[2])
            production_digest = hashlib.sha256(b"exact production bytes").hexdigest()
            verification_digest = hashlib.sha256(b"different verification bytes").hexdigest()
            self.assertIn(
                "BUCKOS_PRODUCTION_ISO sha256={}".format(production_digest),
                stdout.getvalue(),
            )
            self.assertNotIn(verification_digest, stdout.getvalue())

    def test_same_iso_is_rejected_as_exact_media_wiring(self):
        with tempfile.TemporaryDirectory() as tmp:
            iso = os.path.join(tmp, "same.iso")
            with open(iso, "wb") as stream:
                stream.write(b"same")
            with mock.patch("iso_boot_test.capture_boot") as boot:
                with self.assertRaisesRegex(
                    SystemExit,
                    "exact-media phase: production and verification ISO inputs",
                ):
                    iso_boot_main([
                        "--production-iso", iso,
                        "--verification-iso", iso,
                        "--arch", "x86_64",
                        "--firmware", "bios",
                        "--expected-flavor", "debian",
                        "--expected-version", "13",
                        "--qemu", "/bin/true",
                    ])
            boot.assert_not_called()

    def test_exact_media_failures_are_phase_specific(self):
        cases = (
            (
                BootCapture("Kernel panic - not syncing: fatal\n", False, -9),
                "exact-media phase: guest kernel panic",
            ),
            (
                BootCapture("still booting\n", True, -9),
                "exact-media phase: guest did not reach serial milestone",
            ),
            (
                BootCapture(
                    "BUCKOS_VERIFY flavor=debian version=13 arch=x86_64\n",
                    False,
                    0,
                ),
                "exact-media phase: guest exited with status 0",
            ),
            (
                BootCapture("stopped\n", False, 1),
                "exact-media phase: guest exited with status 1",
            ),
        )
        for capture, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(BootFailure, message):
                    validate_production_capture(capture, "login:")

    def test_verification_failure_keeps_phase_and_marker_diagnostic(self):
        args = type("Args", (), {
            "expected_flavor": "debian",
            "expected_version": "13",
            "architecture": "x86_64",
            "expect_selinux": False,
        })()
        with self.assertRaisesRegex(
            BootFailure,
            "verification phase: guest did not emit BUCKOS_VERIFY before timeout",
        ):
            validate_verification_capture(args, BootCapture("booting\n", True, -9))

    def test_capture_reads_non_newline_milestone_and_cleans_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid_path = os.path.join(tmp, "pid")
            observed = {}

            def command(_args, _iso, temporary):
                observed["temporary"] = temporary
                return [
                    "/bin/sh", "-c",
                    'printf "%s" "$$" > "$1"; printf "buckos login: "; exec sleep 30',
                    "sh", pid_path,
                ]

            with mock.patch("iso_boot_test.qemu_command", side_effect=command):
                capture = capture_boot(
                    self.args(),
                    "/production.iso",
                    "exact-media",
                    lambda output: "login:" in output,
                )

            self.assertIn("login:", capture.output)
            self.assertFalse(capture.timed_out)
            self.assertFalse(os.path.exists(observed["temporary"]))
            self.assert_process_gone(pid_path)

    def test_capture_timeout_cleans_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid_path = os.path.join(tmp, "pid")
            observed = {}

            def command(_args, _iso, temporary):
                observed["temporary"] = temporary
                return [
                    "/bin/sh", "-c",
                    'printf "%s" "$$" > "$1"; exec sleep 30',
                    "sh", pid_path,
                ]

            with mock.patch("iso_boot_test.qemu_command", side_effect=command):
                capture = capture_boot(
                    self.args(timeout=0.2),
                    "/production.iso",
                    "exact-media",
                    lambda _output: False,
                )

            self.assertTrue(capture.timed_out)
            self.assertFalse(os.path.exists(observed["temporary"]))
            self.assert_process_gone(pid_path)

    def test_interruptions_report_phase_and_clean_process(self):
        for phase in ("exact-media", "verification"):
            for signum in (signal.SIGINT, signal.SIGTERM):
                signal_name = signal.Signals(signum).name
                with self.subTest(phase=phase, signal=signal_name), \
                        tempfile.TemporaryDirectory() as tmp:
                    production = os.path.join(tmp, "production.iso")
                    verification = os.path.join(tmp, "verification.iso")
                    with open(production, "wb") as stream:
                        stream.write(b"exact production bytes")
                    with open(verification, "wb") as stream:
                        stream.write(b"different verification bytes")
                    pid_path = os.path.join(tmp, "pid")
                    observed = {}

                    def command(_args, _iso, temporary):
                        observed["temporary"] = temporary
                        return [
                            "/bin/sh", "-c",
                            'printf "%s" "$$" > "$1"; printf ready; exec sleep 30',
                            "sh", pid_path,
                        ]

                    def boot(args, iso, active_phase, complete):
                        if active_phase != phase:
                            return BootCapture("buckos login: ", False, -9)

                        def interrupt(output):
                            if "ready" in output:
                                os.kill(os.getpid(), signum)
                            return complete(output)

                        return capture_boot(args, iso, active_phase, interrupt)

                    stdout = io.StringIO()
                    stderr = io.StringIO()
                    with mock.patch(
                        "iso_boot_test.qemu_command",
                        side_effect=command,
                    ), mock.patch(
                        "iso_boot_test.capture_boot",
                        side_effect=boot,
                    ), contextlib.redirect_stdout(stdout), \
                            contextlib.redirect_stderr(stderr):
                        with self.assertRaises(SystemExit) as raised:
                            iso_boot_main([
                                "--production-iso", production,
                                "--verification-iso", verification,
                                "--arch", "x86_64",
                                "--firmware", "bios",
                                "--expected-flavor", "debian",
                                "--expected-version", "13",
                                "--qemu", "/bin/true",
                            ])

                    self.assertEqual(
                        "{} phase: boot validation interrupted by {}".format(
                            phase,
                            signal_name,
                        ),
                        str(raised.exception),
                    )
                    self.assertFalse(os.path.exists(observed["temporary"]))
                    self.assert_process_gone(pid_path)


class TestIsoBootMatrix(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules = load_iso_test_graph()

    def test_complete_matrix_shape_and_wiring(self):
        rootfs_rules = {
            rule["name"]: rule for rule in self.rules["rootfs_overlay"]
        }
        squashfs_rules = {
            rule["name"]: rule for rule in self.rules["squashfs"]
        }
        iso_rules = {rule["name"]: rule for rule in self.rules["iso_image"]}
        boot_rules = {
            rule["name"]: rule for rule in self.rules["iso_boot_test"]
        }

        self.assertEqual(32, len(rootfs_rules))
        self.assertEqual(32, len(squashfs_rules))
        self.assertEqual(32, len(iso_rules))
        self.assertEqual(48, len(boot_rules))
        self.assertEqual(len(rootfs_rules), len(self.rules["rootfs_overlay"]))
        self.assertEqual(len(squashfs_rules), len(self.rules["squashfs"]))
        self.assertEqual(len(iso_rules), len(self.rules["iso_image"]))
        self.assertEqual(len(boot_rules), len(self.rules["iso_boot_test"]))

        production_isos = set()
        production_edges = set()
        verification_edges = set()
        expected_boot_names = set()
        for flavor, release, layout, expect_selinux in ISO_MATRIX:
            for architecture in ARCHITECTURES:
                for variant in IMAGE_VARIANTS:
                    variant_suffix = "-" + variant if variant else ""
                    image_suffix = "{}-{}-{}".format(
                        variant_suffix,
                        release,
                        architecture,
                    )
                    test_suffix = "{}{}-{}-{}".format(
                        flavor,
                        variant_suffix,
                        release.replace(".", "_"),
                        architecture,
                    )
                    rootfs_name = "rootfs-verify-" + test_suffix
                    squashfs_name = "squashfs-verify-" + test_suffix
                    iso_name = "iso-verify-" + test_suffix
                    production_rootfs = "//flavors/{}:rootfs-live{}".format(
                        flavor,
                        image_suffix,
                    )
                    production_iso = "//flavors/{}:iso-live{}".format(
                        flavor,
                        image_suffix,
                    )

                    self.assertEqual(
                        production_rootfs,
                        rootfs_rules[rootfs_name]["rootfs"],
                    )
                    self.assertEqual(
                        ":" + rootfs_name,
                        squashfs_rules[squashfs_name]["rootfs"],
                    )
                    self.assertEqual(
                        "//flavors/{}:kernel-live{}".format(flavor, image_suffix),
                        iso_rules[iso_name]["kernel"],
                    )
                    self.assertEqual(
                        "//flavors/{}:initramfs-live{}".format(flavor, image_suffix),
                        iso_rules[iso_name]["initramfs"],
                    )
                    self.assertEqual(
                        ":" + squashfs_name,
                        iso_rules[iso_name]["squashfs"],
                    )
                    self.assertEqual(layout, iso_rules[iso_name]["layout"])
                    self.assertEqual(architecture, iso_rules[iso_name]["target_cpu"])
                    self.assertEqual(
                        "hybrid" if architecture == "x86_64" else "uefi",
                        iso_rules[iso_name]["boot_mode"],
                    )

                    production_isos.add(production_iso)
                    firmwares = (
                        ("bios", "uefi")
                        if architecture == "x86_64"
                        else ("uefi",)
                    )
                    for firmware in firmwares:
                        boot_name = "boot-{}-{}".format(test_suffix, firmware)
                        expected_boot_names.add(boot_name)
                        boot = boot_rules[boot_name]
                        self.assertEqual(
                            production_iso,
                            boot["production_iso"],
                            "exact-media production ISO wiring for " + boot_name,
                        )
                        self.assertEqual(
                            ":" + iso_name,
                            boot["verification_iso"],
                            "verification ISO wiring for " + boot_name,
                        )
                        self.assertEqual("login:", boot["production_milestone"])
                        production_edges.add((boot_name, boot["production_iso"]))
                        verification_edges.add((boot_name, boot["verification_iso"]))
                        self.assertEqual(architecture, boot["architecture"])
                        self.assertEqual(firmware, boot["firmware"])
                        self.assertEqual(flavor, boot["expected_flavor"])
                        self.assertEqual(release, boot["expected_version"])
                        self.assertEqual(expect_selinux, boot["expect_selinux"])

        expected_production_isos = {
            "//flavors/{}:iso-live{}-{}-{}".format(
                flavor,
                "-" + variant if variant else "",
                release,
                architecture,
            )
            for flavor, release, _layout, _expect_selinux in ISO_MATRIX
            for architecture in ARCHITECTURES
            for variant in IMAGE_VARIANTS
        }
        self.assertEqual(32, len(expected_production_isos))
        self.assertEqual(expected_production_isos, production_isos)
        self.assertEqual(expected_boot_names, set(boot_rules))
        self.assertEqual(48, len(production_edges))
        self.assertEqual(48, len(verification_edges))

    def test_debian_prebuilt_boot_set_is_complete(self):
        self.assertEqual(
            {
                "boot-debian-prebuilt-13-x86_64-bios",
                "boot-debian-prebuilt-13-x86_64-uefi",
                "boot-debian-prebuilt-13-aarch64-uefi",
            },
            {
                rule["name"]
                for rule in self.rules["iso_boot_test"]
                if rule["name"].startswith("boot-debian-prebuilt-")
            },
        )

    def test_ubuntu_prebuilt_boot_set_is_complete(self):
        self.assertEqual(
            {
                "boot-ubuntu-prebuilt-26_04-x86_64-bios",
                "boot-ubuntu-prebuilt-26_04-x86_64-uefi",
                "boot-ubuntu-prebuilt-26_04-aarch64-uefi",
            },
            {
                rule["name"]
                for rule in self.rules["iso_boot_test"]
                if rule["name"].startswith("boot-ubuntu-prebuilt-")
            },
        )


class TestUbuntuIsoManifest(unittest.TestCase):
    def test_writes_sorted_checksums_without_hashing_itself(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "casper"))
            with open(os.path.join(tmp, "z-file"), "wb") as stream:
                stream.write(b"z")
            with open(os.path.join(tmp, "casper", "a-file"), "wb") as stream:
                stream.write(b"a")
            _write_md5sums(tmp)
            with open(os.path.join(tmp, "md5sum.txt"), encoding="utf-8") as stream:
                rows = stream.readlines()
        self.assertEqual(
            rows,
            [
                "0cc175b9c0f1b6a831c399e269772661  ./casper/a-file\n",
                "fbade9e36a3f36d3d676c1b808451dd7  ./z-file\n",
            ],
        )


class TestIsoBootCommand(unittest.TestCase):
    def x86_args(self, firmware):
        return type("Args", (), {
            "qemu": "/bin/true",
            "architecture": "x86_64",
            "firmware": firmware,
            "firmware_path": "",
            "firmware_vars": "",
        })()

    def arm_args(self, firmware):
        return type("Args", (), {
            "qemu": "/bin/true",
            "architecture": "aarch64",
            "firmware": "uefi",
            "firmware_path": firmware,
        })()

    def test_native_arm_uses_kvm_host_cpu(self):
        with tempfile.TemporaryDirectory() as tmp:
            firmware = os.path.join(tmp, "QEMU_EFI.fd")
            open(firmware, "wb").close()
            with mock.patch("iso_boot_test.platform.machine", return_value="aarch64"), \
                    mock.patch("iso_boot_test.os.access", return_value=True):
                command = qemu_command(self.arm_args(firmware), "/image.iso", tmp)
        self.assertIn("kvm", command)
        self.assertIn("-nographic", command)
        self.assertEqual("host", command[command.index("-cpu") + 1])
        self.assertEqual("/image.iso", command[command.index("-cdrom") + 1])

    def test_native_x86_uses_host_cpu(self):
        with mock.patch("iso_boot_test.platform.machine", return_value="x86_64"), \
                mock.patch("iso_boot_test.os.access", return_value=True):
            command = qemu_command(self.x86_args("bios"), "/image.iso", "/tmp")
        self.assertIn("kvm", command)
        self.assertEqual("host", command[command.index("-cpu") + 1])

    def test_cross_x86_uses_max_cpu(self):
        with mock.patch("iso_boot_test.platform.machine", return_value="aarch64"), \
                mock.patch("iso_boot_test.os.access", return_value=True):
            command = qemu_command(self.x86_args("bios"), "/image.iso", "/tmp")
        self.assertIn("tcg,thread=multi", command)
        self.assertEqual("max", command[command.index("-cpu") + 1])

    def test_cross_arm_uses_tcg_firmware_compatible_cpu(self):
        with tempfile.TemporaryDirectory() as tmp:
            firmware = os.path.join(tmp, "QEMU_EFI.fd")
            open(firmware, "wb").close()
            with mock.patch("iso_boot_test.platform.machine", return_value="x86_64"), \
                    mock.patch("iso_boot_test.os.access", return_value=True):
                command = qemu_command(self.arm_args(firmware), "/image.iso", tmp)
        self.assertIn("tcg,thread=multi", command)
        self.assertEqual("cortex-a57", command[command.index("-cpu") + 1])


class TestIsoBuildBootloaderPaths(unittest.TestCase):
    def test_staging_times_are_pinned_before_xorriso(self):
        # xorriso copies each staged entry's mtime into its ISO9660 directory
        # record, and --modification-date reaches only the volume
        # descriptors.  Two builds minutes apart differed in 304 bytes that
        # decoded to their own wall-clock minute and second.
        with tempfile.TemporaryDirectory() as iso_root:
            nested = os.path.join(iso_root, "EFI", "BOOT")
            os.makedirs(nested)
            payload = os.path.join(nested, "grub.cfg")
            with open(payload, "w") as handle:
                handle.write("set timeout=5\n")
            os.symlink("grub.cfg", os.path.join(nested, "link.cfg"))

            _pin_tree_times(iso_root, "1700000000")

            for path in (iso_root, nested, payload):
                self.assertEqual(1700000000, int(os.stat(path).st_mtime), path)

    def test_bios_loader_accepts_rpm_and_debian_paths(self):
        script = _bios_script("/iso")
        self.assertIn("/usr/share/syslinux/isolinux.bin", script)
        self.assertIn("/usr/lib/ISOLINUX/isolinux.bin", script)
        self.assertIn("/usr/lib/syslinux/modules/bios/ldlinux.c32", script)

    def test_isohybrid_mbr_accepts_rpm_and_debian_paths(self):
        args = type("Args", (), {
            "boot_mode": "hybrid",
            "volume_label": "TEST",
        })()
        script = _xorriso_script(args, "/iso", "/out.iso", "2020010100000000")
        self.assertIn("/usr/share/syslinux/isohdpfx.bin", script)
        self.assertIn("/usr/lib/ISOLINUX/isohdpfx.bin", script)


class TestEfiImageIsAFunctionOfItsInputs(unittest.TestCase):
    """The FAT image was the last thing in an RPM ISO that still moved.

    Two builds of identical inputs differed in eight bytes, all inside
    images/efiboot.img: the volume serial, and the creation and write times
    of the volume label's directory entry.  Both came from mkfs.vfat
    reading the clock.  See the comment in _efi_script for why the label is
    applied separately rather than with the option that exists for it.
    """

    def script(self, epoch="1700000000"):
        return _efi_script("/iso", "x86_64", epoch)

    def test_volume_id_is_derived_from_the_epoch(self):
        self.assertEqual("6553F100", _fat_volume_id("1700000000"))
        self.assertEqual(
            _fat_volume_id("1700000000"), _fat_volume_id(1700000000),
            "a string and an int epoch are the same declared input",
        )
        self.assertNotEqual(
            _fat_volume_id("1700000000"), _fat_volume_id("1700000001"),
            "a deliberately changed epoch should move the serial",
        )

    def test_volume_id_is_a_whole_eight_digit_field(self):
        # Short of eight digits mkfs.vfat still accepts it, and the image
        # then differs from one built with the padded form.
        for epoch in ("0", "1", "1700000000", "4294967295"):
            with self.subTest(epoch=epoch):
                value = _fat_volume_id(epoch)
                self.assertRegex(value, r"\A[0-9A-F]{8}\Z")

    def test_mkfs_pins_the_serial(self):
        self.assertIn("-i {}".format(_fat_volume_id("1700000000")), self.script())

    def test_mkfs_does_not_write_the_label_entry(self):
        """The regression guard, because folding -n back in looks like a tidy-up.

        mkfs.vfat -n writes a volume label directory entry stamped from the
        clock, and no mkfs.vfat option pins those two timestamps.
        """
        mkfs = [
            line for line in self.script().splitlines()
            if line.startswith("mkfs.vfat ")
        ]
        self.assertEqual(1, len(mkfs), mkfs)
        self.assertNotIn(" -n ", mkfs[0])

    def test_the_label_is_applied_with_mtools_afterwards(self):
        lines = self.script().splitlines()
        mkfs = next(i for i, l in enumerate(lines) if l.startswith("mkfs.vfat "))
        mlabel = next(i for i, l in enumerate(lines) if l.startswith("mlabel "))
        self.assertGreater(mlabel, mkfs)
        self.assertIn("::EFIBOOT", lines[mlabel])


if __name__ == "__main__":
    unittest.main()
