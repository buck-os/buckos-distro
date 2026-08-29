#!/usr/bin/env bash
set -euo pipefail

CACHE_TARGET='//flavors/debian:hostname-13-x86_64-source'
X86_TARGET='//flavors/debian:hostname-13-x86_64-build'
AARCH64_TARGET='//flavors/debian:hostname-13-aarch64-build'
X86_PROBE_TARGET='//infra/remote-execution:worker-architecture-x86_64'
AARCH64_PROBE_TARGET='//infra/remote-execution:worker-architecture-aarch64'
script_dir=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)

stage=''
client_a=''
client_b=''
client_a_isolation=''
client_b_isolation=''
endpoint=''
instance_name=''
tls=''
tls_ca=''
tls_client_chain=''
tls_client_key=''
buck_tls_client_cert=''
cross_host=false
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
client_a_daemon_touched=false
client_b_daemon_touched=false
active_command_pid=''

usage() {
    cat <<'EOF'
Usage:
  smoke-test.sh --stage STAGE --client-a DIR --client-b DIR \
    --endpoint HOST:PORT --instance-name NAME --tls true|false \
    --event-dir ABSOLUTE_DIR [OPTIONS]

Stages:
  readiness        Run Capabilities and CAS round-trip checks through --grpc-helper.
  cache            Prove Client A upload and clean Client B cache reuse.
  x86_64           Attest x86_64 execution, then run the bounded Debian source build.
  aarch64          Attest native AArch64 execution, then run the bounded Debian source build.
  host-provenance  Prove a supplied host-buildroot action remains local on two clients.
  all              Run readiness, cache, x86_64, and aarch64 in that order.

Options:
  --client-a-isolation NAME      Buck isolation directory for Client A.
  --client-b-isolation NAME      Buck isolation directory for Client B.
                                 Defaults are distinct names derived from the
                                 canonical event directory.
  --buck PATH                    Buck2 executable, absolute or relative to each client.
                                 Default: ./buck2
  --grpc-helper PATH             Executable used by readiness. It must support:
                                   capabilities --endpoint E --instance-name I --tls B
                                   cas-round-trip --endpoint E --instance-name I --tls B
                                 TLS calls also receive --tls-ca,
                                 --tls-client-chain, and --tls-client-key.
                                 The first command must validate REAPI v2 and
                                 SHA-256 capability. The second must upload,
                                 read back, and hash-check a bounded CAS blob.
                                 Default: reapi_readiness.py beside this script.
  --grpc-python PATH             Explicit interpreter for the default helper.
                                 Default: /usr/bin/python3. Ignored for an
                                 external --grpc-helper override.
  --tls-ca PATH                  PEM CA bundle for the REAPI server.
  --tls-client-chain PATH        PEM client certificate chain for readiness.
  --tls-client-key PATH          PEM client private key for readiness.
  --buck-tls-client-cert PATH    Combined PEM client identity for Buck2.
  --cross-host                   Reject plaintext transport for this invocation.
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
evidence are retained under --event-dir. Every Buck invocation uses its
client's isolation directory. On normal exit, failure, HUP, INT, or TERM, the
harness attempts `buck2 --isolation-dir NAME kill` only for clients that ran a
daemon-capable command. Cleanup output is retained under --event-dir; cleanup
failure changes a successful run to failure but never replaces an earlier
failure status.

Plaintext is limited to an explicitly local invocation. Pass --cross-host for
any published or routed endpoint; it requires --tls true and all credential
paths. Buck2 uses --buck-tls-client-cert as its combined certificate-chain and
private-key PEM, while the readiness helper receives those files separately.
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
        --client-a-isolation)
            require_value "$1" "$#"
            client_a_isolation=$2
            shift 2
            ;;
        --client-b-isolation)
            require_value "$1" "$#"
            client_b_isolation=$2
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
        --tls-ca)
            require_value "$1" "$#"
            tls_ca=$2
            shift 2
            ;;
        --tls-client-chain)
            require_value "$1" "$#"
            tls_client_chain=$2
            shift 2
            ;;
        --tls-client-key)
            require_value "$1" "$#"
            tls_client_key=$2
            shift 2
            ;;
        --buck-tls-client-cert)
            require_value "$1" "$#"
            buck_tls_client_cert=$2
            shift 2
            ;;
        --cross-host)
            cross_host=true
            shift
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

