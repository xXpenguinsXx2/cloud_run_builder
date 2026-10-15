import base64
import csv
import fnmatch
import json
import logging
import os
import re
import tempfile
import uuid
from contextlib import closing
from pathlib import Path

from flask import Request, jsonify
import google.auth
from google.auth.transport.requests import AuthorizedSession
from google.auth.transport.requests import Request as AuthRequest
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
    "source_project",
    "dataset_id",
    "table_id",
    "source_bucket",
    "format",
)
FORMAT_EXTENSIONS = {"CSV": "csv", "PARQUET": "parquet"}
EXPORT_DATE_PATTERN = re.compile(r"^\d{8}$")
INSERT_BATCH_SIZE = 1000
CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
CONNECTION_TEST_PATH = "/connection-test"
DEBUG_ENVIRONMENT_KEYS = (
    "DB_HOST",
    "DB_PORT",
    "DB_NAME",
    "DB_USER",
    "DB_SCHEMA",
    "DB_CONNECT_TIMEOUT_SECONDS",
    "DB_SECRET_NAME",
    "DB_SECRET_PROJECT",
    "DB_SECRET_VERSION",
    "CLOUD_SQL_CONNECTION_NAME",
    "LOG_LEVEL",
    "PORT",
    "K_SERVICE",
    "K_REVISION",
    "K_CONFIGURATION",
)
EXPORTS_PREFIX = "services/exports"


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

    for optional_field in ("source_object", "export_date"):
        value = payload.get(optional_field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            return None, f"{optional_field} must be a non-empty string when provided"

    source_object = payload.get("source_object")
    if source_object and source_object.count("*") > 1:
        return None, "source_object may contain at most one '*' wildcard"

    export_date = payload.get("export_date")
    if export_date and not EXPORT_DATE_PATTERN.match(export_date.strip()):
        return None, "export_date must be in YYYYMMDD format"

    return {**payload, "format": export_format}, None


def resolve_source_object(storage_client, payload):
    """Return the object pattern to import.

    Uses source_object when given; otherwise the bq_to_gcs export folder
    services/exports/<table_id>/<export_date or latest YYYYMMDD>/part_*.<ext>.
    """
    if payload.get("source_object"):
        return payload["source_object"]

    table_id = payload["table_id"]
    extension = FORMAT_EXTENSIONS[payload["format"]]
    table_prefix = f"{EXPORTS_PREFIX}/{table_id}/"

    export_date = (payload.get("export_date") or "").strip()
    if not export_date:
        part_pattern = re.compile(
            re.escape(table_prefix) + r"(\d{8})/part_[^/]*\." + re.escape(extension) + "$"
        )
        export_dates = {
            match.group(1)
            for blob in storage_client.list_blobs(payload["source_bucket"], prefix=table_prefix)
            if (match := part_pattern.match(blob.name))
        }
        if not export_dates:
            raise ValueError(
                f"no {payload['format']} exports found under "
                f"gs://{payload['source_bucket']}/{table_prefix}"
            )
        export_date = max(export_dates)
        logger.info("Using latest export folder %s%s/", table_prefix, export_date)

    return f"{table_prefix}{export_date}/part_*.{extension}"


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


def get_schema_object_name(payload):
    """Schema file written by bq_to_gcs next to the table's export folders."""
    override = payload.get("schema_object")
    if isinstance(override, str) and override.strip():
        return override.strip()
    table_id = payload["table_id"]
    return f"{EXPORTS_PREFIX}/{table_id}/{table_id}_schema.json"


def load_schema(storage_client, payload):
    """Return (schema, source) from the exported schema.json, else BigQuery."""
    bucket_name = payload["source_bucket"]
    schema_object = get_schema_object_name(payload)
    blob = storage_client.bucket(bucket_name).get_blob(schema_object)
    if blob is not None:
        fields = json.loads(blob.download_as_text())
        if not isinstance(fields, list):
            raise ValueError(f"gs://{bucket_name}/{schema_object} must contain a JSON list of fields")
        logger.info("Using exported schema gs://%s/%s", bucket_name, schema_object)
        return (
            [bigquery.SchemaField.from_api_repr(field) for field in fields],
            f"gs://{bucket_name}/{schema_object}",
        )

    source_table_id = (
        f"{payload['source_project']}.{payload['dataset_id']}.{payload['table_id']}"
    )
    logger.warning(
        "Schema file gs://%s/%s not found; reading schema from BigQuery table %s",
        bucket_name,
        schema_object,
        source_table_id,
    )
    source_table = bigquery.Client(project=payload["source_project"]).get_table(source_table_id)
    return source_table.schema, f"bigquery:{source_table_id}"


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


def read_secret(name, project, version="latest"):
    """Return a Secret Manager secret value using Application Default Credentials."""
    if name.startswith("projects/"):
        resource = name.split("/versions/")[0]
    elif project:
        resource = f"projects/{project}/secrets/{name}"
    else:
        raise ValueError("DB_SECRET_PROJECT is required unless DB_SECRET_NAME is a full projects/... name")
    credentials, _ = google.auth.default(scopes=[CLOUD_PLATFORM_SCOPE])
    response = AuthorizedSession(credentials).get(
        f"https://secretmanager.googleapis.com/v1/{resource}/versions/{version}:access",
        timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"Secret Manager HTTP {response.status_code}: {response.text[:200]}"
        )
    return base64.b64decode(response.json()["payload"]["data"]).decode("utf-8").strip()


def get_access_token():
    """Return an OAuth access token for the runtime identity (IAM database auth)."""
    credentials, _ = google.auth.default(scopes=[CLOUD_PLATFORM_SCOPE])
    credentials.refresh(AuthRequest())
    return credentials.token


def get_database_password():
    password = os.environ.get("DB_PASSWORD", "")
    if password:
        return password
    secret_name = os.environ.get("DB_SECRET_NAME", "").strip()
    if secret_name:
        return read_secret(
            secret_name,
            os.environ.get("DB_SECRET_PROJECT", "").strip(),
            os.environ.get("DB_SECRET_VERSION", "").strip() or "latest",
        )
    return get_access_token()


def get_database_connection():
    settings = {
        name: os.environ.get(name, "").strip()
        for name in ("DB_HOST", "DB_NAME", "DB_USER")
    }
    missing = [name for name, value in settings.items() if not value]
    if missing:
        raise ValueError(f"missing required environment variable(s): {', '.join(missing)}")
    return pg8000.connect(
        host=settings["DB_HOST"],
        port=int(os.environ.get("DB_PORT", "5432")),
        database=settings["DB_NAME"],
        user=settings["DB_USER"],
        password=get_database_password(),
        timeout=float(os.environ.get("DB_CONNECT_TIMEOUT_SECONDS", "15")),
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


def get_password_source():
    if os.environ.get("DB_PASSWORD", ""):
        return "DB_PASSWORD"
    if os.environ.get("DB_SECRET_NAME", "").strip():
        return "secret_manager"
    return "access_token"


def check_database_connection(*, include_environment: bool = False):
    target: dict[str, object] = {
        "host": os.environ.get("DB_HOST", "").strip(),
        "port": os.environ.get("DB_PORT", "5432").strip(),
        "database": os.environ.get("DB_NAME", "").strip(),
        "user": os.environ.get("DB_USER", "").strip(),
        "password_source": get_password_source(),
    }
    if include_environment:
        target["environment"] = {
            **{name: os.environ.get(name) for name in DEBUG_ENVIRONMENT_KEYS},
            "DB_PASSWORD_SET": bool(os.environ.get("DB_PASSWORD")),
        }
    try:
        with closing(get_database_connection()) as connection:
            cursor = connection.cursor()
            cursor.execute("SELECT session_user")
            session_user = cursor.fetchone()[0]
    except Exception as exc:
        logger.exception("PostgreSQL connection test failed")
        error = (
            f"PostgreSQL connection test failed ({type(exc).__name__}); see service logs"
            if include_environment
            else str(exc)
        )
        return jsonify({"status": "failed", **target, "error": error}), 500
    return jsonify({"status": "connected", **target, "session_user": session_user}), 200


def import_gcs_to_postgresql(request: Request):
    if request.path.rstrip("/") == CONNECTION_TEST_PATH:
        return check_database_connection()

    body = request.get_json(silent=True)
    if isinstance(body, dict) and "action" in body:
        if body["action"] == "debug":
            return check_database_connection(include_environment=True)
        if body["action"] != "import":
            return jsonify({"error": "unsupported action; expected debug or import"}), 400

    payload, validation_error = get_request_payload(request)
    if validation_error:
        return jsonify({"error": validation_error}), 400

    table_name = (
        f"parq_{payload['table_id']}"
        if payload["format"] == "PARQUET"
        else payload["table_id"]
    )
    schema_name = os.environ.get("DB_SCHEMA", "public").strip()
    if not schema_name:
        return jsonify({"error": "DB_SCHEMA must not be empty"}), 500
    if len(schema_name.encode("utf-8")) > 63 or len(table_name.encode("utf-8")) > 63:
        return jsonify(
            {"error": "destination schema and table names must be at most 63 bytes"}
        ), 400
    connection = None
    try:
        storage_client = storage.Client(project=payload["source_project"])
        payload = {**payload, "source_object": resolve_source_object(storage_client, payload)}
        logger.info(
            "Loading schema before importing %s objects from gs://%s/%s",
            payload["format"],
            payload["source_bucket"],
            payload["source_object"],
        )
        schema, schema_source = load_schema(storage_client, payload)
        columns = get_table_columns(schema)
        column_names = [field.name for field in schema]

        blobs = get_matching_blobs(
            storage_client,
            payload["source_bucket"],
            payload["source_object"],
        )
        if not blobs:
            raise ValueError(
                "no GCS objects match "
                f"gs://{payload['source_bucket']}/"
                f"{payload['source_object']}"
            )

        connection = get_database_connection()
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
                        f"gs://{payload['source_bucket']}/"
                        f"{payload['source_object']}"
                    ),
                    "imported": (
                        f"{payload['source_project']}:{payload['dataset_id']}."
                        f"{payload['table_id']}"
                    ),
                    "table": f"{schema_name}.{table_name}",
                    "schema_source": schema_source,
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


if __name__ == "__main__":
    from flask import Flask, request

    app = Flask(__name__)

    @app.route("/", methods=["POST"])
    @app.route(CONNECTION_TEST_PATH, methods=["GET", "POST"])
    def _root():
        return import_gcs_to_postgresql(request)

    app.run("0.0.0.0", port=int(os.environ.get("PORT", 8080)))
