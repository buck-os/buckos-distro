# Remote execution deployment

This repository is ready to use a standard Remote Execution API backend for shared action caching and remote execution. NativeLink is the reference implementation for the first deployment, but Buck-facing configuration remains backend-neutral.

The initial deployment uses one trusted control host and private networking to native worker hosts. It consists of one persistent scheduler/cache service, one native x86_64 worker, and one native AArch64 worker. Adding hosts uses the same Buck platform properties and does not require graph changes.

## Goals

- Reuse deterministic package, rootfs, initramfs, squashfs, and ISO actions across independent checkouts and clean clients.
- Run hermetic `binary-seed` actions on architecture-matched workers.
- Keep host-provenance actions local and excluded from shared cache upload.
- Isolate upstream package recipes with the same Linux namespace contract used by local builds.
- Make cache hits, worker selection, capacity, and failures observable.

Firmware boot tests remain a separate validation tier. The initial deployment builds and caches their ISO inputs remotely, then runs QEMU tests on controlled local hosts with the required firmware and virtualization support.

## Topology

```text
                         persistent storage
                        +-------------------+
                        | CAS + action cache |
                        +---------+---------+
                                  |
Buck2 clients ---- REAPI scheduler/gateway
                                  |
                    +-------------+-------------+
                    |                           |
             x86_64 worker                AArch64 worker
             isolated actions             isolated actions
```

The reference NativeLink deployment uses:

| Component | Responsibility | Persistence |
| --- | --- | --- |
| Scheduler/gateway | REAPI endpoint, action scheduling, worker registration | Configuration only |
| CAS/action cache | Content-addressed inputs, outputs, and action results | Required persistent volume |
| x86_64 worker | Executes `platform.arch=x86_64` actions | Disposable worker state plus large scratch volume |
| AArch64 worker | Executes `platform.arch=aarch64` actions | Disposable worker state plus large scratch volume |
| Client | Runs Buck2 analysis, uploads inputs, and requests actions | Ordinary Buck client state |

Pin every service image by digest. Do not use floating image tags. The CAS volume must survive service and worker replacement; worker scratch may be discarded.

## Repository and deployment boundary

Repository-owned configuration defines action eligibility and scheduling properties:

- `platform.OSFamily=linux`
- `platform.arch=x86_64` or `platform.arch=aarch64`
- `buck2-default` as the initial use case
- `BuildrootInfo.hermetic` as the source of cache-upload and local-only policy

Deployment-owned configuration defines:

- service addresses and instance name;
- TLS certificates or private-network transport policy;
- authentication material;
- CAS capacity, high/low watermarks, and garbage collection;
- worker concurrency, CPU, memory, scratch space, and container limits; and
- NativeLink image digests.

Addresses, credentials, and certificate paths belong in ignored local files or the deployment secret store. They must not be committed.

The implementation is organized as follows:

```text
infra/remote-execution/
  README.md
  nativelink/
    control.json5
    deployment.json
    nativelink.service
    worker-x86_64.json5
    worker-aarch64.json5
  sdme/
    offline-oci-archives.json
    README.md
    worker-preflight.conf
    worker-rootfs.sdme
  scripts/
    check_deployment.py
    oci_archive.py
    preflight-worker.sh
    sdme-provision.sh
    smoke-test.sh
```

The deployment templates contain safe local defaults and environment placeholders, but no credentials. The worker configurations differ only where architecture, resources, or worker identity require it. See `infra/remote-execution/README.md` for the operator workflow.

## Worker contract

Workers execute untrusted upstream package recipes. A container boundary alone is insufficient because build actions create nested namespaces and must retain arbitrary package ownership inside their target buildroots.

Every worker must pass all of these checks before registration:

1. The worker runs as a dedicated service identity with subordinate UID and GID ranges plus working `newuidmap` and `newgidmap` helpers.
2. Bubblewrap can create user, network, PID, and IPC namespaces and configure loopback without host networking.
3. A nested namespace can create files and change ownership to multiple IDs, including an ID other than 0. A single-ID user namespace is not sufficient.
4. `/proc`, `/dev`, and `/tmp` mounts used by the existing isolation wrapper behave as expected.
5. Worker scratch has enough bytes and inodes for several concurrent unpacked distro buildroots. Scratch and CAS storage must not share a small quota.
6. The worker provides the Linux tools documented in `README.md`, including Bubblewrap or the supported unshare path and package-format helpers.
7. Native AArch64 workers advertise only `platform.arch=aarch64`. An emulated worker must instead prove a persistent `qemu-aarch64` binfmt handler and be placed in a distinct, lower-priority pool.
8. Workers do not mount developer home directories or mutable host toolchains into actions.

