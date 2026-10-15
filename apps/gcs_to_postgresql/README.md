# GCS to PostgreSQL importer

This private Cloud Run HTTP function imports CSV or Parquet objects from Cloud Storage into PostgreSQL. The request fields are: `sink_project`, `sink_dataset_id`, `sink_table_id`, `target_import_bucket`, `destination_object`, and `format`.

The function reads the BigQuery table schema to create the PostgreSQL table. It selects GCS objects matching the exact object name or the single `*` wildcard in `destination_object`, sorts shards by object name, imports them into a staging table, then atomically replaces the destination table after all rows load successfully. CSV imports replace `public.<sink_table_id>`; Parquet imports replace `public.parq_<sink_table_id>`. Nested and repeated BigQuery fields are not supported.

## Local import test

See [dev_test_apps/README.md](../../dev_test_apps/README.md#local-import-workflow-gcs_to_postgresql_helpers) for the full `make -C dev_test_apps local-import` workflow (Docker Compose, auth prerequisites, row-count verification, and teardown). The local database is available at `localhost:5433` with the test-only credentials `testuser` / `testpass` and database `testdb`; the importer is available at `http://localhost:8081`. The local importer container mounts the user's gcloud configuration read-only and uses Application Default Credentials for BigQuery schema lookup and GCS reads — the identity needs BigQuery Data Viewer access to the source table and Storage Object Viewer access to the bucket.

Handler-level unit tests (no Google Cloud access required) live in [smoke_tests/test_gcs_to_postgresql_helpers.py](../../smoke_tests/test_gcs_to_postgresql_helpers.py) at the repository root and run via `make test` (see [repository README](../../README.md#local-wrapper-commands)).

## Cloud SQL deployment

The Cloud Run deployment uses the Cloud SQL Python Connector with automatic IAM database authentication. Before deploying, replace the placeholders in `deploy/<environment>.env`:

- `CLOUD_SQL_CONNECTION_NAME`: `project-id:region:instance-id`
- `DB_NAME`: the Cloud SQL database name
- `RUNTIME_SERVICE_ACCOUNT`: set through the Cloud Build trigger as described in the repository README

The deployment derives `DB_IAM_USER` automatically from the configured runtime service account email, stripping its `.gserviceaccount.com` suffix — Cloud SQL's IAM database username for a service account is its email address with that suffix removed, not the full email. Add that truncated username to the Cloud SQL instance as an IAM database user (matching the full service account's identity), grant it `roles/cloudsql.client` and `roles/cloudsql.instanceUser`, and grant the database user the schema/table privileges needed to create, insert, drop, and replace import tables. For private-IP-only instances, set `CLOUD_SQL_IP_TYPE=PRIVATE` and configure Cloud Run VPC connectivity. The runtime identity also needs BigQuery Data Viewer and Storage Object Viewer permissions.

Enable the Cloud SQL Admin API and IAM database authentication on the instance. The deployment attaches the instance, exposes its connection name to the service, and sets the request timeout to one hour.

The HTTP service is private by default. Callers must be granted Cloud Run Invoker access. Send a JSON `POST` request using the export-request fields to import the specified files.
