"""Binary remote-upstream overlays tied to an exact base distribution lock."""

load("//defs:deb_family.bzl", "deb_buildroot_target", "deb_download_url")
load("//defs:rpm_family.bzl", "rpm_buildroot_target", "rpm_download_url")
load("//defs/rules:rootfs.bzl", "deb_rootfs", "rootfs")

def _base_downloads(family, data):
    entries = []
    if family == "deb":
        entries.extend(getattr(data, "BASE_DEBS", getattr(data, "SEED_DEBS", [])))
        for source in data.SOURCES:
            entries.extend(source.get("build_deps", []))
            entries.extend(source.get("files", []))
    else:
        entries.extend(data.SEED_RPMS)
        entries.extend(getattr(data, "VARIANT_SEED_RPMS", []))
        entries.extend(data.SOURCE_RPMS)
    for package_set in data.IMAGE_SETS.values():
        entries.extend(package_set)
    return entries

def _repo_base(base_data, overlay):
    result = dict(base_data.REPO_BASE)
    for repository in overlay.REPOSITORIES:
        if repository.get("kind") == "rpm":
            result[repository["name"]] = repository["base"]
    return result

def upstream_overlay_rootfs(
        name,
        flavor,
        base_data,
        overlay,
        suffix = "",
        default_target_platform = None,
        exec_compatible_with = []):
    """Define a pinned, prebuilt rootfs from one generated overlay."""
    if name != overlay.NAME:
        fail("overlay target name {} does not match lock name {}".format(name, overlay.NAME))
    if flavor != overlay.BASE["flavor"]:
        fail("overlay {} targets {}, not {}".format(name, overlay.BASE["flavor"], flavor))
    if str(base_data.RELEASE) != overlay.BASE["release"]:
        fail("overlay {} targets release {}, not {}".format(
            name,
            overlay.BASE["release"],
            base_data.RELEASE,
        ))
    if base_data.TARGET_CPU != overlay.BASE["architecture"]:
        fail("overlay {} targets {}, not {}".format(
            name,
            overlay.BASE["architecture"],
            base_data.TARGET_CPU,
        ))
    if base_data.LOCK_SHA256 != overlay.BASE["sha256"]:
        fail(("overlay {} was solved against a different base lock; " +
              "re-run tools/upstream_lock.py").format(name))

    existing = {}
    for entry in _base_downloads(overlay.FAMILY, base_data):
        existing[entry["target"]] = entry["sha256"]

    rpm_url_data = None
    if overlay.FAMILY == "rpm":
        rpm_url_data = struct(
            RELEASE = base_data.RELEASE,
            REPO_BASE = _repo_base(base_data, overlay),
        )

    for entry in overlay.PACKAGES:
        target = entry["target"]
        previous = existing.get(target)
        if previous != None:
            if previous != entry["sha256"]:
                fail("{}: base and overlay give the same target different digests".format(target))
            continue
        filename = (
            entry["filename"]
            if overlay.FAMILY == "deb"
            else entry["location"].split("/")[-1]
        )
        kwargs = {
            "name": target + suffix,
            "out": filename,
            "sha256": entry["sha256"],
            "urls": [
                deb_download_url(flavor, base_data, entry)
                if overlay.FAMILY == "deb"
                else rpm_download_url(flavor, rpm_url_data, entry)
            ],
            "default_target_platform": default_target_platform,
            "visibility": ["PUBLIC"],
        }
        if overlay.FAMILY == "deb":
            kwargs["size_bytes"] = entry["size"]
        native.http_file(**kwargs)

    artifacts = [":" + entry["target"] + suffix for entry in overlay.PACKAGES]
    if overlay.FAMILY == "deb":
        deb_rootfs(
            name = "rootfs-" + name + suffix,
            architecture = base_data.TARGET_CPU,
            buildroot = deb_buildroot_target(flavor, suffix),
            debs = artifacts,
            flavor = flavor,
            package_provenance = "upstream-binary",
            release = base_data.RELEASE,
            role = "base",
            default_target_platform = default_target_platform,
            exec_compatible_with = exec_compatible_with,
            visibility = ["PUBLIC"],
        )
    elif overlay.FAMILY == "rpm":
        rootfs(
            name = "rootfs-" + name + suffix,
            architecture = base_data.TARGET_CPU,
            buildroot = rpm_buildroot_target(flavor, suffix),
            flavor = flavor,
            package_provenance = "upstream-binary",
            release = base_data.RELEASE,
            role = "base",
            rpms = artifacts,
            selinux_modules = (
                ["//flavors/centos-hyperscale:systemd-260-compat.cil"]
                if flavor == "centos-hyperscale"
                else []
            ),
            default_target_platform = default_target_platform,
            exec_compatible_with = exec_compatible_with,
            visibility = ["PUBLIC"],
        )
    else:
        fail("unsupported upstream overlay family: {}".format(overlay.FAMILY))
