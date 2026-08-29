# CentOS Hyperscale flavor

CentOS Hyperscale 9 and 10 layer the SIG's `main` repository on the corresponding CentOS Stream repositories. Release 9 also uses EPEL and EPEL Next; release 10 uses EPEL. The flavor has independent x86_64 and AArch64 lock graphs so ordinary CentOS Stream and Hyperscale can coexist at the same release numbers. Release 10 is the default.

```sh
buck2 build //flavors/centos-hyperscale:hello-9-x86_64
buck2 build //flavors/centos-hyperscale:iso-live-10-x86_64
buck2 build -c buckos.aarch64_emulation=true \
  //flavors/centos-hyperscale:iso-live-10-aarch64
```

Release-only and unsuffixed targets are x86_64 compatibility aliases. The default `binary-seed` buildroot is hermetic and eligible for remote execution. Set `[buckos.centos-hyperscale] buildroot = host` only for native local development.

The live rootfs installs a narrow SELinux compatibility module for systemd operations not covered by the matching base policy, including BTF access, Varlink registration, hardware-database reads, and EFI variable probing. Boot tests require enforcing mode with zero AVC denials.

Refresh each architecture from the public repositories recorded in its lockfile:

```sh
python3 tools/rpm_relock.py \
  --template flavors/centos-hyperscale/lock/centos-hyperscale-10-x86_64.lock.json \
  --target-cpu x86_64 \
  --output flavors/centos-hyperscale/lock/centos-hyperscale-10-x86_64.lock.json
python3 tools/rpm_relock.py \
  --template flavors/centos-hyperscale/lock/centos-hyperscale-10-aarch64.lock.json \
  --target-cpu aarch64 \
  --output flavors/centos-hyperscale/lock/centos-hyperscale-10-aarch64.lock.json
```

Use the same command shape for release 9. `rpm_relock.py` regenerates the matching Starlark data unless `--no-generate` is passed.

`[buckos.centos-hyperscale] mirror_base` rewrites the common CentOS mirror root, including Stream and SIG paths. EPEL URLs keep their Fedora Project bases. Use `package_url_template` to redirect every pinned RPM into one content-addressed store.
