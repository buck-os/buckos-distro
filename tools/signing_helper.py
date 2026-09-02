#!/usr/bin/env python3
"""File-backed implementation of the BuckOS signing-key command contract.

This helper is intended for checked-in test keys and local development keys.
Production key targets should expose the same command line through an HSM/KMS
client so private key bytes never become Buck inputs.
"""

import argparse
import base64
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile


def _member_name(member):
    name = member.name
    while name.startswith("./"):
        name = name[2:]
    if name in ("", "."):
        return None
    if name.startswith("/") or name == ".." or name.startswith("../"):
        raise ValueError("unsafe rootfs member path: {!r}".format(member.name))
    return name


def _pseudo_name(name):
    if any(char in name for char in " \t\r\n\\"):
        raise ValueError(
            "mksquashfs pseudo files cannot safely address {!r}".format(name)
        )
    return name


def _is_signable(member, header, mode):
    if not member.isfile():
        return False
    if mode == "all":
        return True
    return bool(member.mode & 0o111) or header.startswith(b"\x7fELF")


def _run_ima_sign(evmctl, private_key, certificate, path):
    result = subprocess.run(
        [
            evmctl,
            "ima_sign",
            "--sigfile",
            "--key",
            private_key,
            "--keyid-from-cert",
            certificate,
            path,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    signature = path + ".sig"
    # evmctl commonly returns non-zero after successfully writing --sigfile
    # because its subsequent security.ima setxattr is not permitted.  The
    # detached signature is the output that matters here.
    if not os.path.isfile(signature) or os.path.getsize(signature) == 0:
        detail = (result.stderr or result.stdout or "no signature produced").strip()
        raise RuntimeError("IMA signing failed for {}: {}".format(path, detail))
    return signature


def write_ima_manifest(rootfs, output, private_key, certificate, evmctl, mode):
    """Sign selected final rootfs files and emit mksquashfs xattr entries."""
    rows = []
    with tarfile.open(rootfs) as archive, tempfile.TemporaryDirectory(
        prefix="buckos-ima-"
    ) as temporary:
        # Rootfs overlays append replacement members, so the final occurrence
        # of a path is authoritative just as it is during tar extraction.
        members = {}
        for member in archive.getmembers():
            name = _member_name(member)
            if name is not None:
                members[name] = member

        payload = os.path.join(temporary, "payload")
        for name in sorted(members):
            member = members[name]
            if not member.isfile():
                continue
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError("cannot read rootfs member {}".format(name))
            header = source.read(4)
            if not _is_signable(member, header, mode):
                continue
            with open(payload, "wb") as destination:
                destination.write(header)
                shutil.copyfileobj(source, destination)
            os.chmod(payload, member.mode & 0o7777)
            signature = _run_ima_sign(
                evmctl, private_key, certificate, payload
            )
            with open(signature, "rb") as stream:
                encoded = base64.b64encode(stream.read()).decode("ascii")
            rows.append("{} x security.ima=0s{}\n".format(
                _pseudo_name(name), encoded
            ))
            os.unlink(signature)

    if not rows:
        raise RuntimeError("IMA signing selected no files from {}".format(rootfs))
    os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
    temporary_output = output + ".tmp"
    with open(temporary_output, "w", encoding="ascii", newline="\n") as stream:
        stream.writelines(rows)
    os.replace(temporary_output, output)
    return len(rows)


def sign_pe(source, output, private_key, certificate, signer):
    """Authenticode-sign and self-verify one PE/COFF image."""
    with open(source, "rb") as stream:
        if stream.read(2) != b"MZ":
            raise RuntimeError("{} is not a PE/COFF image".format(source))

    basename = os.path.basename(signer)
    if "osslsigncode" in basename:
        command = [
            signer,
            "sign",
            "-h",
            "sha256",
            "-certs",
            certificate,
            "-key",
            private_key,
            "-in",
            source,
            "-out",
            output,
        ]
        verify = [signer, "verify", "-CAfile", certificate, "-in", output]
    elif basename == "sbsign":
        command = [
            signer,
            "--key",
            private_key,
            "--cert",
            certificate,
            "--output",
            output,
            source,
        ]
        verifier = os.path.join(os.path.dirname(signer), "sbverify")
        verify = [verifier, "--cert", certificate, output]
    else:
        raise RuntimeError("unsupported PE signer: {}".format(signer))

    subprocess.run(command, check=True)
    subprocess.run(verify, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-key", required=True)
    parser.add_argument("--certificate", required=True)
    parser.add_argument("--evmctl", default="/usr/bin/evmctl")
    parser.add_argument("--pe-signer", default="/usr/bin/osslsigncode")
    commands = parser.add_subparsers(dest="command", required=True)

    ima = commands.add_parser("ima-manifest")
    ima.add_argument("--rootfs", required=True)
    ima.add_argument("--out", required=True)
    ima.add_argument("--mode", choices=("executables", "all"), default="all")

    pe = commands.add_parser("pe-sign")
    pe.add_argument("--in", dest="source", required=True)
    pe.add_argument("--out", required=True)
    args = parser.parse_args()

    try:
        if args.command == "ima-manifest":
            count = write_ima_manifest(
                args.rootfs,
                args.out,
                args.private_key,
                args.certificate,
                args.evmctl,
                args.mode,
            )
            print("IMA-signed {} rootfs files".format(count))
        else:
            sign_pe(
                args.source,
                args.out,
                args.private_key,
                args.certificate,
                args.pe_signer,
            )
            print("signed PE/COFF image {}".format(args.out))
    except (OSError, RuntimeError, subprocess.CalledProcessError, ValueError) as error:
        print("signing failed: {}".format(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
