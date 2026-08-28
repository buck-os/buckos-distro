#!/usr/bin/env python3
"""Boot an ISO through PC or Arm firmware and verify the guest marker."""

import argparse
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading


MARKER = "BUCKOS_VERIFY "
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


def find_file(path, suffix=None):
    if os.path.isfile(path):
        return path
    if os.path.isdir(path):
        for root, _dirs, names in os.walk(path):
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iso", required=True)
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
    args = parser.parse_args()

    iso = find_file(args.iso, ".iso")
    if not iso:
        sys.exit("ISO not found under {}".format(args.iso))

    with tempfile.TemporaryDirectory(prefix="buckos-iso-boot-") as temporary:
        command = qemu_command(args, iso, temporary)
        print("+ {}".format(" ".join(command)), file=sys.stderr, flush=True)
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            start_new_session=True,
        )

        def terminate():
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        timer = threading.Timer(args.timeout, terminate)
        timer.start()
        lines = []
        marker = None
        panic = None
        try:
            for line in process.stdout:
                clean = ANSI_RE.sub("", line).replace("\r", "")
                lines.append(clean)
                marker = parse_marker(clean) or marker
                if marker is not None:
                    terminate()
                    break
                if "Kernel panic - not syncing:" in clean:
                    panic = clean.strip()
                    terminate()
                    break
        finally:
            timer.cancel()
            terminate()
            process.wait()
            process.stdout.close()

    if marker is None:
        sys.stderr.write("".join(lines[-300:]))
        if panic:
            sys.exit("guest kernel panic: " + panic)
        sys.exit("guest did not emit {} before timeout".format(MARKER.strip()))
    errors = validate(args, marker)
    if errors:
        sys.stderr.write("".join(lines[-300:]))
        sys.exit("boot validation failed: " + "; ".join(errors))
    print(MARKER + " ".join("{}={}".format(key, marker[key]) for key in sorted(marker)))


if __name__ == "__main__":
    main()
