# CentOS Hyperscale flavor

CentOS Hyperscale 9 and 10 layer the SIG's `main` repository on the corresponding CentOS Stream BaseOS, AppStream, and CRB repositories. Release 9 uses EPEL and EPEL Next; `centos-release-hyperscale` requires both release packages. Release 10 uses EPEL without EPEL Next.

The flavor has its own lock graph because ordinary CentOS Stream and Hyperscale must coexist at the same release numbers. Hyperscale packages are selected as newer drop-in replacements where the `main` repository publishes them. The default `binary-seed` buildroot includes the release's EPEL RPM macros, source replays use `.hs.el9` or `.hs.el10`, and live images install the Hyperscale release package. Release 10 is the default.

```sh
buck2 build //flavors/centos-hyperscale:hello-9
buck2 build //flavors/centos-hyperscale:iso-live-9
buck2 build //flavors/centos-hyperscale:hello-10
buck2 build //flavors/centos-hyperscale:iso-live-10
buck2 build //flavors/centos-hyperscale:hello \
  -c buckos.flavor=centos-hyperscale
```

Set `[buckos.centos-hyperscale] buildroot = host` only for local development. The binary-seeded buildroot is hermetic and eligible for remote execution.

Regenerate release 9 from BaseOS, AppStream, CRB, Extras Common, EPEL, EPEL Next, and Hyperscale Main binary `primary.xml` metadata:

```sh
PYTHONPATH=tools python3 tools/solve.py \
  --flavor centos-hyperscale --seed-only --strict \
  --binary-primary /path/to/baseos-primary.xml.gz \
  --binary-base https://mirror.stream.centos.org/9-stream/BaseOS/x86_64/os \
  --binary-repo baseos \
  --binary-primary /path/to/appstream-primary.xml.gz \
  --binary-base https://mirror.stream.centos.org/9-stream/AppStream/x86_64/os \
  --binary-repo appstream \
  --binary-primary /path/to/crb-primary.xml.gz \
  --binary-base https://mirror.stream.centos.org/9-stream/CRB/x86_64/os \
  --binary-repo crb \
  --binary-primary /path/to/extras-common-primary.xml.gz \
  --binary-base https://mirror.stream.centos.org/SIGs/9-stream/extras/x86_64/extras-common \
  --binary-repo extras-common \
  --binary-primary /path/to/epel-primary.xml.xz \
  --binary-base https://dl.fedoraproject.org/pub/epel/9/Everything/x86_64 \
  --binary-repo epel \
  --binary-primary /path/to/epel-next-primary.xml.xz \
  --binary-base https://dl.fedoraproject.org/pub/epel/next/9/Everything/x86_64 \
  --binary-repo epel-next \
  --binary-primary /path/to/hyperscale-main-primary.xml.gz \
  --binary-base https://mirror.stream.centos.org/SIGs/9-stream/hyperscale/x86_64/packages-main \
  --binary-repo hyperscale-main \
  --seed-package epel-rpm-macros \
  --override /usr/bin/gdb-add-index=gdb-minimal \
  --override /usr/bin/systemd-sysusers=systemd-sysusers \
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
  --image image-tools=rpm,xorriso,squashfs-tools,dosfstools,mtools,grub2-tools-extra,grub2-efi-x64-modules,grub2-efi-x64,shim-x64,syslinux,syslinux-nonlinux,bash,coreutils,findutils,sed,gawk,grep,diffutils,tar,gzip,xz,cpio,util-linux,filesystem \
  --image live=kernel,systemd,systemd-udev,systemd-resolved,dracut,dracut-live,dracut-squash,dracut-config-generic,bash,coreutils,util-linux,selinux-policy-targeted,policycoreutils,dnf,centos-release-hyperscale,filesystem,shadow-utils,NetworkManager,iproute,iputils,less,vim-minimal,sudo,squashfs-tools,dosfstools,e2fsprogs,kbd,glibc-langpack-en,tzdata,ca-certificates,openssh-server,rootfiles \
  --release 9 --dist-tag .hs.el9 \
  --out flavors/centos-hyperscale/lock/centos-hyperscale-9.lock.json
python3 tools/generate.py \
  flavors/centos-hyperscale/lock/centos-hyperscale-9.lock.json
```

