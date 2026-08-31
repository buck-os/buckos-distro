"""Shared Debian-family downloads, buildroots, source replay, and images."""

load("//defs:flavor.bzl", "package", "subpackage_deb_target")
load(
    "//defs:architectures.bzl",
    "ARCHITECTURES",
    "DEFAULT_ARCHITECTURE",
    "execution_compatible_with",
    "release_arch_suffix",
    "target_platform",
)
load("//defs:releases.bzl", "iso_volume_label", "release_suffix")
load("//defs/rules/boot.bzl", "initramfs", "kernel_image")
load("//defs/rules/buildroot.bzl", "host_buildroot", "seeded_deb_buildroot")
load("//defs/rules/dsc.bzl", "prebuilt_deb")
load("//defs/rules/image.bzl", "iso_image", "squashfs")
load("//defs/rules/rootfs.bzl", "deb_rootfs")

_DISTRO_SUPPLIERS = {
    "debian": "Organization: Debian",
    "ubuntu": "Organization: Ubuntu",
}

_IMAGE_VARIANTS = ["", "-prebuilt"]

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

def _download(flavor, data, entry, suffix, defined, platform):
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
        default_target_platform = platform,
        visibility = ["PUBLIC"],
    )

def deb_downloads(flavor, data, suffix, platform):
    _validate_flavor(flavor)
    defined = {}
    for entry in _base_debs(data):
        _download(flavor, data, entry, suffix, defined, platform)
    for name in sorted(data.IMAGE_SETS):
        for entry in data.IMAGE_SETS[name]:
            _download(flavor, data, entry, suffix, defined, platform)
    for source in data.SOURCES:
        for entry in source.get("build_deps", []):
            _download(flavor, data, entry, suffix, defined, platform)
        for entry in source["files"]:
            _download(flavor, data, entry, suffix, defined, platform)

def _base_debs(data):
    base = getattr(data, "BASE_DEBS", None)
    if base != None:
        return base
    return data.SEED_DEBS

def _fakeroot_debs(data):
    return [
        entry
        for entry in _base_debs(data)
        if entry["package"] in ("fakeroot", "libfakeroot")
    ]

def _target_cpu(flavor, architecture):
    if architecture == "amd64":
        return "x86_64"
    if architecture == "arm64":
        return "aarch64"
    fail("unsupported {} architecture: {}".format(flavor, architecture))

def _deb_build_type(binary_metadata):
    has_all = False
    has_arch = False
    for package_name in binary_metadata:
        if binary_metadata[package_name]["architecture"] == "all":
            has_all = True
        else:
            has_arch = True
    if has_all and not has_arch:
        return "indep"
    if has_arch and not has_all:
        return "arch"
    return "binary"

def deb_buildroots(flavor, data, suffix, platform, exec_constraints):
    _validate_flavor(flavor)
    target_cpu = _target_cpu(flavor, data.ARCHITECTURE)
    host_buildroot(
        name = "buildroot-host" + suffix,
        target_cpu = target_cpu,
        default_target_platform = platform,
        visibility = ["PUBLIC"],
    )
    seeded_deb_buildroot(
        name = "buildroot-binary-seed" + suffix,
        seed_debs = [":" + entry["target"] + suffix for entry in _base_debs(data)],
        target_cpu = target_cpu,
        default_target_platform = platform,
        visibility = ["PUBLIC"],
    )

def deb_buildroot_target(flavor, suffix):
    provenance = read_config("buckos." + flavor, "buildroot", "binary-seed")
    return ":buildroot-{}{}".format(provenance, suffix)

def deb_packages(
        flavor,
        data,
        suffix,
        platform,
        exec_constraints,
        build_env_by_source = None,
        build_options_by_source = None):
    _validate_flavor(flavor)
    build_env_by_source = build_env_by_source or {}
    build_options_by_source = build_options_by_source or {}
    base_targets = {entry["target"]: True for entry in _base_debs(data)}
    build_deps = {}
    for source in data.SOURCES:
        for entry in source.get("build_deps", []):
            if entry["target"] in base_targets:
                continue
            build_deps[entry["target"]] = entry

    for target in sorted(build_deps):
        entry = build_deps[target]
        prebuilt_deb(
            name = _seedroot_target(entry, suffix),
            deb = ":" + entry["target"] + suffix,
            package_name = entry["package"],
            version = entry["version"],
            architecture = entry["architecture"],
            source_name = entry.get("source_name", entry["source"]),
            source_version = entry.get("source_version", entry["version"]),
            flavor = flavor,
            supplier = _DISTRO_SUPPLIERS[flavor],
            default_target_platform = platform,
            visibility = ["PUBLIC"],
        )

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

        binary_metadata = {
            entry["package"]: entry
            for entry in source.get("binary_metadata", [])
        }
        subpackages = sorted(binary_metadata) if binary_metadata else source["binaries"]
        package(
            name = source["name"] + suffix,
            flavor = flavor,
            dsc = dsc,
            source_files = source_files,
            source_name = source["name"],
            version = source["version"],
            version_full = source.get("version_full", ""),
            release = source["release"],
            subpackages = subpackages,
            binary_metadata = binary_metadata,
            build_env = build_env_by_source.get(source["name"], {}),
            build_options = build_options_by_source.get(source["name"], []),
            build_type = _deb_build_type(binary_metadata),
            build_deps = [
                ":" + _seedroot_target(entry, suffix)
                for entry in source.get("build_deps", [])
                if entry["target"] not in base_targets
            ],
            buildroot = deb_buildroot_target(flavor, suffix),
            homepage = source["homepage"],
            supplier = _DISTRO_SUPPLIERS[flavor],
            src_uri = _download_url(flavor, data, source_blob),
            src_sha256 = source_blob["sha256"],
            default_target_platform = platform,
            exec_compatible_with = exec_constraints,
            visibility = ["PUBLIC"],
        )

