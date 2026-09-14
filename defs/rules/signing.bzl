"""Signing identities and image-signing rules.

The key target is the abstraction boundary.  Consumers invoke its RunInfo and
never assume that private key bytes exist as a Buck artifact.  This permits a
checked-in test key, a locally mounted release key, or an HSM/KMS client to
provide the same command interface:

    <signer> ima-manifest --rootfs INPUT.tar --out OUTPUT.pseudo --mode ...
    <signer> pe-sign      --in INPUT.efi    --out OUTPUT.efi

SigningKeyInfo.operations makes partial implementations explicit. A remote
service may implement only the PE operation through Authenticode; an fs-verity
CMS flow is not compatible with Linux IMA signatures.

Production signers should be local-only and non-cacheable.  A remote-executed
action would necessarily send its inputs to the remote CAS, which is never an
acceptable transport for a release private key.
"""

load("//defs:providers.bzl", "SigningKeyInfo")
load("//defs/rules:rootfs.bzl", "rootfs_artifact")


def _file_signing_key_impl(ctx: AnalysisContext) -> list[Provider]:
    command = cmd_args(
        ctx.attrs._signer[RunInfo],
        "--private-key",
        ctx.attrs.private_key,
        "--certificate",
        ctx.attrs.certificate,
        "--evmctl",
        ctx.attrs.evmctl,
        "--pe-signer",
        ctx.attrs.pe_signer,
    )
    return [
        DefaultInfo(default_output = ctx.attrs.certificate),
        RunInfo(args = command),
        SigningKeyInfo(
            certificate = ctx.attrs.certificate,
            key_id = ctx.attrs.key_id,
            operations = ["ima-manifest", "pe-sign"],
            cacheable = ctx.attrs.cacheable,
            local_only = True,
        ),
    ]


file_signing_key = rule(
    impl = _file_signing_key_impl,
    attrs = {
        "private_key": attrs.source(),
        "certificate": attrs.source(),
        "key_id": attrs.string(),
        # File-backed keys always execute locally so their private material is
        # never uploaded as a remote-execution input.  Shared-cache upload is
        # separately opt-in and is appropriate only for public test keys.
        "cacheable": attrs.bool(default = False),
        "evmctl": attrs.string(default = "/usr/bin/evmctl"),
        "pe_signer": attrs.string(default = "/usr/bin/osslsigncode"),
        "_signer": attrs.default_only(
            attrs.exec_dep(default = "//tools:signing_helper"),
        ),
    },
)


def _external_signing_key_impl(ctx: AnalysisContext) -> list[Provider]:
    command = cmd_args(ctx.attrs.signer[RunInfo])
    command.add(ctx.attrs.signer_args)
    return [
        DefaultInfo(default_output = ctx.attrs.certificate),
        RunInfo(args = command),
        SigningKeyInfo(
            certificate = ctx.attrs.certificate,
            key_id = ctx.attrs.key_id,
            operations = ctx.attrs.operations,
            cacheable = ctx.attrs.cacheable,
            local_only = ctx.attrs.local_only,
        ),
    ]


external_signing_key = rule(
    impl = _external_signing_key_impl,
    attrs = {
        # The executable implements the command contract documented above;
        # signer_args can select a key by opaque HSM/KMS identifier.
        "signer": attrs.exec_dep(),
        "signer_args": attrs.list(attrs.string(), default = []),
        "certificate": attrs.source(),
        "key_id": attrs.string(),
        "operations": attrs.list(
            attrs.enum(["ima-manifest", "pe-sign"]),
            default = ["ima-manifest", "pe-sign"],
        ),
        "cacheable": attrs.bool(default = False),
        "local_only": attrs.bool(default = True),
    },
)


