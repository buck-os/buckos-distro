#!/usr/bin/env python3
"""Adapt a remote Authenticode service to the BuckOS signer contract."""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile


def _resolve_executable(executable):
    if os.path.sep in executable:
        if not os.path.isfile(executable) or not os.access(executable, os.X_OK):
            raise RuntimeError("executable is not available: {}".format(executable))
        return executable
    resolved = shutil.which(executable)
    if resolved is None:
        raise RuntimeError("executable is not on PATH: {}".format(executable))
    return resolved


def _require_pe(path):
    with open(path, "rb") as stream:
        if stream.read(2) != b"MZ":
            raise RuntimeError("{} is not a PE/COFF image".format(path))


def sign_pe(
    source,
    output,
    client,
    sign_key,
    certificate,
    sign_description,
    verifier,
    tier=None,
    timeout_ms=None,
):
    """Sign one EFI PE/COFF image remotely and verify its identity."""
    _require_pe(source)
    client = _resolve_executable(client)
    verifier = _resolve_executable(verifier)

    output_dir = os.path.dirname(os.path.abspath(output))
    os.makedirs(output_dir, exist_ok=True)
    descriptor, temporary_output = tempfile.mkstemp(
        prefix=".remote-signing-", suffix=".efi", dir=output_dir
    )
    os.close(descriptor)
    os.unlink(temporary_output)
    try:
        command = [
            client,
            "authenticode",
            source,
            "--sign-key",
            sign_key,
            "--sign-description",
            sign_description,
            "--file-type",
            "bin",
            "--output-file",
            temporary_output,
        ]
        if tier:
            command.extend(["--tier", tier])
        if timeout_ms:
            command.extend(["--timeout", str(timeout_ms)])
        subprocess.run(command, check=True)

        if not os.path.isfile(temporary_output) or not os.path.getsize(
            temporary_output
        ):
            raise RuntimeError("remote signing service produced no output")
        _require_pe(temporary_output)
        subprocess.run(
            [
                verifier,
                "verify",
                "-CAfile",
                certificate,
                "-in",
                temporary_output,
            ],
            check=True,
        )
        os.replace(temporary_output, output)
    finally:
        if os.path.exists(temporary_output):
            os.unlink(temporary_output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", required=True)
    parser.add_argument("--sign-key", required=True)
    parser.add_argument("--certificate", required=True)
    parser.add_argument("--sign-description", default="BuckOS Secure Boot")
    parser.add_argument("--tier")
    parser.add_argument("--timeout-ms", type=int)
    parser.add_argument("--verifier", default="/usr/bin/osslsigncode")
    commands = parser.add_subparsers(dest="command", required=True)
    pe = commands.add_parser("pe-sign")
    pe.add_argument("--in", dest="source", required=True)
    pe.add_argument("--out", required=True)
    args = parser.parse_args()

    try:
        sign_pe(
            args.source,
            args.out,
            args.client,
            args.sign_key,
            args.certificate,
            args.sign_description,
            args.verifier,
            tier=args.tier,
            timeout_ms=args.timeout_ms,
        )
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print("remote signing failed: {}".format(error), file=sys.stderr)
        return 1
    print("remotely signed PE/COFF image {}".format(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
