# Remote execution operations

This directory contains the reproducible deployment and validation assets for a NativeLink v1.6.6 remote cache and execution service running in separate SDME containers.

The deployment uses one control container with persistent CAS and action-cache storage, one native x86_64 worker, and one native AArch64 worker. The NativeLink image is pinned by multi-architecture digest in `nativelink/deployment.json`. Worker registration is blocked unless the production Bubblewrap launcher passes namespace, ownership, architecture, tool, and scratch checks.

## Validate the tracked assets

Run these checks before provisioning:

```sh
python3 tools/nativelink_config.py
python3 infra/remote-execution/scripts/oci_archive.py metadata \
  infra/remote-execution/sdme/offline-oci-archives.json \
  --expect 'ubuntu=docker.io/library/ubuntu@sha256:2260313b31c8c011cd2eebe728008efac1b3982be73eb71348ea2648d2c0e09b' \
  --expect 'nativelink=ghcr.io/tracemachina/nativelink@sha256:5c2e6eca51c6d3ac40b94f703e08a243fd036cc136cc858a99040ca90fa57d61'
bash -n infra/remote-execution/scripts/*.sh
python3 -m unittest discover -s infra/remote-execution/scripts -p '*_test.py'
python3 -m unittest -v infra/remote-execution/tests/smoke_test_test.py
```

## Regenerate offline OCI archives

An operator with public registry access can reproduce the trusted archives for either architecture. The output directory must not already contain any tracked archive filename:

```sh
buck2 run //infra/remote-execution/scripts:oci_acquire -- \
  --architecture x86_64 \
  --metadata infra/remote-execution/sdme/offline-oci-archives.json \
  --output-directory /path/to/offline-oci-output

buck2 run //infra/remote-execution/scripts:oci_acquire -- \
  --architecture aarch64 \
  --metadata infra/remote-execution/sdme/offline-oci-archives.json \
  --output-directory /path/to/offline-oci-output
```

The producer fetches only the digest-pinned parent indexes and selected platform closures. It verifies every descriptor while downloading, writes deterministic USTAR archives, and runs the same admission validator used by provisioning. It publishes mode-0600 files only when their final SHA-256 values and sizes exactly match the tracked metadata.

## Review an SDME plan

The `plan` operation is non-mutating. Use an absolute persistent data path outside a checkout or home directory:

```sh
infra/remote-execution/scripts/sdme-provision.sh plan control \
  --data-root /srv/buckos-re
```

A worker also requires a reachable control address, a native immutable probe sysroot, its deterministic tree digest, and measured scratch admission thresholds:

```sh
infra/remote-execution/scripts/sdme-provision.sh plan worker \
  --data-root /srv/buckos-re \
  --control-address CONTROL_ADDRESS \
  --probe-sysroot /srv/buckos-re/probe-root \
  --probe-sysroot-sha256 SHA256 \
  --min-scratch-bytes BYTES \
  --min-scratch-inodes 0
```

Use `--min-scratch-inodes 0` only for a filesystem such as Btrfs that reports dynamic inode accounting. Fixed-inode filesystems require a measured positive value.

On a fresh worker, prepare the native runtime before creating the immutable probe root. The preparation phase acquires and imports the pinned images, builds the runtime filesystem, and validates its provenance without creating a container or writing service state:

```sh
infra/remote-execution/scripts/sdme-provision.sh prepare-runtime worker \
  --arch x86_64 \
  --data-root /srv/buckos-re

probe_sha256=$(infra/remote-execution/scripts/prepare-worker-probe-root.sh apply \
  --runtime-fs buckos-re-runtime-5c2e6eca51c6 \
  --arch x86_64 \
  --destination /srv/buckos-re/probe-root)

infra/remote-execution/scripts/sdme-provision.sh plan worker \
  --arch x86_64 \
  --data-root /srv/buckos-re \
  --control-address CONTROL_ADDRESS \
  --probe-sysroot /srv/buckos-re/probe-root \
  --probe-sysroot-sha256 "$probe_sha256" \
  --min-scratch-bytes BYTES \
  --min-scratch-inodes 0
```

After reviewing the worker plan, replace `plan` with `apply` while retaining the probe path and digest. Run each mutating command as root and select the host's native architecture.

## Apply the deployment

