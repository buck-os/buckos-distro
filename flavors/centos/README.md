# CentOS Stream flavor

CentOS Stream 10 uses the shared RPM source-replay and image paths with buildroots assembled from SHA-256-pinned BaseOS and AppStream packages. The `//flavors/centos:hello` target builds the checked-in SRPM fixture with the Stream 10 compiler, RPM macros, and `.el10` distribution tag. The live image uses pinned binary RPMs and boots through both BIOS and UEFI with SELinux enforcing.

```sh
buck2 build //flavors/centos:hello -c buckos.flavor=centos
buck2 build //flavors/centos:iso-live-10
```

The default `binary-seed` buildroot is hermetic and eligible for remote execution. Set `[buckos.centos] buildroot = host` only for local development.

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

`[buckos.centos] package_url_template` supports `{sha256}`, `{sha256_12}`, `{filename}`, `{stem}`, `{ext}`, and `{release}`. `mirror_base` can redirect the canonical Stream mirror prefix without changing the lock.
