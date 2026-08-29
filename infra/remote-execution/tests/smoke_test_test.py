#!/usr/bin/env python3
"""Focused tests for the bounded Buck remote-execution smoke harness."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import time
import unittest


TEST_ROOT = Path(__file__).resolve().parent
PACKAGED_SMOKE_TEST = TEST_ROOT / "smoke-test.sh"
if PACKAGED_SMOKE_TEST.is_file():
    REPOSITORY_ROOT = TEST_ROOT
    SMOKE_TEST = PACKAGED_SMOKE_TEST
else:
    REPOSITORY_ROOT = TEST_ROOT.parents[2]
    SMOKE_TEST = REPOSITORY_ROOT / "infra/remote-execution/scripts/smoke-test.sh"


FAKE_BUCK = r"""#!/usr/bin/env bash
set -euo pipefail

printf '%s|%s\n' "$(basename "$PWD")" "$*" >>"${FAKE_CALL_LOG:?}"

if [[ ${1:-} != --isolation-dir || -z ${2:-} ]]; then
    echo 'missing leading --isolation-dir' >&2
    exit 91
fi
isolation=$2
shift 2

if [[ ${1:-} == --version ]]; then
    echo 'buck2 test-version'
    exit 0
fi

if [[ ${1:-} == kill ]]; then
    echo "cleanup isolation=$isolation"
    if [[ ${FAKE_SCENARIO:-} == *cleanup_failure* ]]; then
        echo 'simulated cleanup failure' >&2
        exit 77
    fi
    exit 0
fi

if [[ ${1:-} == audit && ${2:-} == config ]]; then
    remote_execution=''
    remote_x86_64_properties=''
    remote_aarch64_properties=''
    remote_x86_64_use_case=''
    remote_aarch64_use_case=''
    endpoint=''
    instance=''
    tls=''
    tls_ca_certs=''
    tls_client_cert=''
    while (($#)); do
        if [[ $1 == --config ]]; then
            case $2 in
                buckos.remote_execution=*) remote_execution=${2#*=} ;;
                buckos.remote_x86_64_properties=*) remote_x86_64_properties=${2#*=} ;;
                buckos.remote_aarch64_properties=*) remote_aarch64_properties=${2#*=} ;;
                buckos.remote_x86_64_use_case=*) remote_x86_64_use_case=${2#*=} ;;
                buckos.remote_aarch64_use_case=*) remote_aarch64_use_case=${2#*=} ;;
                buck2_re_client.engine_address=*) endpoint=${2#*=} ;;
                buck2_re_client.instance_name=*) instance=${2#*=} ;;
                buck2_re_client.tls=*) tls=${2#*=} ;;
                buck2_re_client.tls_ca_certs=*) tls_ca_certs=${2#*=} ;;
                buck2_re_client.tls_client_cert=*) tls_client_cert=${2#*=} ;;
            esac
            shift 2
        else
            shift
        fi
    done
    cat <<EOF
[buck2_re_client]
    action_cache_address = $endpoint
    cas_address = $endpoint
    engine_address = $endpoint
    instance_name = $instance
    tls = $tls
    tls_ca_certs = $tls_ca_certs
    tls_client_cert = $tls_client_cert
[buckos]
    remote_cache = true
    remote_execution = $remote_execution
    remote_aarch64_properties = $remote_aarch64_properties
    remote_aarch64_use_case = $remote_aarch64_use_case
    remote_x86_64_properties = $remote_x86_64_properties
    remote_x86_64_use_case = $remote_x86_64_use_case
EOF
    exit 0
fi

if [[ ${1:-} == audit && ${2:-} == execution-platform-resolution ]]; then
    target=${3:?}
    if [[ $target == *aarch64* ]]; then
        if [[ ${FAKE_SCENARIO:-} == wrong_aarch64_platform ]]; then
            echo '  Execution platform: buckos//platforms:platforms-remote-x86_64'
        else
            cat <<'EOF'
  Execution platform: buckos//platforms:platforms-remote-aarch64
    Skipped buckos//platforms:platforms-remote-x86_64
      exec_compatible_with requires `buckos//platforms:can-execute-aarch64` but it was not satisfied
EOF
        fi
    else
        echo '  Execution platform: buckos//platforms:platforms-remote-x86_64'
    fi
    exit 0
fi

if [[ ${1:-} == build ]]; then
    if [[ ${FAKE_SCENARIO:-} == *command_failure* ]]; then
        echo 'simulated command failure' >&2
        exit 42
    fi
    if [[ ${FAKE_SCENARIO:-} == signal_wait ]]; then
        : >"${FAKE_BLOCK_MARKER:?}"
        trap 'exit 143' TERM
        while true; do
            sleep 1
        done
    fi
    event_log=''
    output=''
    target=${2:?}
    while (($#)); do
        case $1 in
            --event-log)
                event_log=$2
                shift 2
                ;;
            --out)
                output=$2
                shift 2
                ;;
            *)
                shift
                ;;
        esac
    done
    [[ -n $event_log ]]
    printf 'fake event log\n' >"$event_log"
    if [[ -n $output ]]; then
        architecture=x86_64
        [[ $target == *aarch64* ]] && architecture=aarch64
        [[ ${FAKE_SCENARIO:-} == wrong_probe_output ]] && architecture=x86_64
        printf '%s\n' "$architecture" >"$output"
    fi
    exit 0
fi

if [[ ${1:-} == log && ${2:-} == summary ]]; then
    event_log=${!#}
    local_actions=0
    remote_actions=0
    cached_actions=0
    case $(basename "$event_log") in
        cache-client-a.json-lines.gz)
            local_actions=1
            ;;
        cache-client-b.json-lines.gz)
            if [[ ${FAKE_SCENARIO:-} == no_cache_hit ]]; then
                local_actions=1
            else
                cached_actions=1
            fi
            ;;
        probe-x86_64.json-lines.gz|probe-aarch64.json-lines.gz|re-x86_64.json-lines.gz|re-aarch64.json-lines.gz)
            if [[ ${FAKE_SCENARIO:-} == probe_local_fallback && $(basename "$event_log") == probe-* ]]; then
                local_actions=1
            elif [[ ${FAKE_SCENARIO:-} == probe_cache_hit && $(basename "$event_log") == probe-* ]]; then
                cached_actions=1
            else
                remote_actions=1
            fi
            ;;
        host-client-a.json-lines.gz|host-client-b.json-lines.gz)
            local_actions=1
            ;;
    esac
    cat <<EOF
