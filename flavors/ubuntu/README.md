# Ubuntu flavor

Ubuntu 26.04 (`resolute`) has a source replay path backed by a pinned binary buildroot. The `//flavors/ubuntu:hello` target downloads the release's Debian source package, verifies the `.dsc` manifest, runs `dpkg-buildpackage -b`, and exposes the resulting `hello` package as an installroot.

```sh
buck2 build //flavors/ubuntu:hello -c buckos.flavor=ubuntu
```

`tools/ubuntu_lock.py` asks APT to resolve the source build dependencies plus the essential base system against an empty status database. It records every selected source and binary artifact by URL and SHA-256. `tools/ubuntu_generate.py` converts that lockfile into the pure Starlark data loaded by the flavor.

The `binary-seed` buildroot is the default and is eligible for remote execution. Set `[buckos.ubuntu] buildroot = host` for local development against the machine's installed Debian toolchain; host-provenance actions are local-only and never uploaded to a shared cache.

Regenerate the lock inside a matching Ubuntu 26.04 rootfs with `deb-src` enabled:

```sh
PYTHONPATH=tools python3 tools/ubuntu_lock.py --release 26.04 --codename resolute --source hello --output flavors/ubuntu/lock/ubuntu-26.04.lock.json
python3 tools/ubuntu_generate.py flavors/ubuntu/lock/ubuntu-26.04.lock.json
```

`[buckos.ubuntu] package_url_template` accepts the same `{sha256}`, `{sha256_12}`, `{filename}`, `{stem}`, `{ext}`, and `{release}` placeholders as Fedora.
