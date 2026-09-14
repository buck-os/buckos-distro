# Secure Boot

buckos-distro can produce live media whose UEFI entry point is a signed
Unified Kernel Image (UKI). This is a production image path, not a test-only
wrapper: firmware loads the UKI directly from `EFI/BOOT/BOOTX64.EFI` or
`EFI/BOOT/BOOTAA64.EFI`.

## Trust boundary

The repository owns the generic pipeline: architecture validation, UKI
assembly, signing-rule contracts, ISO placement, and firmware-level tests. A
deployment owns the signing identity and firmware trust enrollment. Private
release keys and service-specific credentials do not belong in this
repository or Buck's content-addressed storage.

A signing target provides `SigningKeyInfo` and `RunInfo`. File-backed keys are
useful for local tests. Production should select `external_signing_key` or
`authenticode_signing_key`, which can invoke an HSM or signing service while
exposing only a stable public certificate to the build graph.

`authenticode_signing_key` accepts the signing client and verifier as Buck
executable dependencies (`client_target` and `verifier_target`) so an internal
build does not depend on ambient host paths. Installed executable paths remain
available for deployments outside a monorepo.

## Configuration

Define or import the signing target, then select it in `.buckconfig.local`:

```ini
[buckos.security]
  secure_boot_signing_key = //keys:secureboot-release-key
  efi_stub_x86_64 = //third-party/systemd:linuxx64.efi.stub
  efi_stub_aarch64 = //third-party/systemd:linuxaa64.efi.stub
```

An explicit stub is optional when the configured `KernelInfo` already carries
an architecture-matching `efi_stub`. The UKI embeds the kernel, initramfs,
minimal OS release data, live-root arguments, image arguments, and additive
kernel-producer `boot_args`. Assembly uses `objdump` and `objcopy` from the
target distro buildroot and rejects a stub for the wrong architecture.

A prebuilt kernel producer can expose both trust inputs without teaching the
image pipeline about its build system:

```python
kernel_artifacts(
    name = "production-kernel",
    architecture = select({
        "//platforms:is_x86_64": "x86_64",
        "//platforms:is_aarch64": "aarch64",
    }),
    image = select(...),
    modules = select(...),
    release = select(...),
    efi_stub = select(...),
    ima_certificate = "//keys:ima-signing-certificate",
    boot_args = ["lsm=selinux,bpf"],
)
```

The EFI stub and kernel image remain architecture-selected artifacts, while
the IMA certificate is an explicit producer attestation checked against the
rootfs signer during image composition.

Enabling `secure_boot_signing_key` changes the UEFI path of every generated
live ISO. The x86_64 BIOS path remains available. The direct UKI path supports
one kernel per ISO because it does not include a signed boot manager or menu.

Secure Boot authenticates the UKI, including its kernel, initramfs, and command
line. The live SquashFS remains a separate ISO payload and is not authenticated
by Secure Boot alone. Deployments that require runtime file authenticity must
also enable the repository's IMA path or add a measured root mechanism such as
dm-verity. The BIOS path on hybrid media is likewise outside the UEFI Secure
Boot trust chain.

With IMA enabled, the final rootfs's regular files are signed and emitted as
`security.ima` xattrs in the SquashFS. The exact public certificate is placed
in the initramfs for loading into the kernel's dedicated IMA keyring. IMA mode
therefore requires a configured custom `KernelInfo`; an ordinary distro kernel
has no deployment-specific trust assertion. A prebuilt-kernel adapter must
declare that same certificate only after its kernel configuration enables
appraisal, `CONFIG_IMA_LOAD_X509`, and `CONFIG_IMA_READ_POLICY`; the image
graph rejects a missing or different declaration. `linux_kernel` also places
the public certificate in the system trust roots so the restricted IMA keyring
will accept it during early boot. Deployments should use identities with
separate private keys and authorization policies for IMA, module signing, and
Secure Boot even when their public trust hierarchy is shared.

The boot-test overlay is signed from its final composed rootfs as well. With
IMA configured, firmware tests require both the IMA subsystem and the
`ima_appraise=enforce ima_appraise_tcb` policy selection reported by the
guest, while the production-image phase must still reach its normal login
milestone under that policy.

## Other bootloaders

`iso_image.secure_boot_image` consumes `EfiImageInfo`, not a GRUB- or UKI-
specific type. `uki_image` plus `efi_sign` is the built-in producer, and
`efi_image` can adapt another self-contained, already signed EFI executable.
This covers bootloaders whose complete trusted configuration is contained in
that executable.

For an unsigned EFI application, use `efi_image(signed = False)` and pass that
target to `efi_sign`; the result has the same signed `EfiImageInfo` contract as
a UKI. Ordinary executables and shared libraries in the rootfs are instead
signed by the IMA manifest path.

A shim plus GRUB arrangement is a chain of several signed binaries and often
loads mutable configuration. It needs a provider describing the whole chain,
its trust relationships, and its files before buckos-distro can validate it
with the same strength. Supplying only one signed file must not be presented as
verification of such a chain.

## Verification

Secure Boot tests require firmware code and a writable variable-store template
whose enrolled database trusts the configured signing certificate:

```ini
[buckos]
  ovmf_secure_code = /path/to/OVMF_CODE.secboot.fd
  ovmf_secure_vars = /path/to/OVMF_VARS.enrolled.fd
  aarch64_secure_uefi = /path/to/AAVMF_CODE.secboot.fd
  aarch64_secure_vars = /path/to/AAVMF_VARS.enrolled.fd
```

Each test copies the variable store before QEMU starts, so a boot cannot mutate
the configured template. The production ISO must reach its normal login
milestone. The instrumented ISO must additionally report
`secure_boot=enabled`, read from the UEFI `SecureBoot` variable inside the
guest. Missing firmware paths, a disabled firmware policy, an untrusted UKI,
or a boot that bypasses enforcement therefore fails the test.
