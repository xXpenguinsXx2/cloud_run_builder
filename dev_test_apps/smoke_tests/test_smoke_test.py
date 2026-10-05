import unittest
from unittest.mock import Mock, patch

from smoke_test import find_matching_export_blobs, run_local_export_smoke_test
from cloud_run_smoke_test import get_cloud_run_service_url, get_identity_token


class ExportObjectMatchingTests(unittest.TestCase):
    def setUp(self):
        self.storage_client = Mock()
        self.storage_client.list_blobs.return_value = [
            Mock(name="example/export-001.csv"),
            Mock(name="example/export-002.csv"),
            Mock(name="example/other.csv"),
        ]
        for blob, name in zip(
            self.storage_client.list_blobs.return_value,
            (
                "example/export-001.csv",
                "example/export-002.csv",
                "example/other.csv",
            ),
        ):
            blob.name = name

    def test_matches_wildcard_export_pattern(self):
        matching_blobs = find_matching_export_blobs(
            self.storage_client, "exports", "example/export-*.csv"
        )

        self.assertEqual(
            [blob.name for blob in matching_blobs],
            ["example/export-001.csv", "example/export-002.csv"],
        )
        self.storage_client.list_blobs.assert_called_once_with(
            "exports", prefix="example/export-"
        )

    def test_matches_exact_export_object(self):
        matching_blobs = find_matching_export_blobs(
            self.storage_client, "exports", "example/other.csv"
        )

        self.assertEqual([blob.name for blob in matching_blobs], ["example/other.csv"])


class LocalExportSmokeTestTests(unittest.TestCase):
    @patch("smoke_test.run_export_smoke_test")
    @patch("smoke_test.run_validation_smoke_test")
    def test_checks_readiness_before_export(self, validation_test, export_test):
        call_order = []
        validation_test.side_effect = lambda *_: call_order.append("readiness") or 0
        export_test.side_effect = lambda *_: call_order.append("export") or 0

        result = run_local_export_smoke_test("http://localhost:8080", 30)

        self.assertEqual(result, 0)
        self.assertEqual(call_order, ["readiness", "export"])
        validation_test.assert_called_once_with("http://localhost:8080", 30)
        export_test.assert_called_once_with("http://localhost:8080", 30)

    @patch("smoke_test.run_export_smoke_test")
    @patch("smoke_test.run_validation_smoke_test")
    def test_skips_export_when_readiness_check_fails(
        self, validation_test, export_test
    ):
        validation_test.return_value = 1

        result = run_local_export_smoke_test("http://localhost:8080", 30)

        self.assertEqual(result, 1)
        export_test.assert_not_called()


class CloudRunAuthenticationTests(unittest.TestCase):
    @patch("cloud_run_smoke_test.run_gcloud_command")
    def test_resolves_deployed_service_url(self, run_gcloud):
        run_gcloud.return_value = "https://service-abc.a.run.app"

        service_url = get_cloud_run_service_url(
            "gcloud", "project", "us-central1", "service"
        )

        self.assertEqual(service_url, "https://service-abc.a.run.app")
        run_gcloud.assert_called_once_with(
            "gcloud",
            "run",
            "services",
            "describe",
            "service",
            "--project=project",
            "--region=us-central1",
            "--format=value(status.url)",
        )

    @patch("cloud_run_smoke_test.run_gcloud_command")
    def test_rejects_missing_service_url(self, run_gcloud):
        run_gcloud.return_value = ""

        with self.assertRaisesRegex(RuntimeError, "invalid URL"):
            get_cloud_run_service_url("gcloud", "project", "region", "service")

    @patch("cloud_run_smoke_test.run_gcloud_command")
    def test_gets_identity_token_without_proxy(self, run_gcloud):
        run_gcloud.return_value = "identity-token"

        self.assertEqual(get_identity_token("gcloud"), "identity-token")
        run_gcloud.assert_called_once_with(
            "gcloud", "auth", "print-identity-token"
        )


if __name__ == "__main__":
    unittest.main()