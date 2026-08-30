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
readonly IMAGE_PROVENANCE_PATH='/buckos-re-image-provenance.json'
readonly RUNTIME_PROVENANCE_PATH='/etc/buckos-re-runtime-provenance.json'
readonly BUILD_PROXY_PATH='/etc/buckos-re-build-proxy.env'
readonly BUILD_PROXY_SENTINEL='buckos-sdme-proxy-transport-v1'
readonly DEPLOYMENT_IDENTITY_PATH='/etc/nativelink/deployment.identity'
readonly SERVICE_UNIT='nativelink.service'
readonly SERVICE_UNIT_DIR='/etc/systemd/system'
readonly TLS_DIRECTORY='/etc/nativelink/tls'
readonly TLS_MIN_VALIDITY_SECONDS='86400'
readonly TRANSACTION_SCHEMA_VERSION='1'
readonly CONTROL_ADDRESS_WAIT_SECONDS='30'
readonly CONTROL_ADDRESS_POLL_SECONDS='1'
readonly CONTROL_ADDRESS_KILL_GRACE_MILLISECONDS='100'
readonly ADDRESS_NOT_READY_EXIT='3'
readonly ADDRESS_QUERY_TIMEOUT_EXIT='3'
readonly TIMEOUT_EXIT='124'
readonly TIMEOUT_KILLED_EXIT='137'

script_dir=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
repo_root=$(CDPATH='' cd -- "$script_dir/../../.." && pwd -P)
asset_root="$repo_root/infra/remote-execution"

operation=''
role=''
arch=''
data_root=''
zone='buckos-re'
zone_supplied=0
container_name=''
control_address=''
control_container_name=''
control_container_name_set=0
control_dns=''
control_dns_set=0
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
security_mode='plaintext'
security_mode_set=0
client_cidrs=''
worker_cidrs=''
firewall_check=''
tls_control_chain=''
tls_control_chain_set=0
tls_control_key=''
tls_control_key_set=0
tls_control_ca=''
tls_control_ca_set=0
tls_reapi_client_ca=''
tls_reapi_client_ca_set=0
tls_worker_client_ca=''
tls_worker_client_ca_set=0
tls_worker_chain=''
tls_worker_chain_set=0
tls_worker_key=''
tls_worker_key_set=0
tls_worker_issuer_ca=''
tls_worker_issuer_ca_set=0
ubuntu_oci_archive=''
nativelink_oci_archive=''
ubuntu_oci_archive_set=0
nativelink_oci_archive_set=0
verbose=0

sdme_bin=''
podman_bin=''
python_bin=''
tar_bin=''
sha256_bin=''
systemctl_bin=''
flock_bin=''
sleep_bin=''
timeout_bin=''
openssl_bin=''
oci_archive_tool=''
oci_archive_metadata=''
tls_tool=''
cleanup_paths=()
transaction_dir=''
provision_lock_fd=''
runtime_build_definition=''
runtime_proxy_file=''
tls_identity_json=''
tls_helper_args=()
tls_stage_dir=''
expected_deployment_identity=''
expected_deployment_identity_sha256=''
container_snapshot_dir=''
install_deployment_assets=0

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
  prepare-runtime
             acquire/import pinned images and build/validate the worker runtime
  apply      import images, build the runtime, and create/update the service
  status     show the container and NativeLink unit status
  start      start the existing container
  stop       stop the existing container without removing persistent data
  restart    restart the existing container without removing persistent data

Roles:
  control    combined CAS, action cache, scheduler, and REAPI service
  worker     native worker selected from the host architecture

Required for plan/prepare-runtime/apply:
  --data-root PATH              absolute persistent data root

Required for worker plan/apply:
  --probe-sysroot PATH          immutable native probe sysroot
  --probe-sysroot-sha256 HEX    deterministic tree digest
  --min-scratch-bytes N         measured admission threshold
  --min-scratch-inodes N        measured admission threshold

Required for plaintext worker plan/apply:
  --control-address NAME        exact local control container name

Required for mTLS plan/apply:
  --control-dns NAME            exact DNS SAN and worker endpoint name

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
  --security-mode MODE         plaintext (default) or mtls
  --control-container-name NAME
                               local plaintext control; default: buckos-re-control
  --tls-control-chain PATH     control server certificate chain
  --tls-control-key PATH       control server private key
  --tls-control-ca PATH        control server trust anchor
  --tls-reapi-client-ca PATH   combined Buck and worker client trust
  --tls-worker-client-ca PATH  worker-only client trust
  --tls-worker-chain PATH      worker client certificate chain
  --tls-worker-key PATH        worker client private key
  --tls-worker-issuer-ca PATH  worker client issuing trust anchor
  --ubuntu-oci-archive PATH    trusted offline Ubuntu OCI archive
  --nativelink-oci-archive PATH
                               trusted offline NativeLink OCI archive
  --publish                    publish control ports through SDME
  --client-cidrs LIST          comma-separated client allowlist
  --worker-cidrs LIST          comma-separated worker allowlist
  --firewall-check PATH        read-only external policy verifier
  -v, --verbose

Publishing requires mTLS, both allowlists, and a firewall/VPN checker. The
script does not install or modify firewall policy.
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

