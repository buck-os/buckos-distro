"""Shared Debian package helpers for buckos-distro action scripts."""

import hashlib
import os
import shutil
import subprocess
import sys


def run(cmd, **kwargs):
    """Run a command, echoing it, and fail with captured output."""
    printable = " ".join(str(part) for part in cmd)
    print("+ {}".format(printable), file=sys.stderr, flush=True)
    kwargs.setdefault("check", True)
    try:
        return subprocess.run([str(part) for part in cmd], **kwargs)
    except subprocess.CalledProcessError as exc:
        print(
            "command failed (exit {}): {}".format(exc.returncode, printable),
            file=sys.stderr,
        )
        for stream_name in ("stdout", "stderr"):
            stream = getattr(exc, stream_name, None)
            if stream:
                text = stream.decode(errors="replace") if isinstance(stream, bytes) else stream
                print("--- {} ---\n{}".format(stream_name, text), file=sys.stderr)
        raise


def require_tool(name):
    path = shutil.which(name)
    if not path:
        sys.exit(
            "buckos-distro: required tool {!r} not found on PATH.\n"
            "  PATH={}".format(name, os.environ.get("PATH", ""))
        )
    return path


def clear_signed_payload(text):
    """Return the RFC822 payload from an optional clearsigned document."""
    marker = "-----BEGIN PGP SIGNED MESSAGE-----"
    if not text.startswith(marker):
        return text

    lines = text.splitlines()
    try:
        start = lines.index("") + 1
        end = lines.index("-----BEGIN PGP SIGNATURE-----", start)
    except ValueError as exc:
        raise ValueError("malformed clearsigned control file") from exc

    payload = []
    for line in lines[start:end]:
        payload.append(line[2:] if line.startswith("- ") else line)
    return "\n".join(payload) + "\n"


def parse_control(text):
    """Parse one Debian control paragraph without external Python modules."""
    fields = {}
    current = None
    for raw_line in clear_signed_payload(text).splitlines():
        if not raw_line:
            if fields:
                break
            continue
        if raw_line[0].isspace():
            if current is None:
                raise ValueError("control continuation without a field")
            fields[current] += "\n" + raw_line[1:]
            continue
        if ":" not in raw_line:
            raise ValueError("malformed control line: {!r}".format(raw_line))
        current, value = raw_line.split(":", 1)
        current = current.strip()
        fields[current] = value.lstrip()
    return fields


def parse_control_paragraphs(text):
    """Parse all paragraphs from Debian control-file text."""
    paragraphs = []
    current = []
    for line in text.splitlines():
        if line:
            current.append(line)
        elif current:
            paragraphs.append(parse_control("\n".join(current) + "\n"))
            current = []
    if current:
        paragraphs.append(parse_control("\n".join(current) + "\n"))
    return paragraphs


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def dsc_files(fields):
    """Return {basename: (sha256, size)} from a parsed .dsc paragraph."""
    checksums = fields.get("Checksums-Sha256")
    if not checksums:
        raise ValueError(".dsc has no Checksums-Sha256 field")

    files = {}
    for line in checksums.splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 3:
            raise ValueError("malformed Checksums-Sha256 line: {!r}".format(line))
        digest, size_text, name = parts
        if os.path.basename(name) != name or name in (".", ".."):
            raise ValueError("unsafe source filename in .dsc: {!r}".format(name))
        if name in files:
            raise ValueError("duplicate source filename in .dsc: {!r}".format(name))
        if len(digest) != 64 or any(c not in "0123456789abcdefABCDEF" for c in digest):
            raise ValueError("invalid SHA-256 for {!r}".format(name))
        try:
            size = int(size_text)
        except ValueError as exc:
            raise ValueError("invalid size for {!r}".format(name)) from exc
        files[name] = (digest.lower(), size)
    return files


def deb_field(path, field):
    result = run(
        [require_tool("dpkg-deb"), "--field", path, field],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def extract_deb(path, out):
    run([require_tool("dpkg-deb"), "--extract", path, out])