Actions
- Local actions: $local_actions
- Remote actions: $remote_actions
- Cached actions: $cached_actions
Network
- Total uploaded: 0B
- Total downloaded: 0B
- RE downloaded: 0B
EOF
    exit 0
fi

if [[ ${1:-} == log && ${2:-} == what-uploaded ]]; then
    event_log=${!#}
    if [[ $(basename "$event_log") == cache-client-a.json-lines.gz ]]; then
        echo 'total: digests: 2, bytes: 128'
    else
        echo 'total: digests: 0, bytes: 0'
    fi
    exit 0
fi

if [[ ${1:-} == log && ${2:-} == what-ran ]]; then
    event_log=${!#}
    case $(basename "$event_log") in
        cache-client-a.json-lines.gz)
            echo '{"identity":"buckos//flavors/debian:hostname-13-x86_64-source (dsc_unpack)","reproducer":{"executor":"Local"}}'
            ;;
        cache-client-b.json-lines.gz)
            if [[ ${FAKE_SCENARIO:-} == no_cache_hit ]]; then
                echo '{"identity":"buckos//flavors/debian:hostname-13-x86_64-source (dsc_unpack)","reproducer":{"executor":"Local"}}'
            else
                echo '{"identity":"buckos//flavors/debian:hostname-13-x86_64-source (dsc_unpack)","reproducer":{"executor":"Cache"}}'
            fi
            ;;
        re-x86_64.json-lines.gz)
            echo '{"identity":"buckos//flavors/debian:hostname-13-x86_64-build (deb_build)","reproducer":{"executor":"RE","details":{"action_digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:42"}}}'
            ;;
        re-aarch64.json-lines.gz)
            echo '{"identity":"buckos//flavors/debian:hostname-13-aarch64-build (deb_build)","reproducer":{"executor":"RE","details":{"action_digest":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb:84"}}}'
            ;;
        probe-x86_64.json-lines.gz)
            echo '{"identity":"buckos//infra/remote-execution:worker-architecture-x86_64 (genrule)","reproducer":{"executor":"RE","details":{"action_digest":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc:21"}}}'
            ;;
        probe-aarch64.json-lines.gz)
            echo '{"identity":"buckos//infra/remote-execution:worker-architecture-aarch64 (genrule)","reproducer":{"executor":"RE","details":{"action_digest":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd:21"}}}'
            ;;
        host-client-a.json-lines.gz|host-client-b.json-lines.gz)
            echo '{"identity":"buckos//tests:hello-build (srpm_build)","reproducer":{"executor":"Local"}}'
            ;;
    esac
    exit 0