def _authenticode_signing_key_impl(ctx: AnalysisContext) -> list[Provider]:
    if ctx.attrs.timeout_ms < 0:
        fail("remote signer timeout_ms must be non-negative")
    command = cmd_args(
        ctx.attrs._signer[RunInfo],
        "--client",
        ctx.attrs.client,
        "--sign-key",
        ctx.attrs.key_name,
        "--certificate",
        ctx.attrs.certificate,
        "--sign-description",
        ctx.attrs.sign_description,
        "--verifier",
        ctx.attrs.verifier,
    )
    if ctx.attrs.tier:
        command.add("--tier", ctx.attrs.tier)
    if ctx.attrs.timeout_ms:
        command.add("--timeout-ms", str(ctx.attrs.timeout_ms))
    return [
        DefaultInfo(default_output = ctx.attrs.certificate),
        RunInfo(args = command),
        SigningKeyInfo(
            certificate = ctx.attrs.certificate,
            key_id = ctx.attrs.key_name,
            # Authenticode is compatible with the PE operation only. A remote
            # fs-verity CMS flow cannot be installed as a security.ima value.
            operations = ["pe-sign"],
            # The call is authenticated as the local user. Do not move it to
            # an RE worker or publish release signatures through action cache.
            cacheable = False,
            local_only = True,
        ),
    ]


authenticode_signing_key = rule(
    impl = _authenticode_signing_key_impl,
    attrs = {
        "key_name": attrs.string(),
        # Keep the public certificate declared and reviewable. The adapter
        # verifies that the service used this identity before returning output.
        "certificate": attrs.source(),
        "sign_description": attrs.string(default = "BuckOS Secure Boot"),
        # Deployment-owned path to the remote signing client executable.
        "client": attrs.string(),
        # Empty lets the client use its own signed service configuration.
        "tier": attrs.string(default = ""),
        "timeout_ms": attrs.int(default = 0),
        "verifier": attrs.string(default = "/usr/bin/osslsigncode"),
        "_signer": attrs.default_only(
            attrs.exec_dep(default = "//tools:authenticode_signer"),
        ),
    },
)


def _ima_manifest_impl(ctx: AnalysisContext) -> list[Provider]:
    rootfs = rootfs_artifact(ctx.attrs.rootfs)
    key = ctx.attrs.signing_key[SigningKeyInfo]
    if "ima-manifest" not in key.operations:
        fail("signing key {} does not support ima-manifest".format(key.key_id))
    out = ctx.actions.declare_output(ctx.attrs.name + ".pseudo")
    command = cmd_args(
        ctx.attrs.signing_key[RunInfo],
        "ima-manifest",
        "--rootfs",
        rootfs,
        "--out",
        out.as_output(),
        "--mode",
        ctx.attrs.mode,
    )
    ctx.actions.run(
        command,
        category = "ima_manifest",
        identifier = ctx.attrs.name,
        # re-contract: buildroot-independent -- the rootfs is read as a tar
        # archive and all signing dependencies and execution policy come from
        # the selected signing-key target.
        local_only = key.local_only,
        allow_cache_upload = key.cacheable,
    )
    return [DefaultInfo(default_output = out)]


ima_manifest = rule(
    impl = _ima_manifest_impl,
    attrs = {
        "rootfs": attrs.dep(),
        "signing_key": attrs.dep(providers = [SigningKeyInfo, RunInfo]),
        # `all` matches appraise_tcb. `executables` also includes ELF shared
        # objects and is safe only with a correspondingly narrower policy.
        "mode": attrs.enum(["executables", "all"], default = "all"),
    },
)


def _efi_sign_impl(ctx: AnalysisContext) -> list[Provider]:
    source = ctx.attrs.efi[DefaultInfo].default_outputs[0]
    key = ctx.attrs.signing_key[SigningKeyInfo]
    if "pe-sign" not in key.operations:
        fail("signing key {} does not support pe-sign".format(key.key_id))
    out = ctx.actions.declare_output(ctx.attrs.name + ".efi")
    command = cmd_args(
        ctx.attrs.signing_key[RunInfo],
        "pe-sign",
        "--in",
        source,
        "--out",
        out.as_output(),
    )
    ctx.actions.run(
        command,
        category = "efi_sign",
        identifier = ctx.attrs.name,
        # re-contract: buildroot-independent -- this transforms one declared
        # PE artifact through the selected signing-key target, which owns the
        # signer implementation and its execution policy.
        local_only = key.local_only,
        allow_cache_upload = key.cacheable,
    )
    return [DefaultInfo(default_output = out)]


efi_sign = rule(
    impl = _efi_sign_impl,
    attrs = {
        "efi": attrs.dep(),
        "signing_key": attrs.dep(providers = [SigningKeyInfo, RunInfo]),
    },
)
