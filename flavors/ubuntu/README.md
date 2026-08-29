# Ubuntu flavor

Ubuntu 26.04 (`resolute`) supports source replay and live ISO construction on x86_64 and AArch64. The live image uses Ubuntu's `casper` stack.

```sh
buck2 build //flavors/ubuntu:hello-26.04-x86_64
buck2 build //flavors/ubuntu:iso-live-26.04-x86_64
buck2 build //flavors/ubuntu:iso-live-prebuilt-26.04-x86_64
buck2 build -c buckos.aarch64_emulation=true \
  //flavors/ubuntu:iso-live-26.04-aarch64
```

The unsuffixed `rootfs-live`, `kernel-live`, `initramfs-live`, `squashfs-live`, and `iso-live` targets consume locally built DEBs. Their `-prebuilt` siblings consume only pinned Ubuntu archive DEBs. Release-only and unsuffixed targets are x86_64 compatibility aliases. The default `binary-seed` buildroot is remotely executable and cacheable. Host provenance remains available for native local development.

x86_64 rebuilds 175 of 197 live payload packages from 113 exact live source identities. AArch64 rebuilds 174 of 194 live payload packages from 112 exact live source identities. The explicit `hello` compatibility fixture adds one source recipe to each lock. Both architectures pin 18 opaque split firmware payloads and two Canonical-signed kernel artifacts. x86_64 additionally pins AMD and Intel microcode. The `linux-firmware` metapackage remains source-built.

Regenerate the locks on a system with APT, the Ubuntu archive keyring, and working public repository access:

