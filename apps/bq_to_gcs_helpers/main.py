from google.cloud import bigquery, storage
from google.api_core.exceptions import PreconditionFailed
from flask import Request, jsonify
from datetime import datetime, timezone
import csv
import io
import json
import logging
import os
import time

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


HISTORY_OBJECT = "services/history/export_history.csv"
LEGACY_HISTORY_OBJECT = "history/export_history.csv"
HISTORY_HEADER = [
    "exported_table",
    "destination",
    "finished_at_utc",
    "status",
    "source_modified_time",
]
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def get_history_blob(bucket):
    """Read the new history object, falling back to its former location."""
    blob = bucket.get_blob(HISTORY_OBJECT)
    return blob if blob is not None else bucket.get_blob(LEGACY_HISTORY_OBJECT)


def append_history(bucket, row, attempts=5):
    """Append a row (dict keyed by HISTORY_HEADER) to the CSV history object.
    GCS objects can't be appended to, so the file is rewritten guarded by a
    generation precondition. Older files with fewer columns are upgraded; rows
    without a status are treated as successful exports."""
    blob = bucket.blob(HISTORY_OBJECT)
    for _ in range(attempts):
        existing = bucket.get_blob(HISTORY_OBJECT)
        generation = 0 if existing is None else existing.generation
        if existing is None:
            existing = bucket.get_blob(LEGACY_HISTORY_OBJECT)
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=HISTORY_HEADER, lineterminator="\n")
        writer.writeheader()
        if existing is not None:
            for old in csv.DictReader(io.StringIO(existing.download_as_text())):
                normalized = {k: old.get(k) or "" for k in HISTORY_HEADER}
                normalized["status"] = normalized["status"] or "success"
                writer.writerow(normalized)
        writer.writerow({k: row.get(k, "") for k in HISTORY_HEADER})
        try:
            blob.upload_from_string(
                out.getvalue(), content_type="text/csv", if_generation_match=generation
            )
            return f"gs://{bucket.name}/{HISTORY_OBJECT}"
        except PreconditionFailed:
            continue
    raise RuntimeError("could not update export history after concurrent writes")


EPOCH_DEFAULT = datetime(1900, 1, 1, tzinfo=timezone.utc)


def get_latest_extract(bucket, exported_table):
    """Return (finished_at, destination_uri) of the latest *successful* export
    for exported_table, or (1900-01-01, None) if there is none."""
    blob = get_history_blob(bucket)
    if blob is None:
        return EPOCH_DEFAULT, None
    latest, destination = EPOCH_DEFAULT, None
    for row in csv.DictReader(io.StringIO(blob.download_as_text())):
        if row.get("exported_table") != exported_table:
            continue
        if (row.get("status") or "success") != "success":
            continue
        try:
            finished = datetime.strptime(
                row["finished_at_utc"], TIME_FORMAT
            ).replace(tzinfo=timezone.utc)
        except (KeyError, ValueError, TypeError):
            continue
        if finished >= latest:
            latest, destination = finished, row.get("destination")
    return latest, destination


def get_latest_extract_time(bucket, exported_table):
    """Latest finished_at_utc for exported_table in the history CSV, or
    1900-01-01 if the file or a matching row doesn't exist."""
    return get_latest_extract(bucket, exported_table)[0]


def get_source_modified_time(client, project, dataset_id, table_id, location, table):
    """Latest modification time of the source table from the dataset's
    INFORMATION_SCHEMA.PARTITIONS (falls back to table metadata if empty)."""
    query = (
        f"SELECT MAX(last_modified_time) AS modified "
        f"FROM `{project}.{dataset_id}.INFORMATION_SCHEMA.PARTITIONS` "
        f"WHERE table_name = @table_name"
    )
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("table_name", "STRING", table_id)]
    )
    rows = list(client.query(query, location=location, job_config=job_config).result())
    modified = rows[0]["modified"] if rows else None
    if modified is None:
        modified = table.modified
    if modified.tzinfo is None:
        modified = modified.replace(tzinfo=timezone.utc)
    return modified


