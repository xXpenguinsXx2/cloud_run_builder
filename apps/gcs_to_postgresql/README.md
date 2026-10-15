# GCS to PostgreSQL importer

This private Cloud Run HTTP function imports CSV or Parquet objects from Cloud Storage into PostgreSQL. The required request fields are `source_project`, `dataset_id`, `table_id`, `source_bucket`, and `format`. These fields are optional:

- `export_date` — a `YYYYMMDD` export folder to import, `gs://<source_bucket>/services/exports/<table_id>/<export_date>/part_*.<csv|parquet>`.
- `source_object` — an explicit object name or pattern, with at most one `*`. It overrides `export_date`.
- `schema_object` — an explicit schema JSON object path.

If neither `source_object` nor `export_date` is given, the importer uses the latest `YYYYMMDD` folder under `services/exports/<table_id>/` that has `part_*` files for the requested format. That is the layout `bq_to_gcs` writes.

The function loads the table schema from the `schema.json` file that `bq_to_gcs` writes next to the exports, `gs://<source_bucket>/services/exports/<table_id>/<table_id>_schema.json`. `schema_object` overrides that object path. If the file does not exist, it falls back to reading the schema from the BigQuery table `source_project.dataset_id.table_id`. The response's `schema_source` shows which one was used, and its `source` shows the resolved object pattern. The function uses the schema to create the PostgreSQL table. It selects the matching GCS objects, sorts shards by object name, imports them into a staging table, then atomically replaces the destination table after all rows load successfully. CSV imports replace `public.<table_id>`; Parquet imports replace `public.parq_<table_id>`. Nested and repeated BigQuery fields are not supported.

## Local import test

See [dev_test_apps/README.md](../../dev_test_apps/README.md#local-import-workflow-gcs_to_postgresql) for the full `make -C dev_test_apps local-import` workflow (Docker Compose, auth prerequisites, row-count verification, and teardown). The local database is available at `localhost:5433` with the test-only credentials `testuser` / `testpass` and database `testdb`; the importer is available at `http://localhost:8081`. The local importer container mounts the user's gcloud configuration read-only and uses Application Default Credentials for BigQuery schema lookup and GCS reads — the identity needs BigQuery Data Viewer access to the source table and Storage Object Viewer access to the bucket.

Handler-level unit tests (no Google Cloud access required) live in [smoke_tests/test_gcs_to_postgresql.py](../../smoke_tests/test_gcs_to_postgresql.py) at the repository root and run via `make test` (see [repository README](../../README.md#local-wrapper-commands)).

## Database connection and deployment

The importer connects directly to PostgreSQL over TCP (no SSL, no Cloud SQL connector), configured entirely through environment variables:

- `DB_HOST`, `DB_NAME`, `DB_USER` (required); `DB_PORT` (default `5432`); `DB_CONNECT_TIMEOUT_SECONDS` (default `15`)
- Password, first match wins:
  1. `DB_PASSWORD` — e.g. a Cloud Run secret exposed with `RUN_SECRETS=DB_PASSWORD=<secret>:latest`
  2. `DB_SECRET_NAME` (+ `DB_SECRET_PROJECT`, or a full `projects/.../secrets/...` name; optional `DB_SECRET_VERSION`, default `latest`) — read from Secret Manager with Application Default Credentials
  3. Otherwise an access token for the runtime identity (IAM database authentication)

Before deploying, replace the `__SET_*__` placeholders in `deploy/<environment>.env`. The runtime identity needs Secret Manager Secret Accessor on the secret (when used), BigQuery Data Viewer, and Storage Object Viewer; the database user needs the schema/table privileges to create, insert, drop, and replace import tables. Because SSL is off, the instance must accept non-SSL connections from Cloud Run's egress. The deployment sets the request timeout to one hour.

From the repository root, `make deploy APP=gcs_to_postgresql DEPLOY_ENV=dev` loads `deploy/dev.env`, builds and pushes the image, and applies `RUN_ENV_VARS` to a new Cloud Run revision. Use `DEPLOY_ENV=stage` or `DEPLOY_ENV=prod` for the other profiles. Make command-line settings override the profile. Editing the file or rebuilding the image alone does not change the running service's environment.

The HTTP service is private by default. Callers must be granted Cloud Run Invoker access. Send a JSON `POST` request using the export-request fields to import the specified files.

### Connection test

To debug the deployed importer without importing data, POST the [debug request example](../../dev_test_apps/smoke_tests/debug-request.json) to the service root:

```json
{
  "action": "debug"
}
```

This uses the service's current runtime configuration (not values from your local config file). It returns `status`, the connection target and `password_source`, an `environment` snapshot, and `session_user` on success. The snapshot allowlists database settings, Secret Manager reference names, `CLOUD_SQL_CONNECTION_NAME`, `LOG_LEVEL`, `PORT`, and Cloud Run's `K_SERVICE`, `K_REVISION`, and `K_CONFIGURATION`. Unset variables are `null`; effective connection defaults (such as port `5432`) are shown in the connection target. `DB_PASSWORD_SET` reports only whether a password is configured. Passwords, tokens, and unrelated environment variables are never included in the snapshot.

The debug action only connects and executes `SELECT session_user`; it does not read GCS/BigQuery or import, replace, or modify tables. HTTP 200 with `status: connected` means the query succeeded; HTTP 500 with `status: failed` includes the environment snapshot and a sanitized error type. See service logs for detailed errors. Cloud Run authentication and Invoker permission are still required.

From the repository root in PowerShell, after deploying the updated app:

```powershell
$serviceUrl = gcloud run services describe gcs-to-postgresql-dev --project=jag-pgsql-gke --region=us-central1 --format="value(status.url)"
$token = gcloud auth print-identity-token
curl.exe -sS -X POST "$serviceUrl" -H "Authorization: Bearer $token" -H "Content-Type: application/json" --data-binary "@dev_test_apps/smoke_tests/debug-request.json"
```

Normal import JSON requests continue to work without an `action`; `action: import` is also accepted. An unrecognized action returns HTTP 400. For debug requests no import parameters are required, and any supplied import fields are ignored. The example is a request body for HTTP clients, not an input for `local_import_smoke_test.py`, which expects an import result.

`GET /connection-test` only connects to the configured database with the service's current environment values and runs `SELECT session_user`; it does not touch BigQuery or GCS. It returns HTTP 200 with `"status": "connected"` or HTTP 500 with `"status": "failed"` and the error. The response includes the host, port, database, user, and password source but never the password.

```sh
SERVICE_URL=$(gcloud run services describe gcs-to-postgresql-dev --region=us-central1 --format='value(status.url)')
curl -sS -H "Authorization: Bearer $(gcloud auth print-identity-token)" "$SERVICE_URL/connection-test"
```
