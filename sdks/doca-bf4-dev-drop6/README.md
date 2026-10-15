# DOCA 3.3 BF4 dev-drop6 SDK payload

This opt-in target imports 16 SHA-256-pinned AArch64 RPMs from the requested
NVIDIA Oracle Linux 9 DPU repository. It contains 84 static archives:

| Component | Static archives |
| --- | ---: |
| DOCA Common, Flow, DPDK Bridge, Argp, Management, Telemetry | 10 |
| DPDK 25.11.0 | 73 |
| NVIDIA hardware steering (`nvhws`) | 1 |

RPM hashes were checked against primary repository metadata; primary and
filelists metadata were checked against `repomd.xml`.
The extracted archives contain 852 ELF members, all reporting AArch64.

`packages.bzl` and `packages.lock.json.gz` hold the public upstream URLs and
digests. `libraries.bzl` declares the archive and header paths confirmed by
the extracted RPMs. RPM bytes under `rpms/` are ignored by Git.

## Refresh package pins

Run from the repository root:

```sh
./buck2 run //tools:rpm_bundle_lock -- \
  --repository https://linux.mellanox.com/public/repo/doca/3.3.0-bf4-dev-drop6/oraclelinux9/arm64-dpu/ \
  --architecture aarch64 \
  --repo-cache downloads/doca/bf4-dev-drop6 \
  --package doca-sdk-common --package doca-sdk-common-devel \
  --package doca-sdk-flow --package doca-sdk-flow-devel \
  --package doca-sdk-dpdk-bridge --package doca-sdk-dpdk-bridge-devel \
  --package doca-sdk-argp --package doca-sdk-argp-devel \
  --package doca-sdk-mgmt --package doca-sdk-mgmt-devel \
  --package doca-sdk-telemetry --package doca-sdk-telemetry-devel \
  --package dpdk-community --package dpdk-community-devel \
  --package nvhws --package nvhws-devel \
  --output sdks/doca-bf4-dev-drop6/packages.bzl
```

This refreshes package selection against current metadata. Review changed
pins before using them; NVIDIA may update the repository. To restore exactly
the existing pins into local staging without refreshing them, download each
public URL and verify its recorded digest. For example:

```sh
python3 - <<'PY'
from pathlib import Path
import sys
import urllib.request
sys.path.insert(0, "tools")
from relock import sha256_file
from _lockfile import load_lockfile
root = Path("sdks/doca-bf4-dev-drop6")
for package in load_lockfile(str(root / "packages.lock.json.gz"))["packages"]:
    filename = package["url"].rsplit("/", 1)[1]
    destination = root / "rpms" / package["sha256"] / filename
    if destination.is_file() and sha256_file(destination) == package["sha256"]:
        continue
    destination.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(package["url"], timeout=300) as response:
        destination.write_bytes(response.read())
    if sha256_file(destination) != package["sha256"]:
        destination.unlink()
        raise ValueError("package digest mismatch")
PY
```

## Build without a container or chroot

```sh
./buck2 build //sdks/doca-bf4-dev-drop6:sdk \
  -c buckos.rpm_sdk_local_rpm_dir=rpms --show-output
```

This consumes the staged RPMs as declared Buck inputs and rechecks every
SHA-256 digest during the build. It produces `payload.tar`; `[tree]` and
`[manifest]` select the extracted tree and inventory. The current archive is
46,090,240 bytes. Header directories are shared across the library imports.

With direct upstream access, omit the configuration flag to use SHA-pinned
`http_file` downloads.

## C++ imports and remaining dependencies

Individual archive imports include `:sdk-common`, `:sdk-flow`,
`:sdk-flow-definitions`, `:sdk-dpdk-bridge`, `:sdk-nvhws`, and `:sdk-rte-eal`.
These expose vendor archives and headers. They do not encode the complete
static dependency closure; declare that closure in your consumer or extend
the library definitions using the vendor `.pc` files in the payload.

The inspected `.pc` files declare these external dependencies:

- Common: `libibverbs`, `libmlx5`, `libnl-3.0`, C++ runtime, pthreads and math.
- Flow: Common, `nvhws`, `libibverbs`, `libmlx5`, zlib, C++ runtime and `librt`.
- DPDK: `libmlx5`, `libibverbs`, libpcap and libelf, plus the private link
  options in `libdpdk-libs.pc`.

DOCA and DPDK `.pc` files also specify archives requiring whole-archive
linking; map these to `link_whole` in your final library definitions.

This repository has no rdma-core packages, or DOCA DMA, RDMA, Comch or DevEmu
packages. Those require a separately selected compatible repository. This
payload is therefore an SDK import, not a verified complete static DOCA
application or a runnable NVIDIA daemon bundle. No AArch64 application or
hardware test has been run, and no application package has been published.
