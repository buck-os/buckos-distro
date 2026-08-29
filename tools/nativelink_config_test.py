#!/usr/bin/env python3

import copy
import os
import unittest
from typing import Any

from nativelink_config import (
    NativeLinkConfigError,
    load_deployment,
    repo_root,
    validate_documents,
)


class TestNativeLinkDeployment(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        directory = os.path.join(repo_root(), "infra", "remote-execution", "nativelink")
        cls.documents = load_deployment(directory)

    def copied_documents(self) -> Any:
        return copy.deepcopy(self.documents)

    def assert_invalid(self, documents: Any, message: str) -> None:
        with self.assertRaisesRegex(NativeLinkConfigError, message):
            validate_documents(*documents)

    def test_checked_in_deployment_is_valid(self) -> None:
        validate_documents(*self.copied_documents())

    def test_rejects_image_tag_instead_of_digest(self) -> None:
        metadata, control, workers, unit = self.copied_documents()
        metadata["image"]["reference"] = "ghcr.io/tracemachina/nativelink:v1.6.6"
        self.assert_invalid((metadata, control, workers, unit), "must be digest-pinned")

    def test_rejects_inconsistent_control_instance(self) -> None:
        metadata, control, workers, unit = self.copied_documents()
        control["servers"][0]["services"]["cas"][0]["instance_name"] = ""
        self.assert_invalid(
            (metadata, control, workers, unit), "instance_name must be 'main'"
        )

    def test_rejects_inconsistent_worker_instance(self) -> None:
        metadata, control, workers, unit = self.copied_documents()
        workers["aarch64"]["stores"][0]["grpc"]["instance_name"] = "other"
        self.assert_invalid(
            (metadata, control, workers, unit), "instance_name must be 'main'"
        )

    def test_rejects_missing_exact_platform_property(self) -> None:
        metadata, control, workers, unit = self.copied_documents()
        properties = control["schedulers"][0]["simple"]["supported_platform_properties"]
        properties["platform.arch"] = "priority"
        self.assert_invalid(
            (metadata, control, workers, unit),
            "scheduler property 'platform.arch' must be exact",
        )

    def test_rejects_wrong_worker_architecture(self) -> None:
        metadata, control, workers, unit = self.copied_documents()
        worker = workers["aarch64"]["workers"][0]["local"]
        worker["platform_properties"]["platform.arch"]["values"] = ["arm64"]
        self.assert_invalid(
            (metadata, control, workers, unit),
            r"platform.arch must be exactly \['aarch64'\]",
        )

    def test_rejects_public_worker_api_bind(self) -> None:
        metadata, control, workers, unit = self.copied_documents()
        control["servers"][1]["listener"]["http"]["socket_address"] = "0.0.0.0:50061"
        self.assert_invalid(
            (metadata, control, workers, unit),
            "worker_api listener must default to a private address",
        )

    def test_rejects_worker_api_on_public_listener(self) -> None:
        metadata, control, workers, unit = self.copied_documents()
        control["servers"][0]["services"]["worker_api"] = {
            "scheduler": "MAIN_SCHEDULER"
        }
        self.assert_invalid(
            (metadata, control, workers, unit),
            "worker_api must not share a listener",
        )

    def test_rejects_native_worker_namespaces(self) -> None:
        metadata, control, workers, unit = self.copied_documents()
        worker = workers["x86_64"]["workers"][0]["local"]
        worker["use_namespaces"] = True
        self.assert_invalid(
            (metadata, control, workers, unit), "use_namespaces must be false"
        )

    def test_rejects_unbounded_store(self) -> None:
        metadata, control, workers, unit = self.copied_documents()
        filesystem = control["stores"][1]["filesystem"]
        del filesystem["eviction_policy"]
        self.assert_invalid(
            (metadata, control, workers, unit), "max_bytes must be bounded"
        )

    def test_rejects_systemd_namespace_blocker(self) -> None:
        metadata, control, workers, unit = self.copied_documents()
        unit = unit.replace("PrivateTmp=yes", "NoNewPrivileges=yes")
        self.assert_invalid(
            (metadata, control, workers, unit),
            "must not set NoNewPrivileges",
        )

    def test_requires_route_netlink_for_action_isolation(self) -> None:
        metadata, control, workers, unit = self.copied_documents()
        unit = unit.replace(" AF_NETLINK", "")
        self.assert_invalid(
            (metadata, control, workers, unit),
            "RestrictAddressFamilies must be 'AF_UNIX AF_INET AF_INET6 AF_NETLINK'",
        )


if __name__ == "__main__":
    unittest.main()
