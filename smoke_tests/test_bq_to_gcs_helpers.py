import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from flask import Flask, request

APP_DIR = Path(__file__).resolve().parents[1] / "apps" / "bq_to_gcs_helpers"
sys.path.insert(0, str(APP_DIR))

from main import append_history, export_table_to_gcs, get_latest_extract_time


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

    @patch("main.datetime")
    @patch("main.storage.Client")
    @patch("main.bigquery.Client")
    def test_completed_extract_returns_success(
        self, mock_bigquery_client, mock_storage_client, mock_datetime
    ):
        mock_datetime.now.return_value.strftime.side_effect = (
            lambda fmt: "20261014" if fmt == "%Y%m%d" else "2026-10-14T15:00:00Z"
        )
        extract_job = Mock(state="DONE", job_id="test-job")
        client = mock_bigquery_client.return_value
        client.extract_table.return_value = extract_job
        field = Mock()
        field.to_api_repr.return_value = {"name": "id", "type": "INTEGER"}
        client.get_table.return_value.schema = [field]
        client.query.return_value.result.return_value = [
            {"modified": datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)}
        ]
        bucket = mock_storage_client.return_value.bucket.return_value
        bucket.name = "test-bucket"
        bucket.get_blob.return_value = None
        blob = bucket.blob.return_value

        response, status = self.call_handler(
            {
                "source_project": "test-project",
                "dataset_id": "test_dataset",
                "table_id": "test_table",
                "destination_bucket": "test-bucket",
                "format": "CSV",
            }
        )

        self.assertEqual(status, 200)
        self.assertEqual(
            response.get_json(),
            {
                "status": "success",
                "exported": "test-project:test_dataset.test_table",
                "destination": "gs://test-bucket/services/exports/test_table/20261014/part_*.csv",
                "latest_folder": "gs://test-bucket/services/exports/test_table/20261014/",
                "export_skipped": False,
                "source_modified_time": "2026-10-14T12:00:00Z",
                "schema": "gs://test-bucket/services/exports/test_table/test_table_schema.json",
                "history": "gs://test-bucket/services/history/export_history.csv",
                "last_extract_time": "2026-10-14T15:00:00Z",
                "previous_extract_time": "1900-01-01T00:00:00Z",
            },
        )
        self.assertEqual(
            [c[0][0] for c in bucket.blob.call_args_list],
            [
                "services/exports/test_table/test_table_schema.json",
                "services/history/export_history.csv",
            ],
        )
        uploads = blob.upload_from_string.call_args_list
        self.assertIn('"name": "id"', uploads[0][0][0])
        history_csv = uploads[1][0][0]
        self.assertTrue(
            history_csv.startswith(
                "exported_table,destination,finished_at_utc,status,source_modified_time\n"
            )
        )
        self.assertIn(
            "test-project.test_dataset.test_table,"
            "gs://test-bucket/services/exports/test_table/20261014/part_*.csv,"
            "2026-10-14T15:00:00Z,success,2026-10-14T12:00:00Z",
            history_csv,
        )
        self.assertEqual(uploads[1][1]["if_generation_match"], 0)
        extract_job.result.assert_called_once_with()


    def _skip_case(self, modified, mock_bigquery_client, mock_storage_client):
        client = mock_bigquery_client.return_value
        client.extract_table.return_value = Mock(state="DONE", job_id="j")
        client.query.return_value.result.return_value = [{"modified": modified}]
        bucket = mock_storage_client.return_value.bucket.return_value
        bucket.get_blob.return_value.download_as_text.return_value = (
            "exported_table,destination,finished_at_utc\n"
            "test-project.test_dataset.test_table,"
            "gs://test-bucket/services/exports/test_table/20261013/part_*.csv,"
            "2026-10-13T10:00:00Z\n"
        )
        return client, self.call_handler(
            {
                "source_project": "test-project",
                "dataset_id": "test_dataset",
                "table_id": "test_table",
                "destination_bucket": "test-bucket",
            }
        )

    @patch("main.storage.Client")
    @patch("main.bigquery.Client")
    def test_unchanged_source_skips_export(self, mock_bq, mock_storage):
        client, (response, status) = self._skip_case(
            datetime(2026, 10, 12, tzinfo=timezone.utc), mock_bq, mock_storage
        )
        self.assertEqual(status, 200)
        body = response.get_json()
        self.assertTrue(body["export_skipped"])
        self.assertEqual(
            body["latest_folder"],
            "gs://test-bucket/services/exports/test_table/20261013/",
        )
        self.assertEqual(body["last_extract_time"], "2026-10-13T10:00:00Z")
        client.extract_table.assert_not_called()
        bucket = mock_storage.return_value.bucket.return_value
        history_csv = bucket.blob.return_value.upload_from_string.call_args[0][0]
        self.assertIn(",skipped,2026-10-12T00:00:00Z", history_csv)
        # legacy 3-column row is upgraded and treated as a success
        self.assertIn("2026-10-13T10:00:00Z,success,", history_csv)

    @patch("main.storage.Client")
    @patch("main.bigquery.Client")
    def test_failed_extract_writes_failed_row(self, mock_bq, mock_storage):
        client, (response, status) = self._skip_case(
            datetime(2026, 10, 14, tzinfo=timezone.utc), mock_bq, mock_storage
        )
        # re-run with the extract failing
        client.extract_table.side_effect = RuntimeError("boom")
        bucket = mock_storage.return_value.bucket.return_value
        bucket.blob.return_value.upload_from_string.reset_mock()
        response, status = self.call_handler(
            {
                "source_project": "test-project",
                "dataset_id": "test_dataset",
                "table_id": "test_table",
                "destination_bucket": "test-bucket",
            }
        )
        self.assertEqual(status, 500)
        history_csv = bucket.blob.return_value.upload_from_string.call_args[0][0]
        self.assertIn(",failed,2026-10-14T00:00:00Z", history_csv)

    @patch("main.storage.Client")
    @patch("main.bigquery.Client")
    def test_modified_source_triggers_export(self, mock_bq, mock_storage):
        client, (response, status) = self._skip_case(
            datetime(2026, 10, 14, tzinfo=timezone.utc), mock_bq, mock_storage
        )
        client.extract_table.assert_called_once()


