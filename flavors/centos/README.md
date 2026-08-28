# CentOS Stream flavor

CentOS Stream 9 with EPEL Next and CentOS Stream 10 use the shared RPM source-replay and image paths. Release 9 layers BaseOS, AppStream, CRB, EPEL, and EPEL Next; its buildroot includes `epel-rpm-macros`, and its live image installs `epel-next-release` and `epel-release`. The EPEL Next repository remains available to dependency resolution without forcing an unrelated `.el9.next` package into the baseline image.

Release 10 remains the default. Both releases have SHA-256-pinned binary buildroots, checked-in SRPM replay fixtures, and live ISO targets. Release 10 has been boot-verified through BIOS and UEFI with SELinux enforcing; release 9 uses the same image pipeline.

```sh
buck2 build //flavors/centos:hello-9
buck2 build //flavors/centos:iso-live-9
buck2 build //flavors/centos:hello -c buckos.flavor=centos
buck2 build //flavors/centos:iso-live-10
```

The default `binary-seed` buildroot is hermetic and eligible for remote execution. Set `[buckos.centos] buildroot = host` only for local development.

Regenerate the CentOS Stream 9 with EPEL Next buildroot and image closures from current BaseOS, AppStream, CRB, EPEL, and EPEL Next binary `primary.xml` metadata:

```sh
PYTHONPATH=tools python3 tools/solve.py --flavor centos --seed-only --strict \
  --binary-primary /path/to/baseos-primary.xml.gz --binary-base https://mirror.stream.centos.org/9-stream/BaseOS/x86_64/os --binary-repo baseos \
  --binary-primary /path/to/appstream-primary.xml.gz --binary-base https://mirror.stream.centos.org/9-stream/AppStream/x86_64/os --binary-repo appstream \
  --binary-primary /path/to/crb-primary.xml.gz --binary-base https://mirror.stream.centos.org/9-stream/CRB/x86_64/os --binary-repo crb \
  --binary-primary /path/to/epel-primary.xml.xz --binary-base https://dl.fedoraproject.org/pub/epel/9/Everything/x86_64 --binary-repo epel \
  --binary-primary /path/to/epel-next-primary.xml.xz --binary-base https://dl.fedoraproject.org/pub/epel/next/9/Everything/x86_64 --binary-repo epel-next \
  --seed-package epel-rpm-macros \
  --override /usr/bin/gdb-add-index=gdb-minimal \
  --override fips-provider-so=openssl-fips-provider \
  --override glibc-langpack=glibc-minimal-langpack \
  --override kernel-uname-r=kernel-core \
  --override 'libcurl(x86-64)=libcurl-minimal' \
  --override 'libcurl.so.4()(64bit)=libcurl-minimal' \
  --override 'libgpgme.so.11()(64bit)=gpgme' \
  --override 'libgpgme.so.11(GPGME_1.0)(64bit)=gpgme' \
  --override 'libgpgme.so.11(GPGME_1.1)(64bit)=gpgme' \
  --override selinux-policy-any=selinux-policy-targeted \
  --override system-release=centos-stream-release \
  --image-override live:/usr/bin/systemd-sysusers=systemd \
  --image image-tools=rpm,xorriso,squashfs-tools,dosfstools,mtools,grub2-tools-extra,grub2-efi-x64-modules,grub2-efi-x64,shim-x64,syslinux,syslinux-nonlinux,bash,coreutils,findutils,sed,gawk,grep,diffutils,tar,gzip,xz,cpio,util-linux,filesystem \
  --image live=kernel,systemd,systemd-udev,systemd-resolved,dracut,dracut-live,dracut-squash,dracut-config-generic,bash,coreutils,util-linux,selinux-policy-targeted,policycoreutils,dnf,centos-stream-release,epel-next-release,filesystem,shadow-utils,NetworkManager,iproute,iputils,less,vim-minimal,sudo,squashfs-tools,dosfstools,e2fsprogs,kbd,glibc-langpack-en,tzdata,ca-certificates,openssh-server,rootfiles \
  --release 9 --dist-tag .el9 \
  --out flavors/centos/lock/centos-9.lock.json
python3 tools/generate.py flavors/centos/lock/centos-9.lock.json
```

Regenerate the checked-in buildroot and image closures from current CentOS Stream 10 BaseOS, AppStream, and CRB binary `primary.xml` metadata:

```sh
PYTHONPATH=tools python3 tools/solve.py --flavor centos --seed-only --strict \
  --binary-primary /path/to/baseos-primary.xml.gz --binary-base https://mirror.stream.centos.org/10-stream/BaseOS/x86_64/os --binary-repo baseos \
  --binary-primary /path/to/appstream-primary.xml.gz --binary-base https://mirror.stream.centos.org/10-stream/AppStream/x86_64/os --binary-repo appstream \
  --binary-primary /path/to/crb-primary.xml.gz --binary-base https://mirror.stream.centos.org/10-stream/CRB/x86_64/os --binary-repo crb \
  --override /usr/bin/gdb-add-index=gdb-minimal \
  --override fips-provider-so=openssl-fips-provider \
  --override glibc-langpack=glibc-minimal-langpack \
  --override kernel-uname-r=kernel-core \
  --override 'libcurl(x86-64)=libcurl-minimal' \
  --override 'libcurl.so.4()(64bit)=libcurl-minimal' \
  --override selinux-policy-any=selinux-policy-targeted \
  --override system-release=centos-stream-release \
  --image-override live:/usr/bin/systemd-sysusers=systemd \
  --image image-tools=rpm,xorriso,squashfs-tools,dosfstools,mtools,grub2-tools-extra,grub2-efi-x64-modules,grub2-efi-x64,shim-x64,syslinux,syslinux-nonlinux,bash,coreutils,findutils,sed,gawk,grep,diffutils,tar,gzip,xz,cpio,util-linux,filesystem \
  --image live=kernel,systemd,systemd-udev,systemd-resolved,dracut,dracut-live,dracut-squash,dracut-config-generic,bash,coreutils,util-linux,selinux-policy-targeted,policycoreutils,dnf,centos-stream-release,filesystem,shadow-utils,NetworkManager,iproute,iputils,less,vim-minimal,sudo,squashfs-tools,dosfstools,e2fsprogs,kbd,glibc-langpack-en,tzdata,ca-certificates,openssh-server,rootfiles \
  --release 10 --dist-tag .el10 \
  --out flavors/centos/lock/centos-10.lock.json
python3 tools/generate.py flavors/centos/lock/centos-10.lock.json
```

`[buckos.centos] package_url_template` supports `{sha256}`, `{sha256_12}`, `{filename}`, `{stem}`, `{ext}`, and `{release}`. `mirror_base` is a common mirror root above `9-stream` and `10-stream`; it redirects CentOS Stream repositories without changing the lock. Use `package_url_template` for one content-addressed store spanning both CentOS and EPEL repositories.
