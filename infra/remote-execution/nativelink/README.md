# NativeLink deployment inputs

This directory pins NativeLink `v1.6.6` and provides one control-plane config plus architecture-specific worker configs. The files use the strict JSON subset of JSON5 so the repository validator can parse them without another dependency. NativeLink still performs its documented environment expansion when loading the files.

The default addresses provide a safe local smoke-test setup without edits:

- public REAPI: `0.0.0.0:50051`;
- private worker API: `127.0.0.1:50061`;
- worker connections: `127.0.0.1:50051` and `127.0.0.1:50061`.

For the architecture-native worker hosts, set `NATIVELINK_WORKER_BIND_ADDRESS` to the coordinator's private interface and set `NATIVELINK_REAPI_ADDRESS` and `NATIVELINK_WORKER_API_ADDRESS` on each worker to the corresponding reachable coordinator addresses. Never set the worker bind address to a wildcard or publish port 50061 outside the worker network.

The following optional byte-count variables override bounded cache defaults:

- `NATIVELINK_CAS_MAX_BYTES`;
- `NATIVELINK_AC_MAX_BYTES`;
- `NATIVELINK_WORKER_CAS_MAX_BYTES`.

The common systemd unit expects the binary at `/usr/bin/nativelink`, configs under `/etc/nativelink`, and a persistent writable tree at `/var/lib/nativelink`. Select a role in `/etc/nativelink/nativelink.env`:

```ini
NATIVELINK_CONFIG=/etc/nativelink/worker-x86_64.json5
NATIVELINK_REAPI_ADDRESS=control.internal
NATIVELINK_WORKER_API_ADDRESS=control.internal
NL_OTEL_ENDPOINT=http://otel-collector.internal:4317
```

Provision the `nativelink` service account before starting the unit. Worker hosts also need subordinate UID and GID ranges for that account and the setuid `newuidmap` and `newgidmap` helpers used by BuckOS action isolation.

The unit deliberately does not enable systemd restrictions that block child user or mount namespaces, setuid ID-map helpers, route-netlink access, or executable memory. BuckOS actions provide their own full-range namespace isolation, and NativeLink's `use_namespaces` and `use_mount_namespace` layers therefore remain disabled.

Validate the checked-in deployment before installation:

```console
$ python3 tools/nativelink_config.py
```

`deployment.json` is the machine-readable source for the immutable image reference. Deploy the multi-architecture index digest from that file, not the mutable version tag.
