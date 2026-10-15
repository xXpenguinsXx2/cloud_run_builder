import csv
import fnmatch
import logging
import os
import tempfile
import uuid
from contextlib import closing
from pathlib import Path

from flask import Request, jsonify
from google.cloud import bigquery, storage
import pg8000
import pyarrow.parquet as parquet

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

POSTGRES_TYPES = {
    "STRING": "TEXT",
    "INTEGER": "BIGINT",
    "INT64": "BIGINT",
    "FLOAT": "DOUBLE PRECISION",
    "FLOAT64": "DOUBLE PRECISION",
    "NUMERIC": "NUMERIC",
    "DECIMAL": "NUMERIC",
    "BIGNUMERIC": "NUMERIC",
    "BIGDECIMAL": "NUMERIC",
    "BOOLEAN": "BOOLEAN",
    "BOOL": "BOOLEAN",
    "DATE": "DATE",
    "DATETIME": "TIMESTAMP",
    "TIME": "TIME",
    "TIMESTAMP": "TIMESTAMPTZ",
    "GEOGRAPHY": "TEXT",
    "JSON": "JSONB",
}
REQUIRED_FIELDS = (
    "sink_project",
    "sink_dataset_id",
    "sink_table_id",
    "target_import_bucket",
    "destination_object",
    "format",
)
INSERT_BATCH_SIZE = 1000


def quote_identifier(value):
    return '"' + value.replace('"', '""') + '"'


def get_request_payload(request: Request):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return None, "request body must be a JSON object"

    missing_fields = [
        field
        for field in REQUIRED_FIELDS
        if not isinstance(payload.get(field), str) or not payload[field].strip()
    ]
    if missing_fields:
        return None, "missing required parameter(s): " + ", ".join(missing_fields)

    export_format = payload["format"].strip().upper()
    if export_format not in ("CSV", "PARQUET"):
        return None, "unsupported format; expected CSV or PARQUET"

    object_pattern = payload["destination_object"]
    if object_pattern.count("*") > 1:
        return None, "destination_object may contain at most one '*' wildcard"

    return {**payload, "format": export_format}, None


def get_matching_blobs(storage_client, bucket_name, object_pattern):
    prefix = object_pattern.split("*", 1)[0]
    blobs = [
        blob
        for blob in storage_client.list_blobs(bucket_name, prefix=prefix)
        if (
            fnmatch.fnmatchcase(blob.name, object_pattern)
            if "*" in object_pattern
            else blob.name == object_pattern
        )
    ]
    return sorted(blobs, key=lambda blob: blob.name)


def get_table_columns(schema):
    columns = []
    for field in schema:
        if len(field.name.encode("utf-8")) > 63:
            raise ValueError(
                f"field name {field.name!r} exceeds PostgreSQL's 63-byte limit"
            )
        field_type = field.field_type.upper()
        if field.mode == "REPEATED" or field_type in ("RECORD", "STRUCT"):
            raise ValueError(
                f"field {field.name} uses unsupported nested/repeated type "
                f"{field.mode} {field_type}"
            )

        postgres_type = POSTGRES_TYPES.get(field_type)
        if postgres_type is None:
            raise ValueError(
                f"field {field.name} has unsupported BigQuery type {field_type}"
            )
        not_null = " NOT NULL" if field.mode == "REQUIRED" else ""
        columns.append(
            f"{quote_identifier(field.name)} {postgres_type}{not_null}"
        )
    if not columns:
        raise ValueError("BigQuery table has no columns")
    return columns


