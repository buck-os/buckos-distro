#!/usr/bin/env python3
"""Assemble a Unified Kernel Image from a systemd EFI stub."""

import argparse
import os
import subprocess
import tempfile


def maximum_section_end(objdump_output):
    """Return the highest VMA plus size in `objdump -h` output."""
    end = 0
    for line in objdump_output.splitlines():
        fields = line.split()
        if len(fields) < 4 or not fields[0].isdigit():
            continue
        try:
            size = int(fields[2], 16)
            vma = int(fields[3], 16)
        except ValueError:
            continue
        end = max(end, vma + size)
    if not end:
        raise ValueError("objdump reported no PE sections")
    return end


def section_layout(stub_end, sections, alignment=0x10000):
    """Assign non-overlapping aligned VMAs to named section payloads."""
    def align(value):
        return (value + alignment - 1) & ~(alignment - 1)

    address = align(stub_end)
    layout = []
    for name, path in sections:
        if path is None:
            continue
        layout.append((name, path, address))
        address = align(address + os.path.getsize(path))
    return layout


def assemble(args):
    with open(args.stub, "rb") as stream:
        if stream.read(2) != b"MZ":
            raise ValueError("{} is not a PE/COFF EFI stub".format(args.stub))

    section_table = subprocess.check_output(
        [args.objdump, "-h", args.stub], text=True
    )
    stub_end = maximum_section_end(section_table)

    with tempfile.TemporaryDirectory(prefix="buckos-uki-") as temporary:
        cmdline = os.path.join(temporary, "cmdline")
        with open(cmdline, "wb") as stream:
            stream.write(args.cmdline.encode("utf-8").rstrip(b"\0") + b"\0")

        sections = section_layout(stub_end, [
            (".osrel", args.osrel),
            (".uname", args.uname),
            (".cmdline", cmdline),
            (".linux", args.linux),
            (".initrd", args.initrd),
        ])
        command = [args.objcopy]
        for name, path, address in sections:
            command += [
                "--add-section",
                "{}={}".format(name, path),
                "--change-section-vma",
                "{}={:#x}".format(name, address),
            ]
        command += [args.stub, args.out]
        subprocess.run(command, check=True)

    if not os.path.isfile(args.out) or not os.path.getsize(args.out):
        raise RuntimeError("objcopy produced no UKI")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--objcopy", required=True)
    parser.add_argument("--objdump", required=True)
    parser.add_argument("--stub", required=True)
    parser.add_argument("--linux", required=True)
    parser.add_argument("--initrd", required=True)
    parser.add_argument("--osrel", required=True)
    parser.add_argument("--uname", required=True)
    parser.add_argument("--cmdline", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    try:
        assemble(args)
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
