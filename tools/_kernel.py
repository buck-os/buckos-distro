"""Shared validation helpers for producer-neutral kernel artifacts."""

import base64
import binascii
import os
import re


_RELEASE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+~-]*$")


def read_kernel_release(path):
    with open(path, "r", encoding="utf-8") as stream:
        value = stream.read()
    if value.endswith("\n"):
        value = value[:-1]
    if not value or "\n" in value or "\r" in value or not _RELEASE.fullmatch(value):
        raise ValueError("invalid kernel release in {}: {!r}".format(path, value))
    return value


def certificate_der(path):
    """Return comparable DER bytes for a PEM or DER X.509 certificate."""
    with open(path, "rb") as stream:
        data = stream.read()
    begin = b"-----BEGIN CERTIFICATE-----"
    end = b"-----END CERTIFICATE-----"
    if begin in data:
        if end not in data:
            raise ValueError("certificate {} has no PEM end marker".format(path))
        body = data.split(begin, 1)[1].split(end, 1)[0]
        try:
            data = base64.b64decode(b"".join(body.split()), validate=True)
        except (ValueError, binascii.Error) as error:
            raise ValueError("certificate {} has invalid PEM data".format(path)) from error
    if not data or data[0] != 0x30:
        raise ValueError("certificate {} is not DER X.509 data".format(path))
    return data


def write_certificate_pem(source, destination):
    encoded = base64.b64encode(certificate_der(source)).decode("ascii")
    os.makedirs(os.path.dirname(os.path.abspath(destination)), exist_ok=True)
    with open(destination, "w", encoding="ascii", newline="\n") as stream:
        stream.write("-----BEGIN CERTIFICATE-----\n")
        for offset in range(0, len(encoded), 64):
            stream.write(encoded[offset:offset + 64] + "\n")
        stream.write("-----END CERTIFICATE-----\n")
