import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from flask import Flask, request

APP_DIR = Path(__file__).resolve().parents[1] / "apps" / "bq_to_gcs_helpers"
sys.path.insert(0, str(APP_DIR))

from main import export_table_to_gcs


class BigQueryToGcsSmokeTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)

    def call_handler(self, payload):
        with self.app.test_request_context("/", json=payload):
            return export_table_to_gcs(request)

    def test_missing_required_fields_returns_bad_request(self):
        response, status = self.call_handler({})

        self.assertEqual(status, 400)
        self.assertIn("missing required parameter(s)", response.get_json()["error"])

    def test_unsupported_export_format_returns_bad_request(self):
        response, status = self.call_handler(
            {
                "source_project": "test-project",
                "dataset_id": "test_dataset",
                "table_id": "test_table",
                "destination_bucket": "test-bucket",
                "format": "XML",
            }
        )

        self.assertEqual(status, 400)
        self.assertEqual(response.get_json(), {"error": "unsupported format"})

    @patch("main.bigquery.Client")
    def test_completed_extract_returns_success(self, mock_bigquery_client):
        extract_job = Mock(state="DONE", job_id="test-job")
        client = mock_bigquery_client.return_value
        client.extract_table.return_value = extract_job

        response, status = self.call_handler(
            {
                "source_project": "test-project",
                "dataset_id": "test_dataset",
                "table_id": "test_table",
                "destination_bucket": "test-bucket",
                "destination_object": "exports/test.csv",
                "format": "CSV",
            }
        )

        self.assertEqual(status, 200)
        self.assertEqual(
            response.get_json(),
            {
                "status": "success",
                "exported": "test-project:test_dataset.test_table",
                "destination": "gs://test-bucket/exports/test.csv",
            },
        )
        extract_job.result.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()