fi

echo "unexpected fake Buck2 invocation: $*" >&2
exit 90
"""


FAKE_GRPC_HELPER = r"""#!/usr/bin/env bash
set -euo pipefail

printf '%s\n' "$*" >>"${FAKE_HELPER_CALL_LOG:?}"
case ${1:-} in
    capabilities)
        echo 'capabilities ok'
        ;;
    cas-round-trip)
        echo 'cas round trip ok'
        ;;
    *)
        exit 90
        ;;
esac
"""


FAKE_PYTHON = r"""#!/usr/bin/env bash
set -euo pipefail

printf '%s\n' "$*" >>"${FAKE_PYTHON_CALL_LOG:?}"
if [[ ${1:-} == -I && ${2:-} == -c ]]; then
    if [[ ${FAKE_SCENARIO:-} == missing_grpc ]]; then
        echo 'No module named grpc' >&2
        exit 7
    fi
    echo '1.51.1'
    exit 0
fi

[[ ${1:-} == -I ]]
helper=${2:?}
operation=${3:?}
[[ $(basename "$helper") == reapi_readiness.py ]]
case $operation in
    capabilities)
        echo 'PASS reapi.capabilities'
        ;;
    cas-round-trip)
        echo 'PASS reapi.cas-round-trip'
        ;;
    *)
        exit 90
        ;;
