#!/usr/bin/env python3

import os
import struct
import tempfile
import unittest

from uki_assemble import (
    _objcopy_command,
    maximum_section_end,
    pe_architecture,
    section_layout,
)


class TestUkiLayout(unittest.TestCase):
    def pe_file(self, temporary, machine):
        path = os.path.join(temporary, "stub.efi")
        payload = bytearray(0x88)
        payload[0:2] = b"MZ"
        payload[0x3C:0x40] = struct.pack("<I", 0x80)
        payload[0x80:0x84] = b"PE\0\0"
        payload[0x84:0x86] = struct.pack("<H", machine)
        with open(path, "wb") as stream:
            stream.write(payload)
        return path

    def test_reads_supported_pe_architectures(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(
                "x86_64",
                pe_architecture(self.pe_file(temporary, 0x8664)),
            )
            self.assertEqual(
                "aarch64",
                pe_architecture(self.pe_file(temporary, 0xAA64)),
            )

    def test_rejects_unsupported_pe_architecture(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "unsupported PE machine"):
                pe_architecture(self.pe_file(temporary, 0x14C))

    def test_finds_the_highest_existing_section_end(self):
        output = """
Sections:
Idx Name          Size      VMA               LMA
  0 .text         00001234  0000000000010000  0000000000010000
  1 .data         00000020  0000000000021000  0000000000021000
"""
        self.assertEqual(0x21020, maximum_section_end(output))

    def test_places_payloads_at_non_overlapping_aligned_addresses(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = os.path.join(temporary, "first")
            second = os.path.join(temporary, "second")
            with open(first, "wb") as stream:
                stream.write(b"a" * 0x10001)
            with open(second, "wb") as stream:
                stream.write(b"b")

            layout = section_layout(
                0x21020,
                [(".first", first), (".second", second)],
            )

            self.assertEqual(0x30000, layout[0][2])
            self.assertEqual(0x50000, layout[1][2])

    def test_rejects_an_objdump_without_sections(self):
        with self.assertRaisesRegex(ValueError, "no PE sections"):
            maximum_section_end("not a section table")

    def test_objcopy_marks_uki_sections_loadable_and_read_only(self):
        command = _objcopy_command(
            "/usr/bin/objcopy",
            "stub.efi",
            "uki.efi",
            [(".linux", "vmlinuz", 0x30000)],
        )
        self.assertIn(".linux=contents,alloc,load,readonly,data", command)
        self.assertEqual(["stub.efi", "uki.efi"], command[-2:])


if __name__ == "__main__":
    unittest.main()