query_sdme_with_timeout() {
    local duration="$1"
    shift
    (umask 022; run_command "$timeout_bin" --kill-after=0.100s "$duration" "$sdme_bin" "$@")
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

validate_apply_path_ancestry() {
    [[ "$operation" =~ ^(prepare-runtime|apply)$ ]] || return 0
    local current="$data_root"
    local mode owner
    while [[ ! -e "$current" ]]; do
        current=$(dirname -- "$current")
    done
    current=$(realpath -e -- "$current") || die "cannot resolve --data-root ancestry"
    while :; do
        [[ -d "$current" && ! -L "$current" ]] || \
            die "--data-root ancestor is not a real directory: $current"
        owner=$(stat -c '%u' -- "$current")
        mode=$(stat -c '%a' -- "$current")
        [[ "$owner" == 0 ]] || die "--data-root ancestor is not root-owned: $current"
        (( (8#$mode & 8#022) == 0 )) || \
            die "--data-root ancestor is group/world-writable: $current"
        [[ "$current" == / ]] && break
        current=$(dirname -- "$current")
    done
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

validate_control_dns() {
    reject_placeholder '--control-dns' "$control_dns"
    [[ ${#control_dns} -le 253 ]] || die "--control-dns is too long"
    [[ "$control_dns" =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]] || \
        die "--control-dns must be a canonical lowercase DNS name"
}

tls_options_supplied() {
    ((tls_control_chain_set || tls_control_key_set || tls_control_ca_set || \
       tls_reapi_client_ca_set || tls_worker_client_ca_set || \
       tls_worker_chain_set || tls_worker_key_set || tls_worker_issuer_ca_set))
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

set_option_once() {
    local option="$1"
    local value_name="$2"
    local set_name="$3"
    local value="$4"
    (( ${!set_name} == 0 )) || die "$option may be supplied only once"
    printf -v "$value_name" '%s' "$value"
    printf -v "$set_name" '%d' 1
}

parse_args() {
    (($# >= 2)) || { usage >&2; exit 2; }
    operation="$1"
    role="$2"
    shift 2

    case "$operation" in
        plan|prepare-runtime|apply|status|start|stop|restart) ;;
        *) die "unknown operation: $operation" ;;
    esac
    case "$role" in
        control|worker) ;;
        *) die "unknown role: $role" ;;
    esac

    while (($#)); do
        case "$1" in
            --data-root) (($# >= 2)) || die "$1 needs a value"; data_root="$2"; shift 2 ;;
            --zone) (($# >= 2)) || die "$1 needs a value"; zone="$2"; zone_supplied=1; shift 2 ;;
            --container-name) (($# >= 2)) || die "$1 needs a value"; container_name="$2"; shift 2 ;;
            --arch) (($# >= 2)) || die "$1 needs a value"; arch=$(normalize_arch "$2") || die "unsupported architecture: $2"; shift 2 ;;
            --control-address) (($# >= 2)) || die "$1 needs a value"; control_address="$2"; shift 2 ;;
            --control-container-name)
                (($# >= 2)) || die "$1 needs a value"
                set_option_once "$1" control_container_name control_container_name_set "$2"
                shift 2
                ;;
            --control-dns)
                (($# >= 2)) || die "$1 needs a value"
                set_option_once "$1" control_dns control_dns_set "$2"
                shift 2
                ;;
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
            --security-mode)
                (($# >= 2)) || die "$1 needs a value"
                set_option_once "$1" security_mode security_mode_set "$2"
                shift 2
                ;;
            --tls-control-chain)
                (($# >= 2)) || die "$1 needs a value"
                set_option_once "$1" tls_control_chain tls_control_chain_set "$2"
                shift 2
                ;;
            --tls-control-key)
                (($# >= 2)) || die "$1 needs a value"
                set_option_once "$1" tls_control_key tls_control_key_set "$2"
                shift 2
                ;;
            --tls-control-ca)
                (($# >= 2)) || die "$1 needs a value"
                set_option_once "$1" tls_control_ca tls_control_ca_set "$2"
                shift 2
                ;;
            --tls-reapi-client-ca)
                (($# >= 2)) || die "$1 needs a value"
                set_option_once "$1" tls_reapi_client_ca tls_reapi_client_ca_set "$2"
                shift 2
                ;;
            --tls-worker-client-ca)
                (($# >= 2)) || die "$1 needs a value"
                set_option_once "$1" tls_worker_client_ca tls_worker_client_ca_set "$2"
                shift 2
                ;;
            --tls-worker-chain)
                (($# >= 2)) || die "$1 needs a value"
                set_option_once "$1" tls_worker_chain tls_worker_chain_set "$2"
                shift 2
                ;;
            --tls-worker-key)
                (($# >= 2)) || die "$1 needs a value"
                set_option_once "$1" tls_worker_key tls_worker_key_set "$2"
                shift 2
                ;;
            --tls-worker-issuer-ca)
                (($# >= 2)) || die "$1 needs a value"
                set_option_once "$1" tls_worker_issuer_ca tls_worker_issuer_ca_set "$2"
                shift 2
                ;;
            --ubuntu-oci-archive)
                (($# >= 2)) || die "$1 needs a value"
                ((ubuntu_oci_archive_set == 0)) || die "$1 may be supplied only once"
                ubuntu_oci_archive="$2"
                ubuntu_oci_archive_set=1
                shift 2
                ;;
            --nativelink-oci-archive)
                (($# >= 2)) || die "$1 needs a value"
                ((nativelink_oci_archive_set == 0)) || die "$1 may be supplied only once"
                nativelink_oci_archive="$2"
                nativelink_oci_archive_set=1
                shift 2
                ;;
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
    case "$security_mode" in
        plaintext|mtls) ;;
        *) die "invalid --security-mode: $security_mode" ;;
    esac
    if [[ -z "$arch" ]]; then
        arch=$(normalize_arch "$(uname -m)") || die "unsupported native architecture: $(uname -m)"
    fi
    if [[ "$operation" == prepare-runtime ]]; then
        return
    fi
    validate_name "$zone"

    if [[ "$role" == control ]]; then
        container_name=${container_name:-buckos-re-control}
        memory=${memory:-32G}
        cpus=${cpus:-8}
        root_disk=${root_disk:-20G}
    else
        container_name=${container_name:-buckos-re-worker-${arch//_/-}}
        if [[ "$security_mode" == plaintext ]]; then
            control_container_name=${control_container_name:-buckos-re-control}
        fi
        memory=${memory:-128G}
        cpus=${cpus:-48}
        root_disk=${root_disk:-32G}
    fi
    validate_name "$container_name"
    if [[ -n "$control_container_name" ]]; then
        validate_name "$control_container_name"
    fi
    validate_size '--memory' "$memory"
    validate_cpus "$cpus"
    validate_size '--root-disk' "$root_disk"
}

# Everything the option matrix can decide on its own, so that a malformed,
# incomplete, duplicate, cross-role, or plaintext-publication request is
# refused before any host tool is resolved or any system state is probed.
validate_option_matrix() {
    case "$operation" in
        plan|prepare-runtime|apply)
            reject_placeholder '--data-root' "$data_root"
            validate_data_root
            ;;
    esac

    if [[ "$operation" == prepare-runtime ]]; then
        [[ "$role" == worker ]] || die "prepare-runtime is valid only for the worker role"
        if [[ -n "$container_name$control_address$memory$cpus$root_disk$probe_sysroot$probe_sysroot_sha256$min_scratch_bytes$min_scratch_inodes$cas_max_bytes$ac_max_bytes$worker_cas_max_bytes$client_cidrs$worker_cidrs$firewall_check" ]] || ((publish || zone_supplied || security_mode_set || control_container_name_set || control_dns_set)) || tls_options_supplied; then
            die "prepare-runtime accepts only --data-root, --arch, and acquisition options"
        fi
    fi

    if [[ ! "$operation" =~ ^(plan|apply)$ ]]; then
        if ((security_mode_set || control_container_name_set || control_dns_set)) || \
           tls_options_supplied; then
            die "security and TLS options are valid only for plan/apply"
        fi
    fi

    if [[ "$operation" =~ ^(plan|apply)$ ]]; then
        if [[ "$security_mode" == plaintext ]]; then
            ((control_dns_set == 0)) || die "--control-dns requires --security-mode mtls"
            tls_options_supplied && die "TLS credential options require --security-mode mtls"
        else
            validate_control_dns
            if [[ "$role" == control ]]; then
                ((tls_control_chain_set && tls_control_key_set && tls_control_ca_set && \
                   tls_reapi_client_ca_set && tls_worker_client_ca_set)) || \
                    die "mTLS control requires control chain, key, CA, REAPI client CA, and worker client CA"
                ((tls_worker_chain_set == 0 && tls_worker_key_set == 0 && \
                   tls_worker_issuer_ca_set == 0)) || \
                    die "worker TLS identity options are invalid for the control role"
            else
                ((tls_control_ca_set && tls_worker_chain_set && tls_worker_key_set && \
                   tls_worker_issuer_ca_set)) || \
                    die "mTLS worker requires control CA, worker chain, key, and issuer CA"
                ((tls_control_chain_set == 0 && tls_control_key_set == 0 && \
                   tls_reapi_client_ca_set == 0 && tls_worker_client_ca_set == 0)) || \
                    die "control TLS identity options are invalid for the worker role"
            fi
        fi
    fi

    if [[ "$role" == worker && "$operation" =~ ^(plan|apply)$ ]]; then
        if [[ "$security_mode" == plaintext ]]; then
            validate_control_address
            [[ "$control_address" == "$control_container_name" ]] || \
                die "plaintext worker --control-address must equal the local control container name"
        else
            [[ -z "$control_address" ]] || \
                die "--control-address is invalid for an mTLS worker; use --control-dns"
            ((control_container_name_set == 0)) || \
                die "--control-container-name is invalid for an mTLS worker"
        fi
        validate_positive_integer '--min-scratch-bytes' "$min_scratch_bytes"
        validate_nonnegative_integer '--min-scratch-inodes' "$min_scratch_inodes"
        validate_probe_root
    fi

    if [[ "$operation" =~ ^(plan|apply)$ ]]; then
        if [[ "$role" == control && -n "$control_address$probe_sysroot$probe_sysroot_sha256$min_scratch_bytes$min_scratch_inodes$worker_cas_max_bytes" ]]; then
            die "worker-only options were supplied for the control role"
        fi
        if [[ "$role" == control ]] && ((control_container_name_set)); then
            die "--control-container-name is valid only for a plaintext worker"
        fi
        if [[ "$role" == worker && -n "$cas_max_bytes$ac_max_bytes" ]]; then
            die "control-only cache options were supplied for the worker role"
        fi
    fi

    if [[ ! "$operation" =~ ^(plan|prepare-runtime|apply)$ ]] && \
       ((ubuntu_oci_archive_set || nativelink_oci_archive_set)); then
        die "local OCI archive options are valid only for plan/prepare-runtime/apply"
    fi

    if [[ -n "$cas_max_bytes" ]]; then validate_positive_integer '--cas-max-bytes' "$cas_max_bytes"; fi
    if [[ -n "$ac_max_bytes" ]]; then validate_positive_integer '--ac-max-bytes' "$ac_max_bytes"; fi
    if [[ -n "$worker_cas_max_bytes" ]]; then validate_positive_integer '--worker-cas-max-bytes' "$worker_cas_max_bytes"; fi

    if ((publish)); then
        [[ "$role" == control ]] || die "--publish is valid only for the control role"
        [[ "$security_mode" == mtls ]] || die "--publish requires --security-mode mtls"
    elif [[ -n "$client_cidrs$worker_cidrs$firewall_check" ]]; then
        die "firewall options require --publish"
    fi
}

# Checks that need a resolved interpreter or that read host state. These run
# only once the option matrix above has been accepted.
validate_operation_environment() {
    if [[ "$operation" =~ ^(plan|prepare-runtime|apply)$ ]]; then
        if ((ubuntu_oci_archive_set)); then
            [[ -n "$ubuntu_oci_archive" ]] || die "--ubuntu-oci-archive must not be empty"
            ubuntu_oci_archive=$(validate_safe_file '--ubuntu-oci-archive' "$ubuntu_oci_archive")
            path_is_beneath "$ubuntu_oci_archive" "$data_root" && \
                die "--ubuntu-oci-archive must be outside --data-root"
        fi
        if ((nativelink_oci_archive_set)); then
            [[ -n "$nativelink_oci_archive" ]] || die "--nativelink-oci-archive must not be empty"
            nativelink_oci_archive=$(validate_safe_file '--nativelink-oci-archive' "$nativelink_oci_archive")
            path_is_beneath "$nativelink_oci_archive" "$data_root" && \
                die "--nativelink-oci-archive must be outside --data-root"
        fi
    fi

    if ((publish)); then
        validate_cidrs '--client-cidrs' "$client_cidrs"
        validate_cidrs '--worker-cidrs' "$worker_cidrs"
        firewall_check=$(validate_safe_file '--firewall-check' "$firewall_check")
        [[ -x "$firewall_check" ]] || die "firewall checker is not executable: $firewall_check"
    fi

    if [[ "$operation" =~ ^(prepare-runtime|apply)$ || \
          ( "$operation" == plan && "$security_mode" == mtls ) ]]; then
        local native
        if [[ "$operation" != plan ]]; then
            native=$(normalize_arch "$(uname -m)") || die "unsupported native architecture: $(uname -m)"
            [[ "$arch" == "$native" ]] || die "$operation requires a native $arch host; this host is $native"
        fi
        ((EUID == 0)) || die "$operation must run as root"
        if [[ "$operation" != plan ]]; then
            validate_apply_path_ancestry
        fi
    fi
}

validate_runtime_metadata() {
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
}

# The only place that decides which tracked NativeLink config a role,
# architecture, and security mode uses. The name comes from the validated
# deployment metadata; nothing else in this script names a config file.
deployment_config_basename() {
    local target_role="$1"
    local target_arch="$2"
    local target_mode="$3"
    local metadata selected
    validate_runtime_metadata
    metadata=$(validate_safe_file 'NativeLink deployment metadata' \
        "$asset_root/nativelink/deployment.json")
    selected=$("$python_bin" - "$metadata" "$target_role" "$target_arch" "$target_mode" 2>&1 <<'PY'
import json
import sys

path, role, architecture, mode = sys.argv[1:]
try:
    with open(path, encoding="utf-8") as stream:
        data = json.load(stream)
except (OSError, ValueError) as error:
    raise SystemExit("cannot read NativeLink deployment metadata: {}".format(error))
configs = data.get("configs")
if not isinstance(configs, dict):
    raise SystemExit("NativeLink deployment metadata has no config mapping")
if mode == "mtls":
    configs = configs.get("mtls")
    if not isinstance(configs, dict):
        raise SystemExit("NativeLink deployment metadata has no mTLS config mapping")
if role == "control":
    selected = configs.get("control")
else:
    workers = configs.get("workers")
    if not isinstance(workers, dict):
        raise SystemExit("NativeLink deployment metadata has no worker config mapping")
    selected = workers.get(architecture)
if not isinstance(selected, str) or not selected:
    raise SystemExit(
        "NativeLink deployment metadata selects no {} {} config".format(mode, role)
    )
if (
    selected in (".", "..")
    or "/" in selected
    or "\0" in selected
    or selected != selected.strip()
):
    raise SystemExit("NativeLink deployment config name is not a plain basename")
print(selected)
PY
    ) || die "$selected"
    # Re-assert the basename shape here so that nothing but a plain tracked
    # config name can reach path construction.
    [[ "$selected" =~ ^[A-Za-z0-9._-]+$ && "$selected" != . && "$selected" != .. ]] || \
        die "NativeLink deployment config name is not a plain basename"
    printf '%s\n' "$selected"
}

# Bind a selected basename to the exact tracked file, refusing any escape or
# symlink substitution before the caller can hash, print, or install it.
resolve_deployment_config() {
    local selected="$1"
    local nativelink_dir candidate resolved
    nativelink_dir=$(realpath -e -- "$asset_root/nativelink") || \
        die "tracked NativeLink directory does not exist"
    candidate="$nativelink_dir/$selected"
    [[ ! -L "$candidate" ]] || \
        die "selected NativeLink config is a symlink: $selected"
    [[ -f "$candidate" ]] || \
        die "selected NativeLink config is not a regular file: $selected"
    resolved=$(realpath -e -- "$candidate") || \
        die "cannot resolve selected NativeLink config: $selected"
    [[ "$resolved" == "$candidate" ]] || \
        die "selected NativeLink config escapes the tracked directory: $selected"
    validate_safe_file 'selected NativeLink config' "$resolved" >/dev/null
    printf '%s\n' "$resolved"
}

select_security_profile() {
    local target_role="$1"
    local target_arch="$2"
    local target_mode="$3"
    local selected
    selected=$(deployment_config_basename "$target_role" "$target_arch" "$target_mode")
    resolve_deployment_config "$selected"
}

validate_oci_archive_metadata() {
    run_command "$python_bin" "$oci_archive_tool" metadata "$oci_archive_metadata" \
        --expect "ubuntu=$UBUNTU_IMAGE" \
        --expect "nativelink=$NATIVELINK_IMAGE"
}

validate_metadata() {
    validate_runtime_metadata
    validate_oci_archive_metadata
    "$python_bin" "$repo_root/tools/nativelink_config.py" "$asset_root/nativelink"
}

validate_runtime_assets() {
    local files=(
        "$asset_root/nativelink/deployment.json"
        "$asset_root/sdme/offline-oci-archives.json"
        "$asset_root/sdme/worker-rootfs.sdme"
        "$asset_root/scripts/oci_archive.py"
    )
    local file
    for file in "${files[@]}"; do
        validate_safe_file 'runtime asset' "$file" >/dev/null
    done
    validate_runtime_metadata
    validate_oci_archive_metadata
}

validate_assets() {
    local files=(
        "$asset_root/nativelink/deployment.json"
        "$asset_root/sdme/offline-oci-archives.json"
        "$asset_root/nativelink/nativelink.service"
        "$asset_root/nativelink/control.json5"
        "$asset_root/nativelink/control-mtls.json5"
        "$asset_root/nativelink/worker-x86_64.json5"
        "$asset_root/nativelink/worker-x86_64-mtls.json5"
        "$asset_root/nativelink/worker-aarch64.json5"
        "$asset_root/nativelink/worker-aarch64-mtls.json5"
        "$asset_root/sdme/worker-rootfs.sdme"
        "$asset_root/scripts/sdme_select_address.py"
        "$asset_root/scripts/sdme_tls.py"
        "$asset_root/scripts/oci_archive.py"
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
    [[ -x "$tls_tool" ]] || die "mTLS credential helper is not executable"
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
    if [[ "$operation" =~ ^(prepare-runtime|apply)$ ]]; then
        flock_bin=$(resolve_command flock)
    fi
    if [[ "$operation" =~ ^(prepare-runtime|apply)$ && \
          ( -z "$ubuntu_oci_archive" || -z "$nativelink_oci_archive" ) ]]; then
        podman_bin=$(resolve_command podman)
    fi
    if [[ "$operation" == apply ]]; then
        systemctl_bin=$(resolve_command systemctl)
        if [[ "$role" == control ]]; then
            sleep_bin=$(resolve_command sleep)
            timeout_bin=$(resolve_command timeout)
        fi
    fi
    if [[ "$security_mode" == mtls && "$operation" =~ ^(plan|apply)$ ]]; then
        openssl_bin=$(resolve_command openssl)
    fi
    oci_archive_tool="$asset_root/scripts/oci_archive.py"
    oci_archive_metadata="$asset_root/sdme/offline-oci-archives.json"
    tls_tool="$asset_root/scripts/sdme_tls.py"
}

validate_host_prerequisites() {
    [[ "$operation" == apply ]] || return 0
    "$systemctl_bin" is-active --quiet systemd-networkd.service || \
        die "systemd-networkd.service must be active for the private SDME zone"
}

runtime_paths() {
    images_dir="$data_root/images"
    provision_dir="$data_root/provision"
    transaction_dir="$provision_dir/transactions"
    ubuntu_archive="$images_dir/ubuntu-2604-${arch}.oci.tar"
    nativelink_archive="$images_dir/nativelink-166-${arch}.oci.tar"
}

role_paths() {
    runtime_paths
    if [[ "$role" == control ]]; then
        state_dir="$data_root/control"
        scratch_dir=''
    else
        state_dir="$data_root/worker-$arch/state"
        scratch_dir="$data_root/worker-$arch/scratch"
    fi
    config_file=$(select_security_profile "$role" "$arch" "$security_mode")
    unit_file="$asset_root/nativelink/nativelink.service"
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
        if [[ "$security_mode" == mtls ]]; then
            printf 'NATIVELINK_CONTROL_DNS=%s\n' "$control_dns"
        else
            printf 'NATIVELINK_REAPI_ADDRESS=%s\n' "$control_address"
            printf 'NATIVELINK_WORKER_API_ADDRESS=%s\n' "$control_address"
        fi
        printf 'BUCKOS_RE_WORKER_ARCH=%s\n' "$arch"
        printf 'BUCKOS_RE_MIN_SCRATCH_BYTES=%s\n' "$min_scratch_bytes"
        printf 'BUCKOS_RE_MIN_SCRATCH_INODES=%s\n' "$min_scratch_inodes"
        if [[ -n "$worker_cas_max_bytes" ]]; then printf 'NATIVELINK_WORKER_CAS_MAX_BYTES=%s\n' "$worker_cas_max_bytes"; fi
    fi
}

set_tls_helper_arguments() {
    tls_helper_args=(
        "$python_bin"
        "$tls_tool"
        --openssl "$openssl_bin"
        --role "$role"
        --control-dns "$control_dns"
        --minimum-validity-seconds "$TLS_MIN_VALIDITY_SECONDS"
        # Credential sources must live outside anything this deployment owns
        # or rewrites, so neither the checkout nor the managed data root may
        # supply them.
        --exclude-root "$repo_root"
        --exclude-root "$data_root"
        --tls-control-ca "$tls_control_ca"
    )
    if [[ "$role" == control ]]; then
        tls_helper_args+=(
            --tls-control-chain "$tls_control_chain"
            --tls-control-key "$tls_control_key"
            --tls-reapi-client-ca "$tls_reapi_client_ca"
            --tls-worker-client-ca "$tls_worker_client_ca"
        )
    else
        tls_helper_args+=(
            --tls-worker-chain "$tls_worker_chain"
            --tls-worker-key "$tls_worker_key"
            --tls-worker-issuer-ca "$tls_worker_issuer_ca"
        )
    fi
    if ((verbose)); then tls_helper_args+=(-v); fi
}

validate_tls_credentials() {
    [[ "$security_mode" == mtls ]] || return 0
    set_tls_helper_arguments
    if ! tls_identity_json=$(run_command "${tls_helper_args[@]}"); then
        die "mTLS credential validation failed"
    fi
    [[ -n "$tls_identity_json" ]] || die "mTLS credential validation returned no identity"
}

run_oci_archive_tool() {
    local arguments=()
    if ((verbose)); then arguments+=(-v); fi
    run_command "$python_bin" "$oci_archive_tool" "${arguments[@]}" "$@"
}

archive_acquisition() {
    if [[ -n "$1" ]]; then
        printf 'offline\n'
    else
        printf 'registry\n'
    fi
}

archive_provenance_path() {
    printf '%s.provenance.json\n' "$1"
}

validate_offline_archive() {
    local image_name="$1"
    local image="$2"
    local source="$3"
    local destination="${4:-/dev/null}"
    local require_filename="${5:-0}"
    local arguments=()
    if ((require_filename)); then arguments+=(--require-filename); fi
    run_oci_archive_tool verify "$oci_archive_metadata" "$image_name" "$arch" \
        "$image" "$source" --acquisition offline "${arguments[@]}" > "$destination"
}

validate_archive_cache() {
    local image_name="$1"
    local image="$2"
    local output="$3"
    local acquisition="$4"
    local provenance
    local legacy="$output.reference"
    provenance=$(archive_provenance_path "$output")

    if [[ ! -e "$output" && ! -L "$output" && \
          ! -e "$provenance" && ! -L "$provenance" && \
          ! -e "$legacy" && ! -L "$legacy" ]]; then
        return 1
    fi
    [[ ! -L "$output" && ! -L "$provenance" && ! -e "$legacy" && ! -L "$legacy" ]] || \
        die "unsafe or legacy cached OCI archive state: $output"
    [[ -f "$output" && -f "$provenance" ]] || \
        die "incomplete cached OCI archive pair: $output"
    validate_safe_file 'cached OCI archive' "$output" >/dev/null
    validate_safe_file 'cached OCI provenance' "$provenance" >/dev/null
    if [[ "$operation" =~ ^(prepare-runtime|apply)$ ]]; then
        [[ "$(stat -c '%u' -- "$output")" == 0 ]] || \
            die "cached OCI archive is not root-owned: $output"
        [[ "$(stat -c '%u' -- "$provenance")" == 0 ]] || \
            die "cached OCI provenance is not root-owned: $provenance"
    fi
    run_oci_archive_tool cache "$oci_archive_metadata" "$image_name" "$arch" \
        "$image" "$output" "$provenance" --acquisition "$acquisition"
}

plan_archive() {
    local image_name="$1"
    local image="$2"
    local output="$3"
    local platform="$4"
    local source="$5"
    local acquisition result provenance
    acquisition=$(archive_acquisition "$source")
    provenance=$(archive_provenance_path "$output")

    if [[ -n "$source" ]]; then
        validate_offline_archive "$image_name" "$image" "$source" /dev/null 1
    fi
    if validate_archive_cache "$image_name" "$image" "$output" "$acquisition"; then
        printf '# Reuse validated %s OCI archive %s.\n' "$image_name" "$output"
        return
    else
        result=$?
        ((result == 1)) || return "$result"
    fi

    if [[ "$acquisition" == offline ]]; then
        print_command "$python_bin" "$oci_archive_tool" verify "$oci_archive_metadata" \
            "$image_name" "$arch" "$image" "$source" --acquisition offline \
            --require-filename
        print_command install -m 0600 -- "$source" "$output.tmp"
    else
        print_command podman pull --platform "linux/$platform" "$image"
        print_command podman save --format oci-archive --output "$output.tmp" "$image"
    fi
    printf '# Validate %s.tmp and atomically install it with canonical provenance at %s.\n' \
        "$output" "$provenance"
}

plan_commands() {
    local platform
    platform=$(oci_arch "$arch")
    printf '# Native architecture: %s\n' "$arch"
    printf '# NativeLink security mode: %s\n' "$security_mode"
    printf '# Existing matching filesystems and containers are reused. Mismatched containers are refused.\n'
    if [[ "$role" == worker ]]; then
        printf '# Fresh worker bootstrap sequence:\n'
        printf '# 1. Prepare and validate the native runtime without creating a container:\n'
        local prepare_runtime_command=(
            "$asset_root/scripts/sdme-provision.sh" prepare-runtime worker
            --arch "$arch"
            --data-root "$data_root"
        )
        if [[ -n "$ubuntu_oci_archive" ]]; then
            prepare_runtime_command+=(--ubuntu-oci-archive "$ubuntu_oci_archive")
        fi
        if [[ -n "$nativelink_oci_archive" ]]; then
            prepare_runtime_command+=(--nativelink-oci-archive "$nativelink_oci_archive")
        fi
        print_command "${prepare_runtime_command[@]}"
        printf '# 2. Create the immutable probe root; its stdout is the probe SHA-256:\n'
        print_command "$asset_root/scripts/prepare-worker-probe-root.sh" apply \
            --runtime-fs "$RUNTIME_FS" --arch "$arch" --destination "$probe_sysroot"
        printf '# 3. Apply the worker with that probe path and digest:\n'
        local worker_apply=(
            "$asset_root/scripts/sdme-provision.sh" apply worker
            --arch "$arch"
            --data-root "$data_root"
            --zone "$zone"
            --container-name "$container_name"
            --memory "$memory"
            --cpus "$cpus"
            --root-disk "$root_disk"
            --security-mode "$security_mode"
            --probe-sysroot "$probe_sysroot"
            --probe-sysroot-sha256 "$probe_sysroot_sha256"
            --min-scratch-bytes "$min_scratch_bytes"
            --min-scratch-inodes "$min_scratch_inodes"
        )
        if [[ "$security_mode" == mtls ]]; then
            worker_apply+=(
                --control-dns "$control_dns"
                --tls-control-ca "$tls_control_ca"
                --tls-worker-chain "$tls_worker_chain"
                --tls-worker-key "$tls_worker_key"
                --tls-worker-issuer-ca "$tls_worker_issuer_ca"
            )
        else
            worker_apply+=(
                --control-address "$control_address"
                --control-container-name "$control_container_name"
            )
        fi
        if [[ -n "$worker_cas_max_bytes" ]]; then
            worker_apply+=(--worker-cas-max-bytes "$worker_cas_max_bytes")
        fi
        if [[ -n "$ubuntu_oci_archive" ]]; then
            worker_apply+=(--ubuntu-oci-archive "$ubuntu_oci_archive")
        fi
        if [[ -n "$nativelink_oci_archive" ]]; then
            worker_apply+=(--nativelink-oci-archive "$nativelink_oci_archive")
        fi
        print_command "${worker_apply[@]}"
    fi
    print_command install -d -m 0750 "$images_dir" "$provision_dir" "$state_dir"
    if [[ "$role" == worker ]]; then
        print_command install -d -m 0750 "$scratch_dir"
    fi
    plan_archive ubuntu "$UBUNTU_IMAGE" "$ubuntu_archive" "$platform" "$ubuntu_oci_archive"
    print_command sdme fs import "$ubuntu_archive" --name "$UBUNTU_FS" --oci-mode base --install-packages yes
    print_command sdme cp "$(archive_provenance_path "$ubuntu_archive")" \
        "fs:$UBUNTU_FS:$IMAGE_PROVENANCE_PATH"
    plan_archive nativelink "$NATIVELINK_IMAGE" "$nativelink_archive" "$platform" "$nativelink_oci_archive"
    print_command sdme fs import "$nativelink_archive" --name "$NATIVELINK_FS" --install-packages no -f
    print_command sdme cp "$(archive_provenance_path "$nativelink_archive")" \
        "fs:$NATIVELINK_FS:$IMAGE_PROVENANCE_PATH"
    local planned_build_definition="$asset_root/sdme/worker-rootfs.sdme"
    if proxy_environment_enabled; then
        set_runtime_transaction_paths
        validate_runtime_proxy_paths
        planned_build_definition="$runtime_build_definition"
        printf '# Convey allowlisted proxy variables through private transaction files; values are not printed.\n'
        print_command sdme fs build "$RUNTIME_FS" "$planned_build_definition" \
            --timeout 600 --no-cache
    else
        print_command sdme fs build "$RUNTIME_FS" "$planned_build_definition" --timeout 600
    fi
    printf '# Bind the runtime to both admitted image records and the build definition at %s.\n' \
        "$RUNTIME_PROVENANCE_PATH"

    if ((publish)); then
        printf '# Required pre-existing network policy check:\n'
        print_command "$firewall_check" \
            --client-port "$REAPI_PORT" \
            --client-cidrs "$client_cidrs" \
            --worker-port "$WORKER_API_PORT" \
            --worker-cidrs "$worker_cidrs"
    fi

    printf '# Write the generated environment to %s with mode 0600; contents are not printed.\n' "$env_file"

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
    if [[ "$security_mode" == mtls ]]; then
        printf '# Validate, stage, and atomically publish the role-specific credentials at %s without printing their contents.\n' "$TLS_DIRECTORY"
    else
        printf '# Require %s to be absent.\n' "$TLS_DIRECTORY"
    fi
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
        printf '# Discover the running container zone address, preferring RFC1918/ULA over link-local, and rewrite %s without printing its contents.\n' "$env_file"
        print_command sdme cp "$env_file" "$container_name:/etc/nativelink/nativelink.env"
    fi
    if [[ "$role" == control ]]; then
        print_command sdme exec "$container_name" --user root -- install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" /var/lib/nativelink
    else
        print_command sdme exec "$container_name" --user root -- install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "/var/lib/nativelink/worker-$arch" /var/tmp
    fi
    printf '# Atomically publish %s after all deployment assets are complete.\n' "$DEPLOYMENT_IDENTITY_PATH"
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
    local name="$1"
    local timeout_milliseconds="${2:-}"
    local inventory result timeout_duration
    if [[ -n "$timeout_milliseconds" ]]; then
        printf -v timeout_duration '%d.%03ds' \
            "$((timeout_milliseconds / 1000))" \
            "$((timeout_milliseconds % 1000))"
        if inventory=$(query_sdme_with_timeout "$timeout_duration" ps --json); then
            :
        else
            result=$?
            ((result == TIMEOUT_EXIT || result == TIMEOUT_KILLED_EXIT)) && \
                return "$ADDRESS_QUERY_TIMEOUT_EXIT"
            log "failed to query SDME containers"
            return 2
        fi
    elif inventory=$(query_sdme ps --json); then
        :
    else
        log "failed to query SDME containers"
        return 2
    fi
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
' "$name"
}

record_status() {
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
    record_status "$record"
}

generate_archive_provenance() {
    local image_name="$1"
    local image="$2"
    local archive="$3"
    local acquisition="$4"
    local destination="$5"
    local recorded_filename="${6:-$(basename -- "$archive")}"
    if [[ "$acquisition" == offline ]]; then
        validate_offline_archive "$image_name" "$image" "$archive" "$destination" 1
    else
        run_oci_archive_tool verify "$oci_archive_metadata" "$image_name" "$arch" \
            "$image" "$archive" --acquisition registry \
            --record-filename "$recorded_filename" > "$destination"
    fi
    chmod 0600 "$destination"
}

materialize_archive() {
    local image_name="$1"
    local image="$2"
    local output="$3"
    local platform="$4"
    local source="$5"
    local acquisition provenance input_provenance
    local intent_identity object_identity source_identity
    local temporary="$output.tmp.$$"
    local provenance_temporary
    local output_present provenance_present transaction_present
    acquisition=$(archive_acquisition "$source")
    provenance=$(archive_provenance_path "$output")
    provenance_temporary="$provenance.tmp.$$"
    input_provenance="$provision_dir/${image_name}-input-provenance.tmp.$$"
    cleanup_paths+=("$temporary" "$provenance_temporary" "$input_provenance")

    if [[ -n "$source" ]]; then
        validate_offline_archive "$image_name" "$image" "$source" "$input_provenance" 1
        source_identity=$("$sha256_bin" "$input_provenance" | awk '{print $1}')
    else
        source_identity=$(transaction_identity registry "$image_name" "$image" "$arch")
    fi
    intent_identity=$(transaction_identity archive "$image_name" "$image" "$arch" \
        "$acquisition" "$source" "$source_identity" "$output" "$provenance" publishing)

    output_present=0
    provenance_present=0
    transaction_present=0
    [[ -e "$output" || -L "$output" ]] && output_present=1
    [[ -e "$provenance" || -L "$provenance" ]] && provenance_present=1
    transaction_record_exists archive "$image_name" && transaction_present=1

    if ((output_present && provenance_present)); then
        validate_archive_cache "$image_name" "$image" "$output" "$acquisition"
        if ((transaction_present)); then
            object_identity=$("$sha256_bin" "$provenance" | awk '{print $1}')
            transaction_record_matches archive "$image_name" "$intent_identity" \
                "$object_identity" "$acquisition" publishing
            sync_path "$output"
            sync_path "$provenance"
            sync_path "$images_dir"
            clear_transaction_record archive "$image_name"
        fi
        debug "reusing validated $acquisition OCI archive $output"
        rm -f -- "$input_provenance"
        return
    fi
    if ((output_present || provenance_present)); then
        ((transaction_present)) || die "incomplete cached OCI archive pair without a matching transaction: $output"
        [[ ! -L "$output" && ! -L "$provenance" ]] || \
            die "unsafe unpublished OCI archive state: $output"
        ((output_present && ! provenance_present)) || \
            die "unsupported interrupted OCI publication state: $output"
        validate_private_managed_file 'unpublished OCI archive' "$output"
        generate_archive_provenance "$image_name" "$image" "$output" \
            "$acquisition" "$provenance_temporary"
        object_identity=$("$sha256_bin" "$provenance_temporary" | awk '{print $1}')
        transaction_record_matches archive "$image_name" "$intent_identity" \
            "$object_identity" "$acquisition" publishing
        rm -f -- "$output" "$provenance"
        sync_path "$images_dir"
        rm -f -- "$provenance_temporary"
        clear_transaction_record archive "$image_name"
        transaction_present=0
        debug "discarded interrupted OCI archive publication $output"
    elif ((transaction_present)); then
        transaction_record_matches_intent archive "$image_name" "$intent_identity" \
            "$acquisition" publishing
        clear_transaction_record archive "$image_name"
        transaction_present=0
    fi

    if [[ "$acquisition" == offline ]]; then
        run_command install -m 0600 -- "$source" "$temporary"
        validate_offline_archive "$image_name" "$image" "$temporary" "$provenance_temporary"
        cmp -s -- "$input_provenance" "$provenance_temporary" || \
            die "offline OCI archive changed while being copied: $source"
        rm -f -- "$input_provenance"
    else
        run_command "$podman_bin" pull --platform "linux/$platform" "$image"
        run_command "$podman_bin" save --format oci-archive --output "$temporary" "$image"
        chmod 0600 "$temporary"
        generate_archive_provenance "$image_name" "$image" "$temporary" \
            registry "$provenance_temporary" "$(basename -- "$output")"
    fi
    chmod 0600 "$provenance_temporary"
    object_identity=$("$sha256_bin" "$provenance_temporary" | awk '{print $1}')
    if ((transaction_present == 0)); then
        write_transaction_record archive "$image_name" "$intent_identity" \
            "$object_identity" "$acquisition" publishing
    fi
    mv -- "$temporary" "$output"
    sync_path "$output"
    sync_path "$images_dir"
    mv -- "$provenance_temporary" "$provenance"
    sync_path "$provenance"
    sync_path "$images_dir"
    validate_archive_cache "$image_name" "$image" "$output" "$acquisition"
    clear_transaction_record archive "$image_name"
}

image_fs_provenance_matches() {
    local fs_name="$1"
    local provenance="$2"
    local temporary marker
    image_fs_validation_error=''
    temporary=$(mktemp -d "$provision_dir/image-check.XXXXXX")
    cleanup_paths+=("$temporary")
    if ! query_sdme cp "fs:$fs_name:$IMAGE_PROVENANCE_PATH" "$temporary" >/dev/null 2>&1; then
        rm -rf -- "$temporary"
        image_fs_validation_error="rootfs lacks image provenance: $fs_name"
        return 1
    fi
    marker="$temporary/$(basename -- "$IMAGE_PROVENANCE_PATH")"
    if ! cmp -s -- "$provenance" "$marker"; then
        rm -rf -- "$temporary"
        image_fs_validation_error="rootfs image provenance mismatch: $fs_name"
        return 1
    fi
    rm -rf -- "$temporary"
}

validate_image_fs_provenance() {
    image_fs_provenance_matches "$1" "$2" || die "$image_fs_validation_error"
}

install_image_fs_provenance() {
    local fs_name="$1"
    local provenance="$2"
    run_sdme cp "$provenance" "fs:$fs_name:$IMAGE_PROVENANCE_PATH"
    validate_image_fs_provenance "$fs_name" "$provenance"
}

ensure_image_fs() {
    local image_name="$1"
    local image="$2"
    local fs_name="$3"
    local archive="$4"
    local platform="$5"
    local source="$6"
    shift 6
    local provenance provenance_identity result transaction_identity_value transaction_present
    provenance=$(archive_provenance_path "$archive")
    materialize_archive "$image_name" "$image" "$archive" "$platform" "$source"
    provenance_identity=$("$sha256_bin" "$provenance" | awk '{print $1}')
    transaction_identity_value=$(transaction_identity \
        image-filesystem "$fs_name" "$arch" "$archive" \
        "$provenance_identity" "$@" importing)
    transaction_present=0
    transaction_record_exists image "$fs_name" && transaction_present=1
    if ((transaction_present)); then
        transaction_record_matches image "$fs_name" \
            "$transaction_identity_value" "$provenance_identity" \
            "$image_name" importing
    fi

    if fs_exists "$fs_name"; then
        if image_fs_provenance_matches "$fs_name" "$provenance"; then
            if ((transaction_present)); then
                clear_transaction_record image "$fs_name"
            fi
            debug "reusing provenance-validated rootfs $fs_name"
            return
        fi
        ((transaction_present)) || die "$image_fs_validation_error"
        run_sdme fs rm -f "$fs_name"
        debug "discarded interrupted unproven rootfs $fs_name"
    else
        result=$?
        ((result == 1)) || die "could not inspect rootfs $fs_name"
    fi

    if ((transaction_present == 0)); then
        write_transaction_record image "$fs_name" \
            "$transaction_identity_value" "$provenance_identity" \
            "$image_name" importing
    fi
    run_sdme fs import "$archive" --name "$fs_name" "$@"
    install_image_fs_provenance "$fs_name" "$provenance"
    clear_transaction_record image "$fs_name"
}

ensure_runtime_fs() {
    local platform result expected_provenance provenance_identity proxy_mode
    local transaction_present force_rebuild build_definition intent_identity
    platform=$(oci_arch "$arch")
    ensure_image_fs ubuntu "$UBUNTU_IMAGE" "$UBUNTU_FS" "$ubuntu_archive" \
        "$platform" "$ubuntu_oci_archive" --oci-mode base --install-packages yes
    ensure_image_fs nativelink "$NATIVELINK_IMAGE" "$NATIVELINK_FS" \
        "$nativelink_archive" "$platform" "$nativelink_oci_archive" \
        --install-packages no -f

    expected_provenance=$(mktemp "$provision_dir/runtime-provenance.XXXXXX")
    cleanup_paths+=("$expected_provenance")
    generate_runtime_provenance "$expected_provenance"
    chmod 0600 "$expected_provenance"
    provenance_identity=$("$sha256_bin" "$expected_provenance" | awk '{print $1}')
    set_runtime_transaction_paths
    proxy_mode=disabled
    if proxy_environment_enabled; then
        proxy_mode=enabled
        validate_runtime_proxy_paths
    fi
    intent_identity=$(transaction_identity runtime-filesystem "$RUNTIME_FS" \
        "$arch" "$provenance_identity" "$proxy_mode" building)
    transaction_present=0
    force_rebuild=0
    transaction_record_exists runtime "$RUNTIME_FS" && transaction_present=1
    if ((transaction_present)); then
        transaction_record_matches runtime "$RUNTIME_FS" \
            "$intent_identity" "$provenance_identity" "$proxy_mode" building
    fi

    if fs_exists "$RUNTIME_FS"; then
        if runtime_fs_matches "$expected_provenance"; then
            if ((transaction_present)); then
                clear_runtime_transaction
            else
                reject_unowned_runtime_assets
            fi
            debug "reusing provenance-validated rootfs $RUNTIME_FS"
            rm -f -- "$expected_provenance"
            return
        fi
        ((transaction_present)) || die "$runtime_fs_validation_error"
        ((runtime_fs_validation_recoverable)) || die "$runtime_fs_validation_error"
        if [[ "$proxy_mode" == disabled ]] && runtime_transaction_assets_exist; then
            die "unexpected proxy assets for a direct-network runtime transaction"
        fi
        if [[ "$proxy_mode" == enabled ]]; then
            remove_runtime_transaction_assets
        fi
        run_sdme fs rm -f "$RUNTIME_FS"
        force_rebuild=1
        debug "discarded interrupted unproven runtime rootfs $RUNTIME_FS"
    else
        result=$?
        ((result == 1)) || die "could not inspect rootfs $RUNTIME_FS"
    fi

    if ((transaction_present)); then
        if [[ "$proxy_mode" == enabled ]]; then
            remove_runtime_transaction_assets
        elif runtime_transaction_assets_exist; then
            die "unexpected proxy assets for a direct-network runtime transaction"
        fi
    else
        reject_unowned_runtime_assets
        write_transaction_record runtime "$RUNTIME_FS" \
            "$intent_identity" "$provenance_identity" "$proxy_mode" building
    fi

    build_definition="$asset_root/sdme/worker-rootfs.sdme"
    if [[ "$proxy_mode" == enabled ]]; then
        create_runtime_transaction_assets
        build_definition="$runtime_build_definition"
        force_rebuild=1
    fi
    local build_command=(fs build "$RUNTIME_FS" "$build_definition" --timeout 600)
    if ((force_rebuild)); then
        build_command+=(--no-cache)
    fi
    run_sdme "${build_command[@]}"
    validate_runtime_content
    install_runtime_provenance "$expected_provenance"
    validate_runtime_fs "$expected_provenance"
    clear_runtime_transaction
    rm -f -- "$expected_provenance"
}

generate_runtime_provenance() {
    local destination="$1"
    run_oci_archive_tool runtime "$arch" "$asset_root/sdme/worker-rootfs.sdme" \
        "$(archive_provenance_path "$ubuntu_archive")" \
        "$(archive_provenance_path "$nativelink_archive")" > "$destination"
}

install_runtime_provenance() {
    local provenance="$1"
    run_sdme cp "$provenance" "fs:$RUNTIME_FS:$RUNTIME_PROVENANCE_PATH"
}

validate_managed_directories() {
    local directory mode owner
    for directory in "$data_root" "$@"; do
        [[ -d "$directory" && ! -L "$directory" ]] || \
            die "managed data path is not a real directory: $directory"
        owner=$(stat -c '%u' -- "$directory")
        mode=$(stat -c '%a' -- "$directory")
        [[ "$owner" == 0 ]] || die "managed data path is not root-owned: $directory"
        (( (8#$mode & 8#022) == 0 )) || \
            die "managed data path is group/world-writable: $directory"
    done
}

validate_managed_ancestry() {
    local directory="$1"
    local current mode owner
    current=$(dirname -- "$directory")
    while :; do
        path_is_beneath "$current" "$data_root" || \
            die "managed data path escapes --data-root: $directory"
        if [[ -e "$current" || -L "$current" ]]; then
            [[ -d "$current" && ! -L "$current" ]] || \
                die "managed data path ancestor is not a real directory: $current"
            owner=$(stat -c '%u' -- "$current")
            mode=$(stat -c '%a' -- "$current")
            [[ "$owner" == 0 ]] || \
                die "managed data path ancestor is not root-owned: $current"
            (( (8#$mode & 8#022) == 0 )) || \
                die "managed data path ancestor is group/world-writable: $current"
        fi
        [[ "$current" == "$data_root" ]] && break
        current=$(dirname -- "$current")
    done
}

runtime_service_identity() {
    local temporary identity
    temporary=$(mktemp -d "$provision_dir/service-identity.XXXXXX")
    cleanup_paths+=("$temporary")
    if ! query_sdme cp "fs:$RUNTIME_FS:/etc/passwd" "$temporary" >/dev/null 2>&1 || \
       ! query_sdme cp "fs:$RUNTIME_FS:/etc/group" "$temporary" >/dev/null 2>&1; then
        rm -rf -- "$temporary"
        die "runtime rootfs lacks service account metadata: $RUNTIME_FS"
    fi
    if identity=$("$python_bin" - "$temporary/passwd" "$temporary/group" "$SERVICE_USER" 2>&1 <<'PY'
import sys

passwd_path, group_path, name = sys.argv[1:]


def matching_record(path, expected_fields):
    try:
        with open(path, encoding="utf-8") as stream:
            matches = [
                line.rstrip("\n").split(":")
                for line in stream
                if line.split(":", 1)[0] == name
            ]
    except OSError as error:
        raise SystemExit("cannot read runtime service account metadata: {}".format(error))
    if len(matches) != 1 or len(matches[0]) != expected_fields:
        raise SystemExit("runtime service account metadata is missing or ambiguous")
    return matches[0]


user = matching_record(passwd_path, 7)
group = matching_record(group_path, 4)
uid_text, user_gid_text, group_gid_text = user[2], user[3], group[2]
if not all(value.isdecimal() for value in (uid_text, user_gid_text, group_gid_text)):
    raise SystemExit("runtime service account identity is not numeric")
uid, user_gid, group_gid = map(int, (uid_text, user_gid_text, group_gid_text))
if uid == 0 or user_gid == 0 or group_gid == 0:
    raise SystemExit("runtime service account must not use root identity")
if user_gid != group_gid:
    raise SystemExit("runtime service account primary group does not match")
print("{}:{}".format(uid, user_gid))
PY
    ); then
        :
    else
        rm -rf -- "$temporary"
        [[ -n "$identity" ]] || identity='could not inspect runtime service identity'
        die "$identity"
    fi
    rm -rf -- "$temporary"
    printf '%s\n' "$identity"
}

managed_directory_identity() {
    local label="$1"
    local directory="$2"
    local identity mode owner group
    validate_managed_ancestry "$directory"
    [[ -d "$directory" && ! -L "$directory" ]] || \
        die "$label is not a real directory: $directory"
    identity=$(stat -c '%u:%g:%a' -- "$directory")
    [[ "$identity" =~ ^[0-9]+:[0-9]+:[0-7]+$ ]] || \
        die "could not inspect $label ownership: $directory"
    IFS=: read -r owner group mode <<<"$identity"
    (( (8#$mode & 8#022) == 0 )) || \
        die "$label is group/world-writable: $directory"
    printf '%s:%s\n' "$owner" "$group"
}

validate_root_owned_bind_directory() {
    local label="$1"
    local directory="$2"
    local identity
    identity=$(managed_directory_identity "$label" "$directory")
    [[ "$identity" == 0:0 ]] || die "$label is not root-owned: $directory"
}

validate_transition_bind_directory() {
    local label="$1"
    local directory="$2"
    local expected_identity="$3"
    local identity
    identity=$(managed_directory_identity "$label" "$directory")
    [[ "$identity" == 0:0 || "$identity" == "$expected_identity" ]] || \
        die "$label ownership is neither root nor the runtime service account: $directory"
}

validate_service_bind_directory() {
    local label="$1"
    local directory="$2"
    local expected_identity="$3"
    local identity
    identity=$(managed_directory_identity "$label" "$directory")
    [[ "$identity" == "$expected_identity" ]] || \
        die "$label ownership does not match the runtime service account: $directory"
}

validate_optional_transition_bind_directory() {
    local label="$1"
    local directory="$2"
    local expected_identity="$3"
    if [[ ! -e "$directory" && ! -L "$directory" ]]; then
        validate_managed_ancestry "$directory"
        return
    fi
    validate_transition_bind_directory "$label" "$directory" "$expected_identity"
}

validate_private_managed_file() {
    local label="$1"
    local path="$2"
    local mode owner
    [[ -f "$path" && ! -L "$path" ]] || die "$label is not a regular file: $path"
    validate_safe_file "$label" "$path" >/dev/null
    owner=$(stat -c '%u' -- "$path")
    mode=$(stat -c '%a' -- "$path")
    [[ "$owner" == 0 ]] || die "$label is not root-owned: $path"
    [[ "$mode" == 600 ]] || die "$label must have mode 0600: $path"
    [[ "$(stat -c '%h' -- "$path")" == 1 ]] || die "$label has multiple hard links: $path"
}

sync_path() {
    "$python_bin" - "$1" <<'PY'
import os
import sys

path = sys.argv[1]
flags = os.O_RDONLY
if os.path.isdir(path):
    flags |= getattr(os, "O_DIRECTORY", 0)
descriptor = os.open(path, flags)
try:
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY
}

transaction_identity() {
    printf '%s\0' "$@" | "$sha256_bin" | awk '{print $1}'
}

transaction_record_path() {
    local boundary="$1"
    local target="$2"
    validate_name "$boundary"
    validate_name "$target"
    printf '%s/%s-%s.transaction\n' "$transaction_dir" "$boundary" "$target"
}

emit_transaction_record() {
    local boundary="$1"
    local target="$2"
    local intent_identity="$3"
    local object_identity="$4"
    local mode="$5"
    local phase="$6"
    [[ "$intent_identity" =~ ^[0-9a-f]{64}$ ]] || die "invalid transaction intent identity"
    [[ "$object_identity" =~ ^[0-9a-f]{64}$ ]] || die "invalid transaction object identity"
    [[ "$mode" =~ ^[a-z-]+$ ]] || die "invalid transaction mode"
    [[ "$phase" =~ ^[a-z-]+$ ]] || die "invalid transaction phase"
    printf '%s\n' \
        "schema_version=$TRANSACTION_SCHEMA_VERSION" \
        "boundary=$boundary" \
        "target=$target" \
        "architecture=$arch" \
        "intent_sha256=$intent_identity" \
        "object_sha256=$object_identity" \
        "mode=$mode" \
        "phase=$phase"
}

transaction_record_exists() {
    local path
    path=$(transaction_record_path "$1" "$2")
    [[ -e "$path" || -L "$path" ]]
}

transaction_record_matches() {
    local boundary="$1"
    local target="$2"
    local intent_identity="$3"
    local object_identity="$4"
    local mode="$5"
    local phase="$6"
    local path expected
    path=$(transaction_record_path "$boundary" "$target")
    [[ -e "$path" || -L "$path" ]] || return 1
    [[ ! -L "$path" ]] || die "transaction record is a symlink: $path"
    validate_private_managed_file 'transaction record' "$path"
    expected=$(mktemp "$transaction_dir/expected-transaction.XXXXXX")
    cleanup_paths+=("$expected")
    emit_transaction_record "$boundary" "$target" "$intent_identity" \
        "$object_identity" "$mode" "$phase" > "$expected"
    chmod 0600 "$expected"
    cmp -s -- "$expected" "$path" || die "transaction record does not match this invocation: $path"
    rm -f -- "$expected"
}

transaction_record_object_identity() {
    local boundary="$1"
    local target="$2"
    local path
    path=$(transaction_record_path "$boundary" "$target")
    [[ -e "$path" || -L "$path" ]] || return 1
    [[ ! -L "$path" ]] || die "transaction record is a symlink: $path"
    validate_private_managed_file 'transaction record' "$path"
    "$python_bin" - "$path" <<'PY'
import re
import sys

path = sys.argv[1]
keys = (
    "schema_version",
    "boundary",
    "target",
    "architecture",
    "intent_sha256",
    "object_sha256",
    "mode",
    "phase",
)
with open(path, encoding="utf-8") as stream:
    lines = stream.read().splitlines()
if len(lines) != len(keys):
    raise SystemExit("malformed transaction record: {}".format(path))
record = {}
for expected_key, line in zip(keys, lines):
    key, separator, value = line.partition("=")
    if not separator or key != expected_key or not value or key in record:
        raise SystemExit("malformed transaction record: {}".format(path))
    record[key] = value
if record["schema_version"] != "1":
    raise SystemExit("unsupported transaction record schema: {}".format(path))
if not re.fullmatch(r"[0-9a-f]{64}", record["object_sha256"]):
    raise SystemExit("malformed transaction object identity: {}".format(path))
print(record["object_sha256"])
PY
}

transaction_record_matches_intent() {
    local boundary="$1"
    local target="$2"
    local intent_identity="$3"
    local mode="$4"
    local phase="$5"
    local object_identity
    object_identity=$(transaction_record_object_identity "$boundary" "$target") || \
        die "cannot read transaction record object identity"
    transaction_record_matches "$boundary" "$target" "$intent_identity" \
        "$object_identity" "$mode" "$phase"
}

write_transaction_record() {
    local boundary="$1"
    local target="$2"
    local intent_identity="$3"
    local object_identity="$4"
    local mode="$5"
    local phase="$6"
    local path temporary
    path=$(transaction_record_path "$boundary" "$target")
    [[ ! -e "$path" && ! -L "$path" ]] || die "transaction record already exists: $path"
    temporary=$(mktemp "$transaction_dir/new-transaction.XXXXXX")
    cleanup_paths+=("$temporary")
    emit_transaction_record "$boundary" "$target" "$intent_identity" \
        "$object_identity" "$mode" "$phase" > "$temporary"
    chmod 0600 "$temporary"
    sync_path "$temporary"
    mv -- "$temporary" "$path"
    sync_path "$transaction_dir"
}

clear_transaction_record() {
    local path
    path=$(transaction_record_path "$1" "$2")
    [[ ! -L "$path" ]] || die "transaction record is a symlink: $path"
    if [[ -e "$path" ]]; then
        validate_private_managed_file 'transaction record' "$path"
        rm -f -- "$path"
        sync_path "$transaction_dir"
    fi
}

acquire_provision_lock() {
    local lock_path="$provision_dir/sdme-provision.lock"
    local path_identity descriptor_identity
    [[ ! -L "$lock_path" ]] || die "provision lock is a symlink: $lock_path"
    if [[ ! -e "$lock_path" ]]; then
        "$python_bin" - "$lock_path" <<'PY'
import os
import sys

path = sys.argv[1]
try:
    descriptor = os.open(
        path,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
except FileExistsError:
    pass
else:
    os.close(descriptor)
PY
    fi
    validate_private_managed_file 'provision lock' "$lock_path"
    exec {provision_lock_fd}<>"$lock_path"
    path_identity=$(stat -Lc '%d:%i' -- "$lock_path")
    descriptor_identity=$(stat -Lc '%d:%i' -- "/proc/$$/fd/$provision_lock_fd")
    [[ "$path_identity" == "$descriptor_identity" ]] || die "provision lock changed while opening: $lock_path"
    "$flock_bin" -n "$provision_lock_fd" || die "another provisioning operation holds $lock_path"
}

prepare_mutation_root() {
    [[ ! -L "$provision_dir" ]] || die "managed provision path is a symlink: $provision_dir"
    run_command install -d -m 0750 "$provision_dir"
    validate_managed_directories "$provision_dir"
    acquire_provision_lock
    [[ ! -L "$transaction_dir" ]] || die "transaction directory is a symlink: $transaction_dir"
    run_command install -d -m 0700 "$transaction_dir"
    validate_managed_directories "$transaction_dir"
    chmod 0700 "$transaction_dir"
}

proxy_environment_enabled() {
    local variable
    for variable in \
        http_proxy https_proxy all_proxy no_proxy \
        HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY; do
        if [[ -n "${!variable-}" ]]; then
            return 0
        fi
    done
    return 1
}

generate_proxy_environment_file() {
    local destination="$1"
    "$python_bin" - "$destination" "$BUILD_PROXY_SENTINEL" <<'PY'
import os
import shlex
import sys

path, sentinel = sys.argv[1:]
names = (
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
)
values = {name: os.environ[name] for name in names if os.environ.get(name)}
if not values:
    raise SystemExit("proxy environment disappeared")
descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
    stream.write("# {}\n".format(sentinel))
    for name in names:
        if name in values:
            stream.write("{}={}\n".format(name, shlex.quote(values[name])))
    stream.flush()
    os.fsync(stream.fileno())
PY
}

render_runtime_build_definition() {
    local destination="$1"
    local proxy_source="$2"
    "$python_bin" - "$asset_root/sdme/worker-rootfs.sdme" "$destination" "$proxy_source" <<'PY'
import os
import sys

source, destination, proxy_source = sys.argv[1:]
marker = "# PROVISIONER_PROXY_COPY"
with open(source, encoding="utf-8") as stream:
    text = stream.read()
if text.count(marker) != 1:
    raise SystemExit("worker rootfs must contain exactly one proxy COPY marker")
text = text.replace(marker, "COPY {} /etc/buckos-re-build-proxy.env".format(proxy_source))
descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
    stream.write(text)
    stream.flush()
    os.fsync(stream.fileno())
PY
}

set_runtime_transaction_paths() {
    runtime_proxy_file="$transaction_dir/runtime-$RUNTIME_FS.proxy.env"
    runtime_build_definition="$transaction_dir/runtime-$RUNTIME_FS.build.sdme"
}

validate_runtime_proxy_paths() {
    path_is_beneath "$runtime_proxy_file" "$transaction_dir" || \
        die "runtime proxy transport escaped the transaction directory"
    path_is_beneath "$runtime_build_definition" "$transaction_dir" || \
        die "runtime build definition escaped the transaction directory"
    [[ "$runtime_proxy_file" != *[$' \t\r\n']* ]] || \
        die "managed proxy transport path contains whitespace unsupported by SDME"
}

create_runtime_transaction_assets() {
    [[ ! -e "$runtime_proxy_file" && ! -L "$runtime_proxy_file" ]] || \
        die "unexpected runtime proxy transaction file: $runtime_proxy_file"
    [[ ! -e "$runtime_build_definition" && ! -L "$runtime_build_definition" ]] || \
        die "unexpected runtime build transaction file: $runtime_build_definition"
    cleanup_paths+=("$runtime_proxy_file" "$runtime_build_definition")
    generate_proxy_environment_file "$runtime_proxy_file"
    render_runtime_build_definition "$runtime_build_definition" "$runtime_proxy_file"
    sync_path "$transaction_dir"
}

validate_or_remove_runtime_asset() {
    local label="$1"
    local path="$2"
    [[ ! -L "$path" ]] || die "$label is a symlink: $path"
    if [[ -e "$path" ]]; then
        validate_private_managed_file "$label" "$path"
        rm -f -- "$path"
    fi
}

runtime_transaction_assets_exist() {
    [[ -e "$runtime_proxy_file" || -L "$runtime_proxy_file" || \
       -e "$runtime_build_definition" || -L "$runtime_build_definition" ]]
}

reject_unowned_runtime_assets() {
    runtime_transaction_assets_exist || return 0
    die "runtime transport assets exist without a matching transaction record"
}

remove_runtime_transaction_assets() {
    transaction_record_exists runtime "$RUNTIME_FS" || \
        die "runtime transport assets lack a transaction record"
    validate_or_remove_runtime_asset 'runtime proxy environment' "$runtime_proxy_file"
    validate_or_remove_runtime_asset 'runtime build definition' "$runtime_build_definition"
    sync_path "$transaction_dir"
}

clear_runtime_transaction() {
    remove_runtime_transaction_assets
    clear_transaction_record runtime "$RUNTIME_FS"
    sync_path "$transaction_dir"
}

prepare_runtime() {
    prepare_mutation_root
    run_command install -d -m 0750 "$images_dir"
    validate_managed_directories "$images_dir"
    ensure_runtime_fs
}

inspect_runtime_proxy_absence() {
    local archive
    local result
    archive=$(mktemp "$provision_dir/runtime-inspection.XXXXXX.tar")
    cleanup_paths+=("$archive")
    if ! (umask 077; run_command "$sdme_bin" fs export \
            "fs:$RUNTIME_FS" "$archive" --fmt tar -f); then
        runtime_fs_validation_error="runtime proxy inspection failed: $RUNTIME_FS"
        runtime_fs_validation_recoverable=0
        return 2
    fi
    chmod 0600 "$archive"
    if "$python_bin" - "$archive" "$BUILD_PROXY_SENTINEL" "$BUILD_PROXY_PATH" <<'PY'
import os
import sys
import tarfile

archive_path, sentinel, proxy_path = sys.argv[1:]
names = (
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
)
patterns = {sentinel.encode("utf-8")}
patterns.update(os.environ[name].encode("utf-8") for name in names if os.environ.get(name))
max_pattern = max(map(len, patterns))

try:
    with tarfile.open(archive_path, mode="r:") as archive:
        for member in archive:
            normalized = member.name.removeprefix("./").lstrip("/")
            if normalized == proxy_path.lstrip("/"):
                print("runtime contains proxy transport path", file=sys.stderr)
                raise SystemExit(1)
            if not member.isfile():
                continue
            stream = archive.extractfile(member)
            if stream is None:
                raise RuntimeError("cannot inspect regular member")
            tail = b""
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                data = tail + chunk
                if any(pattern in data for pattern in patterns):
                    print("runtime contains proxy transport content", file=sys.stderr)
                    raise SystemExit(1)
                tail = data[-(max_pattern - 1):] if max_pattern > 1 else b""
except SystemExit:
    raise
except (OSError, tarfile.TarError, RuntimeError) as error:
    print("runtime proxy inspection error: {}".format(error), file=sys.stderr)
    raise SystemExit(2)
PY
    then
        rm -f -- "$archive"
        return 0
    else
        result=$?
    fi
    rm -f -- "$archive"
    if ((result == 1)); then
        runtime_fs_validation_error="runtime rootfs contains proxy transport material: $RUNTIME_FS"
        runtime_fs_validation_recoverable=1
        return 1
    fi
    runtime_fs_validation_error="runtime proxy inspection failed: $RUNTIME_FS"
    runtime_fs_validation_recoverable=0
    return 2
}

runtime_content_matches() {
    local temporary marker result
    runtime_fs_validation_error=''
    runtime_fs_validation_recoverable=1
    temporary=$(mktemp -d "$provision_dir/runtime-check.XXXXXX")
    cleanup_paths+=("$temporary")
    if ! query_sdme cp "fs:$RUNTIME_FS:/etc/nativelink/runtime-images" "$temporary" >/dev/null 2>&1; then
        rm -rf -- "$temporary"
        runtime_fs_validation_error="runtime rootfs lacks provenance marker: $RUNTIME_FS"
        return 1
    fi
    marker="$temporary/runtime-images"
    if ! grep -Fxq "ubuntu_image=$UBUNTU_IMAGE" "$marker"; then
        rm -rf -- "$temporary"
        runtime_fs_validation_error="runtime rootfs Ubuntu digest mismatch: $RUNTIME_FS"
        return 1
    fi
    if ! grep -Fxq "nativelink_image=$NATIVELINK_IMAGE" "$marker"; then
        rm -rf -- "$temporary"
        runtime_fs_validation_error="runtime rootfs NativeLink digest mismatch: $RUNTIME_FS"
        return 1
    fi
    if ! grep -Fxq "architecture=$arch" "$marker"; then
        rm -rf -- "$temporary"
        runtime_fs_validation_error="runtime rootfs architecture mismatch: $RUNTIME_FS"
        return 1
    fi
    rm -rf -- "$temporary"
    if inspect_runtime_proxy_absence; then
        return 0
    else
        result=$?
        return "$result"
    fi
}

runtime_provenance_matches() {
    local expected_provenance="$1"
    local temporary actual_provenance
    temporary=$(mktemp -d "$provision_dir/runtime-provenance-check.XXXXXX")
    cleanup_paths+=("$temporary")
    if ! query_sdme cp "fs:$RUNTIME_FS:$RUNTIME_PROVENANCE_PATH" "$temporary" >/dev/null 2>&1; then
        rm -rf -- "$temporary"
        runtime_fs_validation_error="runtime rootfs lacks strict provenance: $RUNTIME_FS"
        runtime_fs_validation_recoverable=1
        return 1
    fi
    actual_provenance="$temporary/$(basename -- "$RUNTIME_PROVENANCE_PATH")"
    if ! cmp -s -- "$expected_provenance" "$actual_provenance"; then
        rm -rf -- "$temporary"
        runtime_fs_validation_error="runtime rootfs strict provenance mismatch: $RUNTIME_FS"
        runtime_fs_validation_recoverable=1
        return 1
    fi
    rm -rf -- "$temporary"
}

runtime_fs_matches() {
    runtime_content_matches || return $?
    runtime_provenance_matches "$1"
}

validate_runtime_content() {
    runtime_content_matches || die "$runtime_fs_validation_error"
}

validate_runtime_fs() {
    local expected_provenance="${1:-}"
    local temporary=''
    if [[ -z "$expected_provenance" ]]; then
        temporary=$(mktemp "$provision_dir/expected-runtime-provenance.XXXXXX")
        cleanup_paths+=("$temporary")
        generate_runtime_provenance "$temporary"
        expected_provenance="$temporary"
    fi
    runtime_fs_matches "$expected_provenance" || die "$runtime_fs_validation_error"
    if [[ -n "$temporary" ]]; then
        rm -f -- "$temporary"
    fi
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

file_sha256() {
    "$sha256_bin" "$1" | awk '{print $1}'
}

deployment_topology_identity() {
    transaction_identity \
        "runtime=$RUNTIME_FS" \
        "role=$role" \
        "architecture=$arch" \
        "security_mode=$security_mode" \
        "zone=$zone" \
        "container=$container_name" \
        "memory=$memory" \
        "cpus=$cpus" \
        "root_disk=$root_disk" \
        "publish=$publish" \
        "state=$state_dir" \
        "scratch=$scratch_dir" \
        "probe=$probe_sysroot" \
        "probe_sha256=$probe_sysroot_sha256" \
        "control_address=$control_address" \
        "control_dns=$control_dns" \
        "cas_max_bytes=$cas_max_bytes" \
        "ac_max_bytes=$ac_max_bytes" \
        "worker_cas_max_bytes=$worker_cas_max_bytes" \
        "min_scratch_bytes=$min_scratch_bytes" \
        "min_scratch_inodes=$min_scratch_inodes"
}

emit_deployment_identity() {
    local config_sha256 unit_sha256 tls_identity_sha256 topology_sha256
    config_sha256=$(file_sha256 "$config_file")
    unit_sha256=$(file_sha256 "$unit_file")
    topology_sha256=$(deployment_topology_identity)
    if [[ "$security_mode" == mtls ]]; then
        tls_identity_sha256=$(printf '%s' "$tls_identity_json" | "$sha256_bin" | awk '{print $1}')
    else
        tls_identity_sha256='none'
    fi
    printf '%s\n' \
        'schema_version=1' \
        "role=$role" \
        "architecture=$arch" \
        "security_mode=$security_mode" \
        "zone=$zone" \
        "container_name=$container_name" \
        "config_basename=$(basename -- "$config_file")" \
        "config_sha256=$config_sha256" \
        "unit_sha256=$unit_sha256" \
        "control_dns=${control_dns:-none}" \
        "tls_identity_sha256=$tls_identity_sha256" \
        "topology_sha256=$topology_sha256"
}

prepare_deployment_identity() {
    local arguments
    if [[ "$security_mode" == mtls ]]; then
        tls_stage_dir=$(mktemp -d "$provision_dir/tls-stage.XXXXXX")
        cleanup_paths+=("$tls_stage_dir")
        chmod 0700 "$tls_stage_dir"
        arguments=("${tls_helper_args[@]}" --stage-dir "$tls_stage_dir")
        if ! tls_identity_json=$(run_command "${arguments[@]}"); then
            die "mTLS credential staging failed"
        fi
    fi
    expected_deployment_identity=$(mktemp "$provision_dir/deployment-identity.XXXXXX")
    cleanup_paths+=("$expected_deployment_identity")
    emit_deployment_identity > "$expected_deployment_identity"
    chmod 0600 "$expected_deployment_identity"
    expected_deployment_identity_sha256=$(file_sha256 "$expected_deployment_identity")
}

prepare_deployment_transaction() {
    if transaction_record_exists deployment "$container_name"; then
        transaction_record_matches deployment "$container_name" \
            "$expected_deployment_identity_sha256" \
            "$expected_deployment_identity_sha256" \
            "$security_mode" installing
    else
        write_transaction_record deployment "$container_name" \
            "$expected_deployment_identity_sha256" \
            "$expected_deployment_identity_sha256" \
            "$security_mode" installing
    fi
}

validate_snapshot_file() {
    local label="$1"
    local path="$2"
    local expected_mode="$3"
    local expected_uid="$4"
    local expected_gid="$5"
    local metadata
    [[ -f "$path" && ! -L "$path" ]] || die "$label is not a regular file"
    metadata=$(stat -c '%a:%u:%g:%h' -- "$path")
    [[ "$metadata" == "$expected_mode:$expected_uid:$expected_gid:1" ]] || \
        die "$label ownership, mode, or hard-link count is wrong"
}

snapshot_container_assets() {
    container_snapshot_dir=$(mktemp -d "$provision_dir/container-assets.XXXXXX")
    cleanup_paths+=("$container_snapshot_dir")
    if ! query_sdme cp "$container_name:/etc/nativelink" "$container_snapshot_dir" >/dev/null; then
        die "could not inspect installed NativeLink assets"
    fi
}

validate_installed_tls() {
    local service_gid="$1"
    local installed_dir="$container_snapshot_dir/nativelink/tls"
    local arguments=(
        "${tls_helper_args[@]}"
        --installed-dir "$installed_dir"
        --service-gid "$service_gid"
    )
    run_command "${arguments[@]}" >/dev/null || \
        die "installed mTLS credentials do not match this invocation"
}

# Complete verification of the still-private staging tree. The single rename
# that publishes the TLS directory may only happen after this passes.
validate_staged_tls() {
    local service_gid="$1"
    local remote_stage="$2"
    local snapshot staged
    [[ "$remote_stage" == "$TLS_DIRECTORY-${expected_deployment_identity_sha256:0:16}.tmp" ]] || \
        die "mTLS staging directory is not the transaction-owned path"
    snapshot=$(mktemp -d "$provision_dir/tls-stage-check.XXXXXX")
    cleanup_paths+=("$snapshot")
    if ! query_sdme cp "$container_name:$remote_stage" "$snapshot" >/dev/null; then
        die "could not inspect the staged mTLS credentials"
    fi
    staged="$snapshot/$(basename -- "$remote_stage")"
    run_command "${tls_helper_args[@]}" \
        --installed-dir "$staged" --service-gid "$service_gid" >/dev/null || \
        die "staged mTLS credentials do not match this invocation"
    rm -rf -- "$snapshot"
}

validate_complete_deployment() {
    local service_identity="$1"
    local service_gid="${service_identity#*:}"
    local installed_root identity installed_config installed_unit
    local other_basename other_config other_mode
    snapshot_container_assets
    installed_root="$container_snapshot_dir/nativelink"
    identity="$installed_root/$(basename -- "$DEPLOYMENT_IDENTITY_PATH")"
    [[ -e "$identity" || -L "$identity" ]] || return 1
    validate_snapshot_file 'installed deployment identity' "$identity" 600 0 0
    cmp -s -- "$expected_deployment_identity" "$identity" || \
        die "existing container deployment identity does not match this invocation"

    installed_config="$installed_root/$(basename -- "$config_file")"
    validate_snapshot_file 'installed NativeLink config' "$installed_config" 644 0 0
    cmp -s -- "$config_file" "$installed_config" || \
        die "installed NativeLink config does not match this invocation"
    if ! query_sdme cp "$container_name:/etc/systemd/system/nativelink.service" \
        "$container_snapshot_dir" >/dev/null; then
        die "could not inspect installed NativeLink service unit"
    fi
    installed_unit="$container_snapshot_dir/nativelink.service"
    validate_snapshot_file 'installed NativeLink service unit' "$installed_unit" 644 0 0
    cmp -s -- "$unit_file" "$installed_unit" || \
        die "installed NativeLink service unit does not match this invocation"

    if [[ "$security_mode" == mtls ]]; then
        validate_installed_tls "$service_gid"
        other_mode=plaintext
    else
        [[ ! -e "$installed_root/tls" && ! -L "$installed_root/tls" ]] || \
            die "plaintext container contains unexpected TLS credentials"
        other_mode=mtls
    fi
    other_basename=$(deployment_config_basename "$role" "$arch" "$other_mode")
    other_config="$installed_root/$other_basename"
    [[ ! -e "$other_config" && ! -L "$other_config" ]] || \
        die "existing container contains a config for another security mode"
}

# An identity-less container may be resumed only while NativeLink is provably
# neither enabled nor active. Enablement is read from the container's own
# systemd tree, which is decisive whether or not the container is running and
# never starts the service. A running container is additionally asked for its
# live unit state. Any missing or unrecognized evidence refuses the resume.
require_quiescent_nativelink() {
    local status="$1"
    local snapshot installed_units enablement state
    snapshot=$(mktemp -d "$provision_dir/service-state.XXXXXX")
    cleanup_paths+=("$snapshot")
    query_sdme cp "$container_name:$SERVICE_UNIT_DIR" "$snapshot" >/dev/null || \
        die "could not inspect NativeLink enablement in $container_name"
    installed_units="$snapshot/$(basename -- "$SERVICE_UNIT_DIR")"
    if ! enablement=$("$python_bin" - "$installed_units" "$SERVICE_UNIT" 2>&1 <<'PY'
import pathlib
import sys

root, unit = sys.argv[1:]
base = pathlib.Path(root)
if base.is_symlink() or not base.is_dir():
    raise SystemExit("installed systemd state is missing")
enabled_in = []
for entry in sorted(base.iterdir()):
    if entry.suffix not in (".wants", ".requires", ".upholds"):
        continue
    if entry.is_symlink() or not entry.is_dir():
        continue
    candidate = entry / unit
    if candidate.is_symlink() or candidate.exists():
        enabled_in.append(entry.name)
if enabled_in:
    raise SystemExit("NativeLink is enabled through {}".format(", ".join(enabled_in)))
PY
    ); then
        die "${enablement:-could not inspect NativeLink enablement}: $container_name"
    fi

    if [[ "$status" == running ]]; then
        state=$(query_sdme exec "$container_name" --user root -- \
            systemctl is-active "$SERVICE_UNIT") || true
        state=${state//[$'\r\n']/}
        case "$state" in
            inactive|unknown) ;;
            '') die "could not determine the NativeLink runtime state in $container_name" ;;
            *) die "NativeLink is $state in $container_name; recreate the container instead" ;;
        esac
        if [[ -f "$installed_units/$SERVICE_UNIT" && ! -L "$installed_units/$SERVICE_UNIT" ]]; then
            state=$(query_sdme exec "$container_name" --user root -- \
                systemctl is-enabled "$SERVICE_UNIT") || true
            state=${state//[$'\r\n']/}
            case "$state" in
                disabled|static|masked|indirect) ;;
                '') die "could not determine the NativeLink enablement state in $container_name" ;;
                *) die "NativeLink is $state in $container_name; recreate the container instead" ;;
            esac
        fi
    fi
    rm -rf -- "$snapshot"
}

validate_local_plaintext_control() {
    [[ "$role" == worker && "$security_mode" == plaintext ]] || return 0
    local record result temporary identity config_sha256
    local control_config control_basename
    control_config=$(select_security_profile control "$arch" plaintext)
    control_basename=$(basename -- "$control_config")
    if record=$(container_record "$control_container_name"); then
        :
    else
        result=$?
        ((result == 1)) && die "local plaintext control container not found: $control_container_name"
        die "could not inspect local plaintext control container: $control_container_name"
    fi
    "$python_bin" - "$record" "$control_container_name" "$zone" "$RUNTIME_FS" <<'PY'
import json
import sys

record = json.loads(sys.argv[1])
name, zone, rootfs = sys.argv[2:]
network = record.get("network") or {}
errors = []
if record.get("name") != name:
    errors.append("name")
if record.get("rootfs") != rootfs:
    errors.append("rootfs")
if record.get("status") != "running":
    errors.append("status")
if not network.get("private_network"):
    errors.append("private network")
if network.get("network_zone") != zone:
    errors.append("zone")
if network.get("ports"):
    errors.append("published ports")
if errors:
    raise SystemExit(
        "local plaintext control topology mismatch: {}".format(", ".join(errors))
    )
PY
    temporary=$(mktemp -d "$provision_dir/local-control.XXXXXX")
    cleanup_paths+=("$temporary")
    if ! query_sdme cp "$control_container_name:/etc/nativelink" \
        "$temporary" >/dev/null; then
        die "local plaintext control lacks a deployment identity"
    fi
    identity="$temporary/nativelink/$(basename -- "$DEPLOYMENT_IDENTITY_PATH")"
    validate_snapshot_file 'local plaintext control identity' "$identity" 600 0 0
    config_sha256=$(file_sha256 "$control_config")
    "$python_bin" - "$identity" "$control_container_name" "$zone" "$config_sha256" \
        "$control_basename" <<'PY'
import sys

path, name, zone, config_sha256, config_basename = sys.argv[1:]
expected = {
    "schema_version": "1",
    "role": "control",
    "security_mode": "plaintext",
    "zone": zone,
    "container_name": name,
    "config_basename": config_basename,
    "config_sha256": config_sha256,
    "control_dns": "none",
    "tls_identity_sha256": "none",
}
record = {}
with open(path, encoding="utf-8") as stream:
    for line in stream:
        key, separator, value = line.rstrip("\n").partition("=")
        if not separator or not key or not value or key in record:
            raise SystemExit("local plaintext control deployment identity is malformed")
        record[key] = value
for key, value in expected.items():
    if record.get(key) != value:
        raise SystemExit("local plaintext control deployment identity mismatch: {}".format(key))
PY
    validate_snapshot_file 'local plaintext control config' \
        "$temporary/nativelink/$control_basename" 644 0 0
    cmp -s -- "$control_config" "$temporary/nativelink/$control_basename" || \
        die "local plaintext control config does not match the tracked config"
    [[ ! -e "$temporary/nativelink/tls" && ! -L "$temporary/nativelink/tls" ]] || \
        die "local plaintext control contains unexpected TLS credentials"
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

install_tls_credentials() {
    local service_identity="$1"
    local service_gid="${service_identity#*:}"
    local remote_stage="$TLS_DIRECTORY-${expected_deployment_identity_sha256:0:16}.tmp"
    local files=()
    local remote_files=()
    if [[ "$security_mode" == plaintext ]]; then
        if [[ -n "$container_snapshot_dir" && \
              ( -e "$container_snapshot_dir/nativelink/tls" || \
                -L "$container_snapshot_dir/nativelink/tls" ) ]]; then
            die "plaintext container contains unexpected TLS credentials"
        fi
        run_sdme exec "$container_name" --user root -- test ! -e "$TLS_DIRECTORY"
        return
    fi

    if [[ -n "$container_snapshot_dir" && \
          ( -e "$container_snapshot_dir/nativelink/tls" || \
            -L "$container_snapshot_dir/nativelink/tls" ) ]]; then
        validate_installed_tls "$service_gid"
        return
    fi

    if [[ "$role" == control ]]; then
        files=(control-chain.pem control-key.pem reapi-client-ca.pem worker-client-ca.pem)
    else
        files=(control-ca.pem worker-chain.pem worker-key.pem)
    fi
    run_sdme exec "$container_name" --user root -- rm -rf -- "$remote_stage"
    run_sdme exec "$container_name" --user root -- \
        install -d -m 0700 -o root -g root "$remote_stage"
    local file
    for file in "${files[@]}"; do
        run_sdme cp "$tls_stage_dir/$file" "$container_name:$remote_stage/$file"
        remote_files+=("$remote_stage/$file")
    done
    run_sdme exec "$container_name" --user root -- \
        chown "root:$SERVICE_USER" "${remote_files[@]}"
    run_sdme exec "$container_name" --user root -- \
        chmod 0440 "${remote_files[@]}"
    run_sdme exec "$container_name" --user root -- \
        chown "root:$SERVICE_USER" "$remote_stage"
    run_sdme exec "$container_name" --user root -- chmod 0750 "$remote_stage"
    validate_staged_tls "$service_gid" "$remote_stage"
    run_sdme exec "$container_name" --user root -- test ! -e "$TLS_DIRECTORY"
    run_sdme exec "$container_name" --user root -- \
        mv -T -- "$remote_stage" "$TLS_DIRECTORY"

    snapshot_container_assets
    validate_installed_tls "$service_gid"
}

copy_assets() {
    run_sdme cp "$config_file" "$container_name:/etc/nativelink/$(basename -- "$config_file")"
    run_sdme cp "$unit_file" "$container_name:/etc/systemd/system/nativelink.service"
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
    run_sdme exec "$container_name" --user root -- \
        chown root:root \
        "/etc/nativelink/$(basename -- "$config_file")" \
        /etc/systemd/system/nativelink.service
    run_sdme exec "$container_name" --user root -- \
        chmod 0644 \
        "/etc/nativelink/$(basename -- "$config_file")" \
        /etc/systemd/system/nativelink.service
    if [[ "$role" == worker ]]; then
        run_sdme exec "$container_name" --user root -- \
            chown root:root \
            /etc/systemd/system/nativelink.service.d/10-worker-preflight.conf \
            /usr/local/libexec/buckos-re/preflight-worker.sh \
            /usr/local/libexec/buckos-re/preflight_worker.py \
            /usr/local/libexec/buckos-re/tools/_isolation.py \
            /usr/local/libexec/buckos-re/tools/_rpm.py
        run_sdme exec "$container_name" --user root -- \
            chmod 0644 \
            /etc/systemd/system/nativelink.service.d/10-worker-preflight.conf \
            /usr/local/libexec/buckos-re/preflight_worker.py \
            /usr/local/libexec/buckos-re/tools/_isolation.py \
            /usr/local/libexec/buckos-re/tools/_rpm.py
        run_sdme exec "$container_name" --user root -- \
            chmod 0755 /usr/local/libexec/buckos-re/preflight-worker.sh
    fi
}

copy_environment() {
    run_sdme cp "$env_file" "$container_name:/etc/nativelink/nativelink.env"
    run_sdme exec "$container_name" --user root -- \
        chown root:root /etc/nativelink/nativelink.env
    run_sdme exec "$container_name" --user root -- \
        chmod 0600 /etc/nativelink/nativelink.env
}

publish_deployment_identity() {
    local remote_temporary="${DEPLOYMENT_IDENTITY_PATH}.${expected_deployment_identity_sha256:0:16}.tmp"
    run_sdme exec "$container_name" --user root -- rm -f -- "$remote_temporary"
    run_sdme cp "$expected_deployment_identity" "$container_name:$remote_temporary"
    run_sdme exec "$container_name" --user root -- \
        chown root:root "$remote_temporary"
    run_sdme exec "$container_name" --user root -- chmod 0600 "$remote_temporary"
    run_sdme exec "$container_name" --user root -- test ! -e "$DEPLOYMENT_IDENTITY_PATH"
    run_sdme exec "$container_name" --user root -- \
        mv -T -- "$remote_temporary" "$DEPLOYMENT_IDENTITY_PATH"
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

monotonic_milliseconds() {
    "$python_bin" -c 'import time; print(time.monotonic_ns() // 1_000_000)'
}

discover_control_worker_bind_address() {
    local record result address now deadline remaining
    local query_budget sleep_duration sleep_milliseconds
    if ! now=$(monotonic_milliseconds) || [[ ! "$now" =~ ^[0-9]+$ ]]; then
        die "could not start control address readiness deadline"
    fi
    deadline=$((now + CONTROL_ADDRESS_WAIT_SECONDS * 1000))

    while :; do
        if ! now=$(monotonic_milliseconds) || [[ ! "$now" =~ ^[0-9]+$ ]]; then
            die "could not evaluate control address readiness deadline"
        fi
        if ((now >= deadline)); then
            die "timed out after ${CONTROL_ADDRESS_WAIT_SECONDS}s waiting for control container $container_name SDME zone address"
        fi
        remaining=$((deadline - now))
        if ((remaining <= CONTROL_ADDRESS_KILL_GRACE_MILLISECONDS)); then
            die "timed out after ${CONTROL_ADDRESS_WAIT_SECONDS}s waiting for control container $container_name SDME zone address"
        fi
        query_budget=$((remaining - CONTROL_ADDRESS_KILL_GRACE_MILLISECONDS))
        if record=$(container_record "$container_name" "$query_budget"); then
            :
        else
            result=$?
            ((result == ADDRESS_QUERY_TIMEOUT_EXIT)) && \
                die "timed out after ${CONTROL_ADDRESS_WAIT_SECONDS}s waiting for control container $container_name SDME zone address"
            ((result == 1)) && die "container disappeared while waiting for its SDME zone address: $container_name"
            die "could not inspect container while waiting for its SDME zone address: $container_name"
        fi
        if address=$(printf '%s' "$record" | "$python_bin" "$asset_root/scripts/sdme_select_address.py" 2>&1); then
            control_worker_bind_address="$address"
            debug "control container $container_name SDME zone address is ready: $address"
            return
        else
            result=$?
        fi
        ((result == ADDRESS_NOT_READY_EXIT)) || die "$address"

        if ! now=$(monotonic_milliseconds) || [[ ! "$now" =~ ^[0-9]+$ ]]; then
            die "could not evaluate control address readiness deadline"
        fi
        if ((now >= deadline)); then
            die "timed out after ${CONTROL_ADDRESS_WAIT_SECONDS}s waiting for control container $container_name SDME zone address"
        fi
        remaining=$((deadline - now))
        sleep_milliseconds=$((CONTROL_ADDRESS_POLL_SECONDS * 1000))
        if ((remaining < sleep_milliseconds)); then
            sleep_milliseconds=$remaining
        fi
        printf -v sleep_duration '%d.%03d' \
            "$((sleep_milliseconds / 1000))" \
            "$((sleep_milliseconds % 1000))"
        debug "waiting for control container $container_name SDME zone address (${remaining}ms before timeout)"
        "$sleep_bin" "$sleep_duration" || die "control address readiness wait was interrupted"
    done
}

apply_deployment() {
    prepare_mutation_root
    run_command install -d -m 0750 "$images_dir"
    validate_managed_directories "$images_dir"

    if ((publish)); then
        run_command "$firewall_check" \
            --client-port "$REAPI_PORT" \
            --client-cidrs "$client_cidrs" \
            --worker-port "$WORKER_API_PORT" \
            --worker-cidrs "$worker_cidrs"
    fi

    ensure_runtime_fs
    prepare_deployment_identity

    if [[ "$role" == worker && "$security_mode" == plaintext ]]; then
        validate_local_plaintext_control
    fi

    local record result service_identity='' worker_state_dir container_state
    worker_state_dir="$state_dir/worker-$arch"
    if record=$(container_record "$container_name"); then
        validate_existing_container "$record"
        service_identity=$(runtime_service_identity)
        if [[ "$role" == control ]]; then
            validate_transition_bind_directory \
                'managed state path' "$state_dir" "$service_identity"
        else
            validate_root_owned_bind_directory 'managed state path' "$state_dir"
            validate_optional_transition_bind_directory \
                'managed worker state path' "$worker_state_dir" "$service_identity"
            validate_transition_bind_directory \
                'managed scratch path' "$scratch_dir" "$service_identity"
        fi
        if validate_complete_deployment "$service_identity"; then
            if transaction_record_exists deployment "$container_name"; then
                transaction_record_matches deployment "$container_name" \
                    "$expected_deployment_identity_sha256" \
                    "$expected_deployment_identity_sha256" \
                    "$security_mode" installing
            fi
            install_deployment_assets=0
        else
            transaction_record_exists deployment "$container_name" || \
                die "existing container lacks a deployment identity and matching transaction"
            transaction_record_matches deployment "$container_name" \
                "$expected_deployment_identity_sha256" \
                "$expected_deployment_identity_sha256" \
                "$security_mode" installing
            container_state=$(record_status "$record") || \
                die "could not determine the state of container: $container_name"
            require_quiescent_nativelink "$container_state"
            install_deployment_assets=1
        fi
        debug "reusing container $container_name"
    else
        result=$?
        ((result == 1)) || die "could not inspect container: $container_name"
        prepare_deployment_transaction
        [[ ! -L "$state_dir" ]] || die "managed state path is a symlink: $state_dir"
        validate_managed_ancestry "$state_dir"
        run_command install -d -m 0750 "$state_dir"
        validate_managed_ancestry "$state_dir"
        validate_root_owned_bind_directory 'managed state path' "$state_dir"
        if [[ "$role" == worker ]]; then
            [[ ! -L "$scratch_dir" ]] || die "managed scratch path is a symlink: $scratch_dir"
            validate_managed_ancestry "$scratch_dir"
            run_command install -d -m 0750 "$scratch_dir"
            validate_managed_ancestry "$scratch_dir"
            validate_root_owned_bind_directory 'managed scratch path' "$scratch_dir"
        fi
        create_container
        service_identity=$(runtime_service_identity)
        install_deployment_assets=1
    fi

    ensure_started
    if ((install_deployment_assets)); then
        install_tls_credentials "$service_identity"
        copy_assets
    fi
    prepare_container_storage
    if [[ -z "$service_identity" ]]; then
        service_identity=$(runtime_service_identity)
    fi
    if [[ "$role" == control ]]; then
        validate_service_bind_directory \
            'managed state path' "$state_dir" "$service_identity"
    else
        validate_root_owned_bind_directory 'managed state path' "$state_dir"
        validate_service_bind_directory \
            'managed worker state path' "$worker_state_dir" "$service_identity"
        validate_service_bind_directory \
            'managed scratch path' "$scratch_dir" "$service_identity"
    fi
    if [[ "$role" == control ]]; then
        discover_control_worker_bind_address
    fi
    write_environment_file
    copy_environment
    if ((install_deployment_assets)); then
        publish_deployment_identity
    fi
    run_sdme exec "$container_name" --user root -- systemctl daemon-reload
    run_sdme exec "$container_name" --user root -- systemctl enable nativelink.service
    run_sdme exec "$container_name" --user root -- systemctl restart nativelink.service
    if transaction_record_exists deployment "$container_name"; then
        clear_transaction_record deployment "$container_name"
    fi
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
    validate_option_matrix
    prepare_tools
    validate_operation_environment
    validate_host_prerequisites

    case "$operation" in
        prepare-runtime)
            runtime_paths
            validate_runtime_assets
            ;;
        plan|apply)
            role_paths
            validate_assets
            validate_tls_credentials
            validate_probe_digest
            ;;
    esac

    case "$operation" in
        plan) plan_commands ;;
        prepare-runtime) prepare_runtime ;;
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
