# Ubuntu flavor

Ubuntu 26.04 (`resolute`) supports source replay and live ISO construction on x86_64 and AArch64. The live image uses Ubuntu's `casper` stack.

```sh
buck2 build //flavors/ubuntu:hello-26.04-x86_64
buck2 build //flavors/ubuntu:iso-live-26.04-x86_64
buck2 build -c buckos.aarch64_emulation=true \
  //flavors/ubuntu:iso-live-26.04-aarch64
```

Release-only and unsuffixed targets are x86_64 compatibility aliases. The default `binary-seed` buildroot is remotely executable and cacheable. Host provenance remains available for native local development.

Regenerate the locks on a system with APT, the Ubuntu archive keyring, and working public repository access:

```sh
PYTHONPATH=tools python3 tools/deb_lock.py \
  --distro ubuntu --release 26.04 --codename resolute --architecture amd64 \
  --source hello \
  --image image-tools=xorriso,squashfs-tools,dosfstools,mtools,grub-pc-bin,grub-efi-amd64-bin,isolinux,syslinux-common \
  --image live=linux-generic,systemd-sysv,casper,openssh-server,sudo,vim-tiny,iproute2,iputils-ping,ca-certificates \
  --output flavors/ubuntu/lock/ubuntu-26.04-x86_64.lock.json
PYTHONPATH=tools python3 tools/deb_lock.py \
  --distro ubuntu --release 26.04 --codename resolute --architecture arm64 \
  --source hello \
  --image image-tools=xorriso,squashfs-tools,dosfstools,mtools,grub-efi-arm64-bin \
  --image live=linux-generic,systemd-sysv,casper,openssh-server,sudo,vim-tiny,iproute2,iputils-ping,ca-certificates \
  --output flavors/ubuntu/lock/ubuntu-26.04-aarch64.lock.json
python3 tools/deb_generate.py flavors/ubuntu/lock/ubuntu-26.04-x86_64.lock.json
python3 tools/deb_generate.py flavors/ubuntu/lock/ubuntu-26.04-aarch64.lock.json
```

`[buckos.ubuntu] package_url_template` accepts `{sha256}`, `{sha256_12}`, `{filename}`, `{stem}`, `{ext}`, and `{release}`.
