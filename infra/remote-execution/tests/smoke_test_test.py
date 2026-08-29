#!/usr/bin/env python3
"""Focused tests for the bounded Buck remote-execution smoke harness."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SMOKE_TEST = REPOSITORY_ROOT / "infra/remote-execution/scripts/smoke-test.sh"


FAKE_BUCK = r"""#!/usr/bin/env bash
set -euo pipefail

printf '%s|%s\n' "$(basename "$PWD")" "$*" >>"${FAKE_CALL_LOG:?}"

if [[ ${1:-} == --version ]]; then
    echo 'buck2 test-version'
    exit 0
fi

if [[ ${1:-} == audit && ${2:-} == config ]]; then
    remote_execution=''
    endpoint=''
    instance=''
    tls=''
    while (($#)); do
        if [[ $1 == --config ]]; then
            case $2 in
                buckos.remote_execution=*) remote_execution=${2#*=} ;;
                buck2_re_client.engine_address=*) endpoint=${2#*=} ;;
                buck2_re_client.instance_name=*) instance=${2#*=} ;;
                buck2_re_client.tls=*) tls=${2#*=} ;;
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
[buckos]
    remote_cache = true
    remote_execution = $remote_execution
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
    event_log=''
    while (($#)); do
        if [[ $1 == --event-log ]]; then
            event_log=$2
            break
        fi
        shift
    done
    [[ -n $event_log ]]
    printf 'fake event log\n' >"$event_log"
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
        re-x86_64.json-lines.gz|re-aarch64.json-lines.gz)
            remote_actions=1
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


class SmokeTestScriptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.client_a = self.root / "client-a"
        self.client_b = self.root / "client-b"
        self.event_dir = self.root / "events"
        self.call_log = self.root / "buck-calls.log"
        self.helper_call_log = self.root / "helper-calls.log"
        self.grpc_helper = self.root / "reapi-helper"

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
        environment["FAKE_SCENARIO"] = scenario
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

    def test_readiness_uses_configured_helper(self) -> None:
        result = self.run_smoke(
            "readiness", "--grpc-helper", str(self.grpc_helper)
        )

        self.assert_success(result)
        self.assertIn("PASS readiness.capabilities", result.stdout)
        self.assertIn("PASS readiness.cas", result.stdout)
        calls = self.helper_call_log.read_text(encoding="utf-8")
        self.assertIn(
            "capabilities --endpoint re.test.invalid:50051 --instance-name main --tls false",
            calls,
        )
        self.assertIn(
            "cas-round-trip --endpoint re.test.invalid:50051 --instance-name main --tls false",
            calls,
        )

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
        calls = self.call_log.read_text(encoding="utf-8")
        self.assertIn(
            "build //flavors/debian:hostname-13-x86_64-build --remote-only --no-remote-cache",
            calls,
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

    def test_aarch64_stage_rejects_wrong_platform(self) -> None:
        result = self.run_smoke(
            "aarch64", scenario="wrong_aarch64_platform"
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL re-aarch64.platform", result.stdout)

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
