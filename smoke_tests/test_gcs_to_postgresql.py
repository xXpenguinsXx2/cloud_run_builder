import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import MagicMock, Mock, patch

from flask import Flask, request

from apps.gcs_to_postgresql.main import (
    get_request_payload,
    get_table_columns,
    import_gcs_to_postgresql,
    resolve_source_object,
)


class GcsToPostgresqlFunctionTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)

    def call_handler(self, payload):
        with self.app.test_request_context("/", json=payload):
            return import_gcs_to_postgresql(request)

    def test_missing_required_fields_returns_bad_request(self):
        response, status = self.call_handler({})

        self.assertEqual(status, 400)
        self.assertIn("missing required parameter(s)", response.get_json()["error"])

    def test_unsupported_format_returns_bad_request(self):
        response, status = self.call_handler(
            {
                "source_project": "source-project",
                "dataset_id": "dataset",
                "table_id": "table",
                "source_bucket": "bucket",
                "source_object": "exports/table.json",
                "format": "JSON",
            }
        )

        self.assertEqual(status, 400)
        self.assertEqual(
            response.get_json(),
            {"error": "unsupported format; expected CSV or PARQUET"},
        )

    def test_nested_bigquery_columns_are_rejected(self):
        field = Mock(field_type="RECORD", mode="REPEATED")
        field.name = "items"

        with self.assertRaisesRegex(ValueError, "unsupported nested/repeated type"):
            get_table_columns([field])

    @patch("apps.gcs_to_postgresql.main.get_database_connection")
    @patch("apps.gcs_to_postgresql.main.storage.Client")
    @patch("apps.gcs_to_postgresql.main.bigquery.Client")
    def test_csv_import_replaces_the_expected_table_and_returns_row_count(
        self,
        bigquery_client_factory,
        storage_client_factory,
        database_connection_factory,
    ):
        schema_field = Mock(field_type="INTEGER", mode="REQUIRED")
        schema_field.name = "id"
        source_table = Mock(schema=[schema_field])
        bigquery_client_factory.return_value.get_table.return_value = source_table
        storage_client_factory.return_value.bucket.return_value.get_blob.return_value = None

        blob = MagicMock()
        blob.name = "example/export-000000000000.csv"
        blob.bucket.name = "test-bucket"
        blob.open.return_value = io.StringIO("id\n1\n2\n")
        storage_client_factory.return_value.list_blobs.return_value = [blob]

        connection = MagicMock()
        cursor = connection.cursor.return_value
        database_connection_factory.return_value = connection

        response, status = self.call_handler(
            {
                "source_project": "source-project",
                "dataset_id": "dataset",
                "table_id": "example",
                "source_bucket": "test-bucket",
                "source_object": "example/export-*.csv",
                "format": "CSV",
            }
        )

        self.assertEqual(status, 200)
        self.assertEqual(response.get_json()["rows"], 2)
        self.assertEqual(response.get_json()["table"], "public.example")
        self.assertEqual(response.get_json()["schema_source"], "bigquery:source-project.dataset.example")
        executed_sql = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertTrue(any("CREATE TABLE" in statement for statement in executed_sql))
        self.assertTrue(any("DROP TABLE IF EXISTS" in statement for statement in executed_sql))
        self.assertTrue(any("RENAME TO" in statement for statement in executed_sql))
        connection.commit.assert_called_once_with()
        connection.close.assert_called_once_with()

    @patch("apps.gcs_to_postgresql.main.get_database_connection")
    @patch("apps.gcs_to_postgresql.main.storage.Client")
    @patch("apps.gcs_to_postgresql.main.bigquery.Client")
    def test_import_uses_exported_schema_json_from_bucket(
        self,
        bigquery_client_factory,
        storage_client_factory,
        database_connection_factory,
    ):
        storage_client = storage_client_factory.return_value
        schema_blob = Mock()
        schema_blob.download_as_text.return_value = (
            '[{"name": "id", "type": "INTEGER", "mode": "REQUIRED"},'
            ' {"name": "label", "type": "STRING", "mode": "NULLABLE"}]'
        )
        storage_client.bucket.return_value.get_blob.return_value = schema_blob

        blob = MagicMock()
        blob.name = "services/exports/example/20261014/part_000000000000.csv"
        blob.bucket.name = "test-bucket"
        blob.open.return_value = io.StringIO("id,label\n1,a\n")
        storage_client.list_blobs.return_value = [blob]

        connection = MagicMock()
        database_connection_factory.return_value = connection

        response, status = self.call_handler(
            {
                "source_project": "source-project",
                "dataset_id": "dataset",
                "table_id": "example",
                "source_bucket": "test-bucket",
                "source_object": "services/exports/example/20261014/part_*.csv",
                "format": "CSV",
            }
        )

        self.assertEqual(status, 200, response.get_json())
        storage_client.bucket.assert_called_with("test-bucket")
        storage_client.bucket.return_value.get_blob.assert_called_once_with(
            "services/exports/example/example_schema.json"
        )
        bigquery_client_factory.assert_not_called()
        self.assertEqual(
            response.get_json()["schema_source"],
            "gs://test-bucket/services/exports/example/example_schema.json",
        )
        self.assertEqual(response.get_json()["columns"], ["id", "label"])
        self.assertEqual(response.get_json()["rows"], 1)
        create_sql = next(
            call.args[0]
            for call in connection.cursor.return_value.execute.call_args_list
            if "CREATE TABLE" in call.args[0]
        )
        self.assertIn('"id" BIGINT NOT NULL', create_sql)
        self.assertIn('"label" TEXT', create_sql)

    @patch("apps.gcs_to_postgresql.main.get_database_connection")
    def test_connection_test_path_reports_success(self, database_connection_factory):
        connection = MagicMock()
        connection.cursor.return_value.fetchone.return_value = ("app-user",)
        database_connection_factory.return_value = connection

        with self.app.test_request_context("/connection-test", method="GET"):
            response, status = import_gcs_to_postgresql(request)

        self.assertEqual(status, 200)
        self.assertEqual(response.get_json()["status"], "connected")
        self.assertEqual(response.get_json()["session_user"], "app-user")
        connection.close.assert_called_once_with()

    @patch("apps.gcs_to_postgresql.main.get_database_connection")
    def test_connection_test_path_reports_failure(self, database_connection_factory):
        database_connection_factory.side_effect = RuntimeError("auth failed")

        with self.app.test_request_context("/connection-test", method="GET"):
            response, status = import_gcs_to_postgresql(request)

        self.assertEqual(status, 500)
        self.assertEqual(response.get_json()["status"], "failed")
        self.assertEqual(response.get_json()["error"], "auth failed")

    @patch("apps.gcs_to_postgresql.main.import_objects")
    @patch("apps.gcs_to_postgresql.main.storage.Client")
    @patch("apps.gcs_to_postgresql.main.bigquery.Client")
    @patch("apps.gcs_to_postgresql.main.get_database_connection")
    def test_debug_example_reports_environment_without_importing(
        self, connection_factory, bigquery_factory, storage_factory, import_objects
    ):
        connection = connection_factory.return_value
        connection.cursor.return_value.fetchone.return_value = ("app-user",)
        example = (
            Path(__file__).resolve().parents[1]
            / "dev_test_apps" / "smoke_tests" / "debug-request.json"
        )
        with patch.dict(os.environ, {
            "DB_HOST": "postgres.example",
            "DB_NAME": "app",
            "DB_USER": "app-user",
            "DB_PASSWORD": "private-password",
            "API_TOKEN": "private-token",
            "K_REVISION": "importer-00001",
        }, clear=True):
            response, status = self.call_handler(json.loads(example.read_text()))

        body = response.get_json()
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "connected")
        self.assertEqual(body["session_user"], "app-user")
        self.assertEqual(body["environment"]["DB_HOST"], "postgres.example")
        self.assertEqual(body["environment"]["K_REVISION"], "importer-00001")
        self.assertIsNone(body["environment"]["DB_PORT"])
        self.assertEqual(body["port"], "5432")
        self.assertTrue(body["environment"]["DB_PASSWORD_SET"])
        self.assertEqual(body["password_source"], "DB_PASSWORD")
        self.assertNotIn("DB_PASSWORD", body["environment"])
        self.assertNotIn("API_TOKEN", body["environment"])
        self.assertNotIn("private-password", response.get_data(as_text=True))
        self.assertNotIn("private-token", response.get_data(as_text=True))
        connection.cursor.return_value.execute.assert_called_once_with("SELECT session_user")
        connection.close.assert_called_once_with()
        bigquery_factory.assert_not_called()
        storage_factory.assert_not_called()
        import_objects.assert_not_called()

    @patch("apps.gcs_to_postgresql.main.get_database_connection")
    def test_debug_failure_still_reports_environment_without_exception_secrets(
        self, connection_factory
    ):
        connection_factory.side_effect = RuntimeError("password=private-password")
        with patch.dict(os.environ, {"DB_HOST": "unreachable"}, clear=True):
            response, status = self.call_handler({"action": "debug"})

        body = response.get_json()
        self.assertEqual(status, 500)
        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["environment"]["DB_HOST"], "unreachable")
        self.assertFalse(body["environment"]["DB_PASSWORD_SET"])
        self.assertIn("RuntimeError", body["error"])
        self.assertNotIn("private-password", response.get_data(as_text=True))

    @patch("apps.gcs_to_postgresql.main.get_database_connection")
    def test_debug_query_failure_closes_connection(self, connection_factory):
        connection = connection_factory.return_value
        connection.cursor.return_value.execute.side_effect = RuntimeError("query failed")

        response, status = self.call_handler({"action": "debug"})

        self.assertEqual(status, 500)
        self.assertEqual(response.get_json()["status"], "failed")
        connection.close.assert_called_once_with()

    @patch("apps.gcs_to_postgresql.main.get_database_connection")
    @patch("apps.gcs_to_postgresql.main.storage.Client")
    def test_unknown_action_is_rejected(self, storage_factory, connection_factory):
        for action in ("deubg", "", None, [], {}):
            with self.subTest(action=action):
                response, status = self.call_handler({"action": action})
                self.assertEqual(status, 400)
                self.assertIn("unsupported action", response.get_json()["error"])
        storage_factory.assert_not_called()
        connection_factory.assert_not_called()

    def test_explicit_import_action_still_requires_import_fields(self):
        response, status = self.call_handler({"action": "import"})
        self.assertEqual(status, 400)
        self.assertIn("missing required parameter(s)", response.get_json()["error"])

    @patch("apps.gcs_to_postgresql.main.import_objects")
    @patch("apps.gcs_to_postgresql.main.storage.Client")
    def test_debug_with_missing_configuration_does_not_fall_through_to_import(
        self, storage_factory, import_objects
    ):
        with patch.dict(os.environ, {}, clear=True):
            response, status = self.call_handler({
                "action": "debug",
                "source_project": "source-project",
                "dataset_id": "dataset",
                "table_id": "example",
                "source_bucket": "bucket",
                "format": "CSV",
            })

        body = response.get_json()
        self.assertEqual(status, 500)
        self.assertEqual(body["status"], "failed")
        self.assertIsNone(body["environment"]["DB_HOST"])
        self.assertIn("ValueError", body["error"])
        storage_factory.assert_not_called()
        import_objects.assert_not_called()

    def test_request_parser_rejects_multiple_wildcards(self):
        payload, error = get_request_payload(
            Mock(
                get_json=lambda silent: {
                    "source_project": "source",
                    "dataset_id": "dataset",
                    "table_id": "table",
                    "source_bucket": "bucket",
                    "source_object": "exports/*/part-*.csv",
                    "format": "CSV",
                }
            )
        )

        self.assertIsNone(payload)
        self.assertIn("at most one", error)

    def test_request_parser_rejects_bad_export_date(self):
        payload, error = get_request_payload(
            Mock(
                get_json=lambda silent: {
                    "source_project": "source",
                    "dataset_id": "dataset",
                    "table_id": "table",
                    "source_bucket": "bucket",
                    "export_date": "2026-10-14",
                    "format": "CSV",
                }
            )
        )

        self.assertIsNone(payload)
        self.assertIn("YYYYMMDD", error)

    def test_resolve_source_object_picks_latest_folder_for_format(self):
        storage_client = Mock()
        names = [
            "services/exports/example/example_schema.json",
            "services/exports/example/20261012/part_000000000000.csv",
            "services/exports/example/20261013/part_000000000000.csv",
            "services/exports/example/20261014/part_000000000000.parquet",
        ]
        blobs = []
        for name in names:
            blob = Mock()
            blob.name = name
            blobs.append(blob)
        storage_client.list_blobs.return_value = blobs
        payload = {"table_id": "example", "source_bucket": "bucket", "format": "CSV"}

        self.assertEqual(
            resolve_source_object(storage_client, payload),
            "services/exports/example/20261013/part_*.csv",
        )
        storage_client.list_blobs.assert_called_once_with(
            "bucket", prefix="services/exports/example/"
        )
        self.assertEqual(
            resolve_source_object(storage_client, {**payload, "format": "PARQUET"}),
            "services/exports/example/20261014/part_*.parquet",
        )

    def test_resolve_source_object_uses_export_date_or_explicit_object(self):
        storage_client = Mock()
        payload = {"table_id": "example", "source_bucket": "bucket", "format": "CSV"}

        self.assertEqual(
            resolve_source_object(storage_client, {**payload, "export_date": "20261001"}),
            "services/exports/example/20261001/part_*.csv",
        )
        self.assertEqual(
            resolve_source_object(storage_client, {**payload, "source_object": "custom/*.csv"}),
            "custom/*.csv",
        )
        storage_client.list_blobs.assert_not_called()

    def test_resolve_source_object_raises_when_no_exports(self):
        storage_client = Mock()
        storage_client.list_blobs.return_value = []

        with self.assertRaisesRegex(ValueError, "no CSV exports found"):
            resolve_source_object(
                storage_client,
                {"table_id": "example", "source_bucket": "bucket", "format": "CSV"},
            )


if __name__ == "__main__":
    unittest.main()
