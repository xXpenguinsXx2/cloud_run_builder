import json
import fnmatch
import os
import re
import shlex
import sys
import tempfile
from pathlib import Path

import google.auth
from google.api_core.exceptions import GoogleAPIError
from google.auth.exceptions import GoogleAuthError
from google.cloud import bigquery
from google.cloud import storage
import pyarrow.parquet as parquet
from psycopg2 import sql
from psycopg2.extras import execute_values
import psycopg2
import requests

EXPORT_FIELDS = {
    "source_project": "SMOKE_TEST_SOURCE_PROJECT",
    "dataset_id": "SMOKE_TEST_DATASET_ID",
    "table_id": "SMOKE_TEST_TABLE_ID",
    "destination_bucket": "SMOKE_TEST_DESTINATION_BUCKET",
    "destination_object": "SMOKE_TEST_DESTINATION_OBJECT",
    "location": "SMOKE_TEST_LOCATION",
    "format": "SMOKE_TEST_FORMAT",
    "compression": "SMOKE_TEST_COMPRESSION",
}
OPTIONAL_EXPORT_FIELDS = ("compression",)
REQUIRED_EXPORT_FIELDS = (
    "source_project",
    "dataset_id",
    "table_id",
    "destination_bucket",
)
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
PARQUET_FDW_TYPES = {**POSTGRES_TYPES, "TIMESTAMP": "TIMESTAMP"}


def response_body(response):
    try:
        return response.json()
    except ValueError:
        return response.text


def find_matching_export_blobs(storage_client, bucket_name, object_pattern):
    prefix = object_pattern.split("*", 1)[0]
    matching_blobs = [
        blob
        for blob in storage_client.list_blobs(bucket_name, prefix=prefix)
        if (
            fnmatch.fnmatchcase(blob.name, object_pattern)
            if "*" in object_pattern
            else blob.name == object_pattern
        )
    ]
    return sorted(matching_blobs, key=lambda blob: blob.name)


def run_validation_smoke_test(function_url, timeout_seconds):
    print(f"Checking local function readiness at {function_url.rstrip('/')}/")
    response = requests.post(
        f"{function_url.rstrip('/')}/", json={}, timeout=timeout_seconds
    )
    body = response_body(response)
    expected_error = "missing required parameter(s)"

    if (
        response.status_code != 400
        or not isinstance(body, dict)
        or expected_error not in body.get("error", "")
    ):
        print(
            f"FAIL: expected HTTP 400 with the missing-parameters error; "
            f"received HTTP {response.status_code}: {body}"
        )
        return 1

    print("PASS: function is reachable and validates missing export parameters.")
    return 0


def load_export_request():
    payload_path = Path(
        os.environ.get(
            "SMOKE_TEST_REQUEST_FILE",
            str(Path(__file__).with_name("export-request.json")),
        )
    )
    try:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"FAIL: could not load export request JSON from {payload_path}: {exc}")
        return 2

    if not isinstance(payload, dict):
        print(f"FAIL: export request JSON in {payload_path} must be an object.")
        return 2

    for key, setting in EXPORT_FIELDS.items():
        value = os.environ.get(setting, "").strip()
        if value:
            payload[key] = value

    invalid_fields = [
        key
        for key in EXPORT_FIELDS
        if key not in OPTIONAL_EXPORT_FIELDS
        if not isinstance(payload.get(key), str) or not payload[key].strip()
    ]
    invalid_fields.extend(
        key
        for key in OPTIONAL_EXPORT_FIELDS
        if key in payload
        and (not isinstance(payload[key], str) or not payload[key].strip())
    )
    invalid_fields.extend(
        key
        for key in REQUIRED_EXPORT_FIELDS
        if isinstance(payload.get(key), str)
        and payload[key].startswith("REPLACE_WITH_")
    )
    if invalid_fields:
        print(
            "FAIL: set these string fields in "
            f"{payload_path} before export mode: "
            + ", ".join(dict.fromkeys(invalid_fields))
        )
        return 2

    unexpected_fields = sorted(set(payload) - set(EXPORT_FIELDS))
    if unexpected_fields:
        print("FAIL: unexpected export request fields: " + ", ".join(unexpected_fields))
        return 2

    print("Export request JSON:")
    print(json.dumps(payload, indent=2))
    return payload


