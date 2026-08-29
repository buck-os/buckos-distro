#!/usr/bin/env bash
set -euo pipefail

CACHE_TARGET='//flavors/debian:hostname-13-x86_64-source'
X86_TARGET='//flavors/debian:hostname-13-x86_64-build'
AARCH64_TARGET='//flavors/debian:hostname-13-aarch64-build'
script_dir=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)

stage=''
client_a=''
client_b=''
endpoint=''
instance_name=''
tls=''
event_dir=''
buck='./buck2'
grpc_helper="$script_dir/reapi_readiness.py"
grpc_helper_overridden=false
grpc_python='/usr/bin/python3'
timeout_seconds=1800
host_target=''
host_category=''
host_buildroot_config=''
verbose=false

client_a_config_fingerprint=''
client_b_config_fingerprint=''
client_a_buck=''
client_b_buck=''
timeout_bin=''
resolved_path=''
config_fingerprint_value=''
parsed_count=''

usage() {
    cat <<'EOF'
Usage:
  smoke-test.sh --stage STAGE --client-a DIR --client-b DIR \
    --endpoint HOST:PORT --instance-name NAME --tls true|false \
    --event-dir ABSOLUTE_DIR [OPTIONS]

Stages:
  readiness        Run Capabilities and CAS round-trip checks through --grpc-helper.
  cache            Prove Client A upload and clean Client B cache reuse.
  x86_64           Force the bounded x86_64 Debian source build through RE.
  aarch64          Force the bounded native AArch64 Debian source build through RE.
  host-provenance  Prove a supplied host-buildroot action remains local on two clients.
  all              Run readiness, cache, x86_64, and aarch64 in that order.

Options:
  --buck PATH                    Buck2 executable, absolute or relative to each client.
                                 Default: ./buck2
  --grpc-helper PATH             Executable used by readiness. It must support:
                                   capabilities --endpoint E --instance-name I --tls B
                                   cas-round-trip --endpoint E --instance-name I --tls B
                                 The first command must validate REAPI v2 and
                                 SHA-256 capability. The second must upload,
                                 read back, and hash-check a bounded CAS blob.
                                 Default: reapi_readiness.py beside this script.
  --grpc-python PATH             Explicit interpreter for the default helper.
                                 Default: /usr/bin/python3. Ignored for an
                                 external --grpc-helper override.
  --timeout-seconds N            Per-command deadline. Default: 1800.
  --host-target LABEL            Required by host-provenance.
  --host-category CATEGORY       Required by host-provenance.
  --host-buildroot-config K=host Required by host-provenance, for example
                                 buckos.fedora.buildroot=host.
  -v, --verbose                  Print executed command descriptions to stderr.
  -h, --help                     Show this help.

The two clients must be distinct clean checkouts at the same commit, with no
buck-out entry when a build stage starts. Configuration is passed with Buck2
--config flags; this script never edits .buckconfig.local. Use an empty
dedicated NativeLink instance for the cache stage. Event logs and derived
evidence are retained under --event-dir.
EOF
}

record() {
    local status=$1
    local check=$2
    shift 2
    printf '%s %s %s\n' "$status" "$check" "$*"
}

debug() {
    if [[ $verbose == true ]]; then
        printf 'smoke-test: %s\n' "$*" >&2
    fi
}

usage_error() {
    record FAIL arguments "$*"
    usage >&2
    exit 2
}

fail() {
    local check=$1
    shift
    record FAIL "$check" "$*"
    exit 1
}

require_value() {
    local option=$1
    local count=$2
    if ((count < 2)); then
        usage_error "$option requires a value"
    fi
}