esac
"""


class SmokeTestScriptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.client_a = self.root / "client-a"
        self.client_b = self.root / "client-b"
        self.event_dir = self.root / "events"
        self.call_log = self.root / "buck-calls.log"
        self.helper_call_log = self.root / "helper-calls.log"
        self.python_call_log = self.root / "python-calls.log"
        self.block_marker = self.root / "buck-blocked"
        self.grpc_helper = self.root / "reapi-helper"
        self.grpc_python = self.root / "python3"
        self.tls_ca = self.root / "ca.pem"
        self.tls_client_chain = self.root / "client-chain.pem"
        self.tls_client_key = self.root / "client-key.pem"
        self.buck_tls_client_cert = self.root / "buck-client.pem"

        for client in (self.client_a, self.client_b):
            client.mkdir()
            (client / ".buckconfig").write_text("[build]\n", encoding="utf-8")
            (client / ".buckconfig.local").write_text(
                "[sentinel]\n  unchanged = true\n", encoding="utf-8"
            )
            buck = client / "buck2"
            buck.write_text(FAKE_BUCK, encoding="utf-8")
            buck.chmod(0o755)

        self.grpc_helper.write_text(FAKE_GRPC_HELPER, encoding="utf-8")
        self.grpc_helper.chmod(0o755)
        self.grpc_python.write_text(FAKE_PYTHON, encoding="utf-8")
        self.grpc_python.chmod(0o755)
        self.tls_ca.write_text("test ca\n", encoding="utf-8")
        self.tls_client_chain.write_text("test chain\n", encoding="utf-8")
        self.tls_client_key.write_text("test key\n", encoding="utf-8")
        self.buck_tls_client_cert.write_text(
            "test combined identity\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def run_smoke(
        self,
        stage: str,
        *extra: str,
        scenario: str = "",
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["FAKE_CALL_LOG"] = str(self.call_log)
        environment["FAKE_HELPER_CALL_LOG"] = str(self.helper_call_log)
        environment["FAKE_PYTHON_CALL_LOG"] = str(self.python_call_log)
        environment["FAKE_SCENARIO"] = scenario
        environment["FAKE_BLOCK_MARKER"] = str(self.block_marker)
        command = [
            "bash",
            str(SMOKE_TEST),
            "--stage",
            stage,
            "--client-a",
            str(self.client_a),
            "--client-b",
            str(self.client_b),
            "--endpoint",
            "re.test.invalid:50051",
            "--instance-name",
            "main",
            "--tls",
            "false",
            "--event-dir",
            str(self.event_dir),
            "--timeout-seconds",
            "5",
            *extra,
        ]
        return subprocess.run(
            command,
            check=False,
            cwd=REPOSITORY_ROOT,
            env=environment,
            text=True,
            capture_output=True,
        )

    def buck_calls(self) -> list[tuple[str, list[str]]]:
        calls = []
        for line in self.call_log.read_text(encoding="utf-8").splitlines():
            client, arguments = line.split("|", 1)
            calls.append((client, arguments.split()))
        return calls

    def assert_success(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(
            result.returncode,
            0,
            textwrap.dedent(
                f"""\
                stdout:
                {result.stdout}
                stderr:
                {result.stderr}
                """
            ),
        )

    def tls_arguments(self) -> tuple[str, ...]:
        return (
            "--tls", "true",
            "--tls-ca", str(self.tls_ca),
            "--tls-client-chain", str(self.tls_client_chain),
            "--tls-client-key", str(self.tls_client_key),
            "--buck-tls-client-cert", str(self.buck_tls_client_cert),
            "--cross-host",
        )

    def test_readiness_uses_configured_helper(self) -> None:
        result = self.run_smoke(
            "readiness", "--grpc-helper", str(self.grpc_helper)
        )

        self.assert_success(result)
        self.assertIn("PASS readiness.capabilities", result.stdout)
        self.assertIn("PASS readiness.cas", result.stdout)
        self.assertIn(
            "PASS cleanup.client-a skipped=no-daemon-capable-command",
            result.stdout,
        )
        self.assertIn(
            "PASS cleanup.client-b skipped=no-daemon-capable-command",
            result.stdout,
        )
        calls = self.helper_call_log.read_text(encoding="utf-8")
        self.assertIn(
            "capabilities --endpoint re.test.invalid:50051 --instance-name main --tls false",
            calls,
        )
        self.assertIn(
            "cas-round-trip --endpoint re.test.invalid:50051 --instance-name main --tls false",
            calls,
        )
        self.assertFalse(self.python_call_log.exists())

    def test_readiness_uses_explicit_python_for_default_helper(self) -> None:
        result = self.run_smoke(
            "readiness", "--grpc-python", str(self.grpc_python)
        )

        self.assert_success(result)
        self.assertIn(
            "PASS readiness.python interpreter={} grpcio=1.51.1".format(
                self.grpc_python
            ),
            result.stdout,
        )
        calls = self.python_call_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(3, len(calls))
        self.assertTrue(calls[0].startswith("-I -c import grpc, sys;"))
        self.assertIn("-I ", calls[1])
        self.assertIn("reapi_readiness.py capabilities --endpoint", calls[1])
        self.assertIn("-I ", calls[2])
        self.assertIn("reapi_readiness.py cas-round-trip --endpoint", calls[2])

    def test_tls_credentials_reach_helper_and_buck(self) -> None:
        result = self.run_smoke(
            "all",
            *self.tls_arguments(),
            "--grpc-helper", str(self.grpc_helper),
        )

        self.assert_success(result)
        helper_calls = self.helper_call_log.read_text(encoding="utf-8")
        self.assertIn("--tls true", helper_calls)
        self.assertIn("--tls-ca {}".format(self.tls_ca), helper_calls)
        self.assertIn(
            "--tls-client-chain {}".format(self.tls_client_chain), helper_calls
        )
        self.assertIn(
            "--tls-client-key {}".format(self.tls_client_key), helper_calls
        )
        buck_calls = self.call_log.read_text(encoding="utf-8")
        self.assertIn(
            "--config buck2_re_client.tls_ca_certs={}".format(self.tls_ca),
            buck_calls,
        )
        self.assertIn(
            "--config buck2_re_client.tls_client_cert={}".format(
                self.buck_tls_client_cert
            ),
            buck_calls,
        )
        self.assertNotIn("test key", result.stdout + result.stderr)

    def test_tls_rejects_incomplete_credential_set(self) -> None:
        result = self.run_smoke(
            "readiness",
            "--tls", "true",
            "--tls-ca", str(self.tls_ca),
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("--tls-client-chain is required", result.stdout)
        self.assertFalse(self.helper_call_log.exists())

    def test_plaintext_rejects_tls_credentials(self) -> None:
        result = self.run_smoke(
            "readiness",
            "--tls-ca", str(self.tls_ca),
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("TLS credential options require --tls true", result.stdout)
        self.assertFalse(self.helper_call_log.exists())

    def test_cross_host_rejects_plaintext(self) -> None:
        result = self.run_smoke("readiness", "--cross-host")

        self.assertEqual(2, result.returncode)
        self.assertIn("--cross-host requires --tls true", result.stdout)
        self.assertFalse(self.helper_call_log.exists())

    def test_missing_grpc_fails_before_default_helper_operation(self) -> None:
        result = self.run_smoke(
            "readiness",
            "--grpc-python",
            str(self.grpc_python),
            scenario="missing_grpc",
        )

        self.assertEqual(7, result.returncode)
        self.assertIn("FAIL readiness.python command exited 7", result.stdout)
        calls = self.python_call_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(1, len(calls))
        self.assertTrue(calls[0].startswith("-I -c import grpc, sys;"))

    def test_readiness_fails_without_helper(self) -> None:
        result = self.run_smoke(
            "readiness", "--grpc-helper", str(self.root / "missing-helper")
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("FAIL arguments grpc-helper cannot be resolved", result.stdout)

    def test_all_runs_only_bounded_stages(self) -> None:
        result = self.run_smoke(
            "all", "--grpc-helper", str(self.grpc_helper)
        )

        self.assert_success(result)
        self.assertIn("PASS readiness.cas", result.stdout)
        self.assertIn("PASS cache.client-b", result.stdout)
        self.assertIn("PASS re-x86_64 ", result.stdout)
        self.assertIn("PASS re-aarch64 ", result.stdout)
        self.assertIn("PASS probe-x86_64 ", result.stdout)
        self.assertIn("PASS probe-aarch64 ", result.stdout)
        self.assertIn("WARN host-provenance.skipped", result.stdout)
        calls = self.call_log.read_text(encoding="utf-8")
        self.assertNotIn(":iso-", calls)

    def test_cache_stage_proves_upload_and_clean_client_hit(self) -> None:
        before = (self.client_a / ".buckconfig.local").read_bytes()

        result = self.run_smoke("cache")

        self.assert_success(result)
        self.assertIn("PASS cache.client-a.upload value=2", result.stdout)
        self.assertIn("PASS cache.client-b.cache-hit value=1", result.stdout)
        self.assertIn("PASS cache.no-execute", result.stdout)
        self.assertEqual(
            (self.client_a / ".buckconfig.local").read_bytes(), before
        )
        calls = self.call_log.read_text(encoding="utf-8")
        self.assertIn(
            "build //flavors/debian:hostname-13-x86_64-source", calls
        )
        self.assertIn("--config buckos.remote_execution=false", calls)
        self.assertNotIn(":iso-", calls)

    def test_every_buck_call_uses_distinct_derived_isolation(self) -> None:
        result = self.run_smoke("cache")

        self.assert_success(result)
        calls = self.buck_calls()
        isolations: dict[str, set[str]] = {"client-a": set(), "client-b": set()}
        for client, arguments in calls:
            self.assertGreaterEqual(len(arguments), 3)
            self.assertEqual("--isolation-dir", arguments[0])
            isolations[client].add(arguments[1])
        self.assertEqual(1, len(isolations["client-a"]))
        self.assertEqual(1, len(isolations["client-b"]))
        self.assertNotEqual(isolations["client-a"], isolations["client-b"])
        self.assertIn("PASS lifecycle.isolation", result.stdout)
        self.assertIn("PASS cleanup.client-a", result.stdout)
        self.assertIn("PASS cleanup.client-b", result.stdout)
        for client in ("client-a", "client-b"):
            evidence = self.event_dir / f"cleanup-{client}.log"
            self.assertIn("exit_status=0", evidence.read_text(encoding="utf-8"))

    def test_accepts_explicit_distinct_isolation_names(self) -> None:
        result = self.run_smoke(
            "cache",
            "--client-a-isolation", "operator-a",
            "--client-b-isolation", "operator-b",
        )

        self.assert_success(result)
        self.assertIn(
            "PASS lifecycle.isolation client-a=operator-a client-b=operator-b",
            result.stdout,
        )
        for client, arguments in self.buck_calls():
            expected = "operator-a" if client == "client-a" else "operator-b"
            self.assertEqual(["--isolation-dir", expected], arguments[:2])

    def test_rejects_identical_isolation_names(self) -> None:
        result = self.run_smoke(
            "cache",
            "--client-a-isolation", "same",
            "--client-b-isolation", "same",
        )

        self.assertEqual(2, result.returncode)
        self.assertIn("client isolation directories must be distinct", result.stdout)

    def test_command_failure_preserves_failure_and_cleans_both_clients(self) -> None:
        result = self.run_smoke("cache", scenario="command_failure")

        self.assertEqual(42, result.returncode)
        self.assertIn("FAIL cache.client-a.build command exited 42", result.stdout)
        self.assertIn("PASS cleanup.client-a", result.stdout)
        self.assertIn("PASS cleanup.client-b", result.stdout)
        kill_calls = [args for _client, args in self.buck_calls() if args[2:] == ["kill"]]
        self.assertEqual(2, len(kill_calls))

    def test_cleanup_failure_changes_success_to_failure(self) -> None:
        result = self.run_smoke("cache", scenario="cleanup_failure")

        self.assertEqual(1, result.returncode)
        self.assertIn("FAIL cleanup.client-a command exited 77", result.stdout)
        self.assertIn("FAIL cleanup.client-b command exited 77", result.stdout)
        self.assertIn("FAIL smoke-test post-run validation failed", result.stdout)
        self.assertIn(
            "exit_status=77",
            (self.event_dir / "cleanup-client-a.log").read_text(encoding="utf-8"),
        )

    def test_cleanup_failure_does_not_hide_command_failure(self) -> None:
        result = self.run_smoke(
            "cache", scenario="command_failure,cleanup_failure"
        )

        self.assertEqual(42, result.returncode)
        self.assertIn("FAIL cache.client-a.build command exited 42", result.stdout)
        self.assertIn("FAIL cleanup.client-a command exited 77", result.stdout)
        self.assertIn("FAIL cleanup.client-b command exited 77", result.stdout)
        self.assertNotIn("FAIL smoke-test post-run validation failed", result.stdout)

    def test_term_signal_preserves_status_and_runs_cleanup(self) -> None:
        environment = os.environ.copy()
        environment["FAKE_CALL_LOG"] = str(self.call_log)
        environment["FAKE_HELPER_CALL_LOG"] = str(self.helper_call_log)
        environment["FAKE_SCENARIO"] = "signal_wait"
        environment["FAKE_BLOCK_MARKER"] = str(self.block_marker)
        command = [
            "bash",
            str(SMOKE_TEST),
            "--stage", "cache",
            "--client-a", str(self.client_a),
            "--client-b", str(self.client_b),
            "--endpoint", "re.test.invalid:50051",
            "--instance-name", "main",
            "--tls", "false",
            "--event-dir", str(self.event_dir),
            "--timeout-seconds", "30",
        ]
        process = subprocess.Popen(
            command,
            cwd=REPOSITORY_ROOT,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        deadline = time.monotonic() + 5
        while not self.block_marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.block_marker.exists(), "fake Buck did not enter build")
        process.terminate()
        stdout, stderr = process.communicate(timeout=10)

        self.assertEqual(143, process.returncode, stderr)
        self.assertIn("WARN smoke-test.interrupted signal=TERM", stdout)
        self.assertIn("PASS cleanup.client-a", stdout)
        self.assertIn("PASS cleanup.client-b", stdout)
        self.assertIn(
            "exit_status=0",
            (self.event_dir / "cleanup-client-a.log").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "exit_status=0",
            (self.event_dir / "cleanup-client-b.log").read_text(encoding="utf-8"),
        )

    def test_cache_stage_rejects_missing_second_client_hit(self) -> None:
        result = self.run_smoke("cache", scenario="no_cache_hit")

        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL cache.client-b.cache-hit", result.stdout)

    def test_x86_64_stage_forces_remote_execution(self) -> None:
        result = self.run_smoke("x86_64")

        self.assert_success(result)
        self.assertIn("PASS re-x86_64.remote-actions value=1", result.stdout)
        self.assertIn("PASS re-x86_64.local-fallback value=0", result.stdout)
        self.assertIn("PASS re-x86_64.action-digest", result.stdout)
        self.assertIn("PASS probe-x86_64.output architecture=x86_64", result.stdout)
        self.assertIn("PASS probe-x86_64.remote-actions value=1", result.stdout)
        calls = self.call_log.read_text(encoding="utf-8")
        self.assertIn(
            "build //infra/remote-execution:worker-architecture-x86_64 --remote-only --no-remote-cache",
            calls,
        )
        self.assertIn(
            "build //flavors/debian:hostname-13-x86_64-build --remote-only --no-remote-cache",
            calls,
        )
        self.assertLess(
            calls.index("build //infra/remote-execution:worker-architecture-x86_64"),
            calls.index("build //flavors/debian:hostname-13-x86_64-build"),
        )

    def test_aarch64_stage_proves_platform_and_execution(self) -> None:
        result = self.run_smoke("aarch64")

        self.assert_success(result)
        self.assertIn(
            "PASS re-aarch64.platform selected=buckos//platforms:platforms-remote-aarch64",
            result.stdout,
        )
        self.assertIn("PASS re-aarch64.remote-actions value=1", result.stdout)
        self.assertIn("PASS re-aarch64.local-fallback value=0", result.stdout)
        self.assertIn("PASS probe-aarch64.output architecture=aarch64", result.stdout)
        self.assertIn("PASS probe-aarch64.remote-actions value=1", result.stdout)

    def test_aarch64_stage_rejects_wrong_probe_output(self) -> None:
        result = self.run_smoke(
            "aarch64", scenario="wrong_probe_output"
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "FAIL probe-aarch64.output expected exact architecture 'aarch64'",
            result.stdout,
        )
        calls = self.call_log.read_text(encoding="utf-8")
        self.assertNotIn("hostname-13-aarch64-build", calls)

    def test_architecture_probe_rejects_local_fallback(self) -> None:
        result = self.run_smoke(
            "x86_64", scenario="probe_local_fallback"
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL probe-x86_64.local-fallback", result.stdout)
        calls = self.call_log.read_text(encoding="utf-8")
        self.assertNotIn("hostname-13-x86_64-build", calls)

    def test_architecture_probe_rejects_cache_hit(self) -> None:
        result = self.run_smoke(
            "x86_64", scenario="probe_cache_hit"
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL probe-x86_64.cache-hit", result.stdout)
        calls = self.call_log.read_text(encoding="utf-8")
        self.assertNotIn("hostname-13-x86_64-build", calls)

    def test_architecture_stage_rejects_noncanonical_properties(self) -> None:
        fake_buck = self.client_a / "buck2"
        text = fake_buck.read_text(encoding="utf-8")
        fake_buck.write_text(
            text.replace(
                "remote_aarch64_properties = $remote_aarch64_properties",
                "remote_aarch64_properties = $remote_aarch64_properties,queue=arm",
            ),
            encoding="utf-8",
        )

        result = self.run_smoke("aarch64")

        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL config.re-aarch64.aarch64-properties", result.stdout)

    def test_aarch64_stage_rejects_wrong_platform(self) -> None:
        result = self.run_smoke(
            "aarch64", scenario="wrong_aarch64_platform"
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL probe-aarch64.platform", result.stdout)

    def test_host_provenance_runs_locally_on_both_clients(self) -> None:
        result = self.run_smoke(
            "host-provenance",
            "--host-target",
            "//tests:hello-build",
            "--host-category",
            "srpm_build",
            "--host-buildroot-config",
            "buckos.fedora.buildroot=host",
        )

        self.assert_success(result)
        self.assertIn("PASS host.client-a.remote value=0", result.stdout)
        self.assertIn("PASS host.client-b.remote value=0", result.stdout)
        self.assertIn("PASS host-provenance", result.stdout)
        calls = self.call_log.read_text(encoding="utf-8")
        self.assertIn("--config buckos.fedora.buildroot=host", calls)

    def test_host_provenance_rejects_iso_target(self) -> None:
        result = self.run_smoke(
            "host-provenance",
            "--host-target",
            "//flavors/debian:iso-live-13-x86_64",
            "--host-category",
            "iso_build",
            "--host-buildroot-config",
            "buckos.debian.buildroot=host",
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("ISO targets are forbidden", result.stdout)


if __name__ == "__main__":
    unittest.main()
