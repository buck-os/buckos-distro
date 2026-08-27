"""Ubuntu package downloads, buildroots, and source replay targets."""

load("//defs:flavor.bzl", "package")
load("//defs:releases.bzl", "release_suffix")
load("//defs/rules/buildroot.bzl", "host_buildroot", "seeded_deb_buildroot")

_PACKAGE_URL_TEMPLATE = read_config("buckos.ubuntu", "package_url_template", "")

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

def _render_package_url(template, release, entry):
    if "{sha256}" not in template:
        fail("[buckos.ubuntu] package_url_template must contain {sha256}")

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
        fail("[buckos.ubuntu] package_url_template contains an unsupported placeholder: {}".format(template))

    url = template
    for placeholder, value in replacements.items():
        url = url.replace(placeholder, value)
    return url

def _download_url(data, entry):
    if _PACKAGE_URL_TEMPLATE:
        return _render_package_url(_PACKAGE_URL_TEMPLATE, data.RELEASE, entry)
    return entry["url"]

def _download(data, entry, suffix, defined):
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
        urls = [_download_url(data, entry)],
        sha256 = entry["sha256"],
        size_bytes = entry["size"],
        visibility = ["PUBLIC"],
    )

def ubuntu_downloads(data, suffix):
    defined = {}
    for entry in data.SEED_DEBS:
        _download(data, entry, suffix, defined)
    for source in data.SOURCES:
        for entry in source["files"]:
            _download(data, entry, suffix, defined)

def _target_cpu(architecture):
    if architecture == "amd64":
        return "x86_64"
    if architecture == "arm64":
        return "aarch64"
    fail("unsupported Ubuntu architecture: {}".format(architecture))

def ubuntu_buildroots(data, suffix):
    target_cpu = _target_cpu(data.ARCHITECTURE)
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

def ubuntu_buildroot_target(suffix):
    provenance = read_config("buckos.ubuntu", "buildroot", "binary-seed")
    return ":buildroot-{}{}".format(provenance, suffix)

def ubuntu_packages(data, suffix):
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
            fail("ubuntu source {} has no .dsc file".format(source["name"]))
        if source_blob == None:
            source_blob = source["files"][0]

        package(
            name = source["name"] + suffix,
            flavor = "ubuntu",
            dsc = dsc,
            source_files = source_files,
            source_name = source["name"],
            version = source["version"],
            release = source["release"],
            subpackages = source["binaries"],
            buildroot = ubuntu_buildroot_target(suffix),
            homepage = source["homepage"],
            src_uri = _download_url(data, source_blob),
            src_sha256 = source_blob["sha256"],
            visibility = ["PUBLIC"],
        )

def _data_for(data_by_release, release):
    data = data_by_release.get(release)
    if data == None:
        fail(
            "ubuntu release {} has no generated data; run tools/ubuntu_lock.py and tools/ubuntu_generate.py".format(release),
        )
    return data

def ubuntu_downloads_for(releases, default, data_by_release):
    for release in releases:
        ubuntu_downloads(_data_for(data_by_release, release), release_suffix(release))
    ubuntu_downloads(_data_for(data_by_release, default), "")

def ubuntu_buildroots_for(releases, default, data_by_release):
    for release in releases:
        ubuntu_buildroots(_data_for(data_by_release, release), release_suffix(release))
    ubuntu_buildroots(_data_for(data_by_release, default), "")

def ubuntu_packages_for(releases, default, data_by_release):
    for release in releases:
        ubuntu_packages(_data_for(data_by_release, release), release_suffix(release))
    ubuntu_packages(_data_for(data_by_release, default), "")
