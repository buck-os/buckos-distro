#!/usr/bin/env python3

import json
import os
import subprocess
import tempfile
import unittest

from authenticode_signer import sign_pe


def _executable(path, body):
    with open(path, "w", encoding="utf-8") as stream:
        stream.write("#!/usr/bin/env python3\n")
        stream.write(body)
    os.chmod(path, 0o755)
    return path


class TestAuthenticodeSigner(unittest.TestCase):
    def test_signs_with_authenticode_and_verifies_declared_certificate(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = os.path.join(temporary, "input.efi")
            output = os.path.join(temporary, "output.efi")
            certificate = os.path.join(temporary, "secureboot.crt")
            client_log = os.path.join(temporary, "client.json")
            verifier_log = os.path.join(temporary, "verifier.json")
            with open(source, "wb") as stream:
                stream.write(b"MZunsigned")
            with open(certificate, "w", encoding="ascii") as stream:
                stream.write("test certificate")

            client = _executable(
                os.path.join(temporary, "signing-client"),
                "import json, shutil, sys\n"
                "args = sys.argv[1:]\n"
                "src = args[1]\n"
                "out = args[args.index('--output-file') + 1]\n"
                "shutil.copyfile(src, out)\n"
                "with open(out, 'ab') as f: f.write(b'-signed')\n"
                "with open({!r}, 'w') as f: json.dump(args, f)\n".format(client_log),
            )
            verifier = _executable(
                os.path.join(temporary, "osslsigncode"),
                "import json, sys\n"
                "with open({!r}, 'w') as f: json.dump(sys.argv[1:], f)\n".format(
                    verifier_log
                ),
            )

            sign_pe(
                source,
                output,
                client,
                "buckos-secureboot",
                certificate,
                "BuckOS release",
                verifier,
                tier="signing.example.test",
                timeout_ms=1234,
            )

            with open(output, "rb") as stream:
                self.assertEqual(b"MZunsigned-signed", stream.read())
            with open(client_log, encoding="utf-8") as stream:
                args = json.load(stream)
            self.assertEqual("authenticode", args[0])
            self.assertEqual(source, args[1])
            self.assertEqual("buckos-secureboot", args[args.index("--sign-key") + 1])
            self.assertEqual("bin", args[args.index("--file-type") + 1])
            self.assertEqual(
                "signing.example.test",
                args[args.index("--tier") + 1],
            )
            self.assertEqual("1234", args[args.index("--timeout") + 1])
            with open(verifier_log, encoding="utf-8") as stream:
                verify_args = json.load(stream)
            self.assertEqual(["verify", "-CAfile", certificate, "-in"], verify_args[:4])
            self.assertEqual(os.path.dirname(output), os.path.dirname(verify_args[4]))
            self.assertTrue(
                os.path.basename(verify_args[4]).startswith(".remote-signing-")
            )

    def test_rejects_non_pe_input_before_contacting_service(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = os.path.join(temporary, "input")
            with open(source, "wb") as stream:
                stream.write(b"not PE")
            with self.assertRaisesRegex(RuntimeError, "not a PE/COFF image"):
                sign_pe(
                    source,
                    os.path.join(temporary, "output"),
                    "missing-client",
                    "key",
                    "certificate",
                    "description",
                    "missing-verifier",
                )

    def test_failed_verification_does_not_publish_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = os.path.join(temporary, "input.efi")
            output = os.path.join(temporary, "output.efi")
            with open(source, "wb") as stream:
                stream.write(b"MZunsigned")
            client = _executable(
                os.path.join(temporary, "client"),
                "import shutil, sys\n"
                "args = sys.argv[1:]\n"
                "shutil.copyfile(args[1], args[args.index('--output-file') + 1])\n",
            )
            verifier = _executable(
                os.path.join(temporary, "verifier"),
                "raise SystemExit(1)\n",
            )
            with self.assertRaises(subprocess.CalledProcessError):
                sign_pe(
                    source,
                    output,
                    client,
                    "key",
                    "certificate",
                    "description",
                    verifier,
                )
            self.assertFalse(os.path.exists(output))


if __name__ == "__main__":
    unittest.main()