def run_export_smoke_test(function_url, timeout_seconds, request_headers=None):
    payload = load_export_request()
    if payload is None:
        return 2

    try:
        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        client = bigquery.Client(
            project=payload["source_project"], credentials=credentials
        )

        dataset_id = f"{payload['source_project']}.{payload['dataset_id']}"
        table_id = f"{dataset_id}.{payload['table_id']}"

        print(f"Checking BigQuery dataset: {dataset_id}", flush=True)
        dataset = client.get_dataset(dataset_id)
        print(f"PASS: dataset is reachable in location {dataset.location}.", flush=True)

        if dataset.location.casefold() != payload["location"].casefold():
            print(
                f"FAIL: request location {payload['location']} does not match "
                f"dataset location {dataset.location}."
            )
            return 2

        print(f"Checking BigQuery table: {table_id}", flush=True)
        table = client.get_table(table_id)
        print(
            f"PASS: table is reachable (type={table.table_type}, "
            f"rows={table.num_rows}).",
            flush=True,
        )

        identity_job = client.query("SELECT SESSION_USER() AS principal")
        principal = next(identity_job.result(timeout=timeout_seconds)).principal
    except (GoogleAuthError, GoogleAPIError, StopIteration) as exc:
        print(f"FAIL: Google authentication/BigQuery preflight failed: {exc}")
        if isinstance(exc, GoogleAuthError):
            print("Run `make auth-google-adc` and try the export smoke test again.")
        return 2

    print(f"Google Cloud authenticated as: {principal}")
    print(f"BigQuery project: {payload['source_project']}")
    print(f"Calling export function at {function_url.rstrip('/')}/")

    response = requests.post(
        f"{function_url.rstrip('/')}/",
        json=payload,
        timeout=timeout_seconds,
        headers=request_headers,
    )
    body = response_body(response)
    object_pattern = payload.get("destination_object", f"{payload['table_id']}.csv")
    expected_destination = (
        f"gs://{payload['destination_bucket']}/"
        f"{object_pattern}"
    )

    if (
        response.status_code != 200
        or not isinstance(body, dict)
        or body.get("status") != "success"
        or body.get("destination") != expected_destination
    ):
        print(f"FAIL: export request returned HTTP {response.status_code}: {body}")
        return 1

    print(
        f"PASS: function reports BigQuery extract completed for {body['exported']} "
        f"to {body['destination']} as {principal}."
    )

    try:
        storage_client = storage.Client(
            project=payload["source_project"], credentials=credentials
        )
        matching_blobs = find_matching_export_blobs(
            storage_client, payload["destination_bucket"], object_pattern
        )
    except (GoogleAuthError, GoogleAPIError) as exc:
        print(f"FAIL: could not verify exported GCS objects: {exc}")
        return 2

    if not matching_blobs:
        print(
            "FAIL: no GCS objects match "
            f"gs://{payload['destination_bucket']}/{object_pattern}"
        )
        return 1

    print(
        f"PASS: found {len(matching_blobs)} exported object(s) at "
        f"gs://{payload['destination_bucket']}/: "
        + ", ".join(blob.name for blob in matching_blobs)
    )
    return 0


def run_local_export_smoke_test(function_url, timeout_seconds):
    readiness_result = run_validation_smoke_test(function_url, timeout_seconds)
    if readiness_result != 0:
        return readiness_result

    return run_export_smoke_test(function_url, timeout_seconds)


