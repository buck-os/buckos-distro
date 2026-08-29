# CentOS Stream flavor

CentOS Stream 9 and 10 use the shared RPM source-replay and live-image pipeline on x86_64 and AArch64. Release 9 layers BaseOS, AppStream, CRB, EPEL, and EPEL Next. Release 10 layers BaseOS, AppStream, CRB, and EPEL. Release 10 is the default.

```sh
buck2 build //flavors/centos:hello-9-x86_64
buck2 build //flavors/centos:iso-live-9-x86_64
buck2 build -c buckos.aarch64_emulation=true \
  //flavors/centos:iso-live-10-aarch64
```

Release-only and unsuffixed targets are x86_64 compatibility aliases. The default `binary-seed` buildroot is hermetic and eligible for remote execution. Set `[buckos.centos] buildroot = host` only for native local development.

Refresh each architecture from the public repositories recorded in its lockfile:

```sh
python3 tools/rpm_relock.py \
  --template flavors/centos/lock/centos-9-x86_64.lock.json \
  --target-cpu x86_64 \
  --output flavors/centos/lock/centos-9-x86_64.lock.json
python3 tools/rpm_relock.py \
  --template flavors/centos/lock/centos-9-aarch64.lock.json \
  --target-cpu aarch64 \
  --output flavors/centos/lock/centos-9-aarch64.lock.json
```

Use the same command shape for release 10. `rpm_relock.py` regenerates the matching Starlark data unless `--no-generate` is passed.

`[buckos.centos] package_url_template` supports `{sha256}`, `{sha256_12}`, `{filename}`, `{stem}`, `{ext}`, and `{release}`. `mirror_base` is a common mirror root above `9-stream` and `10-stream`; it redirects CentOS Stream repositories without changing the lock. Use `package_url_template` for one content-addressed store spanning CentOS and EPEL repositories.
