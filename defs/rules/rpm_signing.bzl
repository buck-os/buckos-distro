"""Producer-neutral RPM signing rules.

Source builds already expose one ``*-rpm`` target per binary package. Signing
is a separate release transformation so those unsigned build products remain
stable inputs to dependency resolution and reproducibility comparisons.

An external signer target implements this command contract through RunInfo:

    <signer> rpm-sign --in INPUT.rpm --out OUTPUT.rpm \
        --package-name NAME

The signer must return only after verifying that OUTPUT is an RPM signed by
the identity represented by ``public_key``. Deployments can satisfy the
contract with an HSM-backed service, without exposing private key material or
service-specific details to this repository.
"""

load("//defs:providers.bzl", "RpmFileInfo", "RpmSigningKeyInfo")


def _external_rpm_signing_key_impl(ctx: AnalysisContext) -> list[Provider]:
    command = cmd_args(ctx.attrs.signer[RunInfo])
    command.add(ctx.attrs.signer_args)
    return [
        DefaultInfo(default_output = ctx.attrs.public_key),
        RunInfo(args = command),
        RpmSigningKeyInfo(
            public_key = ctx.attrs.public_key,
            key_id = ctx.attrs.key_id,
            cacheable = ctx.attrs.cacheable,
            local_only = ctx.attrs.local_only,
        ),
    ]


external_rpm_signing_key = rule(
    impl = _external_rpm_signing_key_impl,
    attrs = {
        "signer": attrs.exec_dep(),
        "signer_args": attrs.list(attrs.string(), default = []),
        # Declaring the public half makes the release identity reviewable and
        # gives downstream repository publication a verification artifact.
        "public_key": attrs.source(),
        "key_id": attrs.string(),
        # Release signers are conservatively local and non-cacheable. A test
        # signer using public fixture keys may opt into shared caching.
        "cacheable": attrs.bool(default = False),
        "local_only": attrs.bool(default = True),
    },
)


def _rpm_file_impl(ctx: AnalysisContext) -> list[Provider]:
    if ctx.attrs.signed and not ctx.attrs.signing_key_id:
        fail("a signed rpm_file requires signing_key_id")
    if ctx.attrs.signed and ctx.attrs.verification_key == None:
        fail("a signed rpm_file requires verification_key")
    if not ctx.attrs.signed and ctx.attrs.signing_key_id:
        fail("an unsigned rpm_file cannot declare signing_key_id")
    if not ctx.attrs.signed and ctx.attrs.verification_key != None:
        fail("an unsigned rpm_file cannot declare verification_key")
    return [
        DefaultInfo(default_output = ctx.attrs.rpm),
        RpmFileInfo(
            rpm = ctx.attrs.rpm,
            package_name = ctx.attrs.package_name,
            signed = ctx.attrs.signed,
            signing_key_id = ctx.attrs.signing_key_id or None,
            verification_key = ctx.attrs.verification_key,
        ),
    ]


rpm_file = rule(
    impl = _rpm_file_impl,
    attrs = {
        "rpm": attrs.source(),
        "package_name": attrs.string(),
        "signed": attrs.bool(default = False),
        "signing_key_id": attrs.string(default = ""),
        "verification_key": attrs.option(attrs.source(), default = None),
    },
)


def _rpm_sign_impl(ctx: AnalysisContext) -> list[Provider]:
    source = ctx.attrs.rpm[RpmFileInfo]
    if source.signed:
        fail("{} is already signed by {}".format(source.package_name, source.signing_key_id))

    key = ctx.attrs.signing_key[RpmSigningKeyInfo]
    out = ctx.actions.declare_output(source.package_name + ".rpm")
    command = cmd_args(
        ctx.attrs.signing_key[RunInfo],
        "rpm-sign",
        "--in",
        source.rpm,
        "--out",
        out.as_output(),
        "--package-name",
        source.package_name,
    )
    ctx.actions.run(
        command,
        category = "rpm_sign",
        identifier = ctx.attrs.name,
        # re-contract: buildroot-independent -- this transforms one declared
        # RPM through the selected signing target, which owns both the signer
        # implementation and its execution/cache policy.
        local_only = key.local_only,
        allow_cache_upload = key.cacheable,
    )
    return [
        DefaultInfo(default_output = out, other_outputs = [key.public_key]),
        RpmFileInfo(
            rpm = out,
            package_name = source.package_name,
            signed = True,
            signing_key_id = key.key_id,
            verification_key = key.public_key,
        ),
    ]


rpm_sign = rule(
    impl = _rpm_sign_impl,
    attrs = {
        "rpm": attrs.dep(providers = [RpmFileInfo]),
        "signing_key": attrs.dep(providers = [RpmSigningKeyInfo, RunInfo]),
    },
)