def _seedroot_target(entry, suffix):
    return "seedroot-" + entry["target"] + suffix

def deb_images(flavor, data, release, suffix, platform, exec_constraints):
    if "live" not in data.IMAGE_SETS or "image-tools" not in data.IMAGE_SETS:
        return
    buildroot = deb_buildroot_target(flavor, suffix)
    tools = ":buildroot-image-tools" + suffix
    seeded_deb_buildroot(
        name = "buildroot-image-tools" + suffix,
        seed_debs = [
            ":" + entry["target"] + suffix
            for entry in data.IMAGE_SETS["image-tools"] + _fakeroot_debs(data)
        ],
        target_cpu = data.TARGET_CPU,
        default_target_platform = platform,
        visibility = ["PUBLIC"],
    )
    built = {}
    for source in data.SOURCES:
        for entry in source.get("binary_metadata", []):
            previous = built.get(entry["package"])
            if previous != None:
                fail("two source recipes produce {}: {} and {}".format(
                    entry["package"], previous[0]["name"], source["name"],
                ))
            built[entry["package"]] = (source, entry)

    policy = getattr(data, "SOURCE_POLICY", {})
    policy_sets = {name: True for name in policy.get("image_sets", [])}
    exceptions = {
        entry["package"]: entry
        for entry in policy.get("exceptions", [])
    }

    for variant in _IMAGE_VARIANTS:
        debs = []
        for entry in data.IMAGE_SETS["live"]:
            local = built.get(entry["package"]) if variant == "" else None
            if local != None:
                source, binary = local
                if binary.get("source_version") != entry.get("source_version"):
                    fail("{} source recipe version does not match live package {}".format(
                        source["name"], entry["package"],
                    ))
                debs.append(":" + subpackage_deb_target(
                    source["name"] + suffix,
                    source["name"],
                    entry["package"],
                ))
            else:
                if variant == "" and "live" in policy_sets and entry["package"] not in exceptions:
                    fail("live package {} has no source recipe or approved exception".format(entry["package"]))
                debs.append(":" + entry["target"] + suffix)

        rootfs_target = ":rootfs-live" + variant + suffix
        deb_rootfs(
            name = "rootfs-live" + variant + suffix,
            buildroot = buildroot,
            debs = debs,
            default_target_platform = platform,
            exec_compatible_with = exec_constraints,
            visibility = ["PUBLIC"],
        )
        kernel_image(
            name = "kernel-live" + variant + suffix,
            rootfs = rootfs_target,
            default_target_platform = platform,
            visibility = ["PUBLIC"],
        )
        initramfs(
            name = "initramfs-live" + variant + suffix,
            buildroot = buildroot,
            rootfs = rootfs_target,
            generator = "casper" if flavor == "ubuntu" else "live-boot",
            default_target_platform = platform,
            exec_compatible_with = exec_constraints,
            visibility = ["PUBLIC"],
        )
        squashfs(
            name = "squashfs-live" + variant + suffix,
            buildroot = tools,
            rootfs = rootfs_target,
            default_target_platform = platform,
            exec_compatible_with = exec_constraints,
            visibility = ["PUBLIC"],
        )
        iso_image(
            name = "iso-live" + variant + suffix,
            buildroot = tools,
            kernel = ":kernel-live" + variant + suffix,
            initramfs = ":initramfs-live" + variant + suffix,
            squashfs = ":squashfs-live" + variant + suffix,
            volume_label = iso_volume_label("{}-{}-LIVE{}".format(
                flavor.upper(),
                release,
                "-PREBUILT" if variant else "",
            )),
            kernel_args = "console=tty0 {}".format(
                "console=ttyAMA0,115200" if data.TARGET_CPU == "aarch64" else "console=ttyS0,115200",
            ),
            boot_mode = "hybrid" if data.TARGET_CPU == "x86_64" else "uefi",
            layout = flavor,
            target_cpu = data.TARGET_CPU,
            default_target_platform = platform,
            exec_compatible_with = exec_constraints,
            visibility = ["PUBLIC"],
        )

