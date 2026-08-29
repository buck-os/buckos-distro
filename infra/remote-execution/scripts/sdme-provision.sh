#!/usr/bin/env bash
# Provision digest-pinned NativeLink services through SDME.

set -euo pipefail
umask 077
IFS=$' \t\n'
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
unset CDPATH ENV BASH_ENV PYTHONPATH PYTHONHOME || true

readonly UBUNTU_IMAGE='docker.io/library/ubuntu@sha256:2260313b31c8c011cd2eebe728008efac1b3982be73eb71348ea2648d2c0e09b'
readonly NATIVELINK_IMAGE='ghcr.io/tracemachina/nativelink@sha256:5c2e6eca51c6d3ac40b94f703e08a243fd036cc136cc858a99040ca90fa57d61'
readonly UBUNTU_FS='buckos-re-ubuntu-2260313b31c8'
readonly NATIVELINK_FS='buckos-re-nativelink-5c2e6eca51c6'
readonly RUNTIME_FS='buckos-re-runtime-5c2e6eca51c6'
readonly SERVICE_USER='nativelink'
readonly REAPI_PORT='50051'
readonly WORKER_API_PORT='50061'

script_dir=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
repo_root=$(CDPATH='' cd -- "$script_dir/../../.." && pwd -P)
asset_root="$repo_root/infra/remote-execution"

operation=''
role=''
arch=''
data_root=''
zone='buckos-re'
container_name=''
control_address=''
control_worker_bind_address=''
memory=''
cpus=''
root_disk=''
probe_sysroot=''
probe_sysroot_sha256=''
min_scratch_bytes=''
min_scratch_inodes=''
cas_max_bytes=''
ac_max_bytes=''
worker_cas_max_bytes=''
publish=0
client_cidrs=''
worker_cidrs=''
firewall_check=''
verbose=0

sdme_bin=''
podman_bin=''
python_bin=''
tar_bin=''
sha256_bin=''
systemctl_bin=''
cleanup_paths=()

cleanup() {
    local path
    for path in "${cleanup_paths[@]}"; do
        [[ -n "$data_root" && "$path" == "$data_root/"* ]] || continue
        rm -rf -- "$path"
    done
}

trap cleanup EXIT

usage() {
    cat <<'EOF'
usage: sdme-provision.sh OPERATION ROLE [OPTIONS]

Operations:
  plan       print the planned mutating commands without running them
  apply      import images, build the runtime, and create/update the service
  status     show the container and NativeLink unit status
  start      start the existing container
  stop       stop the existing container without removing persistent data
  restart    restart the existing container without removing persistent data

Roles:
  control    combined CAS, action cache, scheduler, and REAPI service
  worker     native worker selected from the host architecture

Required for plan/apply:
  --data-root PATH              absolute persistent data root

Required for worker plan/apply:
  --control-address HOST        zone hostname or private/VPN address, no port
  --probe-sysroot PATH          immutable native probe sysroot
  --probe-sysroot-sha256 HEX    deterministic tree digest
  --min-scratch-bytes N         measured admission threshold
  --min-scratch-inodes N        measured admission threshold

Optional:
  --arch x86_64|aarch64        defaults to the native host architecture
  --zone NAME                  default: buckos-re
  --container-name NAME        role-specific default
  --memory SIZE                defaults: control 32G, worker 128G
  --cpus N                     defaults: control 8, worker 48
  --root-disk SIZE             defaults: control 20G, worker 32G
  --cas-max-bytes N            control CAS override
  --ac-max-bytes N             control action-cache override
  --worker-cas-max-bytes N     worker fast-CAS override
  --publish                    publish control ports through SDME
  --client-cidrs LIST          comma-separated client allowlist
  --worker-cidrs LIST          comma-separated worker allowlist
  --firewall-check PATH        read-only external policy verifier
  -v, --verbose

Publishing requires both allowlists and a firewall/VPN checker. The script
does not install or modify firewall policy.
EOF
}

die() {
    printf 'error: %s\n' "$*" >&2
    exit 2
}

log() {
    printf 'sdme-provision: %s\n' "$*" >&2
}

debug() {
    if ((verbose)); then
        log "$*"
    fi
}

print_command() {
    printf '  '
    printf '%q ' "$@"
    printf '\n'
}

run_command() {
    if ((verbose)); then
        printf '+ ' >&2
        printf '%q ' "$@" >&2
        printf '\n' >&2
    fi
    "$@"
}

run_sdme() {
    # sdme requires a traversable generated root filesystem. Retain the
    # restrictive host-side umask and relax it only in the sdme child.
    (umask 022; run_command "$sdme_bin" "$@")
}

query_sdme() {
    (umask 022; run_command "$sdme_bin" "$@")
}

normalize_arch() {
    case "$1" in
        x86_64|amd64) printf 'x86_64\n' ;;
        aarch64|arm64) printf 'aarch64\n' ;;
        *) return 1 ;;
    esac
}

oci_arch() {
    case "$1" in
        x86_64) printf 'amd64\n' ;;
        aarch64) printf 'arm64\n' ;;
        *) return 1 ;;
    esac
}