def run_import_smoke_test():
    payload = load_export_request()
    if payload is None:
        return 2

    export_format = payload["format"].strip().upper()
    if export_format not in ("CSV", "PARQUET"):
        print("FAIL: GCS-to-PostgreSQL import supports CSV and PARQUET exports.")
        return 2

    object_pattern = payload["destination_object"]
    if object_pattern.count("*") > 1:
        print("FAIL: destination_object may contain at most one '*' wildcard.")
        return 2

    project_id = payload["source_project"]
    dataset_id = payload["dataset_id"]
    table_id = payload["table_id"]
    postgres_table_id = f"parq_{table_id}" if export_format == "PARQUET" else table_id
    dataset_ref = f"{project_id}.{dataset_id}"
    source_table_ref = f"{dataset_ref}.{table_id}"
    postgres_schema = os.environ.get("SMOKE_TEST_POSTGRES_SCHEMA", "public")

    try:
        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        bigquery_client = bigquery.Client(project=project_id, credentials=credentials)
        print(f"Checking BigQuery dataset schema: {dataset_ref}", flush=True)
        dataset = bigquery_client.get_dataset(dataset_ref)
        if dataset.location.casefold() != payload["location"].casefold():
            raise ValueError(
                f"request location {payload['location']} does not match "
                f"dataset location {dataset.location}"
            )

        print(f"Checking BigQuery table schema: {source_table_ref}", flush=True)
        source_table = bigquery_client.get_table(source_table_ref)
        print(
            f"PASS: source schema loaded ({len(source_table.schema)} columns).",
            flush=True,
        )

        columns = []
        for field in source_table.schema:
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
                sql.SQL("{} {}{}").format(
                    sql.Identifier(field.name), sql.SQL(postgres_type), sql.SQL(not_null)
                )
            )

        storage_client = storage.Client(project=project_id, credentials=credentials)
        bucket_name = payload["destination_bucket"]
        print(
            f"Looking for GCS objects: gs://{bucket_name}/{object_pattern}",
            flush=True,
        )
        matching_blobs = find_matching_export_blobs(
            storage_client, bucket_name, object_pattern
        )
        if not matching_blobs:
            raise ValueError(
                f"no GCS objects match gs://{bucket_name}/{object_pattern}"
            )
        print(
            f"PASS: found {len(matching_blobs)} {export_format} object(s): "
            + ", ".join(blob.name for blob in matching_blobs),
            flush=True,
        )
        postgres_host = os.environ.get("SMOKE_TEST_POSTGRES_HOST", "postgres")
        postgres_port = int(os.environ.get("SMOKE_TEST_POSTGRES_PORT", "5432"))
        postgres_db = os.environ.get("SMOKE_TEST_POSTGRES_DB", "testdb")
        postgres_user = os.environ.get("SMOKE_TEST_POSTGRES_USER", "testuser")
        postgres_password = os.environ.get("SMOKE_TEST_POSTGRES_PASSWORD", "testpass")
        print(
            f"Connecting to PostgreSQL {postgres_host}:{postgres_port}/{postgres_db}",
            flush=True,
        )
        connection = psycopg2.connect(
            host=postgres_host,
            port=postgres_port,
            dbname=postgres_db,
            user=postgres_user,
            password=postgres_password,
        )

        identifier = sql.Identifier(postgres_schema, postgres_table_id)
        column_names = sql.SQL(", ").join(
            sql.Identifier(field.name) for field in source_table.schema
        )
        create_table = sql.SQL("CREATE TABLE {} ({})").format(
            identifier, sql.SQL(", ").join(columns)
        )
        drop_table = sql.SQL("DROP TABLE IF EXISTS {}").format(identifier)
        copy_command = sql.SQL(
            "COPY {} ({}) FROM STDIN WITH (FORMAT CSV, HEADER TRUE)"
        ).format(identifier, column_names)
        insert_command = sql.SQL("INSERT INTO {} ({}) VALUES %s").format(
            identifier, column_names
        ).as_string(connection)
        external_table_id = (
            f"ext_obj_parq_{table_id}"
            if export_format == "PARQUET"
            else f"ext_obj_csv_{table_id}"
        )
        external_identifier = sql.Identifier(postgres_schema, external_table_id)
        external_types = (
            PARQUET_FDW_TYPES if export_format == "PARQUET" else POSTGRES_TYPES
        )
        external_columns = []
        for field in source_table.schema:
            field_type = field.field_type.upper()
            postgres_type = external_types.get(field_type)
            if postgres_type is None:
                raise ValueError(
                    f"field {field.name} with type {field_type} is not supported "
                    f"by the {export_format} foreign table"
                )
            not_null = " NOT NULL" if field.mode == "REQUIRED" else ""
            external_columns.append(
                sql.SQL("{} {}{}").format(
                    sql.Identifier(field.name), sql.SQL(postgres_type), sql.SQL(not_null)
                )
            )
        external_column_definitions = sql.SQL(", ").join(external_columns)
        mount_root = Path(os.environ.get("SMOKE_TEST_GCS_MOUNT", "/mnt/gcs"))
        if export_format == "CSV":
            if not re.fullmatch(r"[A-Za-z0-9_./*-]+", object_pattern) or any(
                part == ".." for part in object_pattern.split("/")
            ):
                raise ValueError(
                    "CSV destination_object contains unsupported path characters"
                )
            file_glob = str(mount_root / object_pattern)
            shell_glob = "*".join(
                shlex.quote(part) for part in file_glob.split("*")
            )
            csv_program = (
                "awk 'FNR == 1 && NR > 1 { next } { print }' " + shell_glob
            )
            create_external_table = sql.SQL(
                "CREATE FOREIGN TABLE {} ({}) SERVER csv_file_server "
                "OPTIONS (program {}, format 'csv', header 'true')"
            ).format(
                external_identifier,
                external_column_definitions,
                sql.Literal(csv_program),
            )
        else:
            parquet_paths = []
            for blob in matching_blobs:
                object_path = Path(blob.name)
                if ".." in object_path.parts or " " in blob.name:
                    raise ValueError(
                        f"Parquet object name cannot be used by parquet_fdw: {blob.name}"
                    )
                parquet_paths.append(str(mount_root / object_path))
            create_external_table = sql.SQL(
                "CREATE FOREIGN TABLE {} ({}) SERVER parquet_server "
                "OPTIONS (filename {})"
            ).format(
                external_identifier,
                external_column_definitions,
                sql.Literal(" ".join(parquet_paths)),
            )

        imported_rows = 0
        try:
            with connection:
                with connection.cursor() as cursor:
                    cursor.execute("CREATE EXTENSION IF NOT EXISTS file_fdw")
                    cursor.execute("CREATE EXTENSION IF NOT EXISTS parquet_fdw")
                    cursor.execute(
                        "CREATE SERVER IF NOT EXISTS csv_file_server "
                        "FOREIGN DATA WRAPPER file_fdw"
                    )
                    cursor.execute(
                        "CREATE SERVER IF NOT EXISTS parquet_server "
                        "FOREIGN DATA WRAPPER parquet_fdw"
                    )
                    cursor.execute(drop_table)
                    cursor.execute(create_table)
                    cursor.execute(
                        sql.SQL("DROP FOREIGN TABLE IF EXISTS {}").format(
                            external_identifier
                        )
                    )
                    cursor.execute(create_external_table)
                    for blob in matching_blobs:
                        print(f"Importing gs://{bucket_name}/{blob.name}", flush=True)
                        if export_format == "CSV":
                            with blob.open("rb") as csv_stream:
                                cursor.copy_expert(
                                    copy_command.as_string(connection), csv_stream
                                )
                            imported_rows += cursor.rowcount
                        else:
                            with tempfile.TemporaryDirectory() as temp_directory:
                                parquet_path = (
                                    Path(temp_directory) / Path(blob.name).name
                                )
                                blob.download_to_filename(str(parquet_path))
                                parquet_file = parquet.ParquetFile(parquet_path)
                                for record_batch in parquet_file.iter_batches(
                                    batch_size=1000
                                ):
                                    rows = [
                                        tuple(record[field.name] for field in source_table.schema)
                                        for record in record_batch.to_pylist()
                                    ]
                                    if rows:
                                        execute_values(
                                            cursor,
                                            insert_command,
                                            rows,
                                            page_size=1000,
                                        )
                                        imported_rows += len(rows)
                    cursor.execute(
                        sql.SQL("SELECT COUNT(*) FROM {}").format(identifier)
                    )
                    table_rows = cursor.fetchone()[0]
                    cursor.execute(
                        sql.SQL("SELECT COUNT(*) FROM {}").format(
                            external_identifier
                        )
                    )
                    external_table_rows = cursor.fetchone()[0]
        finally:
            connection.close()

        print(
            f"PASS: imported {imported_rows} row(s) into "
            f"PostgreSQL {postgres_schema}.{postgres_table_id}; "
            f"verified {table_rows} row(s)."
        )
        print(
            f"PASS: foreign table {postgres_schema}.{external_table_id} "
            f"reads {external_table_rows} row(s) directly from the GCS mount."
        )
        return 0
    except Exception as exc:
        print(f"FAIL: GCS-to-PostgreSQL import failed: {exc}")
        return 1


def main():
    function_url = os.environ.get("FUNCTION_URL", "http://localhost:8080")
    mode = os.environ.get("SMOKE_TEST_MODE", "validation").strip().lower()

    try:
        timeout_seconds = float(os.environ.get("SMOKE_TEST_TIMEOUT_SECONDS", "600"))
        if mode == "validation":
            return run_validation_smoke_test(function_url, timeout_seconds)
        if mode == "export":
            return run_export_smoke_test(function_url, timeout_seconds)
        if mode == "local-export":
            return run_local_export_smoke_test(function_url, timeout_seconds)
        if mode == "import":
            return run_import_smoke_test()
        print(
            "FAIL: SMOKE_TEST_MODE must be 'validation', 'export', "
            "'local-export', or 'import'."
        )
        return 2
    except requests.RequestException as exc:
        print(f"FAIL: could not call the function at {function_url}: {exc}")
        return 1
    except ValueError as exc:
        print(f"FAIL: invalid timeout setting: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())