def get_database_connection():
    connection_name = os.environ.get("CLOUD_SQL_CONNECTION_NAME", "").strip()
    database = os.environ.get("DB_NAME", "").strip()
    if connection_name:
        iam_user = os.environ.get("DB_IAM_USER", "").strip()
        if not database or not iam_user:
            raise ValueError(
                "DB_NAME and DB_IAM_USER are required with "
                "CLOUD_SQL_CONNECTION_NAME"
            )

        from google.cloud.sql.connector import Connector, IPTypes

        ip_type = os.environ.get("CLOUD_SQL_IP_TYPE", "PUBLIC").strip().upper()
        if ip_type not in ("PUBLIC", "PRIVATE"):
            raise ValueError("CLOUD_SQL_IP_TYPE must be PUBLIC or PRIVATE")
        connector = Connector()
        try:
            connection = connector.connect(
                connection_name,
                "pg8000",
                user=iam_user,
                db=database,
                enable_iam_auth=True,
                ip_type=IPTypes.PUBLIC if ip_type == "PUBLIC" else IPTypes.PRIVATE,
            )
        except Exception:
            connector.close()
            raise
        return connection, connector

    host = os.environ.get("DB_HOST", "").strip()
    user = os.environ.get("DB_USER", "").strip()
    password = os.environ.get("DB_PASSWORD", "")
    ssl_context = None
    if os.environ.get("DB_IAM_TOKEN_AUTH", "").strip().lower() in ("1", "true", "yes"):
        # Manual IAM database authentication: an OAuth access token is the password.
        import ssl

        import google.auth
        from google.auth.transport.requests import Request as AuthRequest

        credentials, _ = google.auth.default(
            scopes=[
                "https://www.googleapis.com/auth/cloud-platform",
                "https://www.googleapis.com/auth/sqlservice.login",
            ]
        )
        credentials.refresh(AuthRequest())
        password = credentials.token
        # DB_SSL=false for hosts that terminate TLS elsewhere (e.g. Cloud SQL
        # Auth Proxy), which refuse the SSL request from the client.
        if os.environ.get("DB_SSL", "true").strip().lower() not in ("0", "false", "no"):
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
    if not host or not database or not user or not password:
        raise ValueError(
            "DB_HOST, DB_NAME, DB_USER, and DB_PASSWORD are required for "
            "a direct PostgreSQL connection"
        )
    return (
        pg8000.connect(
            host=host,
            port=int(os.environ.get("DB_PORT", "5432")),
            database=database,
            user=user,
            password=password,
            timeout=float(os.environ.get("DB_CONNECT_TIMEOUT_SECONDS", "15")),
            ssl_context=ssl_context,
        ),
        None,
    )


def insert_rows(cursor, statement, rows):
    batch = []
    count = 0
    for row in rows:
        batch.append(row)
        if len(batch) == INSERT_BATCH_SIZE:
            cursor.executemany(statement, batch)
            count += len(batch)
            batch.clear()
    if batch:
        cursor.executemany(statement, batch)
        count += len(batch)
    return count


def import_csv_blob(cursor, blob, expected_columns, insert_statement):
    imported_rows = 0
    with blob.open("rt", encoding="utf-8", newline="") as stream:
        reader = csv.reader(stream)
        header = next(reader, None)
        if header != expected_columns:
            raise ValueError(
                f"CSV header in {blob.name} does not match the BigQuery table schema"
            )
        rows = (
            tuple(value if value != "" else None for value in row)
            for row in reader
        )
        return insert_rows(cursor, insert_statement, rows)


def import_parquet_blob(cursor, blob, expected_columns, insert_statement):
    with tempfile.TemporaryDirectory() as temp_directory:
        parquet_path = Path(temp_directory) / "source.parquet"
        blob.download_to_filename(str(parquet_path))
        parquet_file = parquet.ParquetFile(parquet_path)
        if parquet_file.schema_arrow.names != expected_columns:
            raise ValueError(
                f"Parquet columns in {blob.name} do not match the BigQuery table schema"
            )

        imported_rows = 0
        for record_batch in parquet_file.iter_batches(batch_size=INSERT_BATCH_SIZE):
            rows = (
                tuple(record.get(column) for column in expected_columns)
                for record in record_batch.to_pylist()
            )
            imported_rows += insert_rows(cursor, insert_statement, rows)
        return imported_rows