class LatestExtractTimeTests(unittest.TestCase):
    def test_missing_history_defaults_to_1900(self):
        bucket = Mock()
        bucket.get_blob.return_value = None
        self.assertEqual(
            get_latest_extract_time(bucket, "p.d.t").year, 1900
        )

    def test_reads_history_from_legacy_location(self):
        bucket = Mock()
        bucket.get_blob.side_effect = [
            None,
            Mock(
                download_as_text=Mock(
                    return_value=(
                        "exported_table,destination,finished_at_utc\n"
                        "p.d.t,gs://bucket/old-export.csv,2026-10-13T10:00:00Z\n"
                    )
                )
            ),
        ]

        self.assertEqual(
            get_latest_extract_time(bucket, "p.d.t"),
            datetime(2026, 10, 13, 10, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            [call.args[0] for call in bucket.get_blob.call_args_list],
            ["services/history/export_history.csv", "history/export_history.csv"],
        )

    def test_appending_history_copies_legacy_rows_to_new_location(self):
        bucket = Mock()
        bucket.name = "test-bucket"
        legacy_blob = Mock(
            generation=3,
            download_as_text=Mock(
                return_value=(
                    "exported_table,destination,finished_at_utc\n"
                    "p.d.t,gs://bucket/old-export.csv,2026-10-13T10:00:00Z\n"
                )
            ),
        )
        bucket.get_blob.side_effect = [None, legacy_blob]
        new_blob = bucket.blob.return_value

        history_uri = append_history(
            bucket,
            {
                "exported_table": "p.d.t",
                "destination": "gs://bucket/services/exports/t/20261014/part_*.csv",
                "finished_at_utc": "2026-10-14T10:00:00Z",
                "status": "success",
                "source_modified_time": "2026-10-14T09:00:00Z",
            },
        )

        self.assertEqual(
            history_uri, "gs://test-bucket/services/history/export_history.csv"
        )
        new_blob.upload_from_string.assert_called_once()
        self.assertEqual(
            new_blob.upload_from_string.call_args.kwargs["if_generation_match"], 0
        )
        migrated_history = new_blob.upload_from_string.call_args.args[0]
        self.assertIn(
            "p.d.t,gs://bucket/old-export.csv,2026-10-13T10:00:00Z,success,",
            migrated_history,
        )
        self.assertIn(
            "p.d.t,gs://bucket/services/exports/t/20261014/part_*.csv,"
            "2026-10-14T10:00:00Z,success,2026-10-14T09:00:00Z",
            migrated_history,
        )

    def test_returns_latest_row_for_table(self):
        bucket = Mock()
        bucket.get_blob.return_value.download_as_text.return_value = (
            "exported_table,destination,finished_at_utc\n"
            "p.d.t,gs://b/t/1/part_*.csv,2026-10-13T10:00:00Z\n"
            "p.d.other,gs://b/o/1/part_*.csv,2026-10-14T10:00:00Z\n"
            "p.d.t,gs://b/t/2/part_*.csv,2026-10-14T09:30:00Z\n"
        )
        latest = get_latest_extract_time(bucket, "p.d.t")
        self.assertEqual(latest.strftime("%Y-%m-%dT%H:%M:%SZ"), "2026-10-14T09:30:00Z")


    def test_non_success_rows_are_ignored(self):
        bucket = Mock()
        bucket.get_blob.return_value.download_as_text.return_value = (
            "exported_table,destination,finished_at_utc,status,source_modified_time\n"
            "p.d.t,gs://b/t/1/part_*.csv,2026-10-13T10:00:00Z,success,\n"
            "p.d.t,gs://b/t/1/part_*.csv,2026-10-14T10:00:00Z,skipped,\n"
            "p.d.t,,2026-10-14T11:00:00Z,failed,\n"
        )
        latest = get_latest_extract_time(bucket, "p.d.t")
        self.assertEqual(latest.strftime("%Y-%m-%dT%H:%M:%SZ"), "2026-10-13T10:00:00Z")


if __name__ == "__main__":
    unittest.main()