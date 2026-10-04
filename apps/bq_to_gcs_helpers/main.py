from google.cloud import bigquery
from flask import Request, jsonify
import logging
import os
import time

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def export_table_to_gcs(request: Request):
    """
    HTTP Cloud Function to export a BigQuery table to GCS.

    Accepts JSON body or query params (or environment variables):
    - source_project
    - dataset_id
    - table_id
    - destination_bucket
    - destination_object (optional; default: <table_id>.csv)
    - location (optional; default: US)
    - format (optional; CSV|NEWLINE_DELIMITED_JSON|PARQUET)
    - compression (optional; BigQuery extract compression, such as SNAPPY)
    """
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
        destination_object = get("destination_object", f"{table_id}.csv")
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

        destination_uri = f"gs://{destination_bucket}/{destination_object}"
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

        job_config = bigquery.job.ExtractJobConfig()
        if fmt == "CSV":
            job_config.destination_format = "CSV"
            job_config.print_header = True
        elif fmt in ("NEWLINE_DELIMITED_JSON", "NDJSON", "JSON"):
            job_config.destination_format = "NEWLINE_DELIMITED_JSON"
        elif fmt == "PARQUET":
            job_config.destination_format = "PARQUET"
        else:
            logger.warning("Export request rejected: unsupported format %s", fmt)
            return jsonify({"error": "unsupported format"}), 400

        if compression:
            job_config.compression = compression.strip().upper()

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

        return (
            jsonify(
                {
                    "status": "success",
                    "exported": f"{source_project}:{dataset_id}.{table_id}",
                    "destination": destination_uri,
                }
            ),
            200,
        )

    except Exception as exc:
        logger.exception("Export failed")
        return jsonify({"error": str(exc)}), 500


if __name__ == "__main__":
    # Local debug sample using Flask's development server via functions-framework
    from flask import Flask, request

    app = Flask(__name__)

    @app.route("/", methods=["GET", "POST"])
    def _root():
        return export_table_to_gcs(request)

    app.run("127.0.0.1", port=int(os.environ.get("PORT", 8080)))
