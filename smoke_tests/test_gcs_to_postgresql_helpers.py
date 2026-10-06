import io
import unittest
from unittest.mock import MagicMock, Mock, patch

from flask import Flask, request

from apps.gcs_to_postgresql_helpers.main import (
    get_request_payload,
    get_table_columns,
    import_gcs_to_postgresql,
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
                "destination_bucket": "bucket",
                "destination_object": "exports/table.json",
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

    @patch("apps.gcs_to_postgresql_helpers.main.get_database_connection")
    @patch("apps.gcs_to_postgresql_helpers.main.storage.Client")
    @patch("apps.gcs_to_postgresql_helpers.main.bigquery.Client")
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

        blob = MagicMock()
        blob.name = "example/export-000000000000.csv"
        blob.bucket.name = "test-bucket"
        blob.open.return_value = io.StringIO("id\n1\n2\n")
        storage_client_factory.return_value.list_blobs.return_value = [blob]

        connection = MagicMock()
        cursor = connection.cursor.return_value
        database_connection_factory.return_value = (connection, None)

        response, status = self.call_handler(
            {
                "source_project": "source-project",
                "dataset_id": "dataset",
                "table_id": "example",
                "destination_bucket": "test-bucket",
                "destination_object": "example/export-*.csv",
                "format": "CSV",
            }
        )

        self.assertEqual(status, 200)
        self.assertEqual(response.get_json()["rows"], 2)
        self.assertEqual(response.get_json()["table"], "public.example")
        executed_sql = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertTrue(any("CREATE TABLE" in statement for statement in executed_sql))
        self.assertTrue(any("DROP TABLE IF EXISTS" in statement for statement in executed_sql))
        self.assertTrue(any("RENAME TO" in statement for statement in executed_sql))
        connection.commit.assert_called_once_with()
        connection.close.assert_called_once_with()

    def test_request_parser_rejects_multiple_wildcards(self):
        payload, error = get_request_payload(
            Mock(
                get_json=lambda silent: {
                    "source_project": "source",
                    "dataset_id": "dataset",
                    "table_id": "table",
                    "destination_bucket": "bucket",
                    "destination_object": "exports/*/part-*.csv",
                    "format": "CSV",
                }
            )
        )

        self.assertIsNone(payload)
        self.assertIn("at most one", error)


if __name__ == "__main__":
    unittest.main()
