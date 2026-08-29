#!/usr/bin/env python3
"""Boot an exact production ISO, then its instrumented verification ISO."""

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
import platform
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time


MARKER = "BUCKOS_VERIFY "
PANIC = "Kernel panic - not syncing:"
PRODUCTION_MILESTONE = "login:"
ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")

FIRMWARE_CANDIDATES = {
    ("x86_64", "code"): (
        "/usr/share/OVMF/OVMF_CODE.fd",
        "/usr/share/OVMF/OVMF_CODE_4M.fd",
        "/usr/share/edk2/ovmf/OVMF_CODE.fd",
        "/usr/share/edk2/x64/OVMF_CODE.fd",
    ),
    ("x86_64", "vars"): (
        "/usr/share/OVMF/OVMF_VARS.fd",
        "/usr/share/OVMF/OVMF_VARS_4M.fd",
        "/usr/share/edk2/ovmf/OVMF_VARS.fd",
        "/usr/share/edk2/x64/OVMF_VARS.fd",
    ),
    ("aarch64", "code"): (
        "/usr/share/qemu-efi-aarch64/QEMU_EFI.fd",
        "/usr/share/AAVMF/AAVMF_CODE.fd",
        "/usr/share/edk2/aarch64/QEMU_EFI.fd",
    ),
}


@dataclass(frozen=True)
class BootCapture:
    output: str
    timed_out: bool
    returncode: int


class BootFailure(RuntimeError):
    pass


class BootInterrupted(RuntimeError):
    pass


def find_file(path, suffix=None):
    if os.path.isfile(path):
        return path
    if os.path.isdir(path):
        for root, dirs, names in os.walk(path):
            dirs.sort()
            for name in sorted(names):
                if suffix is None or name.endswith(suffix):
                    return os.path.join(root, name)
    return None


def find_firmware(explicit, architecture, kind):
    if explicit:
        found = find_file(explicit)
        if found:
            return found
        sys.exit("firmware does not exist: {}".format(explicit))
    for candidate in FIRMWARE_CANDIDATES.get((architecture, kind), ()):
        if os.path.isfile(candidate):
            return candidate
    sys.exit("no {} {} firmware found; set the matching [buckos] firmware path".format(
        architecture,
        kind,
    ))


def qemu_command(args, iso, temporary):
    qemu = shutil.which(args.qemu) if not os.path.isabs(args.qemu) else args.qemu
    if not qemu or not os.path.isfile(qemu):
        sys.exit("QEMU binary not found: {}".format(args.qemu))

    native = {"amd64": "x86_64", "arm64": "aarch64"}.get(
        platform.machine(),
        platform.machine(),
    )
    use_kvm = (
        os.access("/dev/kvm", os.R_OK | os.W_OK)
        and native == args.architecture
    )
    accelerator = "kvm" if use_kvm else "tcg,thread=multi"
    common = [
        qemu,
        "-accel", accelerator,
        "-m", "2048",
        "-smp", "2",
        "-no-reboot",
        "-boot", "d",
    ]

    if args.architecture == "x86_64":
        command = common + [
            "-cpu", "host" if use_kvm else "max",
            "-display", "none",
            "-serial", "stdio",
            "-monitor", "none",
            "-machine", "q35",
            "-cdrom", iso,
        ]
        if args.firmware == "uefi":
            code = find_firmware(args.firmware_path, "x86_64", "code")
            command += ["-drive", "if=pflash,format=raw,readonly=on,file={}".format(code)]
            vars_path = find_firmware(args.firmware_vars, "x86_64", "vars")
            vars_copy = os.path.join(temporary, "OVMF_VARS.fd")
            shutil.copyfile(vars_path, vars_copy)
            command += ["-drive", "if=pflash,format=raw,file={}".format(vars_copy)]
        return command

    if args.firmware != "uefi":
        sys.exit("AArch64 ISO tests require UEFI")
    code = find_firmware(args.firmware_path, "aarch64", "code")
    return common + [
        "-nographic",
        "-machine", "virt",
        "-cpu", "host" if use_kvm else "cortex-a57",
        "-bios", code,
        "-cdrom", iso,
    ]


def parse_marker(line):
    if MARKER not in line:
        return None
    fields = {}
    for item in line.split(MARKER, 1)[1].strip().split():
        key, separator, value = item.partition("=")
        if separator:
            fields[key] = value
    return fields


def validate(args, fields):
    expected = {
        "flavor": args.expected_flavor,
        "version": args.expected_version,
        "arch": args.architecture,
        "pid1": "systemd",
        "failed": "0",
        "avc": "0",
    }
    if args.expect_selinux:
        expected["selinux"] = "Enforcing"
    errors = []
    for key, value in expected.items():
        actual = fields.get(key)
        if actual != value:
            errors.append("{}: expected {!r}, got {!r}".format(key, value, actual))
    return errors


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def clean_console(data):
    return ANSI_RE.sub("", data.decode("utf-8", "replace")).replace("\r", "")


def marker_from_output(output):
    marker = None
    for line in output.splitlines():
        marker = parse_marker(line) or marker
    return marker


def complete_marker_from_output(output):
    return any(
        line.endswith("\n") and parse_marker(line) is not None
        for line in output.splitlines(keepends=True)
    )


def panic_from_output(output):
    for line in output.splitlines():
        if PANIC in line:
            return line.strip()
    return None