Regenerate release 10 from BaseOS, AppStream, CRB, Extras Common, EPEL, and Hyperscale Main metadata. EPEL 10's rich release dependency contains an `=` operator, so its full expression is recorded as the override key:

```sh
PYTHONPATH=tools python3 tools/solve.py \
  --flavor centos-hyperscale --seed-only --strict \
  --binary-primary /path/to/baseos-primary.xml.gz \
  --binary-base https://mirror.stream.centos.org/10-stream/BaseOS/x86_64/os \
  --binary-repo baseos \
  --binary-primary /path/to/appstream-primary.xml.gz \
  --binary-base https://mirror.stream.centos.org/10-stream/AppStream/x86_64/os \
  --binary-repo appstream \
  --binary-primary /path/to/crb-primary.xml.gz \
  --binary-base https://mirror.stream.centos.org/10-stream/CRB/x86_64/os \
  --binary-repo crb \
  --binary-primary /path/to/extras-common-primary.xml.gz \
  --binary-base https://mirror.stream.centos.org/SIGs/10-stream/extras/x86_64/extras-common \
  --binary-repo extras-common \
  --binary-primary /path/to/epel-primary.xml.zst \
  --binary-base https://dl.fedoraproject.org/pub/epel/10/Everything/x86_64 \
  --binary-repo epel \
  --binary-primary /path/to/hyperscale-main-primary.xml.gz \
  --binary-base https://mirror.stream.centos.org/SIGs/10-stream/hyperscale/x86_64/packages-main \
  --binary-repo hyperscale-main \
  --seed-package epel-rpm-macros \
  --override /usr/bin/gdb-add-index=gdb-minimal \
  --override '(redhat-release with system-release(releasever) = 10)=centos-stream-release' \
  --override /usr/bin/systemd-sysusers=systemd-sysusers \
  --override fips-provider-so=openssl-fips-provider \
  --override glibc-langpack=glibc-minimal-langpack \
  --override kernel-uname-r=kernel-core \
  --override 'libcurl(x86-64)=libcurl-minimal' \
  --override 'libcurl.so.4()(64bit)=libcurl-minimal' \
  --override selinux-policy-any=selinux-policy-targeted \
  --override system-release=centos-stream-release \
  --image image-tools=rpm,xorriso,squashfs-tools,dosfstools,mtools,grub2-tools-extra,grub2-efi-x64-modules,grub2-efi-x64,shim-x64,syslinux,syslinux-nonlinux,bash,coreutils,findutils,sed,gawk,grep,diffutils,tar,gzip,xz,cpio,util-linux,filesystem \
  --image live=kernel,systemd,systemd-udev,systemd-resolved,dracut,dracut-live,dracut-squash,dracut-config-generic,bash,coreutils,util-linux,selinux-policy-targeted,policycoreutils,dnf,centos-release-hyperscale,filesystem,shadow-utils,NetworkManager,iproute,iputils,less,vim-minimal,sudo,squashfs-tools,dosfstools,e2fsprogs,kbd,glibc-langpack-en,tzdata,ca-certificates,openssh-server,rootfiles \
  --release 10 --dist-tag .hs.el10 \
  --out flavors/centos-hyperscale/lock/centos-hyperscale-10.lock.json
python3 tools/generate.py \
  flavors/centos-hyperscale/lock/centos-hyperscale-10.lock.json
```

`[buckos.centos-hyperscale] mirror_base` rewrites the common CentOS mirror root, including both Stream and SIG paths. EPEL URLs keep their Fedora Project bases. Use `package_url_template` to redirect every pinned RPM into one content-addressed store.
