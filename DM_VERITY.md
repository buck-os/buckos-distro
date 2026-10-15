# dm-verity

`buckos-distro` can protect the complete live SquashFS with a deterministic
dm-verity version 1 hash tree. The feature covers RPM-family dracut images,
Debian live-boot images, Ubuntu casper images, BIOS boot, ordinary UEFI boot,
and signed Unified Kernel Image (UKI) boot.

## Trust model

dm-verity verifies each data block as the live root is read. A changed data or
hash-tree block fails verification according to the selected corruption
policy. The root hash is the trust anchor.

A signed UKI embeds the root hash and all dm-verity table parameters in its
authenticated command line. When firmware trusts the UKI signer and Secure
Boot is enforcing, replacing the SquashFS, hash tree, or command line cannot
silently substitute another root filesystem.

The unsigned BIOS and GRUB paths also use dm-verity, but their root hash is in
an unauthenticated boot configuration. Those paths detect accidental damage
and inconsistent media. They do not stop an attacker who can replace both the
media payload and its boot configuration.

dm-verity authenticates immutable filesystem blocks. It does not authenticate
mutable state outside the SquashFS, and it does not replace IMA when a policy
must appraise files introduced after the verified root is mounted.

## Enable it

Add the following to `.buckconfig.local`:

```ini
[buckos.security]
  dm_verity = true
```

Every generated live image then gains a `verity-*` target. The corresponding
ISO consumes the padded data image and its separate hash tree, and every
initramfs receives the adapter for its distro's live-root generator. For
example:

```sh
buck2 build //flavors/fedora:iso-live-44-x86_64
buck2 build //flavors/debian:iso-live-13-x86_64
buck2 build //flavors/ubuntu:iso-live-26.04-x86_64
```

The generated artifact targets expose the complete dm-verity contract:

```sh
buck2 build //flavors/fedora:verity-live-44-x86_64
buck2 build //flavors/fedora:verity-live-44-x86_64[hash-tree]
buck2 build //flavors/fedora:verity-live-44-x86_64[metadata]
buck2 build //flavors/fedora:verity-live-44-x86_64[root-hash]
```

The target's default output is the block-aligned data image. Its subtargets are
the no-superblock hash tree, a machine-readable `buckos.dm-verity.v1` metadata
document, and the root hash as hexadecimal text.

To make the root hash an authenticated trust anchor, enable Secure Boot as
described in [SECURE_BOOT.md](SECURE_BOOT.md). The signed UKI and ISO rules use
the same typed `VerityInfo` artifact, so the authenticated parameters and the
staged data and tree cannot come from different Buck targets.

## Policy settings

The defaults are reproducible and work with the checked-in distro kernels:

```ini
[buckos.security]
  dm_verity = true
  dm_verity_algorithm = sha256
  dm_verity_data_block_size = 4096
  dm_verity_hash_block_size = 4096
  dm_verity_salt = auto
  dm_verity_corruption_policy = eio
```

`dm_verity_algorithm` accepts `sha256` or `sha512`. Block sizes must be powers
of two from 512 through 65536 bytes, and the hash block must hold a power-of-two
number of digests. `auto` derives a public, per-image salt from the complete
input and therefore remains deterministic. An even-length hexadecimal salt is
also accepted for deployments with an established format.

The corruption policies are:

- `eio`: return an I/O error for a corrupted block.
- `panic`: ask the kernel to panic on corruption.
- `restart`: ask the kernel to restart on corruption.

The formatter emits the kernel's standard version 1, separate hash-device,
no-superblock layout. It does not require host `veritysetup`, is safe for
remote execution, and includes a fixed multi-level interoperability vector
accepted by `veritysetup verify`.

## Direct rule use

Consumers can apply the transform outside the flavor macros:

```python
load("//defs/rules:image.bzl", "dm_verity_image")

dm_verity_image(
    name = "verified-root",
    image = ":filesystem-squashfs",
    algorithm = "sha256",
    corruption_policy = "eio",
)
```

Pass that target through the `verity` attribute of `iso_image` and
`uki_image`, and build the corresponding `initramfs` with `dm_verity = True`.
Analysis rejects an ISO or UKI whose initramfs does not advertise dm-verity
support.

## Kernel requirements and verification

The boot kernel needs device mapper, dm-verity, loop devices, the chosen hash
algorithm, and the live-media filesystem driver. In kernel configuration terms
this normally includes `CONFIG_BLK_DEV_DM`, `CONFIG_DM_VERITY`,
`CONFIG_CRYPTO_SHA256` or `CONFIG_CRYPTO_SHA512`, `CONFIG_BLK_DEV_LOOP`, and
`CONFIG_ISO9660_FS`, either built in or available to the initramfs. The
supported distro kernels provide these facilities. A custom `KernelInfo`
producer is responsible for declaring a compatible kernel and module tree.

Boot verification checks that `dmsetup table buckos-root` reports a verity
target. With dm-verity enabled, every generated firmware test requires that
marker from the guest. The formatter tests cover one-block and multi-level
trees, deterministic output, malformed metadata, exact early-boot table
construction, initramfs integration for all three generators, ISO graph
wiring, and signed-UKI command-line binding.