After reviewing the plan, replace `plan` with `apply` and run as root. Applying imports the pinned Ubuntu and NativeLink images, builds a native runtime rootfs, creates or validates the requested SDME container, installs the tracked configuration, and starts the systemd service.

Control apply waits up to 30 seconds for the running container to report an acceptable private or link-local SDME zone address, polling once per second. A matching running container is reused during this wait. Container disappearance, inventory errors, malformed records, and invalid address data fail immediately.

Registry acquisition is the default. When a registry is unavailable, pass `--ubuntu-oci-archive ABSOLUTE_PATH` or `--nativelink-oci-archive ABSOLUTE_PATH` to select an independently supplied archive for that image. The archive basename, whole-file SHA-256 and size, pinned parent index, selected platform manifest, config, and layer closure must match `sdme/offline-oci-archives.json`. Offline admission is available only for architecture records present in that file.

The provisioner serializes `prepare-runtime` and `apply` with an exclusive lock under the root-owned provision directory. It writes a mode-0600 intent record before publishing an archive pair, imported image filesystem, or built runtime filesystem. An identical retry may discard and recreate only an unpublished partial object covered by that exact record; absent, changed, malformed, or unsafe records remain fail-closed. Records are removed only after full provenance validation. Reuse requires the same acquisition mode and revalidates the archive and both filesystem provenance records. Legacy `.reference` sidecars and untrusted paths are refused. Repeat the same archive options on later runs that reuse an offline deployment.

When any of `http_proxy`, `https_proxy`, `all_proxy`, `no_proxy`, `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, or `NO_PROXY` is set, the runtime build conveys exactly those nonempty variables through an ephemeral mode-0600 file beneath the locked transaction directory because SDME 0.18 does not forward the host environment into `fs build`. Proxy values are never printed, added to the effective build definition, or included in provenance. Every proxy-bearing build uses `--no-cache`, scrubs the in-runtime transport file before capture, removes host transport inputs on handled exit, and scans the exported runtime for the transport path, sentinel, and current proxy values before provenance publication and on reuse. The direct-network build command retains normal SDME resume behavior.

No ports are published by default. Cross-host clients or workers require `--publish`, explicit client and worker CIDR allowlists, an external read-only firewall policy checker, and a trusted encrypted private network unless NativeLink TLS is configured.

Run the control role on the persistent storage host. Run the worker role separately on native x86_64 and AArch64 hosts. Do not advertise an architecture that differs from the host.

## Admission and smoke tests

Use `scripts/check_deployment.py --help` to configure stage-zero health, capabilities, persistent storage, externally supplied worker evidence, OTLP collector, and client inotify checks. The checker is read-only. Its full worker-evidence gate requires a trustworthy source before enabling shared clients. NativeLink v1.6.6 does not expose a read-only snapshot containing connected worker identities and their accepted platform properties, so do not infer registration from worker names or port reachability.

Use `scripts/smoke-test.sh --help` for the bounded rollout. The sequence proves service readiness and cache-only reuse across clean clients. Each architecture stage first forces an uncached remote `/usr/bin/uname -m` probe, verifies the exact requested platform properties and selected execution platform, rejects local fallback or cache hits, and checks the returned canonical architecture. It then runs the small Debian `hostname` build as a separate real-workload check with distinct event evidence. This execution-backed attestation is required before broader clients or long builds are enabled, but it does not replace a future NativeLink worker-snapshot API. The smoke script does not contain an ISO target.

The readiness stage defaults to `scripts/reapi_readiness.py`. The smoke harness invokes it in isolated mode with the explicit `/usr/bin/python3` interpreter, which must provide the distro `python3-grpcio` package on both EL9 and Debian-family clients. Use `--grpc-python` to select a different explicit interpreter; the harness validates that exact executable and imports `grpc` through `-I` before contacting the endpoint. An explicit `--grpc-helper` remains a direct executable override and bypasses the Python-specific check. The helper validates REAPI v2 execution and cache-update capabilities with SHA-256, then uploads and reads back a bounded random blob through CAS and ByteStream. It uses generic byte RPCs and does not require generated protocol modules, `pip`, binary wheels, or server reflection.

Full source and prebuilt image matrices remain disabled until stage zero and every bounded smoke stage pass. Firmware boot tests continue to run on controlled local hosts.
