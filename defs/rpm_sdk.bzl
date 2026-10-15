"""Pinned vendor RPMs and ordinary C++ prebuilt-library targets."""

load("//defs/rules:rpm_bundle.bzl", "pinned_local_rpm", "rpm_payload_bundle")

def rpm_sdk(name, architecture, packages = [], rpms = [], libraries = {}, source_date_epoch = "1700000000", **kwargs):
    """libraries maps target suffixes to archive, headers, deps and flags.

    No automatic dependency solve: static link requirements must be expressed
    in deps/exported_linker_flags using the vendor's static pkg-config data.
    """
    rpm_inputs = list(rpms)
    local_rpm_dir = read_config("buckos", "rpm_sdk_local_rpm_dir", "")
    for index, package in enumerate(packages):
        rpm_name = "{}-rpm-{}".format(name, index)
        filename = package["url"].split("/")[-1]
        if local_rpm_dir:
            pinned_local_rpm(
                name = rpm_name,
                source = "{}/{}/{}".format(local_rpm_dir, package["sha256"], filename),
                sha256 = package["sha256"],
            )
        else:
            native.http_file(
                name = rpm_name,
                urls = [package["url"]],
                sha256 = package["sha256"],
                out = filename,
            )
        rpm_inputs.append(":" + rpm_name)
    if not rpm_inputs:
        fail("rpm_sdk requires pinned packages or declared RPM inputs")
    exports = {}
    export_directories = []
    header_exports = {}
    library_headers = {}
    for suffix, library in libraries.items():
        exports[suffix + "-archive"] = library["archive"]
        library_headers[suffix] = []
        for path in library.get("headers", []):
            if path not in header_exports:
                export_name = "headers-{}".format(len(header_exports))
                header_exports[path] = export_name
                exports[export_name] = path
                export_directories.append(export_name)
            library_headers[suffix].append(header_exports[path])
    rpm_payload_bundle(
        name = name,
        architecture = architecture,
        rpms = rpm_inputs,
        exports = exports,
        export_directories = export_directories,
        source_date_epoch = source_date_epoch,
        **kwargs
    )
    for suffix, library in libraries.items():
        native.prebuilt_cxx_library(
            name = name + "-" + suffix,
            static_lib = ":{}[{}-archive]".format(name, suffix),
            header_dirs = [
                ":{}[{}]".format(name, export_name)
                for export_name in library_headers[suffix]
            ],
            preferred_linkage = "static",
            link_whole = library.get("link_whole", False),
            deps = library.get("deps", []),
            exported_linker_flags = library.get("exported_linker_flags", []),
            exported_preprocessor_flags = library.get("exported_preprocessor_flags", []),
            **kwargs
        )