def console_tail(output, lines=300):
    return "\n".join(output.splitlines()[-lines:]) + ("\n" if output else "")


def terminate_process(process):
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait()


def capture_boot(args, iso, phase, complete):
    with tempfile.TemporaryDirectory(prefix="buckos-iso-boot-{}-".format(phase)) as temporary:
        command = qemu_command(args, iso, temporary)
        print(
            "{} phase: + {}".format(phase, " ".join(command)),
            file=sys.stderr,
            flush=True,
        )
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        if process.stdout is None:
            terminate_process(process)
            raise BootFailure("{} phase: QEMU stdout pipe is unavailable".format(phase))

        deadline = time.monotonic() + args.timeout
        chunks = []
        timed_out = False
        selector = selectors.DefaultSelector()
        try:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                if not selector.select(remaining):
                    timed_out = True
                    break
                chunk = os.read(process.stdout.fileno(), 1 << 16)
                if not chunk:
                    break
                chunks.append(chunk)
                output = clean_console(b"".join(chunks))
                if panic_from_output(output) or complete(output):
                    break
        finally:
            selector.close()
            terminate_process(process)
            process.stdout.close()

    return BootCapture(
        output=clean_console(b"".join(chunks)),
        timed_out=timed_out,
        returncode=process.returncode,
    )


def validate_production_capture(capture, milestone):
    panic = panic_from_output(capture.output)
    if panic:
        raise BootFailure("exact-media phase: guest kernel panic: " + panic)
    if milestone in capture.output:
        return
    if capture.timed_out:
        raise BootFailure(
            "exact-media phase: guest did not reach serial milestone {!r} "
            "before timeout".format(milestone)
        )
    raise BootFailure(
        "exact-media phase: guest exited with status {} before serial "
        "milestone {!r}".format(capture.returncode, milestone)
    )


def validate_verification_capture(args, capture):
    panic = panic_from_output(capture.output)
    if panic:
        raise BootFailure("verification phase: guest kernel panic: " + panic)
    marker = marker_from_output(capture.output)
    if marker is None:
        if capture.timed_out:
            raise BootFailure(
                "verification phase: guest did not emit {} before timeout".format(
                    MARKER.strip()
                )
            )
        raise BootFailure(
            "verification phase: guest exited with status {} without {}".format(
                capture.returncode,
                MARKER.strip(),
            )
        )
    errors = validate(args, marker)
    if errors:
        raise BootFailure(
            "verification phase: boot validation failed: " + "; ".join(errors)
        )
    return marker


def require_iso(path, phase):
    iso = find_file(path, ".iso")
    if not iso:
        raise BootFailure("{} phase: ISO not found under {}".format(phase, path))
    return os.path.realpath(iso)


@contextmanager
def interrupt_guard():
    previous = {}

    def interrupted(signum, _frame):
        raise BootInterrupted(signal.Signals(signum).name)

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.signal(signum, interrupted)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--production-iso", required=True)
    parser.add_argument("--verification-iso", required=True)
    parser.add_argument("--production-milestone", default=PRODUCTION_MILESTONE)
    parser.add_argument(
        "--arch",
        dest="architecture",
        required=True,
        choices=("x86_64", "aarch64"),
    )
    parser.add_argument("--firmware", required=True, choices=("bios", "uefi"))
    parser.add_argument("--expected-flavor", required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--expect-selinux", action="store_true")
    parser.add_argument("--firmware-path", default="")
    parser.add_argument("--firmware-vars", default="")
    parser.add_argument("--qemu", required=True)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    if not args.production_milestone:
        parser.error("--production-milestone must not be empty")
    return args


def run(args):
    production_iso = require_iso(args.production_iso, "exact-media")
    verification_iso = require_iso(args.verification_iso, "verification")
    if os.path.samefile(production_iso, verification_iso):
        raise BootFailure(
            "exact-media phase: production and verification ISO inputs "
            "resolve to the same file"
        )

    production_sha256 = file_sha256(production_iso)
    print(
        "exact-media phase: production ISO sha256={} path={}".format(
            production_sha256,
            production_iso,
        ),
        file=sys.stderr,
        flush=True,
    )
    if args.verbose:
        print(
            "exact-media phase: waiting for serial milestone {!r}".format(
                args.production_milestone
            ),
            file=sys.stderr,
            flush=True,
        )
    production = capture_boot(
        args,
        production_iso,
        "exact-media",
        lambda output: args.production_milestone in output,
    )
    try:
        validate_production_capture(production, args.production_milestone)
    except BootFailure:
        sys.stderr.write(console_tail(production.output))
        raise
    print(
        "BUCKOS_PRODUCTION_ISO sha256={} milestone={}".format(
            production_sha256,
            args.production_milestone,
        )
    )

    verification = capture_boot(
        args,
        verification_iso,
        "verification",
        complete_marker_from_output,
    )
    try:
        marker = validate_verification_capture(args, verification)
    except BootFailure:
        sys.stderr.write(console_tail(verification.output))
        raise
    print(MARKER + " ".join("{}={}".format(key, marker[key]) for key in sorted(marker)))


def main(argv=None):
    args = parse_args(argv)
    try:
        with interrupt_guard():
            run(args)
    except BootInterrupted as error:
        sys.exit("boot validation interrupted by {}".format(error))
    except BootFailure as error:
        sys.exit(str(error))


if __name__ == "__main__":
    main()
