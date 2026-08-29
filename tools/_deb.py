"""Shared Debian package helpers for buckos-distro action scripts."""

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile


SOURCE_FIELD_RE = re.compile(r"^([^\s()]+)(?:\s+\(([^()]+)\))?$")
BINARY_NMU_RE = re.compile(r"\+b[0-9]+$")

STATUS_FIELDS = (
    "Package",
    "Essential",
    "Status",
    "Priority",
    "Section",
    "Installed-Size",
    "Maintainer",
    "Architecture",
    "Multi-Arch",
    "Source",
    "Version",
    "Replaces",
    "Provides",
    "Pre-Depends",
    "Depends",
    "Conflicts",
    "Breaks",
    "Description",
)

CONTROL_FILES = (
    "conffiles",
    "md5sums",
    "shlibs",
    "symbols",
    "triggers",
)


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


def strip_binary_nmu(version: str) -> str:
    """Return the source version represented by a Debian binary version."""
    return BINARY_NMU_RE.sub("", version)


def source_identity(fields: dict[str, str]) -> tuple[str, str]:
    """Return the exact source name and version for a binary package record."""
    package = fields.get("Package")
    version = fields.get("Version")
    if not package or not version:
        raise ValueError("binary package metadata requires Package and Version")

    value = fields.get("Source", "").strip()
    if not value:
        return package, strip_binary_nmu(version)
    match = SOURCE_FIELD_RE.fullmatch(value)
    if not match:
        raise ValueError("malformed Source field: {!r}".format(value))
    source_name, source_version = match.groups()
    return source_name, source_version or strip_binary_nmu(version)


def compatible_binary_version(actual: str, source_version: str) -> bool:
    """Whether a built binary version belongs to the selected source version."""
    return strip_binary_nmu(actual) == source_version


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


def payload_paths(deb: str) -> list[str]:
    process = subprocess.Popen(
        [require_tool("dpkg-deb"), "--fsys-tarfile", deb],
        stdout=subprocess.PIPE,
    )
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            paths = []
            for member in archive:
                name = member.name.removeprefix("./").rstrip("/")
                if name:
                    paths.append("/" + name)
    finally:
        process.stdout.close()
    status = process.wait()
    if status != 0:
        raise subprocess.CalledProcessError(status, process.args)
    return sorted(set(paths))


def package_key(deb: str) -> str:
    package = deb_field(deb, "Package")
    architecture = deb_field(deb, "Architecture")
    if deb_field(deb, "Multi-Arch") == "same":
        return "{}:{}".format(package, architecture)
    return package


def extract_control(deb: str, root: str) -> None:
    dpkg_deb = require_tool("dpkg-deb")
    with tempfile.TemporaryDirectory(prefix="buckos-deb-control-") as tmp:
        run([dpkg_deb, "--control", deb, tmp])
        key = package_key(deb)
        info = os.path.join(root, "var", "lib", "dpkg", "info")
        os.makedirs(info, exist_ok=True)
        for name in CONTROL_FILES:
            source = os.path.join(tmp, name)
            if os.path.isfile(source):
                shutil.copy2(source, os.path.join(info, "{}.{}".format(key, name)))
        with open(os.path.join(info, key + ".list"), "w", encoding="utf-8") as stream:
            for path in payload_paths(deb):
                stream.write(path + "\n")


def status_paragraph(deb: str) -> str:
    result = run(
        [require_tool("dpkg-deb"), "--field", deb],
        capture_output=True,
        text=True,
    )
    fields = parse_control(result.stdout)
    fields["Status"] = "install ok installed"
    lines = []
    for name in STATUS_FIELDS:
        value = fields.get(name)
        if value:
            lines.append("{}: {}".format(name, value.replace("\n", "\n ")))
    return "\n".join(lines) + "\n"


def register_debs(debs: list[str], root: str) -> None:
    """Merge binary package metadata into an existing buildroot dpkg database."""
    status_path = os.path.join(root, "var", "lib", "dpkg", "status")
    existing = []
    if os.path.isfile(status_path):
        with open(status_path, encoding="utf-8") as stream:
            existing = parse_control_paragraphs(stream.read())

    paragraphs = {}
    for fields in existing:
        key = fields["Package"]
        if fields.get("Multi-Arch") == "same":
            key = "{}:{}".format(key, fields["Architecture"])
        paragraphs[key] = "\n".join(
            "{}: {}".format(name, value.replace("\n", "\n "))
            for name, value in fields.items()
        ) + "\n"

    for deb in debs:
        extract_control(deb, root)
        paragraphs[package_key(deb)] = status_paragraph(deb)

    os.makedirs(os.path.dirname(status_path), exist_ok=True)
    with open(status_path, "w", encoding="utf-8") as stream:
        for key in sorted(paragraphs):
            stream.write(paragraphs[key])
            stream.write("\n")
