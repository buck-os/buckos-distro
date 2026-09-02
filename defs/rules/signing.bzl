"""Signing identities and image-signing rules.

The key target is the abstraction boundary.  Consumers invoke its RunInfo and
never assume that private key bytes exist as a Buck artifact.  This permits a
checked-in test key, a locally mounted release key, or an HSM/KMS client to
provide the same command interface:

    <signer> ima-manifest --rootfs INPUT.tar --out OUTPUT.pseudo --mode ...
    <signer> pe-sign      --in INPUT.efi    --out OUTPUT.efi

Production signers should be local-only and non-cacheable.  A remote-executed
action would necessarily send its inputs to the remote CAS, which is never an
acceptable transport for a release private key.
"""

load("//defs:providers.bzl", "SigningKeyInfo")


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
        "cacheable": attrs.bool(default = False),
        "local_only": attrs.bool(default = True),
    },
)


def _ima_manifest_impl(ctx: AnalysisContext) -> list[Provider]:
    rootfs = ctx.attrs.rootfs[DefaultInfo].default_outputs[0]
    key = ctx.attrs.signing_key[SigningKeyInfo]
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
