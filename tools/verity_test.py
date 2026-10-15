#!/usr/bin/env python3

import json
import os
import tempfile
import unittest

from _verity import format_image, kernel_arguments, load_metadata


class TestVerityImage(unittest.TestCase):
    def format(self, directory, payload, **kwargs):
        source = os.path.join(directory, "source.img")
        outputs = [os.path.join(directory, name) for name in (
            "data.img", "hash.img", "metadata.json", "roothash.txt",
        )]
        with open(source, "wb") as stream:
            stream.write(payload)
        metadata = format_image(source, *outputs, **kwargs)
        return outputs, metadata

    def test_pads_data_and_emits_a_stable_version_one_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            outputs, metadata = self.format(
                tmp,
                b"buckos dm-verity fixture\n",
                salt_value="00" * 32,
            )
            data, tree, manifest, roothash = outputs
            self.assertEqual(4096, os.path.getsize(data))
            self.assertEqual(0, os.path.getsize(tree))
            self.assertEqual(1, metadata["data_blocks"])
            self.assertEqual("buckos.dm-verity.v1", metadata["schema"])
            self.assertEqual("0" * 64, metadata["salt"])
            with open(roothash, encoding="ascii") as stream:
                self.assertEqual(metadata["root_hash"], stream.read())
            self.assertEqual(metadata, load_metadata(manifest))

    def test_auto_salt_and_outputs_are_reproducible(self):
        payload = (b"reproducible verity input" * 1000) + b"!"
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            first_outputs, first_metadata = self.format(first, payload)
            second_outputs, second_metadata = self.format(second, payload)
            self.assertEqual(first_metadata, second_metadata)
            for first_path, second_path in zip(first_outputs, second_outputs):
                with open(first_path, "rb") as left, open(second_path, "rb") as right:
                    self.assertEqual(left.read(), right.read())

    def test_tree_has_multiple_levels_and_root_first_layout(self):
        # sha256 with 512-byte hash blocks holds 16 hashes.  Seventeen data
        # blocks therefore produce two leaf blocks plus one root-level block.
        with tempfile.TemporaryDirectory() as tmp:
            outputs, metadata = self.format(
                tmp,
                bytes(range(256)) * 34,
                salt_value="ab" * 32,
                data_block_size=512,
                hash_block_size=512,
            )
            self.assertEqual(17, metadata["data_blocks"])
            self.assertEqual(3 * 512, os.path.getsize(outputs[1]))
            # Fixed interoperability vector, also accepted by
            # `veritysetup verify` with a separate no-superblock hash device.
            self.assertEqual(
                "16a3d8f793c28424bbb832f1ef0df625a7302e3f2d61dd41cc9a68dc95c5f9e8",
                metadata["root_hash"],
            )

    def test_kernel_arguments_carry_every_table_parameter(self):
        with tempfile.TemporaryDirectory() as tmp:
            outputs, _ = self.format(tmp, b"payload", salt_value="12" * 32)
            metadata = load_metadata(outputs[2])
        arguments = kernel_arguments(
            metadata,
            "CDLABEL=BUCKOS",
            "LiveOS/squashfs.img",
            "LiveOS/squashfs.img.verity",
        )
        joined = " ".join(arguments)
        self.assertIn("rd.buckos.verity=1", joined)
        self.assertIn("rd.buckos.verity.root_hash={}".format(metadata["root_hash"]), joined)
        self.assertIn("rd.buckos.verity.data_blocks=1", joined)

    def test_rejects_malformed_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "metadata.json")
            with open(path, "w", encoding="utf-8") as stream:
                json.dump({"schema": "somebody.else.v1"}, stream)
            with self.assertRaisesRegex(ValueError, "not buckos.dm-verity.v1"):
                load_metadata(path)

    def test_rejects_empty_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "empty"):
                self.format(tmp, b"")


if __name__ == "__main__":
    unittest.main()
