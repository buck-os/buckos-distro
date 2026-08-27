"""Shared Debian-family downloads, buildroots, and source replay targets."""

load("//defs:flavor.bzl", "package")
load("//defs:releases.bzl", "release_suffix")
load("//defs/rules/buildroot.bzl", "host_buildroot", "seeded_deb_buildroot")

_DISTRO_SUPPLIERS = {
    "debian": "Organization: Debian",
    "ubuntu": "Organization: Ubuntu",
}

_PATH_ESCAPES = {
    " ": "%20",
    "%": "%25",
    "#": "%23",
    "?": "%3F",
}

def _escape(text):
    out = ""
    for char in text.elems():
        out += _PATH_ESCAPES.get(char, char)
    return out

def _filename_parts(filename):
    if "." not in filename:
        return filename, ""
    stem, extension = filename.rsplit(".", 1)
    return stem, "." + extension

def _validate_flavor(flavor):
    if flavor not in _DISTRO_SUPPLIERS:
        fail("unsupported Debian-family flavor: {}".format(flavor))

def _render_package_url(flavor, template, release, entry):
    if "{sha256}" not in template:
        fail("[buckos.{}] package_url_template must contain {{sha256}}".format(flavor))

    stem, extension = _filename_parts(entry["filename"])
    replacements = {
        "{ext}": _escape(extension),
        "{filename}": _escape(entry["filename"]),
        "{release}": _escape(release),
        "{sha256}": entry["sha256"],
        "{sha256_12}": entry["sha256"][:12],
        "{stem}": _escape(stem),
    }
    remaining = template
    for placeholder in replacements:
        remaining = remaining.replace(placeholder, "")
    if "{" in remaining or "}" in remaining:
        fail("[buckos.{}] package_url_template contains an unsupported placeholder: {}".format(flavor, template))

    url = template
    for placeholder, value in replacements.items():
        url = url.replace(placeholder, value)
    return url

def _download_url(flavor, data, entry):
    template = read_config("buckos." + flavor, "package_url_template", "")
    if template:
        return _render_package_url(flavor, template, data.RELEASE, entry)
    return entry["url"]

def _download(flavor, data, entry, suffix, defined):
    name = entry["target"] + suffix
    previous = defined.get(name)
    if previous != None:
        if previous != entry["sha256"]:
            fail("{}: two different pins ({} and {})".format(name, previous, entry["sha256"]))
        return
    defined[name] = entry["sha256"]
    native.http_file(
        name = name,
        out = entry["filename"],
        urls = [_download_url(flavor, data, entry)],
        sha256 = entry["sha256"],
        size_bytes = entry["size"],
        visibility = ["PUBLIC"],
    )

def deb_downloads(flavor, data, suffix):
    _validate_flavor(flavor)
    defined = {}
    for entry in data.SEED_DEBS:
        _download(flavor, data, entry, suffix, defined)
    for source in data.SOURCES:
        for entry in source["files"]:
            _download(flavor, data, entry, suffix, defined)

def _target_cpu(flavor, architecture):
    if architecture == "amd64":
        return "x86_64"
    if architecture == "arm64":
        return "aarch64"
    fail("unsupported {} architecture: {}".format(flavor, architecture))

def deb_buildroots(flavor, data, suffix):
    _validate_flavor(flavor)
    target_cpu = _target_cpu(flavor, data.ARCHITECTURE)
    host_buildroot(
        name = "buildroot-host" + suffix,
        target_cpu = target_cpu,
        visibility = ["PUBLIC"],
    )
    seeded_deb_buildroot(
        name = "buildroot-binary-seed" + suffix,
        seed_debs = [":" + entry["target"] + suffix for entry in data.SEED_DEBS],
        target_cpu = target_cpu,
        visibility = ["PUBLIC"],
    )

def deb_buildroot_target(flavor, suffix):
    provenance = read_config("buckos." + flavor, "buildroot", "binary-seed")
    return ":buildroot-{}{}".format(provenance, suffix)

def deb_packages(flavor, data, suffix):
    _validate_flavor(flavor)
    for source in data.SOURCES:
        dsc = None
        source_files = []
        source_blob = None
        for entry in source["files"]:
            label = ":" + entry["target"] + suffix
            if entry["filename"].endswith(".dsc"):
                dsc = label
            else:
                source_files.append(label)
            if ".orig.tar" in entry["filename"] and not entry["filename"].endswith(".asc"):
                source_blob = entry
        if dsc == None:
            fail("{} source {} has no .dsc file".format(flavor, source["name"]))
        if source_blob == None:
            source_blob = source["files"][0]

        package(
            name = source["name"] + suffix,
            flavor = flavor,
            dsc = dsc,
            source_files = source_files,
            source_name = source["name"],
            version = source["version"],
            release = source["release"],
            subpackages = source["binaries"],
            buildroot = deb_buildroot_target(flavor, suffix),
            homepage = source["homepage"],
            supplier = _DISTRO_SUPPLIERS[flavor],
            src_uri = _download_url(flavor, data, source_blob),
            src_sha256 = source_blob["sha256"],
            visibility = ["PUBLIC"],
        )

def _data_for(flavor, data_by_release, release):
    data = data_by_release.get(release)
    if data == None:
        fail(
            "{} release {} has no generated data; run tools/deb_lock.py and tools/deb_generate.py".format(flavor, release),
        )
    if data.DISTRO != flavor:
        fail("{} release {} loaded {} data".format(flavor, release, data.DISTRO))
    return data

def deb_downloads_for(flavor, releases, default, data_by_release):
    for release in releases:
        deb_downloads(flavor, _data_for(flavor, data_by_release, release), release_suffix(release))
    deb_downloads(flavor, _data_for(flavor, data_by_release, default), "")

def deb_buildroots_for(flavor, releases, default, data_by_release):
    for release in releases:
        deb_buildroots(flavor, _data_for(flavor, data_by_release, release), release_suffix(release))
    deb_buildroots(flavor, _data_for(flavor, data_by_release, default), "")

def deb_packages_for(flavor, releases, default, data_by_release):
    for release in releases:
        deb_packages(flavor, _data_for(flavor, data_by_release, release), release_suffix(release))
    deb_packages(flavor, _data_for(flavor, data_by_release, default), "")
