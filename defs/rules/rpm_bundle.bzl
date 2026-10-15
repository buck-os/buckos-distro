"""Extract RPM payloads for SDK consumption and external package staging."""

load("//defs:providers.bzl", "RpmBundleInfo")

def _pinned_local_rpm_impl(ctx: AnalysisContext) -> list[Provider]:
    out = ctx.actions.declare_output(ctx.attrs.source.basename)
    cmd = cmd_args(ctx.attrs._verify[RunInfo])
    cmd.add("--source", ctx.attrs.source, "--out", out.as_output(), "--sha256", ctx.attrs.sha256)
    ctx.actions.run(
        cmd,
        category = "pinned_local_rpm",
        # re-contract: buildroot-independent -- verifies/copies a declared
        # artifact without consulting any buildroot or running target code.
        allow_cache_upload = True,
    )
    return [DefaultInfo(default_output = out)]

pinned_local_rpm = rule(
    impl = _pinned_local_rpm_impl,
    attrs = {
        "source": attrs.source(),
        "sha256": attrs.string(),
        "_verify": attrs.default_only(attrs.exec_dep(default = "//tools:verify_file")),
    },
)

def _rpm_payload_bundle_impl(ctx: AnalysisContext) -> list[Provider]:
    tree = ctx.actions.declare_output("payload", dir = True)
    archive = ctx.actions.declare_output("payload.tar")
    manifest = ctx.actions.declare_output("manifest.json")
    exports = ctx.actions.write_json("exports.json", ctx.attrs.exports)
    cmd = cmd_args(ctx.attrs._bundle[RunInfo])
    cmd.add("--out", tree.as_output(), "--archive", archive.as_output())
    cmd.add("--manifest", manifest.as_output(), "--exports", exports)
    cmd.add("--architecture", ctx.attrs.architecture)
    cmd.add("--source-date-epoch", ctx.attrs.source_date_epoch)
    for rpm in ctx.attrs.rpms:
        cmd.add("--rpm", rpm)
    export_outputs = {}
    for name in ctx.attrs.export_directories:
        if name not in ctx.attrs.exports:
            fail("export_directories names an unknown export: " + name)
    for name in ctx.attrs.exports:
        if name in ["tree", "archive", "manifest"] or "/" in name or ".." in name:
            fail("invalid or reserved export name: " + name)
        export = ctx.actions.declare_output("exports/" + name, dir = name in ctx.attrs.export_directories)
        export_outputs[name] = export
        cmd.add("--export-output", name, export.as_output())
    ctx.actions.run(
        cmd,
        category = "rpm_payload_bundle",
        # re-contract: buildroot-independent -- extraction of declared RPM
        # artifacts runs no target code, package scripts, or package manager.
        allow_cache_upload = True,
    )
    subtargets = {
        "tree": [DefaultInfo(default_output = tree)],
        "archive": [DefaultInfo(default_output = archive)],
        "manifest": [DefaultInfo(default_output = manifest)],
    }
    for name, export in export_outputs.items():
        subtargets[name] = [DefaultInfo(default_output = export)]
    return [
        DefaultInfo(default_output = archive, sub_targets = subtargets),
        RpmBundleInfo(
            tree = tree,
            archive = archive,
            manifest = manifest,
            architecture = ctx.attrs.architecture,
        ),
    ]

rpm_payload_bundle = rule(
    impl = _rpm_payload_bundle_impl,
    attrs = {
        "architecture": attrs.enum(["aarch64", "x86_64"]),
        "exports": attrs.dict(attrs.string(), attrs.string(), default = {}),
        "export_directories": attrs.list(attrs.string(), default = []),
        "rpms": attrs.list(attrs.source()),
        "source_date_epoch": attrs.string(default = "1700000000"),
        "_bundle": attrs.default_only(attrs.exec_dep(default = "//tools:rpm_bundle")),
    },
)
