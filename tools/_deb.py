"""Shared Debian package helpers for buckos-distro action scripts."""

import glob
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from xml.sax.saxutils import quoteattr


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

SKELETON = (
    "builddir",
    "dev",
    "etc",
    "proc",
    "sys",
    "tmp",
    "var/lib/dpkg/info",
    "var/tmp",
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


def merge_base_passwd_database(root: str, name: str) -> None:
    """Add accounts from base-passwd's master database when not configured."""
    target = os.path.join(root, "etc", name)
    master = os.path.join(root, "usr", "share", "base-passwd", name + ".master")
    if not os.path.isfile(master):
        return

    with open(target, encoding="utf-8") as stream:
        existing_lines = stream.read().splitlines()
    existing_names = {
        line.split(":", 1)[0]
        for line in existing_lines
        if line and not line.startswith("#") and ":" in line
    }
    with open(master, encoding="utf-8") as stream:
        master_lines = stream.read().splitlines()
    additions = [
        line for line in master_lines
        if line and not line.startswith("#") and line.split(":", 1)[0] not in existing_names
    ]
    if additions:
        with open(target, "a", encoding="utf-8") as stream:
            for line in additions:
                stream.write(line + "\n")


def ensure_base_files(root: str) -> None:
    """Create the minimal runtime skeleton and package-managed tool links."""
    for rel in SKELETON:
        path = os.path.join(root, rel)
        os.makedirs(path, exist_ok=True)
    os.chmod(os.path.join(root, "tmp"), 0o1777)
    os.chmod(os.path.join(root, "var", "tmp"), 0o1777)

    info_format = os.path.join(root, "var", "lib", "dpkg", "info", "format")
    if not os.path.exists(info_format):
        with open(info_format, "w", encoding="utf-8") as stream:
            stream.write("1\n")

    for name, target in (
        ("bin", "usr/bin"),
        ("sbin", "usr/sbin"),
        ("lib", "usr/lib"),
        ("lib32", "usr/lib32"),
        ("lib64", "usr/lib64"),
        ("libx32", "usr/libx32"),
    ):
        path = os.path.join(root, name)
        target_path = os.path.join(root, target)
        if not os.path.lexists(path) and os.path.exists(target_path):
            os.symlink(target, path)

    passwd = os.path.join(root, "etc", "passwd")
    if not os.path.exists(passwd):
        with open(passwd, "w", encoding="utf-8") as stream:
            stream.write("root:x:0:0:root:/root:/bin/bash\n")
    group = os.path.join(root, "etc", "group")
    if not os.path.exists(group):
        with open(group, "w", encoding="utf-8") as stream:
            stream.write("root:x:0:\n")
    merge_base_passwd_database(root, "passwd")
    merge_base_passwd_database(root, "group")

    # No package owns /etc/hosts on a Debian system; the installer writes it.
    # A buildroot composed from package payloads therefore never has one, and
    # glibc falls through to DNS for localhost and returns EAI_AGAIN. Any
    # %check that binds or connects to localhost then fails or hangs --
    # CPython's test_httpservers is the first one this graph reaches.
    hosts = os.path.join(root, "etc", "hosts")
    if not os.path.exists(hosts):
        with open(hosts, "w", encoding="utf-8") as stream:
            stream.write("127.0.0.1 localhost\n::1 localhost\n")
    # Named without dns on purpose: replay actions run with no network, so a
    # dns source buys nothing but resolver timeouts on every lookup that
    # misses.
    nsswitch = os.path.join(root, "etc", "nsswitch.conf")
    if not os.path.exists(nsswitch):
        with open(nsswitch, "w", encoding="utf-8") as stream:
            stream.write("hosts: files\n")

    # base-files selects the native dpkg vendor in its postinst. Some large
    # source packages, including GCC, query it even during debian/rules clean.
    origins = os.path.join(root, "etc", "dpkg", "origins")
    default_origin = os.path.join(origins, "default")
    if not os.path.lexists(default_origin):
        for vendor in ("ubuntu", "debian"):
            if os.path.isfile(os.path.join(origins, vendor)):
                os.symlink(vendor, default_origin)
                break

    bindir = os.path.join(root, "usr", "bin")
    os.makedirs(bindir, exist_ok=True)
    # ImageMagick installs convert-im7.q16 and registers the plain name
    # through update-alternatives, so the glob rather than a fixed target:
    # the suffix carries a major version and a quantum depth, and pinning
    # either would break on the next release.  debian's Makefile calls
    # plain convert and stops at exit 127 without it.
    for name in ("aclocal", "automake", "convert", "openjade"):
        link = os.path.join(bindir, name)
        candidates = sorted(glob.glob(link + "-*"))
        if not os.path.lexists(link) and len(candidates) == 1:
            os.symlink(os.path.basename(candidates[0]), link)
    for name in ("lua", "luac"):
        link = os.path.join(bindir, name)
        candidates = sorted(glob.glob(link + "[0-9]*"))
        if not os.path.lexists(link) and len(candidates) == 1:
            os.symlink(os.path.basename(candidates[0]), link)

    # These links are normally registered by package maintainer scripts.
    # Buildroots are payload-composed rather than configured, so reproduce
    # the stable defaults when the selected implementation is present.
    for name, target in (
        ("awk", "mawk"),
        ("nawk", "mawk"),
        ("cc", "gcc"),
        ("c++", "g++"),
        ("c89", "c89-gcc"),
        ("c99", "c99-gcc"),
        ("jade", "openjade-1.4devel"),
        ("nsgmls", "onsgmls"),
        ("sgmlnorm", "osgmlnorm"),
        ("spam", "ospam"),
        ("spent", "ospent"),
        ("yacc", "bison.yacc"),
        ("which", "which.debianutils"),
    ):
        link = os.path.join(bindir, name)
        if not os.path.lexists(link) and os.path.isfile(os.path.join(bindir, target)):
            os.symlink(target, link)

    java_bindir = os.path.join(root, "usr", "lib", "jvm", "default-java", "bin")
    if os.path.isdir(java_bindir):
        for executable in sorted(os.listdir(java_bindir)):
            source = os.path.join(java_bindir, executable)
            link = os.path.join(bindir, executable)
            if os.path.isfile(source) and os.access(source, os.X_OK) and not os.path.lexists(link):
                os.symlink("../lib/jvm/default-java/bin/{}".format(executable), link)

    # libc6-i386 normally creates this through its postinst. Its linker script
    # names /lib/ld-linux.so.2, which resolves through merged-/usr to this link.
    i386_loader = os.path.join(root, "usr", "lib32", "ld-linux.so.2")
    loader_link = os.path.join(root, "usr", "lib", "ld-linux.so.2")
    if os.path.isfile(i386_loader) and not os.path.lexists(loader_link):
        os.makedirs(os.path.dirname(loader_link), exist_ok=True)
        os.symlink("../lib32/ld-linux.so.2", loader_link)

    # xml-core normally creates /etc/xml/catalog from package triggers. Point a
    # payload-composed buildroot at every packaged catalog instead.
    catalog = os.path.join(root, "etc", "xml", "catalog")
    packaged_catalogs = sorted(glob.glob(os.path.join(
        root,
        "usr",
        "share",
        "xml",
        "**",
        "catalog*.xml",
    ), recursive=True))
    if packaged_catalogs and not os.path.lexists(catalog):
        os.makedirs(os.path.dirname(catalog), exist_ok=True)
        with open(catalog, "w", encoding="utf-8") as stream:
            stream.write('<?xml version="1.0"?>\n')
            stream.write('<catalog xmlns="urn:oasis:names:tc:entity:xmlns:xml:catalog">\n')
            for packaged_catalog in packaged_catalogs:
                chroot_path = "/" + os.path.relpath(packaged_catalog, root)
                stream.write("  <nextCatalog catalog={}/><!-- packaged -->\n".format(
                    quoteattr(chroot_path),
                ))
            stream.write("</catalog>\n")

    # sgml-base normally regenerates this supercatalog from package triggers.
    # Without it, docbook-utils falls back to every catalog under
    # /usr/share/sgml, including OpenJade's Unicode SGML declaration.  That
    # declaration has limits too small for DocBook and breaks otherwise valid
    # package documentation builds.
    sgml_catalog = os.path.join(root, "etc", "sgml", "catalog")
    packaged_sgml_catalogs = sorted(glob.glob(os.path.join(
        root,
        "etc",
        "sgml",
        "*.cat",
    )))
    if packaged_sgml_catalogs and not os.path.lexists(sgml_catalog):
        supercatalog = os.path.join(root, "var", "lib", "sgml-base", "supercatalog")
        os.makedirs(os.path.dirname(supercatalog), exist_ok=True)
        with open(supercatalog, "w", encoding="utf-8") as stream:
            stream.write("--\n")
            stream.write("## Generated from packaged SGML catalogs.\n")
            stream.write("--\n")
            for packaged_catalog in packaged_sgml_catalogs:
                chroot_path = "/" + os.path.relpath(packaged_catalog, root)
                stream.write("CATALOG {}\n".format(chroot_path))
        os.symlink("/var/lib/sgml-base/supercatalog", sgml_catalog)

    # ca-certificates normally assembles this bundle in its postinst. Python's
    # bundled requests imports it even for offline package installation.
    ca_bundle = os.path.join(root, "etc", "ssl", "certs", "ca-certificates.crt")
    packaged_certificates = sorted(glob.glob(os.path.join(
        root,
        "usr",
        "share",
        "ca-certificates",
        "**",
        "*.crt",
    ), recursive=True))
    if packaged_certificates and not os.path.lexists(ca_bundle):
        os.makedirs(os.path.dirname(ca_bundle), exist_ok=True)
        with open(ca_bundle, "wb") as output:
            for certificate in packaged_certificates:
                with open(certificate, "rb") as source:
                    contents = source.read()
                output.write(contents)
                if contents and not contents.endswith(b"\n"):
                    output.write(b"\n")


def stage_fakeroot_runtime(
    buildroot: str,
    work: str,
    required: bool = True,
) -> dict[str, str] | None:
    """Copy the target's fakeroot runtime into the shared work mount.

    `fakeroot-sysv` and Debian's multiarch `libfakeroot` layout are
    Debian-family spellings, and an RPM buildroot has neither.  The image
    rules are shared, so the ones that serve every flavor pass
    `required=False` and get None back, which `fakeroot_command` then leaves
    the command unwrapped for; those actions run under the subordinate-ID
    user namespace, which is what the RPM path used before fakeroot existed
    here.

    Detecting absence rather than asking the caller which family it is
    costs nothing in safety: a Debian buildroot that lost its fakeroot fails
    in `deb_rootfs_install`, which requires it, long before any image action
    runs.
    """
    sources = {}
    for name in ("fakeroot-sysv", "faked-sysv"):
        source = os.path.join(buildroot, "usr", "bin", name)
        if not os.path.isfile(source):
            if not required:
                return None
            sys.exit("Debian buildroot has no /usr/bin/{}".format(name))
        sources[name] = source

    libraries = glob.glob(os.path.join(
        buildroot,
        "usr",
        "lib",
        "*",
        "libfakeroot",
        "libfakeroot-sysv.so",
    ))
    if len(libraries) != 1:
        if not required and not libraries:
            return None
        sys.exit(
            "Debian buildroot must contain exactly one libfakeroot-sysv.so, "
            "found {}".format(len(libraries))
        )

    runtime = os.path.join(work, "fakeroot")
    os.makedirs(runtime)
    paths = {}
    for name, source in sources.items():
        destination = os.path.join(runtime, name)
        shutil.copy2(source, destination)
        paths[name] = destination
    library = os.path.join(runtime, "libfakeroot-sysv.so")
    shutil.copy2(libraries[0], library)
    paths["library"] = library
    paths["state"] = os.path.join(runtime, "state")
    return paths


def fakeroot_command(
    runtime: dict[str, str] | None,
    command: list[str],
    load: bool = False,
) -> list[str]:
    if runtime is None:
        return list(command)
    wrapped = [
        runtime["fakeroot-sysv"],
        "-f", runtime["faked-sysv"],
        "-l", runtime["library"],
    ]
    if load:
        wrapped.extend(["-i", runtime["state"]])
    wrapped.extend(["-s", runtime["state"], "--"])
    return wrapped + command


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


def deb_fields(path: str) -> dict[str, str]:
    result = run(
        [require_tool("dpkg-deb"), "--field", path],
        capture_output=True,
        text=True,
    )
    return parse_control(result.stdout)


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


def package_key(deb: str, fields: dict[str, str] | None = None) -> str:
    fields = fields or deb_fields(deb)
    package = fields["Package"]
    architecture = fields["Architecture"]
    if fields.get("Multi-Arch") == "same":
        return "{}:{}".format(package, architecture)
    return package


def extract_control(deb: str, root: str, key: str | None = None) -> None:
    dpkg_deb = require_tool("dpkg-deb")
    with tempfile.TemporaryDirectory(prefix="buckos-deb-control-") as tmp:
        run([dpkg_deb, "--control", deb, tmp])
        key = key or package_key(deb)
        info = os.path.join(root, "var", "lib", "dpkg", "info")
        os.makedirs(info, exist_ok=True)
        for name in CONTROL_FILES:
            source = os.path.join(tmp, name)
            if os.path.isfile(source):
                shutil.copy2(source, os.path.join(info, "{}.{}".format(key, name)))
        with open(os.path.join(info, key + ".list"), "w", encoding="utf-8") as stream:
            for path in payload_paths(deb):
                stream.write(path + "\n")


def status_paragraph(deb: str, fields: dict[str, str] | None = None) -> str:
    fields = dict(fields or deb_fields(deb))
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
        fields = deb_fields(deb)
        key = package_key(deb, fields)
        extract_control(deb, root, key)
        paragraphs[key] = status_paragraph(deb, fields)

    os.makedirs(os.path.dirname(status_path), exist_ok=True)
    with open(status_path, "w", encoding="utf-8") as stream:
        for key in sorted(paragraphs):
            stream.write(paragraphs[key])
            stream.write("\n")
