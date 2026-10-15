# BigQuery → GCS exporter

This private Cloud Run HTTP function exports a BigQuery table to a Cloud Storage bucket as CSV or Parquet. It supports a source table in a different GCP project than the project that hosts the service. `functions-framework` serves the handler over HTTP inside the container, the same pattern used by `apps/gcs_to_postgresql_helpers`.

Request fields (JSON body or query parameters):

- `source_project`, `dataset_id`, `table_id` — the BigQuery table to export.
- `destination_bucket` — the destination GCS bucket. There is no `destination_object`; files are always written to `gs://<destination_bucket>/services/exports/<table_id>/<YYYYMMDD>/part_*.<ext>` (UTC date; `<ext>` is `csv`, `json` or `parquet` by `format`).
- The table schema is also written to `gs://<destination_bucket>/services/exports/<table_id>/<table_id>_schema.json` (BigQuery field JSON), overwritten on each export. The caller needs `storage.objects.create` on the bucket (already required for the export).
- Before exporting, the service reads that history file to find the previous `finished_at_utc` for the same `project.dataset.table` (`1900-01-01T00:00:00Z` if there is no file or no matching row), then looks up the source table's latest modification time (`MAX(last_modified_time)` from the dataset's `INFORMATION_SCHEMA.PARTITIONS`, falling back to table metadata) and logs it. If the previous extract is not older than the source's modified time, **no export is started**: the call returns 200 with `export_skipped: true`, a "no changes detected" message, `last_extract_time`, `source_modified_time` and `latest_folder` (the bucket folder holding the latest export). Otherwise the export runs. The response returns `last_extract_time` (when this export finished), `previous_extract_time`, `source_modified_time` and `latest_folder`. The caller needs BigQuery job-create rights and `bigquery.tables.getData`/metadata access on the source dataset to query `INFORMATION_SCHEMA`.
- Every request that reaches the export step appends a row (`exported_table`, `destination`, `finished_at_utc`, `status`, `source_modified_time`) to `gs://<destination_bucket>/services/history/export_history.csv`, e.g. `proj.ds.tbl,gs://bucket/services/exports/tbl/20261014/part_*.csv,2026-10-14T15:20:00Z,success,2026-10-14T12:00:00Z`. Existing history at `gs://<destination_bucket>/history/export_history.csv` is read during the transition and copied to the new location on the next write. `status` is `success` (exported), `skipped` (source not modified since the last successful export; `destination` is the previous export and `finished_at_utc` is the check time) or `failed` (the service errored after the table identity was known; written best-effort and `destination` may be empty). `source_modified_time` is the source metadata the comparison used. Only `success` rows (or legacy rows without a status) count when finding the previous extract time; older 3-column files are upgraded in place on the next write. Because GCS objects can't be appended to, the file is rewritten with a generation precondition (retried on concurrent writes), so the caller also needs `storage.objects.get` and `storage.objects.create` (Storage Object Admin or equivalent) on the bucket. If the history update fails after a successful export the request returns 500.
- `location` — optional, defaults to `US`; must match the BigQuery dataset location.
- `format` — optional, defaults to `CSV`; also supports `PARQUET`/other BigQuery extract formats.
- `compression` — optional BigQuery extract compression (for example `SNAPPY`).

## Run locally with Docker

Build and start the container from this directory:

```powershell
docker build -t bigquery-gcs-export .
docker run --rm -p 8080:8080 bigquery-gcs-export
```

In another terminal, send a request without the required fields to check that the local HTTP handler responds. This smoke test does not call Google Cloud and should return HTTP 400:

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8080 -ContentType 'application/json' -Body '{}'
```

For a real export, authenticate locally and mount your Application Default Credentials into the container:

```powershell
gcloud auth application-default login
docker run --rm -p 8080:8080 `
  -v "$env:APPDATA\gcloud:/root/.config/gcloud:ro" `
  -e GOOGLE_APPLICATION_CREDENTIALS=/root/.config/gcloud/application_default_credentials.json `
  bigquery-gcs-export
```

Then POST the same JSON body shown in [Call the function](#call-the-function) to `http://localhost:8080`. The authenticated identity needs the BigQuery and Storage permissions listed below.

Handler-level unit tests (no Google Cloud access required) live in the root-level [smoke_tests/](../../smoke_tests/) directory and run via `make test` (see [repository README](../../README.md#local-wrapper-commands)); don't confuse that directory with [dev_test_apps/smoke_tests/](../../dev_test_apps/smoke_tests/), which holds the live local/remote export smoke test tooling documented in [dev_test_apps/README.md](../../dev_test_apps/README.md).

## Deploying

This app deploys the same way as every other app in `apps/`: with the root [Makefile](../../Makefile) locally, or automatically through the Cloud Build trigger described in the [repository README](../../README.md#cloud-build-trigger). There is no separate deploy script for this app.

Manual deploy from `cloud_run_builder`:

```bash
make deploy APP=bq_to_gcs_helpers REGION=us-central1 \
  IMAGE_TAG=dev RUNTIME_SERVICE_ACCOUNT=bq-export@YOUR_PROJECT.iam.gserviceaccount.com \
  PROJECT_ID=YOUR_PROJECT_ID
```

For Cloud Build-driven deploys, fill in `deploy/dev.env`, `deploy/stage.env`, and `deploy/prod.env` as needed (only `deploy/prod.env` exists today). See [App layout](../../README.md#app-layout) in the root README for the `.env` file format and the `__SET_*__` placeholder convention.

## Call the function

After deployment, call it with a POST request. The service is private by default, so the caller needs Cloud Run Invoker access and a bearer token (see `make -C dev_test_apps smoke-test-export-remote` for a scripted example).

```bash
curl -X POST \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  -d '{
    "source_project": "source-bigquery-project-id",
    "dataset_id": "your_dataset",
    "table_id": "your_table",
    "destination_bucket": "your-project-id-export-bucket",
    "location": "US",
    "format": "CSV"
  }' \
  "https://SERVICE_URL"
```

Example using query parameters instead:

```bash
curl -X GET \
  -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  "https://SERVICE_URL?source_project=source-bigquery-project-id&dataset_id=your_dataset&table_id=your_table&destination_bucket=your-project-id-export-bucket&location=US&format=CSV"
```

## IAM / cross-project permissions

The Cloud Run runtime service account must have permissions in both projects:

- Source BigQuery project: `roles/bigquery.dataViewer`, `roles/bigquery.jobUser`
- Destination bucket project: `roles/storage.objectAdmin`

Grant these to the `RUNTIME_SERVICE_ACCOUNT` configured in `deploy/<environment>.env` (or the Cloud Build trigger substitution). See [IAM and secrets](../../README.md#iam-and-secrets) in the root README for the Cloud Build service account's own permissions.

## Notes

- The function uses the runtime service account's identity for BigQuery and Storage calls; no service-account key file is needed.
- `location` must match the BigQuery table location.
- CSV is the default export format; Parquet and other BigQuery extract formats are also supported.