```bash
common_exception_args=(
  --source-exception '{"package":"linux-firmware-amd-graphics","source":"linux-firmware-amd-graphics@20260319.git217ca6e4-0ubuntu3.1","kind":"firmware","reason":"Contains opaque firmware for AMD and ATI graphics devices."}'
  --source-exception '{"package":"linux-firmware-amd-misc","source":"linux-firmware-amd-misc@20260319.git217ca6e4-0ubuntu1.1","kind":"firmware","reason":"Contains opaque firmware for AMD NPU accelerators."}'
  --source-exception '{"package":"linux-firmware-broadcom-wireless","source":"linux-firmware-broadcom-wireless@20260319.git217ca6e4-0ubuntu1.1","kind":"firmware","reason":"Contains opaque firmware for Broadcom and Cypress Wi-Fi and Bluetooth adapters."}'
  --source-exception '{"package":"linux-firmware-intel-graphics","source":"linux-firmware-intel-graphics@20260319.git217ca6e4-0ubuntu2.1","kind":"firmware","reason":"Contains opaque firmware for Intel graphics, IPU, and VSC processors."}'
  --source-exception '{"package":"linux-firmware-intel-misc","source":"linux-firmware-intel-misc@20260319.git217ca6e4-0ubuntu1.2","kind":"firmware","reason":"Contains opaque firmware for miscellaneous Intel devices and adapters."}'
  --source-exception '{"package":"linux-firmware-intel-wireless","source":"linux-firmware-intel-wireless@20260319.git217ca6e4-0ubuntu2.1","kind":"firmware","reason":"Contains opaque firmware for Intel Wi-Fi and Bluetooth adapters."}'
  --source-exception '{"package":"linux-firmware-marvell-prestera","source":"linux-firmware-marvell-prestera@20260319.git217ca6e4-0ubuntu1.2","kind":"firmware","reason":"Contains opaque firmware for Marvell Prestera ASIC devices."}'
  --source-exception '{"package":"linux-firmware-marvell-wireless","source":"linux-firmware-marvell-wireless@20260319.git217ca6e4-0ubuntu1.1","kind":"firmware","reason":"Contains opaque firmware for Marvell and NXP Wi-Fi adapters."}'
  --source-exception '{"package":"linux-firmware-mediatek","source":"linux-firmware-mediatek@20260319.git217ca6e4-0ubuntu1.2","kind":"firmware","reason":"Contains opaque firmware for MediaTek Wi-Fi, Bluetooth, Ethernet, and SoC devices."}'
  --source-exception '{"package":"linux-firmware-mellanox-spectrum","source":"linux-firmware-mellanox-spectrum@20260319.git217ca6e4-0ubuntu1.1","kind":"firmware","reason":"Contains opaque firmware for Mellanox Spectrum switches."}'
  --source-exception '{"package":"linux-firmware-misc","source":"linux-firmware-misc@20260319.git217ca6e4-0ubuntu2.2","kind":"firmware","reason":"Contains opaque firmware for miscellaneous devices and adapters."}'
  --source-exception '{"package":"linux-firmware-netronome","source":"linux-firmware-netronome@20260319.git217ca6e4-0ubuntu1.1","kind":"firmware","reason":"Contains opaque firmware for Netronome Ethernet adapters."}'
  --source-exception '{"package":"linux-firmware-nvidia-graphics","source":"linux-firmware-nvidia-graphics@20260319.git217ca6e4-0ubuntu1.1","kind":"firmware","reason":"Contains opaque firmware for Nvidia graphics devices."}'
  --source-exception '{"package":"linux-firmware-qlogic","source":"linux-firmware-qlogic@20260319.git217ca6e4-0ubuntu1.1","kind":"firmware","reason":"Contains opaque firmware for QLogic SCSI, Fibre Channel, InfiniBand, and Ethernet adapters."}'
  --source-exception '{"package":"linux-firmware-qualcomm-graphics","source":"linux-firmware-qualcomm-graphics@20260319.git217ca6e4-0ubuntu1.1","kind":"firmware","reason":"Contains opaque firmware for Qualcomm graphics and video processors."}'
  --source-exception '{"package":"linux-firmware-qualcomm-misc","source":"linux-firmware-qualcomm-misc@20260319.git217ca6e4-0ubuntu2.1","kind":"firmware","reason":"Contains opaque firmware for miscellaneous Qualcomm devices."}'
  --source-exception '{"package":"linux-firmware-qualcomm-wireless","source":"linux-firmware-qualcomm-wireless@20260319.git217ca6e4-0ubuntu1.2","kind":"firmware","reason":"Contains opaque firmware for Qualcomm and Atheros Wi-Fi and Bluetooth adapters."}'
  --source-exception '{"package":"linux-firmware-realtek","source":"linux-firmware-realtek@20260319.git217ca6e4-0ubuntu1.2","kind":"firmware","reason":"Contains opaque firmware for Realtek Wi-Fi, Bluetooth, Ethernet, and audio adapters."}'
  --source-exception $'{"package":"linux-image-7.0.0-30-generic","source":"linux-signed@7.0.0-30.30","kind":"signed-artifact","reason":"Contains the generic kernel image signed with Canonical\'s private key; the source build also requires unavailable linux-generate signing inputs."}'
  --source-exception '{"package":"linux-main-modules-zfs-7.0.0-30-generic","source":"linux-main-signed@7.0.0-30.30","kind":"signed-artifact","reason":"Contains Canonical-signed ZFS kernel modules; the recipe requires unavailable linux-main-generate and downloads the signed module archive."}'
)
x86_exception_args=(
  --source-exception '{"package":"amd64-microcode","source":"amd64-microcode@3.20251202.1ubuntu2","kind":"firmware","reason":"Contains opaque platform firmware and microcode for AMD CPUs and SoCs."}'
  --source-exception '{"package":"intel-microcode","source":"intel-microcode@3.20260210.1ubuntu2","kind":"firmware","reason":"Contains opaque processor microcode for Intel CPUs."}'
  "${common_exception_args[@]}"
)
PYTHONPATH=tools python3 tools/deb_lock.py \
  --distro ubuntu --release 26.04 --codename resolute --architecture amd64 \
  --source hello --source-set live \
  "${x86_exception_args[@]}" \
  --image image-tools=xorriso,squashfs-tools,dosfstools,mtools,grub-pc-bin,grub-efi-amd64-bin,isolinux,syslinux-common \
  --image live=linux-generic,systemd-sysv,casper,openssh-server,sudo,vim-tiny,iproute2,iputils-ping,ca-certificates \
  --output flavors/ubuntu/lock/ubuntu-26.04-x86_64.lock.json
PYTHONPATH=tools python3 tools/deb_lock.py \
  --distro ubuntu --release 26.04 --codename resolute --architecture arm64 \
  --source hello --source-set live \
  "${common_exception_args[@]}" \
  --image image-tools=xorriso,squashfs-tools,dosfstools,mtools,grub-efi-arm64-bin \
  --image live=linux-generic,systemd-sysv,casper,openssh-server,sudo,vim-tiny,iproute2,iputils-ping,ca-certificates \
  --output flavors/ubuntu/lock/ubuntu-26.04-aarch64.lock.json
python3 tools/deb_generate.py flavors/ubuntu/lock/ubuntu-26.04-x86_64.lock.json
python3 tools/deb_generate.py flavors/ubuntu/lock/ubuntu-26.04-aarch64.lock.json
```

`[buckos.ubuntu] package_url_template` accepts `{sha256}`, `{sha256_12}`, `{filename}`, `{stem}`, `{ext}`, and `{release}`.