def import_objects(
    connection,
    schema_name,
    table_name,
    columns,
    column_names,
    blobs,
    export_format,
):
    schema_identifier = quote_identifier(schema_name)
    table_identifier = quote_identifier(table_name)
    staging_name = f"import_{uuid.uuid4().hex}"
    staging_identifier = quote_identifier(staging_name)
    qualified_staging = f"{schema_identifier}.{staging_identifier}"
    qualified_target = f"{schema_identifier}.{table_identifier}"
    insert_statement = (
        f"INSERT INTO {qualified_staging} "
        f"({', '.join(quote_identifier(name) for name in column_names)}) "
        f"VALUES ({', '.join('%s' for _ in columns)})"
    )
    create_statement = (
        f"CREATE TABLE {qualified_staging} ({', '.join(columns)})"
    )

    imported_rows = 0
    try:
        with closing(connection.cursor()) as cursor:
            cursor.execute(f"CREATE SCHEMA IF NOT EXISTS {schema_identifier}")
            cursor.execute(create_statement)
            for blob in blobs:
                logger.info(
                    "Importing gs://%s/%s into %s.%s",
                    blob.bucket.name,
                    blob.name,
                    schema_name,
                    table_name,
                )
                if export_format == "CSV":
                    imported_rows += import_csv_blob(
                        cursor, blob, column_names, insert_statement
                    )
                else:
                    imported_rows += import_parquet_blob(
                        cursor, blob, column_names, insert_statement
                    )
            cursor.execute(f"DROP TABLE IF EXISTS {qualified_target}")
            cursor.execute(
                f"ALTER TABLE {qualified_staging} RENAME TO {table_identifier}"
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return imported_rows


def import_gcs_to_postgresql(request: Request):
    payload, validation_error = get_request_payload(request)
    if validation_error:
        return jsonify({"error": validation_error}), 400

    table_name = (
        f"parq_{payload['sink_table_id']}"
        if payload["format"] == "PARQUET"
        else payload["sink_table_id"]
    )
    schema_name = os.environ.get("DB_SCHEMA", "public").strip()
    if not schema_name:
        return jsonify({"error": "DB_SCHEMA must not be empty"}), 500
    if len(schema_name.encode("utf-8")) > 63 or len(table_name.encode("utf-8")) > 63:
        return jsonify(
            {"error": "destination schema and table names must be at most 63 bytes"}
        ), 400
    connector = None
    connection = None
    try:
        source_table_id = (
            f"{payload['sink_project']}."
            f"{payload['sink_dataset_id']}."
            f"{payload['sink_table_id']}"
        )
        logger.info(
            "Loading schema for %s before importing %s objects from gs://%s/%s",
            source_table_id,
            payload["format"],
            payload["target_import_bucket"],
            payload["destination_object"],
        )
        source_table = bigquery.Client(
            project=payload["sink_project"]
        ).get_table(source_table_id)
        columns = get_table_columns(source_table.schema)
        column_names = [field.name for field in source_table.schema]

        storage_client = storage.Client(project=payload["sink_project"])
        blobs = get_matching_blobs(
            storage_client,
            payload["target_import_bucket"],
            payload["destination_object"],
        )
        if not blobs:
            raise ValueError(
                "no GCS objects match "
                f"gs://{payload['target_import_bucket']}/"
                f"{payload['destination_object']}"
            )

        connection, connector = get_database_connection()
        imported_rows = import_objects(
            connection,
            schema_name,
            table_name,
            columns,
            column_names,
            blobs,
            payload["format"],
        )
        logger.info(
            "Imported %s row(s) from %s object(s) into %s.%s",
            imported_rows,
            len(blobs),
            schema_name,
            table_name,
        )
        return (
            jsonify(
                {
                    "status": "success",
                    "source": (
                        f"gs://{payload['target_import_bucket']}/"
                        f"{payload['destination_object']}"
                    ),
                    "imported": (
                        f"{payload['sink_project']}:{payload['sink_dataset_id']}."
                        f"{payload['sink_table_id']}"
                    ),
                    "table": f"{schema_name}.{table_name}",
                    "format": payload["format"],
                    "objects": len(blobs),
                    "rows": imported_rows,
                    "columns": column_names,
                }
            ),
            200,
        )
    except Exception as exc:
        logger.exception("GCS-to-PostgreSQL import failed")
        return jsonify({"error": str(exc)}), 500
    finally:
        if connection is not None:
            connection.close()
        if connector is not None:
            connector.close()


if __name__ == "__main__":
    from flask import Flask, request

    app = Flask(__name__)

    @app.route("/", methods=["POST"])
    def _root():
        return import_gcs_to_postgresql(request)

    app.run("0.0.0.0", port=int(os.environ.get("PORT", 8080)))
