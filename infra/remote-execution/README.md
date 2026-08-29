# Remote execution operations

This directory contains the reproducible deployment and validation assets for a NativeLink v1.6.6 remote cache and execution service running in separate SDME containers.

The deployment uses one control container with persistent CAS and action-cache storage, one native x86_64 worker, and one native AArch64 worker. The NativeLink image is pinned by multi-architecture digest in `nativelink/deployment.json`. Worker registration is blocked unless the production Bubblewrap launcher passes namespace, ownership, architecture, tool, and scratch checks.

## Validate the tracked assets

Run these checks before provisioning:

```sh
python3 tools/nativelink_config.py
bash -n infra/remote-execution/scripts/*.sh
python3 -m unittest discover -s infra/remote-execution/scripts -p '*_test.py'
python3 -m unittest -v infra/remote-execution/tests/smoke_test_test.py
```

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

## Apply the deployment

After reviewing the plan, replace `plan` with `apply` and run as root. Applying imports the pinned Ubuntu and NativeLink images, builds a native runtime rootfs, creates or validates the requested SDME container, installs the tracked configuration, and starts the systemd service.

No ports are published by default. Cross-host clients or workers require `--publish`, explicit client and worker CIDR allowlists, an external read-only firewall policy checker, and a trusted encrypted private network unless NativeLink TLS is configured.

Run the control role on the persistent storage host. Run the worker role separately on native x86_64 and AArch64 hosts. Do not advertise an architecture that differs from the host.

## Admission and smoke tests

Use `scripts/check_deployment.py --help` to configure stage-zero health, capabilities, persistent storage, worker registration, OTLP collector, and client inotify checks. The checker is read-only and must pass before enabling shared clients.

Use `scripts/smoke-test.sh --help` for the bounded rollout. The sequence proves service readiness, cache-only reuse across clean clients, x86_64 remote execution, and native AArch64 routing with small Debian `hostname` targets. The smoke script does not contain an ISO target.

The readiness stage defaults to `scripts/reapi_readiness.py`. It uses the standard Python `grpcio` transport (`python3-grpcio` on Debian-family clients), validates REAPI v2 execution and cache-update capabilities with SHA-256, then uploads and reads back a bounded random blob through CAS and ByteStream. It uses generic byte RPCs and does not require generated protocol modules or server reflection.

Full source and prebuilt image matrices remain disabled until stage zero and every bounded smoke stage pass. Firmware boot tests continue to run on controlled local hosts.
