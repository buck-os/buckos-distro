#!/usr/bin/env python3

import io
import json
import os
import shutil
import stat
import struct
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path, PurePosixPath
from unittest.mock import patch

import prepare_worker_probe_root as prepare


def write_elf(
    path: Path,
    *,
    machine: int = 62,
    interpreter: str | None = None,
    needed: tuple[str, ...] = (),
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    program_count = 1 + (1 if interpreter else 0) + (1 if needed else 0)
    interpreter_data = (interpreter.encode("ascii") + b"\0") if interpreter else b""
    strings = bytearray(b"\0")
    needed_offsets = []
    for name in needed:
        needed_offsets.append(len(strings))
        strings.extend(name.encode("ascii") + b"\0")

    interpreter_offset = 0x200
    dynamic_offset = 0x300
    string_offset = 0x400
    dynamic_entries = []
    for offset in needed_offsets:
        dynamic_entries.append((1, offset))
    if needed:
        dynamic_entries.extend([
            (5, 0x400000 + string_offset),
            (10, len(strings)),
            (0, 0),
        ])
    dynamic_data = b"".join(
        struct.pack("<QQ", tag, value) for tag, value in dynamic_entries
    )
    size = max(
        64 + program_count * 56,
        interpreter_offset + len(interpreter_data),
        dynamic_offset + len(dynamic_data),
        string_offset + len(strings),
    )
    data = bytearray(size)
    data[:16] = b"\x7fELF" + bytes((2, 1, 1)) + b"\0" * 9
    struct.pack_into(
        "<HHIQQQIHHHHHH",
        data,
        16,
        2,
        machine,
        1,
        0,
        64,
        0,
        0,
        64,
        56,
        program_count,
        0,
        0,
        0,
    )
    program_headers = [
        (1, 5, 0, 0x400000, 0x400000, size, size, 0x1000),
    ]
    if interpreter:
        program_headers.append((
            3,
            4,
            interpreter_offset,
            0x400000 + interpreter_offset,
            0x400000 + interpreter_offset,
            len(interpreter_data),
            len(interpreter_data),
            1,
        ))
    if needed:
        program_headers.append((
            2,
            4,
            dynamic_offset,
            0x400000 + dynamic_offset,
            0x400000 + dynamic_offset,
            len(dynamic_data),
            len(dynamic_data),
            8,
        ))
    for index, values in enumerate(program_headers):
        struct.pack_into("<IIQQQQQQ", data, 64 + index * 56, *values)
    data[interpreter_offset:interpreter_offset + len(interpreter_data)] = (
        interpreter_data
    )
    data[dynamic_offset:dynamic_offset + len(dynamic_data)] = dynamic_data
    data[string_offset:string_offset + len(strings)] = strings
    path.write_bytes(data)
    path.chmod(0o755)


class FakeSdme:
    def __init__(self, source: Path, inventory: str | None = None) -> None:
        self.source = source
        self.inventory = inventory or '[{"name":"runtime"}]'
        self.copies: list[PurePosixPath] = []

    def rootfs_names(self) -> set[str]:
        return prepare.parse_rootfs_inventory(self.inventory)

    def copy(
        self,
        runtime_fs: str,
        source: PurePosixPath,
        destination: Path,
    ) -> None:
        self.copies.append(source)
        if runtime_fs not in self.rootfs_names():
            raise prepare.PreparationError("missing fake rootfs")
        local = self.source.joinpath(*source.parts[1:])
        if not os.path.lexists(local):
            raise prepare.PreparationError("missing fake path {}".format(source))
        target = destination / source.name
        if local.is_symlink():
            target.symlink_to(os.readlink(local))
        elif local.is_dir():
            shutil.copytree(local, target, symlinks=True)
        else:
            shutil.copy2(local, target)


class SdmeClientTest(unittest.TestCase):
    def test_uses_read_only_inventory_and_rootfs_copy_endpoints(self) -> None:
        responses = [
            subprocess.CompletedProcess(
                [],
                0,
                '[{"name":"runtime"}]\n',
                "",
            ),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
        client = prepare.SdmeClient(Path("/usr/local/bin/sdme"))
        with patch(
            "prepare_worker_probe_root.subprocess.run",
            side_effect=responses,
        ) as run:
            names = client.rootfs_names()
            client.copy(
                "runtime",
                PurePosixPath("/usr/bin/python3"),
                Path("/srv/staging"),
            )

        self.assertEqual(names, {"runtime"})
        self.assertEqual(
            run.call_args_list[0].args[0],
            ["/usr/local/bin/sdme", "fs", "ls", "--json"],
        )
        self.assertEqual(
            run.call_args_list[1].args[0],
            [
                "/usr/local/bin/sdme",
                "cp",
                "fs:runtime:/usr/bin/python3",
                "/srv/staging",
            ],
        )


class ProbeRootTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / "sdme-rootfs"
        self.destination = self.base / "probe-root"
        self.tar = Path(shutil.which("tar") or "/usr/bin/tar").resolve()
        self._create_source()

    def _create_source(
        self,
        machine: int = 62,
        architecture: str = "x86_64",
    ) -> None:
        shutil.rmtree(self.source, ignore_errors=True)
        triplet = prepare.LIBRARY_TRIPLETS[architecture]
        if architecture == "x86_64":
            interpreter = "/lib64/ld-linux-x86-64.so.2"
            loader_link = self.source / "lib64/ld-linux-x86-64.so.2"
        else:
            interpreter = "/lib/ld-linux-aarch64.so.1"
            loader_link = self.source / "lib/ld-linux-aarch64.so.1"
        python = self.source / "usr/bin/python3.14"
        write_elf(
            python,
            machine=machine,
            interpreter=interpreter,
            needed=("libpython3.14.so.1.0", "libc.so.6"),
        )
        (self.source / "usr/bin/python3").symlink_to("python3.14")
        stdlib = self.source / "usr/lib/python3.14"
        stdlib.mkdir(parents=True)
        (stdlib / "os.py").write_text("name = 'posix'\n", encoding="utf-8")
        (stdlib / "current.py").symlink_to("os.py")
        write_elf(
            stdlib / "lib-dynload/_socket.so",
            machine=machine,
            needed=("libm.so.6",),
        )
        library = self.source / "lib" / triplet
        library.mkdir(parents=True)
        (library / "libpython3.14.so.1.0").symlink_to(
            "libpython3.14.so.1.0.real"
        )
        write_elf(
            library / "libpython3.14.so.1.0.real",
            machine=machine,
            needed=("libc.so.6",),
        )
        write_elf(library / "libc.so.6", machine=machine)
        write_elf(library / "libm.so.6", machine=machine)
        loader = self.source / "lib" / triplet / "ld-2.40.so"
        write_elf(loader, machine=machine)
        loader_link.parent.mkdir(parents=True, exist_ok=True)
        loader_link.symlink_to("/lib/{}/ld-2.40.so".format(triplet))
        (self.source / "opt").mkdir()
        (self.source / "opt/full-root-only").write_text(
            "must not be copied\n",
            encoding="utf-8",
        )

    def test_apply_copies_minimal_runtime_closure_and_emits_matching_digest(self) -> None:
        client = FakeSdme(self.source)

        digest = prepare.apply(
            client,
            "runtime",
            "x86_64",
            self.destination,
            self.tar,
        )

        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertEqual(
            digest,
            prepare.tree_digest(self.destination, self.tar),
        )
        self.assertEqual(os.readlink(self.destination / "usr/bin/python3"), "python3.14")
        self.assertEqual(
            os.readlink(
                self.destination / "lib/x86_64-linux-gnu/libpython3.14.so.1.0"
            ),
            "libpython3.14.so.1.0.real",
        )
        self.assertEqual(
            os.readlink(self.destination / "lib64/ld-linux-x86-64.so.2"),
            "/lib/x86_64-linux-gnu/ld-2.40.so",
        )
        for name in ("proc", "dev", "tmp"):
            self.assertTrue((self.destination / name).is_dir())
        self.assertFalse((self.destination / "opt/full-root-only").exists())
        self.assertNotIn(PurePosixPath("/"), client.copies)
        for path in (self.destination, *self.destination.rglob("*")):
            if not path.is_symlink():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode) & 0o222, 0)
        manifest = json.loads(
            prepare.manifest_path(self.destination).read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["sha256"], digest)
        self.assertEqual(manifest["runtime_fs"], "runtime")
        self.assertEqual(manifest["architecture"], "x86_64")
        self.assertTrue((self.source / "opt/full-root-only").exists())

    def test_reuses_existing_destination_only_when_manifest_matches(self) -> None:
        client = FakeSdme(self.source)
        first = prepare.apply(
            client,
            "runtime",
            "x86_64",
            self.destination,
            self.tar,
        )
        copy_count = len(client.copies)

        second = prepare.apply(
            client,
            "runtime",
            "x86_64",
            self.destination,
            self.tar,
        )

        self.assertEqual(second, first)
        self.assertEqual(len(client.copies), copy_count)

    def test_builds_aarch64_closure_from_aarch64_runtime(self) -> None:
        self._create_source(machine=183, architecture="aarch64")

        digest = prepare.apply(
            FakeSdme(self.source),
            "runtime",
            "aarch64",
            self.destination,
            self.tar,
        )

        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertTrue(
            (self.destination / "lib/aarch64-linux-gnu/libc.so.6").is_file()
        )
        self.assertTrue(
            (self.destination / "lib/ld-linux-aarch64.so.1").is_symlink()
        )

    def test_rejects_existing_destination_without_recorded_digest(self) -> None:
        self.destination.mkdir()
        sentinel = self.destination / "sentinel"
        sentinel.write_text("preserve\n", encoding="utf-8")

        with self.assertRaisesRegex(prepare.PreparationError, "incomplete existing"):
            prepare.apply(
                FakeSdme(self.source),
                "runtime",
                "x86_64",
                self.destination,
                self.tar,
            )

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve\n")

    def test_rejects_changed_existing_destination(self) -> None:
        client = FakeSdme(self.source)
        prepare.apply(client, "runtime", "x86_64", self.destination, self.tar)
        module = self.destination / "usr/lib/python3.14/os.py"
        module.chmod(0o644)
        module.write_text("changed\n", encoding="utf-8")
        module.chmod(0o444)

        with self.assertRaisesRegex(prepare.PreparationError, "digest mismatch"):
            prepare.apply(
                client,
                "runtime",
                "x86_64",
                self.destination,
                self.tar,
            )

        self.assertEqual(module.read_text(encoding="utf-8"), "changed\n")

    def test_missing_rootfs_does_not_create_destination(self) -> None:
        client = FakeSdme(self.source, inventory='[{"name":"other"}]')

        with self.assertRaisesRegex(prepare.PreparationError, "not imported"):
            prepare.apply(
                client,
                "runtime",
                "x86_64",
                self.destination,
                self.tar,
            )

        self.assertFalse(self.destination.exists())

    def test_architecture_mismatch_removes_new_destination(self) -> None:
        self._create_source(machine=183, architecture="aarch64")

        with self.assertRaisesRegex(prepare.PreparationError, "ELF machine"):
            prepare.apply(
                FakeSdme(self.source),
                "runtime",
                "x86_64",
                self.destination,
                self.tar,
            )

        self.assertFalse(self.destination.exists())
        self.assertFalse(prepare.manifest_path(self.destination).exists())

    def test_plan_is_nonroot_and_does_not_create_destination(self) -> None:
        output = io.StringIO()
        errors = io.StringIO()
        with (
            patch("prepare_worker_probe_root.platform.machine", return_value="x86_64"),
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            status = prepare.main([
                "plan",
                "--runtime-fs", "runtime",
                "--arch", "x86_64",
                "--destination", str(self.destination),
                "--sdme", "/does/not/exist",
            ], effective_uid=1000)

        self.assertEqual(status, 0, errors.getvalue())
        self.assertIn("PLAN copy /usr/bin/python3", output.getvalue())
        self.assertFalse(self.destination.exists())
        self.assertFalse(prepare.manifest_path(self.destination).exists())

    def test_apply_requires_root_before_resolving_sdme(self) -> None:
        errors = io.StringIO()
        with (
            patch("prepare_worker_probe_root.platform.machine", return_value="x86_64"),
            redirect_stdout(io.StringIO()),
            redirect_stderr(errors),
        ):
            status = prepare.main([
                "apply",
                "--runtime-fs", "runtime",
                "--arch", "x86_64",
                "--destination", str(self.destination),
                "--sdme", "/does/not/exist",
            ], effective_uid=1000)

        self.assertEqual(status, 1)
        self.assertIn("apply must run as root", errors.getvalue())
        self.assertFalse(self.destination.exists())

    def test_apply_cli_emits_bare_provisioner_digest(self) -> None:
        client = FakeSdme(self.source)
        output = io.StringIO()
        errors = io.StringIO()

        def resolve(value: str) -> Path:
            if value == "tar":
                return self.tar
            return Path("/usr/local/bin/sdme")

        with (
            patch("prepare_worker_probe_root.platform.machine", return_value="x86_64"),
            patch("prepare_worker_probe_root.resolve_executable", side_effect=resolve),
            patch("prepare_worker_probe_root.SdmeClient", return_value=client),
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            status = prepare.main([
                "apply",
                "--runtime-fs", "runtime",
                "--arch", "x86_64",
                "--destination", str(self.destination),
            ], effective_uid=0)

        self.assertEqual(status, 0, errors.getvalue())
        digest = output.getvalue().strip()
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertEqual(digest, prepare.tree_digest(self.destination, self.tar))

    def test_rejects_home_and_repository_destinations(self) -> None:
        with self.assertRaisesRegex(prepare.PreparationError, "home directory"):
            prepare.validate_destination(Path("/home/operator/probe"))
        with self.assertRaisesRegex(prepare.PreparationError, "repository checkout"):
            prepare.validate_destination(prepare.REPO_ROOT / "probe")

    def test_rejects_invalid_fake_sdme_inventory(self) -> None:
        with self.assertRaisesRegex(prepare.PreparationError, "inventory shape"):
            prepare.parse_rootfs_inventory('{"name":"runtime"}')


if __name__ == "__main__":
    unittest.main()