def _data_for(flavor, data_by_release_arch, release, architecture):
    by_arch = data_by_release_arch.get(release)
    data = by_arch.get(architecture) if by_arch != None else None
    if data == None:
        fail(
            "{} release {} has no generated {} data; run tools/deb_lock.py and tools/deb_generate.py".format(flavor, release, architecture),
        )
    if data.DISTRO != flavor:
        fail("{} release {} loaded {} data".format(flavor, release, data.DISTRO))
    if data.TARGET_CPU != architecture:
        fail("{} release {} {} loaded {} data".format(flavor, release, architecture, data.TARGET_CPU))
    return data

def deb_downloads_for(flavor, releases, default, data_by_release_arch):
    for release in releases:
        for architecture in ARCHITECTURES:
            deb_downloads(flavor, _data_for(flavor, data_by_release_arch, release, architecture), release_arch_suffix(release, architecture), target_platform(flavor, release, architecture))
    for release in releases:
        deb_downloads(flavor, _data_for(flavor, data_by_release_arch, release, DEFAULT_ARCHITECTURE), release_suffix(release), target_platform(flavor, release, DEFAULT_ARCHITECTURE))
    deb_downloads(flavor, _data_for(flavor, data_by_release_arch, default, DEFAULT_ARCHITECTURE), "", target_platform(flavor, default, DEFAULT_ARCHITECTURE))

def deb_buildroots_for(flavor, releases, default, data_by_release_arch):
    for release in releases:
        for architecture in ARCHITECTURES:
            deb_buildroots(flavor, _data_for(flavor, data_by_release_arch, release, architecture), release_arch_suffix(release, architecture), target_platform(flavor, release, architecture), execution_compatible_with(architecture))
    for release in releases:
        deb_buildroots(flavor, _data_for(flavor, data_by_release_arch, release, DEFAULT_ARCHITECTURE), release_suffix(release), target_platform(flavor, release, DEFAULT_ARCHITECTURE), execution_compatible_with(DEFAULT_ARCHITECTURE))
    deb_buildroots(flavor, _data_for(flavor, data_by_release_arch, default, DEFAULT_ARCHITECTURE), "", target_platform(flavor, default, DEFAULT_ARCHITECTURE), execution_compatible_with(DEFAULT_ARCHITECTURE))

def deb_packages_for(
        flavor,
        releases,
        default,
        data_by_release_arch,
        build_env_by_source = None,
        build_options_by_source = None):
    for release in releases:
        for architecture in ARCHITECTURES:
            deb_packages(flavor, _data_for(flavor, data_by_release_arch, release, architecture), release_arch_suffix(release, architecture), target_platform(flavor, release, architecture), execution_compatible_with(architecture), build_env_by_source, build_options_by_source)
    for release in releases:
        deb_packages(flavor, _data_for(flavor, data_by_release_arch, release, DEFAULT_ARCHITECTURE), release_suffix(release), target_platform(flavor, release, DEFAULT_ARCHITECTURE), execution_compatible_with(DEFAULT_ARCHITECTURE), build_env_by_source, build_options_by_source)
    deb_packages(flavor, _data_for(flavor, data_by_release_arch, default, DEFAULT_ARCHITECTURE), "", target_platform(flavor, default, DEFAULT_ARCHITECTURE), execution_compatible_with(DEFAULT_ARCHITECTURE), build_env_by_source, build_options_by_source)

def deb_images_for(flavor, releases, default, data_by_release_arch):
    for release in releases:
        for architecture in ARCHITECTURES:
            deb_images(flavor, _data_for(flavor, data_by_release_arch, release, architecture), release, release_arch_suffix(release, architecture), target_platform(flavor, release, architecture), execution_compatible_with(architecture))
    for release in releases:
        deb_images(flavor, _data_for(flavor, data_by_release_arch, release, DEFAULT_ARCHITECTURE), release, release_suffix(release), target_platform(flavor, release, DEFAULT_ARCHITECTURE), execution_compatible_with(DEFAULT_ARCHITECTURE))
    deb_images(flavor, _data_for(flavor, data_by_release_arch, default, DEFAULT_ARCHITECTURE), default, "", target_platform(flavor, default, DEFAULT_ARCHITECTURE), execution_compatible_with(DEFAULT_ARCHITECTURE))