def export_table_to_gcs(request: Request):
    """
    HTTP Cloud Function to export a BigQuery table to GCS.

    Accepts JSON body or query params (or environment variables):
    - source_project
    - dataset_id
    - table_id
    - destination_bucket
    - location (optional; default: US)
    - format (optional; CSV|NEWLINE_DELIMITED_JSON|PARQUET)
    - compression (optional; BigQuery extract compression, such as SNAPPY)

    Files are written to gs://<destination_bucket>/services/exports/<table_id>/<YYYYMMDD>/part_*.<ext>
    and the schema to gs://<destination_bucket>/services/exports/<table_id>/<table_id>_schema.json.
    """
    ctx = {}
    try:
        logger.info("Export request received")
        request_json = request.get_json(silent=True) or {}
        args = request.args or {}

        def get(k, default=None):
            return request_json.get(k) or args.get(k) or os.environ.get(k.upper(), default)

        source_project = get("source_project")
        dataset_id = get("dataset_id")
        table_id = get("table_id")
        destination_bucket = get("destination_bucket")
        location = get("location", "US")
        fmt = get("format", "CSV").strip().upper()
        compression = get("compression")

        if not all([source_project, dataset_id, table_id, destination_bucket]):
            logger.warning("Export request rejected: missing required parameters")
            return (
                jsonify(
                    {
                        "error": "missing required parameter(s). required: source_project,dataset_id,table_id,destination_bucket"
                    }
                ),
                400,
            )

        job_config = bigquery.job.ExtractJobConfig()
        if fmt == "CSV":
            job_config.destination_format = "CSV"
            job_config.print_header = True
            extension = "csv"
        elif fmt in ("NEWLINE_DELIMITED_JSON", "NDJSON", "JSON"):
            job_config.destination_format = "NEWLINE_DELIMITED_JSON"
            extension = "json"
        elif fmt == "PARQUET":
            job_config.destination_format = "PARQUET"
            extension = "parquet"
        else:
            logger.warning("Export request rejected: unsupported format %s", fmt)
            return jsonify({"error": "unsupported format"}), 400

        # gs://<bucket>/services/exports/<table_id>/<YYYYMMDD (UTC)>/part_*.<format>
        export_date = datetime.now(timezone.utc).strftime("%Y%m%d")
        destination_uri = (
            f"gs://{destination_bucket}/services/exports/{table_id}/"
            f"{export_date}/part_*.{extension}"
        )
        logger.info(
            "Preparing export: source=%s:%s.%s destination=%s location=%s format=%s compression=%s",
            source_project,
            dataset_id,
            table_id,
            destination_uri,
            location,
            fmt,
            compression or "default",
        )

        client = bigquery.Client(project=source_project)

        dataset_ref = bigquery.DatasetReference(source_project, dataset_id)
        table_ref = dataset_ref.table(table_id)

        if compression:
            job_config.compression = compression.strip().upper()

        storage_client = storage.Client(project=source_project)
        bucket = storage_client.bucket(destination_bucket)
        exported_table = f"{source_project}.{dataset_id}.{table_id}"
        ctx.update(bucket=bucket, exported_table=exported_table, destination=destination_uri)
        previous_time, previous_destination = get_latest_extract(bucket, exported_table)
        previous_extract_iso = previous_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        logger.info("Previous extract time for %s: %s", exported_table, previous_extract_iso)

        table = client.get_table(table_ref)
        source_modified = get_source_modified_time(
            client, source_project, dataset_id, table_id, location, table
        )
        source_modified_iso = source_modified.strftime(TIME_FORMAT)
        ctx["source_modified_time"] = source_modified_iso
        logger.info(
            "Source last modified time for %s: %s", exported_table, source_modified_iso
        )

        if previous_destination and previous_time >= source_modified:
            latest_folder = previous_destination.rsplit("/", 1)[0] + "/"
            logger.info(
                "No changes detected since %s; skipping export. Latest folder: %s",
                previous_extract_iso,
                latest_folder,
            )
            history_uri = append_history(
                bucket,
                {
                    "exported_table": exported_table,
                    "destination": previous_destination,
                    "finished_at_utc": datetime.now(timezone.utc).strftime(TIME_FORMAT),
                    "status": "skipped",
                    "source_modified_time": source_modified_iso,
                },
            )
            return (
                jsonify(
                    {
                        "status": "success",
                        "message": f"no changes detected since last_extract_time {previous_extract_iso}",
                        "exported": f"{source_project}:{dataset_id}.{table_id}",
                        "export_skipped": True,
                        "last_extract_time": previous_extract_iso,
                        "source_modified_time": source_modified_iso,
                        "latest_folder": latest_folder,
                        "history": history_uri,
                    }
                ),
                200,
            )

        schema_object = (
            f"services/exports/{table_id}/{table_id}_schema.json"
        )
        bucket.blob(schema_object).upload_from_string(
            json.dumps([f.to_api_repr() for f in table.schema], indent=2),
            content_type="application/json",
        )
        schema_uri = f"gs://{destination_bucket}/{schema_object}"
        logger.info("Schema written: %s", schema_uri)

        logger.info("Submitting BigQuery extract job")
        extract_job = client.extract_table(
            table_ref, destination_uri, location=location, job_config=job_config
        )
        logger.info("BigQuery extract job submitted: job_id=%s", extract_job.job_id)

        poll_interval = max(1.0, float(os.environ.get("EXPORT_JOB_POLL_SECONDS", "5")))
        timeout_seconds = max(
            poll_interval,
            float(os.environ.get("EXPORT_JOB_TIMEOUT_SECONDS", "540")),
        )
        started_at = time.monotonic()
        while True:
            extract_job.reload()
            if extract_job.state == "DONE":
                break

            elapsed = time.monotonic() - started_at
            if elapsed >= timeout_seconds:
                raise TimeoutError(
                    f"BigQuery extract job {extract_job.job_id} did not finish "
                    f"within {timeout_seconds:g} seconds"
                )

            logger.info(
                "BigQuery extract job still running: job_id=%s state=%s elapsed=%.0fs",
                extract_job.job_id,
                extract_job.state,
                elapsed,
            )
            time.sleep(min(poll_interval, timeout_seconds - elapsed))

        extract_job.result()
        logger.info(
            "BigQuery extract job completed: job_id=%s destination=%s",
            extract_job.job_id,
            destination_uri,
        )

        finished_iso = datetime.now(timezone.utc).strftime(TIME_FORMAT)
        history_uri = append_history(
            bucket,
            {
                "exported_table": exported_table,
                "destination": destination_uri,
                "finished_at_utc": finished_iso,
                "status": "success",
                "source_modified_time": source_modified_iso,
            },
        )
        logger.info("Export history updated: %s", history_uri)

        return (
            jsonify(
                {
                    "status": "success",
                    "exported": f"{source_project}:{dataset_id}.{table_id}",
                    "destination": destination_uri,
                    "latest_folder": destination_uri.rsplit("/", 1)[0] + "/",
                    "export_skipped": False,
                    "source_modified_time": source_modified_iso,
                    "schema": schema_uri,
                    "history": history_uri,
                    "last_extract_time": finished_iso,
                    "previous_extract_time": previous_extract_iso,
                }
            ),
            200,
        )

    except Exception as exc:
        logger.exception("Export failed")
        if ctx.get("bucket") is not None:
            try:
                append_history(
                    ctx["bucket"],
                    {
                        "exported_table": ctx["exported_table"],
                        "destination": ctx.get("destination", ""),
                        "finished_at_utc": datetime.now(timezone.utc).strftime(TIME_FORMAT),
                        "status": "failed",
                        "source_modified_time": ctx.get("source_modified_time", ""),
                    },
                )
            except Exception:
                logger.exception("Could not record failed export in history")
        return jsonify({"error": str(exc)}), 500


if __name__ == "__main__":
    # Local debug sample using Flask's development server via functions-framework
    from flask import Flask, request

    app = Flask(__name__)

    @app.route("/", methods=["GET", "POST"])
    def _root():
        return export_table_to_gcs(request)

    app.run("127.0.0.1", port=int(os.environ.get("PORT", 8080)))
