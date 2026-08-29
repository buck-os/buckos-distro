# Debian flavor

Debian 13 (`trixie`) supports source replay and live ISO construction on x86_64 and AArch64. The live image uses Debian's `live-boot` and `live-config` stack.

```sh
buck2 build //flavors/debian:hello-13-x86_64
buck2 build //flavors/debian:iso-live-13-x86_64
buck2 build -c buckos.aarch64_emulation=true \
  //flavors/debian:iso-live-13-aarch64
```

Release-only and unsuffixed targets are x86_64 compatibility aliases. The default `binary-seed` buildroot is remotely executable and cacheable. Host provenance remains available for native local development.

Regenerate the locks on a system with APT, the Debian archive keyring, and working public repository access:

```sh
PYTHONPATH=tools python3 tools/deb_lock.py \
  --distro debian --release 13 --codename trixie --architecture amd64 \
  --source hello \
  --image image-tools=xorriso,squashfs-tools,dosfstools,mtools,grub-pc-bin,grub-efi-amd64-bin,isolinux,syslinux-common \
  --image live=linux-image-amd64,systemd-sysv,live-boot,live-config,openssh-server,sudo,vim-tiny,iproute2,iputils-ping,ca-certificates \
  --output flavors/debian/lock/debian-13-x86_64.lock.json
PYTHONPATH=tools python3 tools/deb_lock.py \
  --distro debian --release 13 --codename trixie --architecture arm64 \
  --source hello \
  --image image-tools=xorriso,squashfs-tools,dosfstools,mtools,grub-efi-arm64-bin \
  --image live=linux-image-arm64,systemd-sysv,live-boot,live-config,openssh-server,sudo,vim-tiny,iproute2,iputils-ping,ca-certificates \
  --output flavors/debian/lock/debian-13-aarch64.lock.json
python3 tools/deb_generate.py flavors/debian/lock/debian-13-x86_64.lock.json
python3 tools/deb_generate.py flavors/debian/lock/debian-13-aarch64.lock.json
```

`[buckos.debian] package_url_template` accepts `{sha256}`, `{sha256_12}`, `{filename}`, `{stem}`, `{ext}`, and `{release}`.
