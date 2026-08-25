"""Noop remote test execution toolchain.

sh_test and python_test require a RemoteTestExecutionToolchainInfo even
when nothing runs remotely.  Copied from buckos-build.
"""

load("@prelude//tests:remote_test_execution_toolchain.bzl", "RemoteTestExecutionToolchainInfo")

def _impl(_ctx: AnalysisContext) -> list[Provider]:
    return [
        DefaultInfo(),
        RemoteTestExecutionToolchainInfo(
            default_profile = None,
            profiles = {},
            default_run_as_bundle = False,
        ),
    ]

noop_remote_test_execution_toolchain = rule(
    impl = _impl,
    attrs = {},
    is_toolchain_rule = True,
)