Avoid blanket privileged containers. Grant only the namespace, mapping, mount, device, and cgroup capabilities proven necessary by the preflight. The worker service needs network access to the scheduler and CAS; package actions should remain network-isolated and consume only declared inputs.

The client host also needs enough inotify instances and watches for long-lived Buck daemons. File-watch exhaustion is a client or host configuration failure, not a reason to weaken action isolation.

## Buck client configuration

Start with cache lookup and upload while keeping execution local:

```ini
[buckos]
  remote_cache = true
  remote_execution = false
  remote_x86_64_properties = platform.OSFamily=linux,platform.arch=x86_64
  remote_aarch64_properties = platform.OSFamily=linux,platform.arch=aarch64
  remote_x86_64_use_case = buck2-default
  remote_aarch64_use_case = buck2-default

[buck2_re_client]
  engine_address = re.example.invalid:50051
  action_cache_address = re.example.invalid:50051
  cas_address = re.example.invalid:50051
  instance_name = main
  tls = false
```

After cross-client cache validation succeeds, set `buckos.remote_execution = true`. Use TLS and authenticated endpoints whenever traffic leaves a private single-host network. Keep this configuration in `.buckconfig.local` or an equivalent ignored include.

## Cache and scheduling policy

- The CAS and action cache are shared by both architecture pools. Platform properties remain part of action identity and prevent cross-architecture result reuse.
- `binary-seed` actions are eligible for remote execution and cache upload. `host` buildroots remain local-only with uploads disabled.
- Buildroot-independent actions follow the explicit contract enforced by `tools/re_contract_test.py`.
- Worker concurrency must be limited by measured memory and scratch usage, not CPU count alone. Package builds frequently expand far beyond source archive size.
- Configure bounded CAS garbage collection with an explicit maximum size. Size the initial volume from at least two complete source and prebuilt matrices plus headroom, then revise it from observed retention and hit rates.
- Export scheduler queue depth, active workers, action latency, CAS size, eviction count, cache hits/misses, failed actions, and worker disconnects.
- Preserve action stdout/stderr and NativeLink service logs long enough to diagnose cache misses and worker-specific failures.

## Bring-up sequence

NativeLink v1.6.6 does not provide a read-only worker-registration snapshot containing worker identity, connection state, and accepted platform properties. Private deployment admission therefore combines control health and a CAS round trip with forced uncached architecture probes through both worker pools. The probe output must exactly match the requested Buck platform properties. This execution-backed check does not replace a future native worker-snapshot API.

1. Pin NativeLink service images and create the private network and persistent CAS volume.
2. Start the scheduler/CAS/action-cache service without workers. Verify health, storage persistence, metrics, and clean restart behavior.
3. Enable remote cache only on two fresh Buck clients. Build the same small target from both and prove that the second client receives cached results.
4. Start the x86_64 worker only after its preflight passes. Enable remote execution and validate the architecture probe before one RPM source build, one Debian source build, one rootfs, and one ISO.
5. Start the native AArch64 worker and validate its architecture probe before proving routing with the same representative action classes.
6. Do not enable broader clients or long builds until the control/CAS checks and both architecture probes pass.
7. Run the complete direct tool suite and `//tools:re_contract_test`, then build every source and prebuilt ISO. Run firmware boot tests on the controlled local validation hosts.
8. Exercise service restart, worker loss, CAS garbage collection, and a clean third client before treating the deployment as shared infrastructure.

## Acceptance gates

The deployment is ready only when all of these are demonstrated:

- A fresh client can build from shared cache without a local materialized output or prior daemon state.
- Repeating a clean build reuses every previously successful cacheable action.
- x86_64 and AArch64 actions are dispatched only to compatible workers.
- A worker loss causes retry or a clear failure without corrupting CAS state.
- Host-provenance actions never execute remotely and never upload results.
- Namespace preflight proves multi-ID ownership and isolated loopback on every worker.
- The RPM and Debian source builds, rootfs assembly, and ISO assembly complete through RE with deterministic outputs.
- Source and prebuilt image matrices build successfully, and local boot tests validate the resulting media.
- Service metrics and logs explain cache misses, queueing, evictions, and worker failures without entering the worker container interactively.

## Initial non-goals

- Public internet exposure of the REAPI endpoint.
- Shared execution by untrusted users or repositories.
- Remote QEMU firmware boot tests.
- Making `host` buildroot provenance cacheable.
- Replacing package mirrors with CAS. Buck downloads remain digest-verified declared inputs and may be served by the existing mirror mechanisms.
