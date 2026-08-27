# Debian flavor

Debian 13 (`trixie`) uses the shared Debian-family source replay path. The `//flavors/debian:hello` target downloads the pinned Debian source package and binary buildroot, verifies the `.dsc` source manifest, runs `dpkg-buildpackage -b`, and exposes the resulting `hello` package as an installroot.

```sh
buck2 build //flavors/debian:hello -c buckos.flavor=debian
```

Regenerate the lock inside a matching Debian 13 rootfs with `deb-src` enabled:

```sh
PYTHONPATH=tools python3 tools/deb_lock.py --distro debian --release 13 --codename trixie --source hello --output flavors/debian/lock/debian-13.lock.json
python3 tools/deb_generate.py flavors/debian/lock/debian-13.lock.json
```

The default `binary-seed` buildroot is remotely cacheable. Host provenance remains available for local development. `[buckos.debian] package_url_template` accepts `{sha256}`, `{sha256_12}`, `{filename}`, `{stem}`, `{ext}`, and `{release}`.
