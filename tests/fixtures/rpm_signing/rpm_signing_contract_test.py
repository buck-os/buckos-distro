#!/usr/bin/env python3

import os
import unittest


class TestRpmSigningContract(unittest.TestCase):
    def test_signer_receives_package_identity(self):
        resource = os.path.join(os.path.dirname(__file__), "signed.rpm")
        with open(resource, "rb") as stream:
            payload = stream.read()
        self.assertEqual(
            b"unsigned fixture rpm\n\nSIGNED buckos-rpm-test example\n",
            payload,
        )


if __name__ == "__main__":
    unittest.main()
