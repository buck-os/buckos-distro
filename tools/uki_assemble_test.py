#!/usr/bin/env python3

import os
import tempfile
import unittest

from uki_assemble import maximum_section_end, section_layout


class TestUkiLayout(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