canonical_readable_file() {
    local path=$1
    local name=$2
    local resolved

    [[ -n $path ]] || usage_error "$name is required with --tls true"
    [[ $path == /* ]] || usage_error "$name must be absolute"
    if ! resolved=$(realpath -e -- "$path"); then
        usage_error "$name cannot be resolved: $path"
    fi
    [[ -f $resolved && -r $resolved ]] || \
        usage_error "$name is not a readable regular file: $resolved"
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

mark_daemon_touched() {
    case $1 in
        client-a)
            client_a_daemon_touched=true
            ;;
        client-b)
            client_b_daemon_touched=true
            ;;
        *)
            fail internal "unknown client identity: $1"
            ;;
    esac
}

cleanup_client() {
    local name=$1
    local client buck_path isolation touched output status

    case $name in
        client-a)
            client=$client_a
            buck_path=$client_a_buck
            isolation=$client_a_isolation
            touched=$client_a_daemon_touched
            ;;
        client-b)
            client=$client_b
            buck_path=$client_b_buck
            isolation=$client_b_isolation
            touched=$client_b_daemon_touched
            ;;
        *)
            record FAIL cleanup "unknown client identity: $name"
            return 1
            ;;
    esac

    if [[ $touched != true ]]; then
        record PASS "cleanup.$name" \
            "skipped=no-daemon-capable-command isolation=$isolation"
        return 0
    fi

    output="$event_dir/cleanup-$name.log"
    if [[ -e $output ]]; then
        record FAIL "cleanup.$name" "refusing to overwrite evidence: $output"
        return 1
    fi

    debug "cleanup.$name: cwd=$client output=$output isolation=$isolation"
    if (
        printf 'client=%s\nisolation=%s\naction=buck2-kill\n' "$name" "$isolation"
        cd "$client"
        "$timeout_bin" --signal=TERM --kill-after=5s 30s \
            "$buck_path" --isolation-dir "$isolation" kill
    ) >"$output" 2>&1; then
        status=0
    else
        status=$?
    fi
    if ! printf 'exit_status=%s\n' "$status" >>"$output"; then
        record FAIL "cleanup.$name" "cannot append outcome to $output"
        return 1
    fi
    if ((status != 0)); then
        record FAIL "cleanup.$name" \
            "command exited $status; isolation=$isolation evidence=$output"
        return 1
    fi
    record PASS "cleanup.$name" "isolation=$isolation evidence=$output"
}

cleanup_daemons() {
    local failed=false

    if ! cleanup_client client-a; then
        failed=true
    fi
    if ! cleanup_client client-b; then
        failed=true
    fi
    [[ $failed == false ]]
}

on_exit() {
    local original_status=$?
    local status=$original_status

    trap - EXIT
    trap ':' HUP INT TERM
    if ! cleanup_daemons; then
        if ((status == 0)); then
            status=1
        fi
    fi
    if ! check_config_unchanged; then
        if ((status == 0)); then
            status=1
        fi
    elif ((original_status == 0)); then
        record PASS config.unchanged '.buckconfig.local unchanged on both clients'
    fi
    if ((status == 0)); then
        record PASS smoke-test "stage=$stage"
    elif ((original_status == 0)); then
        record FAIL smoke-test 'post-run validation failed'
    fi
    exit "$status"
}

on_signal() {
    local name=$1
    local status=$2

    if [[ -n $active_command_pid ]]; then
        kill -TERM "$active_command_pid" 2>/dev/null || true
        wait "$active_command_pid" 2>/dev/null || true
        active_command_pid=''
    fi
    record WARN smoke-test.interrupted "signal=$name"
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
    (
        cd "$cwd"
        exec "$timeout_bin" --signal=TERM --kill-after=30s "${timeout_seconds}s" "$@"
    ) >"$output" 2>&1 &
    active_command_pid=$!
    if wait "$active_command_pid"; then
        active_command_pid=''
        return 0
    else
        local status=$?
        active_command_pid=''
        record FAIL "$check" "command exited $status; see $output"
        exit "$status"
    fi
}

run_buck_capture() {
    local client=$1
    local client_name=$2
    local buck_path=$3
    local isolation=$4
    local check=$5
    local output=$6
    shift 6

    mark_daemon_touched "$client_name"
    run_capture "$client" "$check" "$output" \
        "$buck_path" --isolation-dir "$isolation" "$@"
}

assert_contains() {
    local check=$1
    local path=$2
    local expected=$3
    grep -Fq -- "$expected" "$path" || fail "$check" "missing '$expected' in $path"
}

assert_line() {
    local check=$1
    local path=$2
    local expected=$3
    grep -Fxq -- "$expected" "$path" || fail "$check" "missing exact line '$expected' in $path"
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
        --config 'buckos.remote_x86_64_properties=platform.OSFamily=linux,platform.arch=x86_64'
        --config 'buckos.remote_aarch64_properties=platform.OSFamily=linux,platform.arch=aarch64'
        --config buckos.remote_x86_64_use_case=buck2-default
        --config buckos.remote_aarch64_use_case=buck2-default
        --config "buck2_re_client.engine_address=$endpoint"
        --config "buck2_re_client.action_cache_address=$endpoint"
        --config "buck2_re_client.cas_address=$endpoint"
        --config "buck2_re_client.instance_name=$instance_name"
        --config "buck2_re_client.tls=$tls"
    )
    if [[ $tls == true ]]; then
        CONFIG_ARGS+=(
            --config "buck2_re_client.tls_ca_certs=$tls_ca"
            --config "buck2_re_client.tls_client_cert=$buck_tls_client_cert"
        )
    fi
}

audit_config() {
    local client=$1
    local client_name=$2
    local buck_path=$3
    local isolation=$4
    local name=$5
    local remote_execution=$6
    local output="$event_dir/config-$name.log"

    set_config_args "$remote_execution"
    run_buck_capture "$client" "$client_name" "$buck_path" "$isolation" \
        "config.$name" "$output" audit config \
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
        buck2_re_client.tls_ca_certs \
        buck2_re_client.tls_client_cert \
        "${CONFIG_ARGS[@]}"
    assert_contains "config.$name.remote-cache" "$output" 'remote_cache = true'
    assert_contains "config.$name.remote-execution" "$output" "remote_execution = $remote_execution"
    assert_line "config.$name.x86_64-properties" "$output" \
        '    remote_x86_64_properties = platform.OSFamily=linux,platform.arch=x86_64'
    assert_line "config.$name.aarch64-properties" "$output" \
        '    remote_aarch64_properties = platform.OSFamily=linux,platform.arch=aarch64'
    assert_line "config.$name.x86_64-use-case" "$output" \
        '    remote_x86_64_use_case = buck2-default'
    assert_line "config.$name.aarch64-use-case" "$output" \
        '    remote_aarch64_use_case = buck2-default'
    assert_contains "config.$name.engine" "$output" "engine_address = $endpoint"
    assert_contains "config.$name.action-cache" "$output" "action_cache_address = $endpoint"
    assert_contains "config.$name.cas" "$output" "cas_address = $endpoint"
    assert_contains "config.$name.instance" "$output" "instance_name = $instance_name"
    assert_contains "config.$name.tls" "$output" "tls = $tls"
    if [[ $tls == true ]]; then
        assert_contains "config.$name.tls-ca" "$output" \
            "tls_ca_certs = $tls_ca"
        assert_contains "config.$name.tls-client" "$output" \
            "tls_client_cert = $buck_tls_client_cert"
    fi
    record PASS "config.$name" "validated without editing .buckconfig.local"
}

audit_execution_platform() {
    local architecture=$1
    local target=$2
    local prefix=$3
    local expected_platform=$4

    run_buck_capture \
        "$client_a" client-a "$client_a_buck" "$client_a_isolation" \
        "$prefix.platform" "$event_dir/$prefix-platform.log" \
        audit execution-platform-resolution "$target" "${CONFIG_ARGS[@]}"
    assert_contains "$prefix.platform" "$event_dir/$prefix-platform.log" \
        "Execution platform: $expected_platform"
    if [[ $architecture == aarch64 ]]; then
        assert_contains "$prefix.x86-rejected" "$event_dir/$prefix-platform.log" \
            'Skipped buckos//platforms:platforms-remote-x86_64'
        assert_contains "$prefix.constraint" "$event_dir/$prefix-platform.log" \
            'can-execute-aarch64'
    fi
    record PASS "$prefix.platform" "selected=$expected_platform"
}

run_architecture_probe() {
    local architecture=$1
    local target=$2
    local expected_platform=$3
    local prefix="probe-$architecture"
    local event_log="$event_dir/$prefix.json-lines.gz"
    local output="$event_dir/$prefix-output.txt"
    local local_actions remote_actions cached_actions action_digest

    audit_execution_platform "$architecture" "$target" "$prefix" "$expected_platform"

    reserve_output "$event_log"
    reserve_output "$output"
    run_buck_capture \
        "$client_a" client-a "$client_a_buck" "$client_a_isolation" \
        "$prefix.build" "$event_dir/$prefix-command.log" \
        build "$target" --remote-only --no-remote-cache --out "$output" \
        --event-log "$event_log" "${CONFIG_ARGS[@]}"
    [[ -s $event_log ]] || fail "$prefix.event-log" "missing or empty event log: $event_log"
    [[ -f $output ]] || fail "$prefix.output" "missing architecture output: $output"
    if ! printf '%s\n' "$architecture" | cmp -s - "$output"; then
        fail "$prefix.output" "expected exact architecture '$architecture'; see $output"
    fi
    record PASS "$prefix.output" "architecture=$architecture evidence=$output"

    collect_log_evidence \
        "$client_a" client-a "$client_a_buck" "$client_a_isolation" \
        "$event_log" "$prefix" genrule
    summary_count "$event_dir/$prefix-summary.log" 'Local actions'
    local_actions=$parsed_count
    summary_count "$event_dir/$prefix-summary.log" 'Remote actions'
    remote_actions=$parsed_count
    summary_count "$event_dir/$prefix-summary.log" 'Cached actions'
    cached_actions=$parsed_count
    require_equal "$prefix.local-fallback" "$local_actions" 0
    require_equal "$prefix.cache-hit" "$cached_actions" 0
    require_equal "$prefix.remote-actions" "$remote_actions" 1
    assert_contains "$prefix.action" "$event_dir/$prefix-what-ran.jsonl" "${target##*:}"
    assert_contains "$prefix.executor" "$event_dir/$prefix-what-ran.jsonl" '"executor":"RE"'
    assert_not_contains_regex "$prefix.no-local-record" \
        "$event_dir/$prefix-what-ran.jsonl" '"executor":"Local"'
    assert_not_contains_regex "$prefix.no-cache-record" \
        "$event_dir/$prefix-what-ran.jsonl" '"executor":"Cache"'

    action_digest=$(grep -Eo '[[:xdigit:]]{64}:[0-9]+' "$event_dir/$prefix-what-ran.jsonl" | head -n 1 || true)
    [[ -n $action_digest ]] || fail "$prefix.action-digest" "remote architecture probe action digest is absent"
    record PASS "$prefix.action-digest" "digest=$action_digest"
    record PASS "$prefix" "native architecture attested through uncached remote execution"
}

collect_log_evidence() {
    local client=$1
    local client_name=$2
    local buck_path=$3
    local isolation=$4
    local event_log=$5
    local prefix=$6
    local category=$7

    run_buck_capture "$client" "$client_name" "$buck_path" "$isolation" \
        "$prefix.summary" "$event_dir/$prefix-summary.log" \
        log summary "$event_log"
    run_buck_capture "$client" "$client_name" "$buck_path" "$isolation" \
        "$prefix.uploads" "$event_dir/$prefix-uploads.log" \
        log what-uploaded --format json "$event_log"
    run_buck_capture "$client" "$client_name" "$buck_path" "$isolation" \
        "$prefix.what-ran" "$event_dir/$prefix-what-ran.jsonl" \
        log what-ran --format json --emit-cache-queries \
        --filter-category "$category" "$event_log"
}

run_readiness() {
    local helper grpcio_version python_path
    local -a helper_command tls_arguments

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

    tls_arguments=(--tls "$tls")
    if [[ $tls == true ]]; then
        tls_arguments+=(
            --tls-ca "$tls_ca"
            --tls-client-chain "$tls_client_chain"
            --tls-client-key "$tls_client_key"
        )
    fi

    run_capture "$PWD" readiness.capabilities "$event_dir/readiness-capabilities.log" \
        "${helper_command[@]}" capabilities --endpoint "$endpoint" \
        --instance-name "$instance_name" "${tls_arguments[@]}"
    record PASS readiness.capabilities "endpoint=$endpoint instance=$instance_name"

    run_capture "$PWD" readiness.cas "$event_dir/readiness-cas.log" \
        "${helper_command[@]}" cas-round-trip --endpoint "$endpoint" \
        --instance-name "$instance_name" "${tls_arguments[@]}"
    record PASS readiness.cas "digest-verified round trip completed"
}

run_cache() {
    local event_a="$event_dir/cache-client-a.json-lines.gz"
    local event_b="$event_dir/cache-client-b.json-lines.gz"
    local local_a remote_a cached_a uploads_a remote_b cached_b

    audit_config \
        "$client_a" client-a "$client_a_buck" "$client_a_isolation" \
        cache-client-a false
    audit_config \
        "$client_b" client-b "$client_b_buck" "$client_b_isolation" \
        cache-client-b false
    set_config_args false

    reserve_output "$event_a"
    run_buck_capture \
        "$client_a" client-a "$client_a_buck" "$client_a_isolation" \
        cache.client-a.build "$event_dir/cache-client-a-command.log" \
        build "$CACHE_TARGET" --event-log "$event_a" "${CONFIG_ARGS[@]}"
    [[ -s $event_a ]] || fail cache.client-a.event-log "missing or empty event log: $event_a"
    collect_log_evidence \
        "$client_a" client-a "$client_a_buck" "$client_a_isolation" \
        "$event_a" cache-client-a dsc_unpack

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
    run_buck_capture \
        "$client_b" client-b "$client_b_buck" "$client_b_isolation" \
        cache.client-b.build "$event_dir/cache-client-b-command.log" \
        build "$CACHE_TARGET" --event-log "$event_b" "${CONFIG_ARGS[@]}"
    [[ -s $event_b ]] || fail cache.client-b.event-log "missing or empty event log: $event_b"
    collect_log_evidence \
        "$client_b" client-b "$client_b_buck" "$client_b_isolation" \
        "$event_b" cache-client-b dsc_unpack

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
    local target probe_target expected_platform event_log prefix
    local local_actions remote_actions cached_actions action_digest

    case "$architecture" in
        x86_64)
            target=$X86_TARGET
            probe_target=$X86_PROBE_TARGET
            expected_platform='buckos//platforms:platforms-remote-x86_64'
            prefix=re-x86_64
            ;;
        aarch64)
            target=$AARCH64_TARGET
            probe_target=$AARCH64_PROBE_TARGET
            expected_platform='buckos//platforms:platforms-remote-aarch64'
            prefix=re-aarch64
            ;;
        *)
            fail internal "unsupported architecture: $architecture"
            ;;
    esac
    event_log="$event_dir/$prefix.json-lines.gz"

    audit_config \
        "$client_a" client-a "$client_a_buck" "$client_a_isolation" \
        "$prefix" true
    set_config_args true
    run_architecture_probe "$architecture" "$probe_target" "$expected_platform"
    audit_execution_platform "$architecture" "$target" "$prefix" "$expected_platform"

    reserve_output "$event_log"
    run_buck_capture \
        "$client_a" client-a "$client_a_buck" "$client_a_isolation" \
        "$prefix.build" "$event_dir/$prefix-command.log" \
        build "$target" --remote-only --no-remote-cache \
        --event-log "$event_log" "${CONFIG_ARGS[@]}"
    [[ -s $event_log ]] || fail "$prefix.event-log" "missing or empty event log: $event_log"
    collect_log_evidence \
        "$client_a" client-a "$client_a_buck" "$client_a_isolation" \
        "$event_log" "$prefix" deb_build

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
    local client client_name buck_path isolation name event_log
    local local_actions remote_actions

    validate_host_options
    set_config_args true
    CONFIG_ARGS+=(--config "$host_buildroot_config")

    for name in a b; do
        if [[ $name == a ]]; then
            client=$client_a
            client_name='client-a'
            buck_path=$client_a_buck
            isolation=$client_a_isolation
        else
            client=$client_b
            client_name='client-b'
            buck_path=$client_b_buck
            isolation=$client_b_isolation
        fi
        event_log="$event_dir/host-client-$name.json-lines.gz"
        reserve_output "$event_log"
        run_buck_capture \
            "$client" "$client_name" "$buck_path" "$isolation" \
            "host.client-$name.build" "$event_dir/host-client-$name-command.log" \
            build "$host_target" --event-log "$event_log" "${CONFIG_ARGS[@]}"
        [[ -s $event_log ]] || fail "host.client-$name.event-log" "missing or empty event log: $event_log"
        collect_log_evidence \
            "$client" "$client_name" "$buck_path" "$isolation" \
            "$event_log" "host-client-$name" "$host_category"
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

validate_isolation_name() {
    local name=$1
    local option=$2

    [[ $name =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || \
        usage_error "$option must contain only letters, digits, dot, underscore, or hyphen"
}

configure_isolation_names() {
    local seed

    seed=$(printf '%s' "$event_dir" | sha256sum | awk '{print substr($1, 1, 12)}')
    if [[ -z $client_a_isolation ]]; then
        client_a_isolation="re-smoke-$seed-a"
    fi
    if [[ -z $client_b_isolation ]]; then
        client_b_isolation="re-smoke-$seed-b"
    fi
    validate_isolation_name "$client_a_isolation" --client-a-isolation
    validate_isolation_name "$client_b_isolation" --client-b-isolation
    [[ $client_a_isolation != "$client_b_isolation" ]] || \
        usage_error 'client isolation directories must be distinct'
    record PASS lifecycle.isolation \
        "client-a=$client_a_isolation client-b=$client_b_isolation"
}

validate_arguments() {
    local required_tool

    for required_tool in realpath sha256sum awk grep sed tail head timeout cmp; do
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
    if [[ $cross_host == true && $tls != true ]]; then
        usage_error '--cross-host requires --tls true'
    fi
    if [[ $tls == true ]]; then
        canonical_readable_file "$tls_ca" --tls-ca
        tls_ca=$resolved_path
        canonical_readable_file "$tls_client_chain" --tls-client-chain
        tls_client_chain=$resolved_path
        canonical_readable_file "$tls_client_key" --tls-client-key
        tls_client_key=$resolved_path
        canonical_readable_file "$buck_tls_client_cert" --buck-tls-client-cert
        buck_tls_client_cert=$resolved_path
    elif [[ -n $tls_ca$tls_client_chain$tls_client_key$buck_tls_client_cert ]]; then
        usage_error 'TLS credential options require --tls true'
    fi

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
    configure_isolation_names

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
    trap 'on_signal HUP 129' HUP
    trap 'on_signal INT 130' INT
    trap 'on_signal TERM 143' TERM

    local version_a version_b
    if ! version_a=$(
        cd "$client_a" &&
            "$timeout_bin" --signal=TERM --kill-after=5s 30s \
                "$client_a_buck" --isolation-dir "$client_a_isolation" --version 2>&1
    ); then
        fail prerequisites.buck-a "Buck2 is unavailable in $client_a"
    fi
    if ! version_b=$(
        cd "$client_b" &&
            "$timeout_bin" --signal=TERM --kill-after=5s 30s \
                "$client_b_buck" --isolation-dir "$client_b_isolation" --version 2>&1
    ); then
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

}

main
