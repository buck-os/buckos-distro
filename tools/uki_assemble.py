#!/usr/bin/env python3
"""Assemble a Unified Kernel Image from a systemd EFI stub."""

import argparse
import os
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile

from _isolation import (
    ISOLATION_MODES,
    require_target_execution,
    resolve_isolation,
    run_isolated,
    sandbox_path,
)
from _rpm import reproducible_env, scratch_dir


_PE_MACHINES = {
    0x8664: "x86_64",
    0xAA64: "aarch64",
}


def pe_architecture(path):
    """Return the architecture encoded in a PE/COFF executable."""
    with open(path, "rb") as stream:
        if stream.read(2) != b"MZ":
            raise ValueError("{} is not a PE/COFF EFI stub".format(path))
        stream.seek(0x3C)
        offset_bytes = stream.read(4)
        if len(offset_bytes) != 4:
            raise ValueError("{} has a truncated DOS header".format(path))
        pe_offset = struct.unpack("<I", offset_bytes)[0]
        stream.seek(pe_offset)
        if stream.read(4) != b"PE\0\0":
            raise ValueError("{} has no PE signature".format(path))
        machine_bytes = stream.read(2)
        if len(machine_bytes) != 2:
            raise ValueError("{} has a truncated COFF header".format(path))
    machine = struct.unpack("<H", machine_bytes)[0]
    architecture = _PE_MACHINES.get(machine)
    if architecture is None:
        raise ValueError("{} has unsupported PE machine {:#x}".format(path, machine))
    return architecture


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


def _objcopy_command(objcopy, stub, output, layout):
    command = [objcopy]
    for name, path, address in layout:
        command += [
            "--add-section",
            "{}={}".format(name, path),
            "--change-section-vma",
            "{}={:#x}".format(name, address),
            "--set-section-flags",
            "{}=contents,alloc,load,readonly,data".format(name),
        ]
    return command + [stub, output]


def _write_cmdline(path, command_line):
    with open(path, "wb") as stream:
        stream.write(command_line.encode("utf-8").rstrip(b"\0") + b"\0")


def assemble(args):
    architecture = pe_architecture(args.stub)
    if args.architecture and architecture != args.architecture:
        raise ValueError("EFI stub is {}, expected {}".format(
            architecture,
            args.architecture,
        ))

    section_table = subprocess.check_output(
        [args.objdump, "-h", args.stub], text=True
    )
    stub_end = maximum_section_end(section_table)

    with tempfile.TemporaryDirectory(prefix="buckos-uki-") as temporary:
        cmdline = os.path.join(temporary, "cmdline")
        _write_cmdline(cmdline, args.cmdline)

        sections = section_layout(stub_end, [
            (".osrel", args.osrel),
            (".uname", args.uname),
            (".cmdline", cmdline),
            (".linux", args.linux),
            (".initrd", args.initrd),
        ])
        subprocess.run(
            _objcopy_command(args.objcopy, args.stub, args.out, sections),
            check=True,
        )

    if not os.path.isfile(args.out) or not os.path.getsize(args.out):
        raise RuntimeError("objcopy produced no UKI")
    if pe_architecture(args.out) != architecture:
        raise RuntimeError("objcopy changed the UKI architecture")


def _stage(source, destination):
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    shutil.copy2(os.path.abspath(source), destination)


def assemble_in_buildroot(args):
    """Assemble with objcopy/objdump from the declared target buildroot."""
    require_target_execution(args.target_cpu)
    isolation = resolve_isolation(args.isolation)
    work = os.path.abspath(args.work) if args.work else scratch_dir(
        "buckos-distro-uki-",
        key=args.out,
    )
    if args.work:
        shutil.rmtree(work, ignore_errors=True)
        os.makedirs(work)

    inputs = {}
    for name in ("stub", "linux", "initrd", "osrel", "uname"):
        destination = os.path.join(work, name)
        _stage(getattr(args, name), destination)
        inputs[name] = destination
    cmdline = os.path.join(work, "cmdline")
    _write_cmdline(cmdline, args.cmdline)
    inputs["cmdline"] = cmdline
    section_table = os.path.join(work, "sections.txt")
    assembled = os.path.join(work, "uki.efi")
    sysroot = os.path.abspath(args.buildroot_tree) if args.buildroot_tree else None
    env = reproducible_env(source_date_epoch=args.source_date_epoch)

    try:
        architecture = pe_architecture(inputs["stub"])
        if architecture != args.architecture:
            raise ValueError("EFI stub is {}, expected {}".format(
                architecture,
                args.architecture,
            ))
        inside = {
            name: sandbox_path(path, work, isolation)
            for name, path in inputs.items()
        }
        inside_sections = sandbox_path(section_table, work, isolation)
        run_isolated(
            [
                "/bin/sh",
                "-c",
                "{} -h {} > {}".format(
                    shlex.quote(args.objdump),
                    shlex.quote(inside["stub"]),
                    shlex.quote(inside_sections),
                ),
            ],
            isolation,
            work,
            work,
            sysroot,
            env=env,
        )
        with open(section_table, encoding="utf-8") as stream:
            stub_end = maximum_section_end(stream.read())
        sections = section_layout(stub_end, [
            (".osrel", inside["osrel"]),
            (".uname", inside["uname"]),
            (".cmdline", inside["cmdline"]),
            (".linux", inside["linux"]),
            (".initrd", inside["initrd"]),
        ])
        run_isolated(
            _objcopy_command(
                args.objcopy,
                inside["stub"],
                sandbox_path(assembled, work, isolation),
                sections,
            ),
            isolation,
            work,
            work,
            sysroot,
            env=env,
        )
        if not os.path.isfile(assembled) or not os.path.getsize(assembled):
            raise RuntimeError("objcopy produced no UKI")
        if pe_architecture(assembled) != architecture:
            raise RuntimeError("objcopy changed the UKI architecture")
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        shutil.copy2(assembled, args.out)
    finally:
        if not args.keep_work and not args.work:
            shutil.rmtree(work, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--objcopy", default="/usr/bin/objcopy")
    parser.add_argument("--objdump", default="/usr/bin/objdump")
    parser.add_argument("--stub", required=True)
    parser.add_argument("--linux", required=True)
    parser.add_argument("--initrd", required=True)
    parser.add_argument("--osrel", required=True)
    parser.add_argument("--uname", required=True)
    parser.add_argument("--cmdline", required=True)
    parser.add_argument("--architecture", choices=sorted(_PE_MACHINES.values()))
    parser.add_argument("--buildroot-tree")
    parser.add_argument("--isolation", choices=ISOLATION_MODES, default="none")
    parser.add_argument("--target-cpu", default="")
    parser.add_argument("--source-date-epoch", default="1700000000")
    parser.add_argument("--work")
    parser.add_argument("--keep-work", action="store_true")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    try:
        if args.buildroot_tree or args.isolation != "none":
            if not args.architecture:
                parser.error("--architecture is required with a buildroot")
            assemble_in_buildroot(args)
        else:
            assemble(args)
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