while (($#)); do
    case "$1" in
        --stage)
            require_value "$1" "$#"
            stage=$2
            shift 2
            ;;
        --client-a)
            require_value "$1" "$#"
            client_a=$2
            shift 2
            ;;
        --client-b)
            require_value "$1" "$#"
            client_b=$2
            shift 2
            ;;
        --endpoint)
            require_value "$1" "$#"
            endpoint=$2
            shift 2
            ;;
        --instance-name)
            require_value "$1" "$#"
            instance_name=$2
            shift 2
            ;;
        --tls)
            require_value "$1" "$#"
            tls=$2
            shift 2
            ;;
        --event-dir)
            require_value "$1" "$#"
            event_dir=$2
            shift 2
            ;;
        --buck)
            require_value "$1" "$#"
            buck=$2
            shift 2
            ;;
        --grpc-helper)
            require_value "$1" "$#"
            grpc_helper=$2
            grpc_helper_overridden=true
            shift 2
            ;;
        --grpc-python)
            require_value "$1" "$#"
            grpc_python=$2
            shift 2
            ;;
        --timeout-seconds)
            require_value "$1" "$#"
            timeout_seconds=$2
            shift 2
            ;;
        --host-target)
            require_value "$1" "$#"
            host_target=$2
            shift 2
            ;;
        --host-category)
            require_value "$1" "$#"
            host_category=$2
            shift 2
            ;;
        --host-buildroot-config)
            require_value "$1" "$#"
            host_buildroot_config=$2
            shift 2
            ;;
        -v|--verbose)
            verbose=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            usage_error "unknown option: $1"
            ;;
    esac
done

canonical_directory() {
    local path=$1
    local name=$2
    local resolved

    [[ -n $path ]] || usage_error "$name is required"
    [[ -d $path ]] || usage_error "$name is not a directory: $path"
    if ! resolved=$(realpath -e -- "$path"); then
        usage_error "$name cannot be resolved: $path"
    fi
    resolved_path=$resolved
}

canonical_executable() {
    local path=$1
    local base=$2
    local name=$3
    local candidate resolved

    if [[ $path == /* ]]; then
        candidate=$path
    else
        candidate="$base/${path#./}"
    fi
    if ! resolved=$(realpath -e -- "$candidate"); then
        usage_error "$name cannot be resolved: $candidate"
    fi
    [[ -f $resolved && -x $resolved ]] || usage_error "$name is not an executable regular file: $resolved"
    resolved_path=$resolved
}

config_fingerprint() {
    local path=$1
    if [[ ! -e $path ]]; then
        config_fingerprint_value=absent
        return 0
    fi
    [[ -f $path ]] || return 1
    if ! config_fingerprint_value=$(sha256sum -- "$path" | awk '{print $1}'); then
        return 1
    fi
}

check_config_unchanged() {
    local changed=false

    if [[ -n $client_a_config_fingerprint ]]; then
        if ! config_fingerprint "$client_a/.buckconfig.local"; then
            record FAIL config.client-a ".buckconfig.local is no longer a regular readable file"
            changed=true
        elif [[ $config_fingerprint_value != "$client_a_config_fingerprint" ]]; then
            record FAIL config.client-a ".buckconfig.local changed"
            changed=true
        fi
    fi
    if [[ -n $client_b_config_fingerprint ]]; then
        if ! config_fingerprint "$client_b/.buckconfig.local"; then
            record FAIL config.client-b ".buckconfig.local is no longer a regular readable file"
            changed=true
        elif [[ $config_fingerprint_value != "$client_b_config_fingerprint" ]]; then
            record FAIL config.client-b ".buckconfig.local changed"
            changed=true
        fi
    fi
    [[ $changed == false ]]
}

on_exit() {
    local status=$?
    trap - EXIT
    if ! check_config_unchanged; then
        status=1
    fi
    exit "$status"
}

require_clean_client() {
    local client=$1
    local name=$2
    if [[ -e $client/buck-out ]]; then
        usage_error "$name is not clean: $client/buck-out exists"
    fi
    record PASS "$name.clean" "buck-out absent"
}

reserve_output() {
    local path=$1
    [[ ! -e $path ]] || usage_error "refusing to overwrite evidence: $path"
}

run_capture() {
    local cwd=$1
    local check=$2
    local output=$3
    shift 3

    reserve_output "$output"
    debug "$check: cwd=$cwd output=$output command=$*"
    if (cd "$cwd" && "$timeout_bin" --signal=TERM --kill-after=30s "${timeout_seconds}s" "$@") >"$output" 2>&1; then
        return
    else
        local status=$?
        fail "$check" "command exited $status; see $output"
    fi
}

assert_contains() {
    local check=$1
    local path=$2
    local expected=$3
    grep -Fq -- "$expected" "$path" || fail "$check" "missing '$expected' in $path"
}

assert_not_contains_regex() {
    local check=$1
    local path=$2
    local pattern=$3
    if grep -Eq -- "$pattern" "$path"; then
        fail "$check" "unexpected pattern '$pattern' in $path"
    fi
}

summary_count() {
    local path=$1
    local key=$2
    local value

    value=$(awk -v key="$key" '
        {
            line = $0
            sub(/^[[:space:]]*-[[:space:]]*/, "", line)
            prefix = key ":"
            if (index(line, prefix) == 1) {
                sub("^" prefix "[[:space:]]*", "", line)
                gsub(/,/, "", line)
                print line
                exit
            }
        }
    ' "$path")
    [[ $value =~ ^[0-9]+$ ]] || fail log-summary "cannot parse '$key' from $path"
    parsed_count=$value
}

