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
        metadata, controls, workers, unit = self.copied_documents()
        metadata["image"]["reference"] = "ghcr.io/tracemachina/nativelink:v1.6.6"
        self.assert_invalid((metadata, controls, workers, unit), "must be digest-pinned")

    def test_rejects_inconsistent_control_instance(self) -> None:
        metadata, controls, workers, unit = self.copied_documents()
        control = controls["plaintext"]
        control["servers"][0]["services"]["cas"][0]["instance_name"] = ""
        self.assert_invalid(
            (metadata, controls, workers, unit), "instance_name must be 'main'"
        )

    def test_rejects_inconsistent_worker_instance(self) -> None:
        metadata, controls, workers, unit = self.copied_documents()
        workers["plaintext"]["aarch64"]["stores"][0]["grpc"][
            "instance_name"
        ] = "other"
        self.assert_invalid(
            (metadata, controls, workers, unit), "instance_name must be 'main'"
        )

    def test_rejects_missing_exact_platform_property(self) -> None:
        metadata, controls, workers, unit = self.copied_documents()
        control = controls["plaintext"]
        properties = control["schedulers"][0]["simple"]["supported_platform_properties"]
        properties["platform.arch"] = "priority"
        self.assert_invalid(
            (metadata, controls, workers, unit),
            "scheduler property 'platform.arch' must be exact",
        )

    def test_rejects_wrong_worker_architecture(self) -> None:
        metadata, controls, workers, unit = self.copied_documents()
        worker = workers["plaintext"]["aarch64"]["workers"][0]["local"]
        worker["platform_properties"]["platform.arch"]["values"] = ["arm64"]
        self.assert_invalid(
            (metadata, controls, workers, unit),
            r"platform.arch must be exactly \['aarch64'\]",
        )

    def test_rejects_public_worker_api_bind(self) -> None:
        metadata, controls, workers, unit = self.copied_documents()
        control = controls["plaintext"]
        control["servers"][1]["listener"]["http"]["socket_address"] = "0.0.0.0:50061"
        self.assert_invalid(
            (metadata, controls, workers, unit),
            "worker_api listener must default to a private address",
        )

    def test_rejects_worker_api_on_public_listener(self) -> None:
        metadata, controls, workers, unit = self.copied_documents()
        control = controls["plaintext"]
        control["servers"][0]["services"]["worker_api"] = {
            "scheduler": "MAIN_SCHEDULER"
        }
        self.assert_invalid(
            (metadata, controls, workers, unit),
            "worker_api must not share a listener",
        )

    def test_rejects_native_worker_namespaces(self) -> None:
        metadata, controls, workers, unit = self.copied_documents()
        worker = workers["plaintext"]["x86_64"]["workers"][0]["local"]
        worker["use_namespaces"] = True
        self.assert_invalid(
            (metadata, controls, workers, unit), "use_namespaces must be false"
        )

    def test_rejects_unbounded_store(self) -> None:
        metadata, controls, workers, unit = self.copied_documents()
        control = controls["plaintext"]
        filesystem = control["stores"][1]["filesystem"]
        del filesystem["eviction_policy"]
        self.assert_invalid(
            (metadata, controls, workers, unit), "max_bytes must be bounded"
        )

    def test_rejects_systemd_namespace_blocker(self) -> None:
        metadata, controls, workers, unit = self.copied_documents()
        unit = unit.replace("PrivateTmp=yes", "NoNewPrivileges=yes")
        self.assert_invalid(
            (metadata, controls, workers, unit),
            "must not set NoNewPrivileges",
        )

    def test_requires_route_netlink_for_action_isolation(self) -> None:
        metadata, controls, workers, unit = self.copied_documents()
        unit = unit.replace(" AF_NETLINK", "")
        self.assert_invalid(
            (metadata, controls, workers, unit),
            "RestrictAddressFamilies must be 'AF_UNIX AF_INET AF_INET6 AF_NETLINK'",
        )

    def test_rejects_tls_on_plaintext_listener(self) -> None:
        metadata, controls, workers, unit = self.copied_documents()
        controls["plaintext"]["servers"][0]["listener"]["http"]["tls"] = {}
        self.assert_invalid(
            (metadata, controls, workers, unit),
            "plaintext public REAPI listener must not configure TLS",
        )

    def test_rejects_mtls_reapi_worker_only_trust(self) -> None:
        metadata, controls, workers, unit = self.copied_documents()
        tls = controls["mtls"]["servers"][0]["listener"]["http"]["tls"]
        tls["client_ca_file"] = "/etc/nativelink/tls/worker-client-ca.pem"
        self.assert_invalid(
            (metadata, controls, workers, unit),
            "public REAPI listener must use the combined client trust bundle",
        )

    def test_rejects_mtls_worker_api_buck_client_trust(self) -> None:
        metadata, controls, workers, unit = self.copied_documents()
        tls = controls["mtls"]["servers"][1]["listener"]["http"]["tls"]
        tls["client_ca_file"] = "/etc/nativelink/tls/reapi-client-ca.pem"
        self.assert_invalid(
            (metadata, controls, workers, unit),
            "worker_api listener must trust only the worker client CA",
        )

    def test_rejects_plaintext_worker_with_partial_tls(self) -> None:
        metadata, controls, workers, unit = self.copied_documents()
        endpoint = workers["plaintext"]["x86_64"]["stores"][0]["grpc"][
            "endpoints"
        ][0]
        endpoint["tls_config"] = {"ca_file": "/tmp/ca.pem"}
        self.assert_invalid(
            (metadata, controls, workers, unit),
            "plaintext worker-x86_64.REMOTE_CAS must not configure TLS",
        )

    def test_rejects_mtls_worker_endpoint_schemes(self) -> None:
        paths = ("REMOTE_CAS", "REMOTE_AC", "worker_api")
        for path in paths:
            with self.subTest(path=path):
                metadata, controls, workers, unit = self.copied_documents()
                worker = workers["mtls"]["aarch64"]
                if path == "worker_api":
                    worker["workers"][0]["local"]["worker_api_endpoint"]["uri"] = (
                        "grpc://${NATIVELINK_CONTROL_DNS}:50061"
                    )
                else:
                    store = next(
                        item for item in worker["stores"] if item["name"] == path
                    )
                    store["grpc"]["endpoints"][0]["address"] = (
                        "grpc://${NATIVELINK_CONTROL_DNS}:50051"
                    )
                self.assert_invalid(
                    (metadata, controls, workers, unit),
                    "wrong scheme or address",
                )

    def test_rejects_partial_mtls_on_every_worker_client_path(self) -> None:
        paths = ("REMOTE_CAS", "REMOTE_AC", "worker_api")
        for path in paths:
            with self.subTest(path=path):
                metadata, controls, workers, unit = self.copied_documents()
                worker = workers["mtls"]["x86_64"]
                if path == "worker_api":
                    tls = worker["workers"][0]["local"]["worker_api_endpoint"][
                        "tls_config"
                    ]
                else:
                    store = next(
                        item for item in worker["stores"] if item["name"] == path
                    )
                    tls = store["grpc"]["endpoints"][0]["tls_config"]
                del tls["key_file"]
                self.assert_invalid(
                    (metadata, controls, workers, unit),
                    "complete worker TLS identity",
                )


if __name__ == "__main__":
    unittest.main()
