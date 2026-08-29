# SDME provisioning

`../scripts/sdme-provision.sh` imports digest-pinned Ubuntu 26.04 and NativeLink v1.6.6 images, builds a native runtime rootfs, and creates either the control-plane container or the worker for the current architecture.

Start with the read-only `plan` operation. `apply` performs provisioning. `start`, `stop`, and `restart` change only runtime state and preserve the bind-mounted control and worker data.

Applying a plan requires root access, SDME 0.18 or newer, Podman, a Btrfs-backed SDME storage pool, and active `systemd-networkd`. Workers additionally require a native host architecture and an immutable probe sysroot.

Worker containers use `--userns --userns-nested 1` and explicit capability drops. They intentionally do not use `--hardened` because its `NoNewPrivileges` setting prevents the non-root `nativelink` service from using the setuid `newuidmap` and `newgidmap` helpers. The mandatory worker preflight is installed as an `ExecStartPre` gate.

The control container publishes no ports by default. After its first start, the provisioner discovers its private-zone address and binds the worker API only to that address, never to a wildcard. Cross-host deployment requires `--publish`, explicit client and worker CIDR allowlists, and an external read-only firewall/VPN checker. SDME creates the port forwarding rules but cannot express source-address policy. The tracked endpoints use plaintext gRPC, so cross-host traffic also requires a trusted encrypted network such as WireGuard unless TLS is configured separately.

The worker probe sysroot is deployment input. It must be native, immutable, outside a checkout or home directory, and supplied with the deterministic tree digest accepted by `--probe-sysroot-sha256`.

Set `--min-scratch-inodes 0` when `statvfs` reports dynamic inode accounting, as Btrfs commonly does. Otherwise set a positive admission threshold.

Example plans:

```sh
infra/remote-execution/scripts/sdme-provision.sh plan control \
  --data-root /srv/buckos-re

infra/remote-execution/scripts/sdme-provision.sh plan worker \
  --data-root /srv/buckos-re \
  --control-address buckos-re-control \
  --probe-sysroot /srv/buckos-re/probe-root \
  --probe-sysroot-sha256 SHA256 \
  --min-scratch-bytes BYTES \
  --min-scratch-inodes INODES
```

For a cross-host control plane, add:

```sh
  --publish \
  --client-cidrs CLIENT_CIDRS \
  --worker-cidrs WORKER_CIDRS \
  --firewall-check /absolute/path/to/read-only-policy-check
```

The policy checker receives `--client-port`, `--client-cidrs`, `--worker-port`, and `--worker-cidrs`. It must exit zero only when an existing private network or firewall restricts each port to the supplied sources. Public catch-all CIDRs are rejected before the checker runs.