upload_digest_count() {
    local path=$1
    local value

    value=$(sed -nE 's/.*total: digests: ([0-9,]+), bytes: .*/\1/p' "$path" | tail -n 1)
    value=${value//,/}
    [[ $value =~ ^[0-9]+$ ]] || fail upload-summary "cannot parse uploaded digest count from $path"
    parsed_count=$value
}

require_equal() {
    local check=$1
    local actual=$2
    local expected=$3
    [[ $actual == "$expected" ]] || fail "$check" "expected=$expected actual=$actual"
    record PASS "$check" "value=$actual"
}

require_positive() {
    local check=$1
    local actual=$2
    ((actual > 0)) || fail "$check" "expected a positive value, actual=$actual"
    record PASS "$check" "value=$actual"
}

set_config_args() {
    local remote_execution=$1
    CONFIG_ARGS=(
        --config buckos.remote_cache=true
        --config "buckos.remote_execution=$remote_execution"
        --config buckos.aarch64_emulation=false
        --config buckos.remote_x86_64_properties=platform.OSFamily=linux,platform.arch=x86_64
        --config buckos.remote_aarch64_properties=platform.OSFamily=linux,platform.arch=aarch64
        --config buckos.remote_x86_64_use_case=buck2-default
        --config buckos.remote_aarch64_use_case=buck2-default
        --config "buck2_re_client.engine_address=$endpoint"
        --config "buck2_re_client.action_cache_address=$endpoint"
        --config "buck2_re_client.cas_address=$endpoint"
        --config "buck2_re_client.instance_name=$instance_name"
        --config "buck2_re_client.tls=$tls"
    )
}

audit_config() {
    local client=$1
    local buck_path=$2
    local name=$3
    local remote_execution=$4
    local output="$event_dir/config-$name.log"

    set_config_args "$remote_execution"
    run_capture "$client" "config.$name" "$output" \
        "$buck_path" audit config \
        buckos.remote_cache \
        buckos.remote_execution \
        buckos.aarch64_emulation \
        buckos.remote_x86_64_properties \
        buckos.remote_aarch64_properties \
        buckos.remote_x86_64_use_case \
        buckos.remote_aarch64_use_case \
        buck2_re_client.engine_address \
        buck2_re_client.action_cache_address \
        buck2_re_client.cas_address \
        buck2_re_client.instance_name \
        buck2_re_client.tls \
        "${CONFIG_ARGS[@]}"
    assert_contains "config.$name.remote-cache" "$output" 'remote_cache = true'
    assert_contains "config.$name.remote-execution" "$output" "remote_execution = $remote_execution"
    assert_contains "config.$name.engine" "$output" "engine_address = $endpoint"
    assert_contains "config.$name.action-cache" "$output" "action_cache_address = $endpoint"
    assert_contains "config.$name.cas" "$output" "cas_address = $endpoint"
    assert_contains "config.$name.instance" "$output" "instance_name = $instance_name"
    assert_contains "config.$name.tls" "$output" "tls = $tls"
    record PASS "config.$name" "validated without editing .buckconfig.local"
}

collect_log_evidence() {
    local client=$1
    local buck_path=$2
    local event_log=$3
    local prefix=$4
    local category=$5

    run_capture "$client" "$prefix.summary" "$event_dir/$prefix-summary.log" \
        "$buck_path" log summary "$event_log"
    run_capture "$client" "$prefix.uploads" "$event_dir/$prefix-uploads.log" \
        "$buck_path" log what-uploaded --format json "$event_log"
    run_capture "$client" "$prefix.what-ran" "$event_dir/$prefix-what-ran.jsonl" \
        "$buck_path" log what-ran --format json --emit-cache-queries \
        --filter-category "$category" "$event_log"
}

run_readiness() {
    local helper grpcio_version python_path
    local -a helper_command

    canonical_executable "$grpc_helper" "$PWD" grpc-helper
    helper=$resolved_path
    if [[ $grpc_helper_overridden == true ]]; then
        helper_command=("$helper")
    else
        canonical_executable "$grpc_python" "$PWD" grpc-python
        python_path=$resolved_path
        run_capture "$PWD" readiness.python "$event_dir/readiness-python.log" \
            "$python_path" -I -c \
            'import grpc, sys; version = getattr(grpc, "__version__", ""); print(version) if version else sys.exit("grpcio has no version")'
        grpcio_version=$(tail -n 1 "$event_dir/readiness-python.log")
        [[ -n $grpcio_version ]] || fail readiness.python 'grpcio version output is empty'
        record PASS readiness.python "interpreter=$python_path grpcio=$grpcio_version"
        helper_command=("$python_path" -I "$helper")
    fi

    run_capture "$PWD" readiness.capabilities "$event_dir/readiness-capabilities.log" \
        "${helper_command[@]}" capabilities --endpoint "$endpoint" \
        --instance-name "$instance_name" --tls "$tls"
    record PASS readiness.capabilities "endpoint=$endpoint instance=$instance_name"

    run_capture "$PWD" readiness.cas "$event_dir/readiness-cas.log" \
        "${helper_command[@]}" cas-round-trip --endpoint "$endpoint" \
        --instance-name "$instance_name" --tls "$tls"
    record PASS readiness.cas "digest-verified round trip completed"
}

run_cache() {
    local event_a="$event_dir/cache-client-a.json-lines.gz"
    local event_b="$event_dir/cache-client-b.json-lines.gz"
    local local_a remote_a cached_a uploads_a remote_b cached_b

    audit_config "$client_a" "$client_a_buck" cache-client-a false
    audit_config "$client_b" "$client_b_buck" cache-client-b false
    set_config_args false

    reserve_output "$event_a"
    run_capture "$client_a" cache.client-a.build "$event_dir/cache-client-a-command.log" \
        "$client_a_buck" build "$CACHE_TARGET" --event-log "$event_a" "${CONFIG_ARGS[@]}"
    [[ -s $event_a ]] || fail cache.client-a.event-log "missing or empty event log: $event_a"
    collect_log_evidence "$client_a" "$client_a_buck" "$event_a" cache-client-a dsc_unpack

    summary_count "$event_dir/cache-client-a-summary.log" 'Local actions'
    local_a=$parsed_count
    summary_count "$event_dir/cache-client-a-summary.log" 'Remote actions'
    remote_a=$parsed_count
    summary_count "$event_dir/cache-client-a-summary.log" 'Cached actions'
    cached_a=$parsed_count
    upload_digest_count "$event_dir/cache-client-a-uploads.log"
    uploads_a=$parsed_count
    require_positive cache.client-a.local "$local_a"
    require_equal cache.client-a.remote "$remote_a" 0
    require_equal cache.client-a.cache-hit "$cached_a" 0
    require_positive cache.client-a.upload "$uploads_a"
    assert_contains cache.client-a.action "$event_dir/cache-client-a-what-ran.jsonl" 'hostname-13-x86_64-source'
    assert_contains cache.client-a.executor "$event_dir/cache-client-a-what-ran.jsonl" '"executor":"Local"'
    record PASS cache.client-a "cold local execution uploaded cacheable outputs"

    reserve_output "$event_b"
    run_capture "$client_b" cache.client-b.build "$event_dir/cache-client-b-command.log" \
        "$client_b_buck" build "$CACHE_TARGET" --event-log "$event_b" "${CONFIG_ARGS[@]}"
    [[ -s $event_b ]] || fail cache.client-b.event-log "missing or empty event log: $event_b"
    collect_log_evidence "$client_b" "$client_b_buck" "$event_b" cache-client-b dsc_unpack

    summary_count "$event_dir/cache-client-b-summary.log" 'Remote actions'
    remote_b=$parsed_count
    summary_count "$event_dir/cache-client-b-summary.log" 'Cached actions'
    cached_b=$parsed_count
    require_equal cache.client-b.remote "$remote_b" 0
    require_positive cache.client-b.cache-hit "$cached_b"
    assert_contains cache.client-b.action "$event_dir/cache-client-b-what-ran.jsonl" 'hostname-13-x86_64-source'
    assert_not_contains_regex cache.client-b.no-local-execution \
        "$event_dir/cache-client-b-what-ran.jsonl" '"executor":"Local"'
    record PASS cache.no-execute "remote_execution=false and both event logs report zero remote actions"
    record PASS cache.client-b "clean client reused the shared action cache"
}

run_execution() {
    local architecture=$1
    local target expected_platform event_log prefix
    local local_actions remote_actions cached_actions action_digest

    case "$architecture" in
        x86_64)
            target=$X86_TARGET
            expected_platform='buckos//platforms:platforms-remote-x86_64'
            prefix=re-x86_64
            ;;
        aarch64)
            target=$AARCH64_TARGET
            expected_platform='buckos//platforms:platforms-remote-aarch64'
            prefix=re-aarch64
            ;;
        *)
            fail internal "unsupported architecture: $architecture"
            ;;
    esac
    event_log="$event_dir/$prefix.json-lines.gz"

    audit_config "$client_a" "$client_a_buck" "$prefix" true
    set_config_args true
    run_capture "$client_a" "$prefix.platform" "$event_dir/$prefix-platform.log" \
        "$client_a_buck" audit execution-platform-resolution "$target" "${CONFIG_ARGS[@]}"
    assert_contains "$prefix.platform" "$event_dir/$prefix-platform.log" \
        "Execution platform: $expected_platform"
    if [[ $architecture == aarch64 ]]; then
        assert_contains "$prefix.x86-rejected" "$event_dir/$prefix-platform.log" \
            'Skipped buckos//platforms:platforms-remote-x86_64'
        assert_contains "$prefix.constraint" "$event_dir/$prefix-platform.log" \
            'can-execute-aarch64'
    fi
    record PASS "$prefix.platform" "selected=$expected_platform"

    reserve_output "$event_log"
    run_capture "$client_a" "$prefix.build" "$event_dir/$prefix-command.log" \
        "$client_a_buck" build "$target" --remote-only --no-remote-cache \
        --event-log "$event_log" "${CONFIG_ARGS[@]}"
    [[ -s $event_log ]] || fail "$prefix.event-log" "missing or empty event log: $event_log"
    collect_log_evidence "$client_a" "$client_a_buck" "$event_log" "$prefix" deb_build

    summary_count "$event_dir/$prefix-summary.log" 'Local actions'
    local_actions=$parsed_count
    summary_count "$event_dir/$prefix-summary.log" 'Remote actions'
    remote_actions=$parsed_count
    summary_count "$event_dir/$prefix-summary.log" 'Cached actions'
    cached_actions=$parsed_count
    require_equal "$prefix.local-fallback" "$local_actions" 0
    require_positive "$prefix.remote-actions" "$remote_actions"
    require_equal "$prefix.cache-hit" "$cached_actions" 0
    assert_contains "$prefix.action" "$event_dir/$prefix-what-ran.jsonl" "${target#//flavors/debian:}"
    assert_contains "$prefix.executor" "$event_dir/$prefix-what-ran.jsonl" '"executor":"RE"'
    assert_not_contains_regex "$prefix.no-local-record" \
        "$event_dir/$prefix-what-ran.jsonl" '"executor":"Local"'

    action_digest=$(grep -Eo '[[:xdigit:]]{64}:[0-9]+' "$event_dir/$prefix-what-ran.jsonl" | head -n 1 || true)
    [[ -n $action_digest ]] || fail "$prefix.action-digest" "remote deb_build action digest is absent"
    record PASS "$prefix.action-digest" "digest=$action_digest"
    record PASS "$prefix" "forced remote execution completed without cache or local fallback"
}