validate_name() {
    [[ "$1" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || die "invalid SDME name: $1"
}

reject_placeholder() {
    local label="$1"
    local value="$2"
    local lowered=${value,,}
    [[ -n "$value" ]] || die "$label is required"
    case "$lowered" in
        *'<'*|*'>'*|*'example.invalid'*|*'changeme'*|*'replace_me'*|*'todo'*|*"\${"*|*"\$("*|*'`'*)
            die "$label contains a placeholder or shell expansion: $value"
            ;;
    esac
}

validate_positive_integer() {
    local label="$1"
    local value="$2"
    [[ "$value" =~ ^[1-9][0-9]*$ ]] || die "$label must be a positive integer"
}

validate_nonnegative_integer() {
    local label="$1"
    local value="$2"
    [[ "$value" =~ ^[0-9]+$ ]] || die "$label must be a nonnegative integer"
}

validate_size() {
    local label="$1"
    local value="$2"
    [[ "$value" =~ ^[1-9][0-9]*([KMGT])?$ ]] || die "$label has invalid size: $value"
}

validate_cpus() {
    [[ "$1" =~ ^[1-9][0-9]*([.][0-9]+)?$ ]] || die "invalid CPU limit: $1"
}

canonical_future_path() {
    realpath -m -- "$1"
}

path_is_beneath() {
    local path="$1"
    local root="$2"
    [[ "$path" == "$root" || "$path" == "$root/"* ]]
}

validate_data_root() {
    [[ "$data_root" == /* ]] || die "--data-root must be absolute"
    data_root=$(canonical_future_path "$data_root")
    case "$data_root" in
        /|/home|/srv|/var|/var/lib|/var/lib/sdme)
            die "--data-root is too broad: $data_root"
            ;;
    esac
    if path_is_beneath "$data_root" "$repo_root" || [[ "$data_root" == /home/* ]]; then
        die "--data-root must not be inside a checkout or home directory"
    fi
}

validate_probe_root() {
    [[ "$probe_sysroot" == /* ]] || die "--probe-sysroot must be absolute"
    probe_sysroot=$(realpath -e -- "$probe_sysroot") || die "probe sysroot does not exist"
    [[ -d "$probe_sysroot" && "$probe_sysroot" != / ]] || die "probe sysroot must be a directory other than /"
    if path_is_beneath "$probe_sysroot" "$repo_root" || [[ "$probe_sysroot" == /home/* ]]; then
        die "probe sysroot must not expose a checkout or home directory"
    fi
    [[ "$probe_sysroot_sha256" =~ ^[0-9a-f]{64}$ ]] || die "--probe-sysroot-sha256 must be 64 lowercase hexadecimal characters"
}

validate_control_address() {
    reject_placeholder '--control-address' "$control_address"
    [[ "$control_address" =~ ^[A-Za-z0-9._:-]+$ || "$control_address" =~ ^\[[0-9A-Fa-f:]+\]$ ]] || die "invalid control address: $control_address"
    case "$control_address" in
        localhost|127.*|0.0.0.0|::|'[::]'|'[::1]') die "worker control address must be routable from its container" ;;
    esac
    if [[ "$control_address" == *:* && ! "$control_address" =~ ^\[[0-9A-Fa-f:]+\]$ ]]; then
        die "IPv6 control addresses must be bracketed and addresses must not include a port"
    fi
}

validate_cidrs() {
    local label="$1"
    local value="$2"
    local error
    reject_placeholder "$label" "$value"
    error=$("$python_bin" - "$label" "$value" 2>&1 <<'PY'
import ipaddress
import sys

label, value = sys.argv[1:]
items = value.split(",")
if not items or any(not item or item != item.strip() or "/" not in item for item in items):
    raise SystemExit("{} must be a comma-separated list of CIDRs".format(label))
try:
    networks = [ipaddress.ip_network(item, strict=False) for item in items]
except ValueError as error:
    raise SystemExit("{} contains an invalid CIDR: {}".format(label, error))
if any(network.prefixlen == 0 for network in networks):
    raise SystemExit("{} must not contain a public catch-all".format(label))
PY
    ) || die "$error"
}

validate_safe_file() {
    local label="$1"
    local path="$2"
    local current mode
    [[ "$path" == /* ]] || die "$label must be an absolute path"
    path=$(realpath -e -- "$path") || die "$label does not exist: $path"
    [[ -f "$path" ]] || die "$label is not a regular file: $path"
    current="$path"
    while :; do
        mode=$(stat -c '%a' -- "$current")
        if (( (8#$mode & 8#022) != 0 )); then
            if [[ ! -d "$current" ]] || (( (8#$mode & 8#1000) == 0 )); then
                die "$label has a group/world-writable path component: $current"
            fi
        fi
        [[ "$current" == / ]] && break
        current=$(dirname -- "$current")
    done
    printf '%s\n' "$path"
}

resolve_command() {
    local name="$1"
    local path
    path=$(command -v -- "$name") || die "missing required command: $name"
    path=$(validate_safe_file "$name" "$path")
    [[ -x "$path" ]] || die "required command is not executable: $path"
    printf '%s\n' "$path"
}

parse_args() {
    (($# >= 2)) || { usage >&2; exit 2; }
    operation="$1"
    role="$2"
    shift 2

    case "$operation" in
        plan|apply|status|start|stop|restart) ;;
        *) die "unknown operation: $operation" ;;
    esac
    case "$role" in
        control|worker) ;;
        *) die "unknown role: $role" ;;
    esac

    while (($#)); do
        case "$1" in
            --data-root) (($# >= 2)) || die "$1 needs a value"; data_root="$2"; shift 2 ;;
            --zone) (($# >= 2)) || die "$1 needs a value"; zone="$2"; shift 2 ;;
            --container-name) (($# >= 2)) || die "$1 needs a value"; container_name="$2"; shift 2 ;;
            --arch) (($# >= 2)) || die "$1 needs a value"; arch=$(normalize_arch "$2") || die "unsupported architecture: $2"; shift 2 ;;
            --control-address) (($# >= 2)) || die "$1 needs a value"; control_address="$2"; shift 2 ;;
            --memory) (($# >= 2)) || die "$1 needs a value"; memory="$2"; shift 2 ;;
            --cpus) (($# >= 2)) || die "$1 needs a value"; cpus="$2"; shift 2 ;;
            --root-disk) (($# >= 2)) || die "$1 needs a value"; root_disk="$2"; shift 2 ;;
            --probe-sysroot) (($# >= 2)) || die "$1 needs a value"; probe_sysroot="$2"; shift 2 ;;
            --probe-sysroot-sha256) (($# >= 2)) || die "$1 needs a value"; probe_sysroot_sha256="$2"; shift 2 ;;
            --min-scratch-bytes) (($# >= 2)) || die "$1 needs a value"; min_scratch_bytes="$2"; shift 2 ;;
            --min-scratch-inodes) (($# >= 2)) || die "$1 needs a value"; min_scratch_inodes="$2"; shift 2 ;;
            --cas-max-bytes) (($# >= 2)) || die "$1 needs a value"; cas_max_bytes="$2"; shift 2 ;;
            --ac-max-bytes) (($# >= 2)) || die "$1 needs a value"; ac_max_bytes="$2"; shift 2 ;;
            --worker-cas-max-bytes) (($# >= 2)) || die "$1 needs a value"; worker_cas_max_bytes="$2"; shift 2 ;;
            --publish) publish=1; shift ;;
            --client-cidrs) (($# >= 2)) || die "$1 needs a value"; client_cidrs="$2"; shift 2 ;;
            --worker-cidrs) (($# >= 2)) || die "$1 needs a value"; worker_cidrs="$2"; shift 2 ;;
            --firewall-check) (($# >= 2)) || die "$1 needs a value"; firewall_check="$2"; shift 2 ;;
            -v|--verbose) verbose=1; shift ;;
            -h|--help) usage; exit 0 ;;
            *) die "unknown argument: $1" ;;
        esac
    done
}

set_defaults() {
    if [[ -z "$arch" ]]; then
        arch=$(normalize_arch "$(uname -m)") || die "unsupported native architecture: $(uname -m)"
    fi
    validate_name "$zone"

    if [[ "$role" == control ]]; then
        container_name=${container_name:-buckos-re-control}
        memory=${memory:-32G}
        cpus=${cpus:-8}
        root_disk=${root_disk:-20G}
    else
        container_name=${container_name:-buckos-re-worker-${arch//_/-}}
        memory=${memory:-128G}
        cpus=${cpus:-48}
        root_disk=${root_disk:-32G}
    fi
    validate_name "$container_name"
    validate_size '--memory' "$memory"
    validate_cpus "$cpus"
    validate_size '--root-disk' "$root_disk"
}

validate_operation_args() {
    case "$operation" in
        plan|apply)
            reject_placeholder '--data-root' "$data_root"
            validate_data_root
            ;;
    esac

    if [[ "$role" == worker && "$operation" =~ ^(plan|apply)$ ]]; then
        validate_control_address
        validate_positive_integer '--min-scratch-bytes' "$min_scratch_bytes"
        validate_nonnegative_integer '--min-scratch-inodes' "$min_scratch_inodes"
        validate_probe_root
    fi

    if [[ "$operation" =~ ^(plan|apply)$ ]]; then
        if [[ "$role" == control && -n "$control_address$probe_sysroot$probe_sysroot_sha256$min_scratch_bytes$min_scratch_inodes$worker_cas_max_bytes" ]]; then
            die "worker-only options were supplied for the control role"
        fi
        if [[ "$role" == worker && -n "$cas_max_bytes$ac_max_bytes" ]]; then
            die "control-only cache options were supplied for the worker role"
        fi
    fi

    if [[ -n "$cas_max_bytes" ]]; then validate_positive_integer '--cas-max-bytes' "$cas_max_bytes"; fi
    if [[ -n "$ac_max_bytes" ]]; then validate_positive_integer '--ac-max-bytes' "$ac_max_bytes"; fi
    if [[ -n "$worker_cas_max_bytes" ]]; then validate_positive_integer '--worker-cas-max-bytes' "$worker_cas_max_bytes"; fi

    if ((publish)); then
        [[ "$role" == control ]] || die "--publish is valid only for the control role"
        validate_cidrs '--client-cidrs' "$client_cidrs"
        validate_cidrs '--worker-cidrs' "$worker_cidrs"
        firewall_check=$(validate_safe_file '--firewall-check' "$firewall_check")
        [[ -x "$firewall_check" ]] || die "firewall checker is not executable: $firewall_check"
    elif [[ -n "$client_cidrs$worker_cidrs$firewall_check" ]]; then
        die "firewall options require --publish"
    fi

    if [[ "$operation" == apply ]]; then
        local native
        native=$(normalize_arch "$(uname -m)") || die "unsupported native architecture: $(uname -m)"
        [[ "$arch" == "$native" ]] || die "apply requires a native $arch host; this host is $native"
        ((EUID == 0)) || die "apply must run as root"
    fi
}

validate_metadata() {
    local metadata="$asset_root/nativelink/deployment.json"
    metadata=$(validate_safe_file 'NativeLink deployment metadata' "$metadata")
    "$python_bin" - "$metadata" "$NATIVELINK_IMAGE" <<'PY'
import json
import sys

path, expected = sys.argv[1:]
with open(path, encoding="utf-8") as stream:
    data = json.load(stream)
actual = data.get("image", {}).get("reference")
if actual != expected:
    raise SystemExit("NativeLink metadata reference mismatch: {!r}".format(actual))
if data.get("image", {}).get("version") != "v1.6.6":
    raise SystemExit("NativeLink metadata version is not v1.6.6")
PY

    "$python_bin" "$repo_root/tools/nativelink_config.py" "$asset_root/nativelink"
}

validate_assets() {
    local files=(
        "$asset_root/nativelink/deployment.json"
        "$asset_root/nativelink/nativelink.service"
        "$asset_root/nativelink/control.json5"
        "$asset_root/nativelink/worker-x86_64.json5"
        "$asset_root/nativelink/worker-aarch64.json5"
        "$asset_root/sdme/worker-rootfs.sdme"
        "$asset_root/scripts/sdme_select_address.py"
        "$repo_root/tools/nativelink_config.py"
    )
    if [[ "$role" == worker ]]; then
        files+=(
            "$asset_root/sdme/worker-preflight.conf"
            "$asset_root/scripts/preflight-worker.sh"
            "$asset_root/scripts/preflight_worker.py"
            "$repo_root/tools/_isolation.py"
            "$repo_root/tools/_rpm.py"
        )
    fi
    local file
    for file in "${files[@]}"; do
        validate_safe_file 'deployment asset' "$file" >/dev/null
    done
    if [[ "$role" == worker && ! -x "$asset_root/scripts/preflight-worker.sh" ]]; then
        die "worker preflight wrapper is not executable"
    fi
    validate_metadata
}

probe_tree_digest() {
    "$tar_bin" \
        --sort=name \
        --mtime='UTC 1970-01-01' \
        --owner=0 \
        --group=0 \
        --numeric-owner \
        --format=posix \
        --pax-option=delete=atime,delete=ctime \
        -C "$probe_sysroot" \
        -cf - . | "$sha256_bin" | awk '{print $1}'
}

validate_probe_digest() {
    [[ "$role" == worker ]] || return 0
    local actual
    actual=$(probe_tree_digest)
    [[ "$actual" == "$probe_sysroot_sha256" ]] || die "probe sysroot digest mismatch: expected $probe_sysroot_sha256, got $actual"
}

prepare_tools() {
    python_bin=$(resolve_command python3)
    tar_bin=$(resolve_command tar)
    sha256_bin=$(resolve_command sha256sum)
    if [[ "$operation" != plan ]]; then
        sdme_bin=$(resolve_command sdme)
    fi
    if [[ "$operation" == apply ]]; then
        podman_bin=$(resolve_command podman)
        systemctl_bin=$(resolve_command systemctl)
    fi
}

validate_host_prerequisites() {
    [[ "$operation" == apply ]] || return 0
    "$systemctl_bin" is-active --quiet systemd-networkd.service || \
        die "systemd-networkd.service must be active for the private SDME zone"
}

role_paths() {
    images_dir="$data_root/images"
    provision_dir="$data_root/provision"
    if [[ "$role" == control ]]; then
        state_dir="$data_root/control"
        scratch_dir=''
        config_file="$asset_root/nativelink/control.json5"
    else
        state_dir="$data_root/worker-$arch/state"
        scratch_dir="$data_root/worker-$arch/scratch"
        config_file="$asset_root/nativelink/worker-${arch}.json5"
    fi
    unit_file="$asset_root/nativelink/nativelink.service"
    ubuntu_archive="$images_dir/ubuntu-2604-${arch}.oci.tar"
    nativelink_archive="$images_dir/nativelink-166-${arch}.oci.tar"
    env_file="$provision_dir/${container_name}.env"
}

emit_environment() {
    printf 'NATIVELINK_CONFIG=/etc/nativelink/%s\n' "$(basename -- "$config_file")"
    if [[ "$role" == control ]]; then
        printf 'NATIVELINK_REAPI_BIND_ADDRESS=0.0.0.0\n'
        if [[ -n "$control_worker_bind_address" ]]; then
            printf 'NATIVELINK_WORKER_BIND_ADDRESS=%s\n' "$control_worker_bind_address"
        fi
        if [[ -n "$cas_max_bytes" ]]; then printf 'NATIVELINK_CAS_MAX_BYTES=%s\n' "$cas_max_bytes"; fi
        if [[ -n "$ac_max_bytes" ]]; then printf 'NATIVELINK_AC_MAX_BYTES=%s\n' "$ac_max_bytes"; fi
    else
        printf 'NATIVELINK_REAPI_ADDRESS=%s\n' "$control_address"
        printf 'NATIVELINK_WORKER_API_ADDRESS=%s\n' "$control_address"
        printf 'BUCKOS_RE_WORKER_ARCH=%s\n' "$arch"
        printf 'BUCKOS_RE_MIN_SCRATCH_BYTES=%s\n' "$min_scratch_bytes"
        printf 'BUCKOS_RE_MIN_SCRATCH_INODES=%s\n' "$min_scratch_inodes"
        if [[ -n "$worker_cas_max_bytes" ]]; then printf 'NATIVELINK_WORKER_CAS_MAX_BYTES=%s\n' "$worker_cas_max_bytes"; fi
    fi
}

plan_commands() {
    local platform
    platform=$(oci_arch "$arch")
    printf '# Native architecture: %s\n' "$arch"
    printf '# Existing matching filesystems and containers are reused. Mismatched containers are refused.\n'
    print_command install -d -m 0750 "$images_dir" "$provision_dir" "$state_dir"
    if [[ "$role" == worker ]]; then
        print_command install -d -m 0750 "$scratch_dir"
    fi
    print_command podman pull --platform "linux/$platform" "$UBUNTU_IMAGE"
    print_command podman save --format oci-archive --output "$ubuntu_archive" "$UBUNTU_IMAGE"
    print_command sdme fs import "$ubuntu_archive" --name "$UBUNTU_FS" --oci-mode base --install-packages yes
    print_command podman pull --platform "linux/$platform" "$NATIVELINK_IMAGE"
    print_command podman save --format oci-archive --output "$nativelink_archive" "$NATIVELINK_IMAGE"
    print_command sdme fs import "$nativelink_archive" --name "$NATIVELINK_FS" --oci-mode app --base-fs "$UBUNTU_FS"
    print_command sdme fs build "$RUNTIME_FS" "$asset_root/sdme/worker-rootfs.sdme" --timeout 600

    if ((publish)); then
        printf '# Required pre-existing network policy check:\n'
        print_command "$firewall_check" \
            --client-port "$REAPI_PORT" \
            --client-cidrs "$client_cidrs" \
            --worker-port "$WORKER_API_PORT" \
            --worker-cidrs "$worker_cidrs"
    fi

    printf '# Write %s with mode 0600 and the following contents:\n' "$env_file"
    emit_environment | sed 's/^/#   /'

    local create=(
        sdme create
        --name "$container_name"
        --fs "$RUNTIME_FS"
        --storage btrfs
        --disk "$root_disk"
        --memory "$memory"
        --cpus "$cpus"
        --network-zone "$zone"
        --bind "$state_dir:/var/lib/nativelink"
        --enable
        --restart on-failure
    )
    if [[ "$role" == control ]]; then
        create+=(--hardened)
        if ((publish)); then
            create+=(--port "tcp:$REAPI_PORT:$REAPI_PORT" --port "tcp:$WORKER_API_PORT:$WORKER_API_PORT")
        fi
    else
        create+=(
            --userns
            --userns-nested 1
            --drop-capability CAP_SYS_PTRACE
            --drop-capability CAP_NET_RAW
            --drop-capability CAP_SYS_RAWIO
            --drop-capability CAP_SYS_BOOT
            --bind "$scratch_dir:/var/tmp"
            --bind "$probe_sysroot:/opt/buckos-re/probe-sysroot:ro"
        )
    fi
    print_command "${create[@]}"
    print_command sdme cp "$config_file" "$container_name:/etc/nativelink/$(basename -- "$config_file")"
    print_command sdme cp "$unit_file" "$container_name:/etc/systemd/system/nativelink.service"
    print_command sdme cp "$env_file" "$container_name:/etc/nativelink/nativelink.env"
    if [[ "$role" == worker ]]; then
        print_command sdme cp "$asset_root/sdme/worker-preflight.conf" \
            "$container_name:/etc/systemd/system/nativelink.service.d/10-worker-preflight.conf"
        print_command sdme cp "$asset_root/scripts/preflight-worker.sh" \
            "$container_name:/usr/local/libexec/buckos-re/preflight-worker.sh"
        print_command sdme cp "$asset_root/scripts/preflight_worker.py" \
            "$container_name:/usr/local/libexec/buckos-re/preflight_worker.py"
        print_command sdme cp "$repo_root/tools/_isolation.py" \
            "$container_name:/usr/local/libexec/buckos-re/tools/_isolation.py"
        print_command sdme cp "$repo_root/tools/_rpm.py" \
            "$container_name:/usr/local/libexec/buckos-re/tools/_rpm.py"
    fi
    print_command sdme start "$container_name"
    if [[ "$role" == control ]]; then
        printf '# Discover the running container zone address, preferring RFC1918/ULA over link-local, write it as NATIVELINK_WORKER_BIND_ADDRESS, and recopy %s.\n' "$env_file"
        print_command sdme cp "$env_file" "$container_name:/etc/nativelink/nativelink.env"
    fi
    if [[ "$role" == control ]]; then
        print_command sdme exec "$container_name" --user root -- install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" /var/lib/nativelink
    else
        print_command sdme exec "$container_name" --user root -- install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "/var/lib/nativelink/worker-$arch" /var/tmp
    fi
    print_command sdme exec "$container_name" --user root -- systemctl daemon-reload
    print_command sdme exec "$container_name" --user root -- systemctl enable nativelink.service
    print_command sdme exec "$container_name" --user root -- systemctl restart nativelink.service
}

fs_exists() {
    local inventory
    inventory=$(query_sdme fs ls --json) || {
        log "failed to query SDME filesystems"
        return 2
    }
    printf '%s' "$inventory" | "$python_bin" -c '
import json
import sys

name = sys.argv[1]
try:
    items = json.load(sys.stdin)
except (json.JSONDecodeError, TypeError) as error:
    print("invalid SDME filesystem inventory: {}".format(error), file=sys.stderr)
    raise SystemExit(2)
if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
    print("invalid SDME filesystem inventory shape", file=sys.stderr)
    raise SystemExit(2)
raise SystemExit(0 if any(item.get("name") == name for item in items) else 1)
' "$1"
}

container_record() {
    local inventory
    inventory=$(query_sdme ps --json) || {
        log "failed to query SDME containers"
        return 2
    }
    printf '%s' "$inventory" | "$python_bin" -c '
import json
import sys

name = sys.argv[1]
try:
    items = json.load(sys.stdin)
except (json.JSONDecodeError, TypeError) as error:
    print("invalid SDME container inventory: {}".format(error), file=sys.stderr)
    raise SystemExit(2)
if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
    print("invalid SDME container inventory shape", file=sys.stderr)
    raise SystemExit(2)
for item in items:
    if item.get("name") == name:
        print(json.dumps(item, sort_keys=True))
        raise SystemExit(0)
raise SystemExit(1)
' "$1"
}

container_status() {
    local record
    local result
    if record=$(container_record "$1"); then
        :
    else
        result=$?
        return "$result"
    fi
    "$python_bin" -c '
import json
import sys

try:
    status = json.loads(sys.argv[1])["status"]
except (json.JSONDecodeError, KeyError, TypeError) as error:
    print("invalid SDME container record: {}".format(error), file=sys.stderr)
    raise SystemExit(2)
if not isinstance(status, str):
    print("invalid SDME container status", file=sys.stderr)
    raise SystemExit(2)
print(status)
' "$record"
}

materialize_archive() {
    local image="$1"
    local output="$2"
    local platform="$3"
    local marker="$output.reference"
    local temporary="$output.tmp.$$"
    local marker_temporary="$marker.tmp.$$"
    cleanup_paths+=("$temporary" "$marker_temporary")

    if [[ -e "$output" || -e "$marker" ]]; then
        [[ -f "$output" && -f "$marker" ]] || die "incomplete cached OCI archive pair: $output"
        [[ "$(<"$marker")" == "$image" ]] || die "cached OCI archive reference mismatch: $output"
        debug "reusing pinned OCI archive $output"
        return
    fi

    run_command "$podman_bin" pull --platform "linux/$platform" "$image"
    run_command "$podman_bin" save --format oci-archive --output "$temporary" "$image"
    printf '%s\n' "$image" > "$marker_temporary"
    mv -- "$temporary" "$output"
    mv -- "$marker_temporary" "$marker"
}

ensure_runtime_fs() {
    local platform result
    platform=$(oci_arch "$arch")
    if fs_exists "$UBUNTU_FS"; then
        debug "reusing rootfs $UBUNTU_FS"
    else
        result=$?
        ((result == 1)) || die "could not inspect rootfs $UBUNTU_FS"
        materialize_archive "$UBUNTU_IMAGE" "$ubuntu_archive" "$platform"
        run_sdme fs import "$ubuntu_archive" \
            --name "$UBUNTU_FS" --oci-mode base --install-packages yes
    fi

    if fs_exists "$NATIVELINK_FS"; then
        debug "reusing rootfs $NATIVELINK_FS"
    else
        result=$?
        ((result == 1)) || die "could not inspect rootfs $NATIVELINK_FS"
        materialize_archive "$NATIVELINK_IMAGE" "$nativelink_archive" "$platform"
        run_sdme fs import "$nativelink_archive" \
            --name "$NATIVELINK_FS" --oci-mode app --base-fs "$UBUNTU_FS"
    fi

    if fs_exists "$RUNTIME_FS"; then
        debug "reusing rootfs $RUNTIME_FS"
    else
        result=$?
        ((result == 1)) || die "could not inspect rootfs $RUNTIME_FS"
        run_sdme fs build "$RUNTIME_FS" \
            "$asset_root/sdme/worker-rootfs.sdme" --timeout 600
    fi
    validate_runtime_fs
}

validate_runtime_fs() {
    local temporary
    local marker
    temporary=$(mktemp -d "$provision_dir/runtime-check.XXXXXX")
    cleanup_paths+=("$temporary")
    if ! query_sdme cp "fs:$RUNTIME_FS:/etc/nativelink/runtime-images" "$temporary" >/dev/null; then
        rm -rf -- "$temporary"
        die "runtime rootfs lacks provenance marker: $RUNTIME_FS"
    fi
    marker="$temporary/runtime-images"
    grep -Fxq "ubuntu_image=$UBUNTU_IMAGE" "$marker" || {
        rm -rf -- "$temporary"
        die "runtime rootfs Ubuntu digest mismatch: $RUNTIME_FS"
    }
    grep -Fxq "nativelink_image=$NATIVELINK_IMAGE" "$marker" || {
        rm -rf -- "$temporary"
        die "runtime rootfs NativeLink digest mismatch: $RUNTIME_FS"
    }
    grep -Fxq "architecture=$arch" "$marker" || {
        rm -rf -- "$temporary"
        die "runtime rootfs architecture mismatch: $RUNTIME_FS"
    }
    rm -rf -- "$temporary"
}

write_environment_file() {
    local temporary="$env_file.tmp.$$"
    cleanup_paths+=("$temporary")
    emit_environment > "$temporary"
    if grep -Eiq '<|>|example\.invalid|changeme|replace_me|todo|\$\{' "$temporary"; then
        rm -f -- "$temporary"
        die "generated environment contains a placeholder"
    fi
    chmod 0600 "$temporary"
    mv -- "$temporary" "$env_file"
}

validate_existing_container() {
    local record="$1"
    local expected_ports='none'
    if ((publish)); then expected_ports='published'; fi
    "$python_bin" - "$record" "$RUNTIME_FS" "$zone" "$state_dir" "$scratch_dir" "$probe_sysroot" "$role" "$expected_ports" "$memory" "$cpus" <<'PY'
import json
import sys

record = json.loads(sys.argv[1])
rootfs, zone, state_dir, scratch_dir, probe_root, role, ports, memory, cpus = sys.argv[2:]
errors = []
if record.get("rootfs") != rootfs:
    errors.append("rootfs={!r}".format(record.get("rootfs")))
if not record.get("userns"):
    errors.append("userns is disabled")
if not record.get("enabled"):
    errors.append("autostart is disabled")
network = record.get("network") or {}
if not network.get("private_network"):
    errors.append("private network is disabled")
if network.get("network_zone") != zone:
    errors.append("zone={!r}".format(network.get("network_zone")))
if network.get("network_bridge") is not None:
    errors.append("unexpected network bridge")
if network.get("pod") or network.get("oci_pod"):
    errors.append("unexpected pod network")
limits = record.get("limits") or {}
if limits.get("memory") != memory:
    errors.append("memory={!r}".format(limits.get("memory")))
if limits.get("cpus") != cpus:
    errors.append("cpus={!r}".format(limits.get("cpus")))
expected_binds = {state_dir + ":/var/lib/nativelink:rw"}
if role == "worker":
    expected_binds.add(scratch_dir + ":/var/tmp:rw")
    expected_binds.add(probe_root + ":/opt/buckos-re/probe-sysroot:ro")
actual_binds = set(record.get("binds") or [])
if actual_binds != expected_binds:
    errors.append("binds={!r}".format(sorted(actual_binds)))
actual_ports = set(network.get("ports") or [])
required_ports = {"tcp:50051:50051", "tcp:50061:50061"} if ports == "published" else set()
if actual_ports != required_ports:
    errors.append("ports={!r}".format(sorted(actual_ports)))
if errors:
    raise SystemExit("existing container does not match requested topology: " + "; ".join(errors))
PY
}

create_container() {
    local create=(
        create
        --name "$container_name"
        --fs "$RUNTIME_FS"
        --storage btrfs
        --disk "$root_disk"
        --memory "$memory"
        --cpus "$cpus"
        --network-zone "$zone"
        --bind "$state_dir:/var/lib/nativelink"
        --enable
        --restart on-failure
    )
    if [[ "$role" == control ]]; then
        create+=(--hardened)
        if ((publish)); then
            create+=(--port "tcp:$REAPI_PORT:$REAPI_PORT" --port "tcp:$WORKER_API_PORT:$WORKER_API_PORT")
        fi
    else
        create+=(
            --userns
            --userns-nested 1
            --drop-capability CAP_SYS_PTRACE
            --drop-capability CAP_NET_RAW
            --drop-capability CAP_SYS_RAWIO
            --drop-capability CAP_SYS_BOOT
            --bind "$scratch_dir:/var/tmp"
            --bind "$probe_sysroot:/opt/buckos-re/probe-sysroot:ro"
        )
    fi
    run_sdme "${create[@]}"
}

copy_assets() {
    run_sdme cp "$config_file" "$container_name:/etc/nativelink/$(basename -- "$config_file")"
    run_sdme cp "$unit_file" "$container_name:/etc/systemd/system/nativelink.service"
    run_sdme cp "$env_file" "$container_name:/etc/nativelink/nativelink.env"
    if [[ "$role" == worker ]]; then
        run_sdme cp "$asset_root/sdme/worker-preflight.conf" \
            "$container_name:/etc/systemd/system/nativelink.service.d/10-worker-preflight.conf"
        run_sdme cp "$asset_root/scripts/preflight-worker.sh" \
            "$container_name:/usr/local/libexec/buckos-re/preflight-worker.sh"
        run_sdme cp "$asset_root/scripts/preflight_worker.py" \
            "$container_name:/usr/local/libexec/buckos-re/preflight_worker.py"
        run_sdme cp "$repo_root/tools/_isolation.py" \
            "$container_name:/usr/local/libexec/buckos-re/tools/_isolation.py"
        run_sdme cp "$repo_root/tools/_rpm.py" \
            "$container_name:/usr/local/libexec/buckos-re/tools/_rpm.py"
    fi
}

ensure_started() {
    local status
    local result
    if status=$(container_status "$container_name"); then
        :
    else
        result=$?
        ((result == 1)) && die "container disappeared: $container_name"
        die "could not inspect container: $container_name"
    fi
    if [[ "$status" != running ]]; then
        run_sdme start "$container_name"
    fi
}

prepare_container_storage() {
    if [[ "$role" == control ]]; then
        run_sdme exec "$container_name" --user root -- \
            install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" /var/lib/nativelink
    else
        run_sdme exec "$container_name" --user root -- \
            install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" \
            "/var/lib/nativelink/worker-$arch" /var/tmp
    fi
}

discover_control_worker_bind_address() {
    local record result address
    if record=$(container_record "$container_name"); then
        :
    else
        result=$?
        ((result == 1)) && die "container disappeared: $container_name"
        die "could not inspect container: $container_name"
    fi
    if address=$(printf '%s' "$record" | "$python_bin" "$asset_root/scripts/sdme_select_address.py" 2>&1); then
        :
    else
        die "$address"
    fi
    control_worker_bind_address="$address"
}

apply_deployment() {
    local directories=("$images_dir" "$provision_dir" "$state_dir")
    if [[ "$role" == worker ]]; then directories+=("$scratch_dir"); fi
    run_command install -d -m 0750 "${directories[@]}"

    if ((publish)); then
        run_command "$firewall_check" \
            --client-port "$REAPI_PORT" \
            --client-cidrs "$client_cidrs" \
            --worker-port "$WORKER_API_PORT" \
            --worker-cidrs "$worker_cidrs"
    fi

    ensure_runtime_fs
    write_environment_file

    local record result
    if record=$(container_record "$container_name"); then
        validate_existing_container "$record"
        debug "reusing container $container_name"
    else
        result=$?
        ((result == 1)) || die "could not inspect container: $container_name"
        create_container
    fi

    copy_assets
    ensure_started
    prepare_container_storage
    if [[ "$role" == control ]]; then
        discover_control_worker_bind_address
        write_environment_file
        run_sdme cp "$env_file" "$container_name:/etc/nativelink/nativelink.env"
    fi
    run_sdme exec "$container_name" --user root -- systemctl daemon-reload
    run_sdme exec "$container_name" --user root -- systemctl enable nativelink.service
    run_sdme exec "$container_name" --user root -- systemctl restart nativelink.service
}

status_deployment() {
    local record result
    if record=$(container_record "$container_name"); then
        :
    else
        result=$?
        ((result == 1)) && die "container not found: $container_name"
        die "could not inspect container: $container_name"
    fi
    "$python_bin" -m json.tool <<<"$record"
    if [[ "$(container_status "$container_name")" == running ]]; then
        query_sdme exec "$container_name" --user root -- systemctl --no-pager --full status nativelink.service
    fi
}

lifecycle() {
    local status result
    if status=$(container_status "$container_name"); then
        :
    else
        result=$?
        ((result == 1)) && die "container not found: $container_name"
        die "could not inspect container: $container_name"
    fi
    case "$operation" in
        start)
            if [[ "$status" == running ]]; then
                log "$container_name is already running"
            else
                run_sdme start "$container_name"
            fi
            ;;
        stop)
            if [[ "$status" == running ]]; then
                run_sdme stop "$container_name"
            else
                log "$container_name is already stopped"
            fi
            ;;
        restart)
            if [[ "$status" == running ]]; then
                run_sdme restart "$container_name"
            else
                run_sdme start "$container_name"
            fi
            ;;
    esac
}

main() {
    parse_args "$@"
    set_defaults
    prepare_tools
    validate_operation_args
    validate_host_prerequisites

    if [[ "$operation" =~ ^(plan|apply)$ ]]; then
        role_paths
        validate_assets
        validate_probe_digest
    fi

    case "$operation" in
        plan) plan_commands ;;
        apply) apply_deployment ;;
        status)
            sdme_bin=$(resolve_command sdme)
            status_deployment
            ;;
        start|stop|restart)
            ((EUID == 0)) || die "$operation must run as root"
            sdme_bin=$(resolve_command sdme)
            lifecycle
            ;;
    esac
}

main "$@"