validate_host_options() {
    [[ -n $host_target ]] || usage_error "--host-target is required for host-provenance"
    [[ -n $host_category ]] || usage_error "--host-category is required for host-provenance"
    [[ -n $host_buildroot_config ]] || usage_error "--host-buildroot-config is required for host-provenance"
    [[ $host_buildroot_config =~ ^buckos\.[A-Za-z0-9_.-]+\.buildroot=host$ ]] || \
        usage_error "--host-buildroot-config must be buckos.FLAVOR.buildroot=host"
    if [[ ${host_target,,} == *iso* ]]; then
        usage_error "ISO targets are forbidden: $host_target"
    fi
}

run_host_provenance() {
    local client buck_path name event_log
    local local_actions remote_actions

    validate_host_options
    set_config_args true
    CONFIG_ARGS+=(--config "$host_buildroot_config")

    for name in a b; do
        if [[ $name == a ]]; then
            client=$client_a
            buck_path=$client_a_buck
        else
            client=$client_b
            buck_path=$client_b_buck
        fi
        event_log="$event_dir/host-client-$name.json-lines.gz"
        reserve_output "$event_log"
        run_capture "$client" "host.client-$name.build" "$event_dir/host-client-$name-command.log" \
            "$buck_path" build "$host_target" --event-log "$event_log" "${CONFIG_ARGS[@]}"
        [[ -s $event_log ]] || fail "host.client-$name.event-log" "missing or empty event log: $event_log"
        collect_log_evidence "$client" "$buck_path" "$event_log" "host-client-$name" "$host_category"
        summary_count "$event_dir/host-client-$name-summary.log" 'Local actions'
        local_actions=$parsed_count
        summary_count "$event_dir/host-client-$name-summary.log" 'Remote actions'
        remote_actions=$parsed_count
        require_positive "host.client-$name.local" "$local_actions"
        require_equal "host.client-$name.remote" "$remote_actions" 0
        assert_contains "host.client-$name.action" "$event_dir/host-client-$name-what-ran.jsonl" "${host_target##*:}"
        assert_contains "host.client-$name.executor" "$event_dir/host-client-$name-what-ran.jsonl" '"executor":"Local"'
    done
    record PASS host-provenance "selected action executed locally on both clients and was not reused from RE"
}

validate_arguments() {
    local required_tool

    for required_tool in realpath sha256sum awk grep sed tail head timeout; do
        command -v "$required_tool" >/dev/null 2>&1 || \
            usage_error "required evidence tool is unavailable: $required_tool"
    done

    case "$stage" in
        readiness|cache|x86_64|aarch64|host-provenance|all)
            ;;
        '')
            usage_error '--stage is required'
            ;;
        *)
            usage_error "invalid stage: $stage"
            ;;
    esac

    canonical_directory "$client_a" --client-a
    client_a=$resolved_path
    canonical_directory "$client_b" --client-b
    client_b=$resolved_path
    [[ $client_a != "$client_b" ]] || usage_error '--client-a and --client-b must be distinct'
    [[ -f $client_a/.buckconfig ]] || usage_error "--client-a is not a Buck project root: $client_a"
    [[ -f $client_b/.buckconfig ]] || usage_error "--client-b is not a Buck project root: $client_b"

    [[ -n $endpoint ]] || usage_error '--endpoint is required'
    [[ $endpoint != *://* && $endpoint != *[[:space:]]* && $endpoint == *:* ]] || \
        usage_error '--endpoint must be an authority such as host:50051, without a URI scheme'
    [[ -n $instance_name && $instance_name != *[[:space:]]* ]] || usage_error '--instance-name is required and cannot contain whitespace'
    [[ $tls == true || $tls == false ]] || usage_error '--tls must be true or false'

    [[ -n $event_dir ]] || usage_error '--event-dir is required'
    [[ $event_dir == /* ]] || usage_error '--event-dir must be absolute'
    mkdir -p -- "$event_dir"
    canonical_directory "$event_dir" --event-dir
    event_dir=$resolved_path
    case "$event_dir/" in
        "$client_a/"*|"$client_b/"*)
            usage_error '--event-dir must be outside both client checkouts'
            ;;
    esac

    [[ $timeout_seconds =~ ^[1-9][0-9]*$ ]] || usage_error '--timeout-seconds must be a positive integer'
    timeout_bin=$(command -v timeout || true)
    [[ -n $timeout_bin ]] || usage_error 'GNU timeout is required'
    timeout_bin=$(realpath -e -- "$timeout_bin")

    canonical_executable "$buck" "$client_a" 'Buck2 for Client A'
    client_a_buck=$resolved_path
    canonical_executable "$buck" "$client_b" 'Buck2 for Client B'
    client_b_buck=$resolved_path

    config_fingerprint "$client_a/.buckconfig.local" || usage_error "$client_a/.buckconfig.local is not a regular readable file"
    client_a_config_fingerprint=$config_fingerprint_value
    config_fingerprint "$client_b/.buckconfig.local" || usage_error "$client_b/.buckconfig.local is not a regular readable file"
    client_b_config_fingerprint=$config_fingerprint_value
    trap on_exit EXIT

    local version_a version_b
    if ! version_a=$(cd "$client_a" && "$timeout_bin" --signal=TERM --kill-after=5s 30s "$client_a_buck" --version 2>&1); then
        fail prerequisites.buck-a "Buck2 is unavailable in $client_a"
    fi
    if ! version_b=$(cd "$client_b" && "$timeout_bin" --signal=TERM --kill-after=5s 30s "$client_b_buck" --version 2>&1); then
        fail prerequisites.buck-b "Buck2 is unavailable in $client_b"
    fi
    [[ $version_a == "$version_b" ]] || fail prerequisites.buck-version "Client versions differ: '$version_a' versus '$version_b'"
    record PASS prerequisites.buck-version "$version_a"

    case "$stage" in
        cache|all)
            require_clean_client "$client_a" client-a
            require_clean_client "$client_b" client-b
            ;;
        x86_64|aarch64)
            require_clean_client "$client_a" client-a
            ;;
        host-provenance)
            require_clean_client "$client_a" client-a
            require_clean_client "$client_b" client-b
            validate_host_options
            ;;
    esac
}

main() {
    validate_arguments

    case "$stage" in
        readiness)
            run_readiness
            ;;
        cache)
            run_cache
            ;;
        x86_64)
            run_execution x86_64
            ;;
        aarch64)
            run_execution aarch64
            ;;
        host-provenance)
            run_host_provenance
            ;;
        all)
            run_readiness
            run_cache
            run_execution x86_64
            run_execution aarch64
            record WARN host-provenance.skipped 'run as a separate stage with two fresh clients'
            ;;
    esac

    if ! check_config_unchanged; then
        trap - EXIT
        exit 1
    fi
    record PASS config.unchanged '.buckconfig.local unchanged on both clients'
    trap - EXIT
    record PASS smoke-test "stage=$stage"
}

main
