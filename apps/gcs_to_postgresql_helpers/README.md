# GCS to PostgreSQL importer

This private Cloud Run HTTP function imports CSV or Parquet objects from Cloud Storage into PostgreSQL. The request uses the same fields as the BigQuery-to-GCS export request: `source_project`, `dataset_id`, `table_id`, `destination_bucket`, `destination_object`, and `format`.

The function reads the BigQuery table schema to create the PostgreSQL table. It selects GCS objects matching the exact object name or the single `*` wildcard in `destination_object`, sorts shards by object name, imports them into a staging table, then atomically replaces the destination table after all rows load successfully. CSV imports replace `public.<table_id>`; Parquet imports replace `public.parq_<table_id>`. Nested and repeated BigQuery fields are not supported.

## Local import test

From `cloud_run_builder`, authenticate with Google Application Default Credentials and run:

```powershell
make -C dev_test_apps local-import
```

This starts local PostgreSQL and the importer in Docker Compose, imports the configured CSV and Parquet requests, then checks the imported tables. The local database is available at `localhost:5433` with the test-only credentials `testuser` / `testpass` and database `testdb`. The importer is available at `http://localhost:8081`. Its database volume persists between runs; stop the services with:

```powershell
make -C dev_test_apps local-import-postgres-down
```

The local import requests in `dev_test_apps/smoke_tests/import-request-csv.json` and `import-request-parquet.json` consume `example/cloud-run-export-*` objects. Run the remote export smoke test first if those objects are not present.

The local importer container mounts the user's gcloud configuration read-only and uses Application Default Credentials for BigQuery schema lookup and GCS reads. The identity needs BigQuery Data Viewer access to the source table and Storage Object Viewer access to the bucket.

## Cloud SQL deployment

The Cloud Run deployment uses the Cloud SQL Python Connector with automatic IAM database authentication. Before deploying, replace the placeholders in `deploy/<environment>.env`:

- `CLOUD_SQL_CONNECTION_NAME`: `project-id:region:instance-id`
- `DB_NAME`: the Cloud SQL database name
- `RUNTIME_SERVICE_ACCOUNT`: set through the Cloud Build trigger as described in the repository README

The deployment sets `DB_IAM_USER` to the configured runtime service account email. Add that service account to the Cloud SQL instance as an IAM database user, grant it `roles/cloudsql.client` and `roles/cloudsql.instanceUser`, and grant the database user the schema/table privileges needed to create, insert, drop, and replace import tables. For private-IP-only instances, set `CLOUD_SQL_IP_TYPE=PRIVATE` and configure Cloud Run VPC connectivity. The runtime identity also needs BigQuery Data Viewer and Storage Object Viewer permissions.

Enable the Cloud SQL Admin API and IAM database authentication on the instance. The IAM database username for a service account is its full email address. The deployment attaches the instance, exposes its connection name to the service, and sets the request timeout to one hour.

The HTTP service is private by default. Callers must be granted Cloud Run Invoker access. Send a JSON `POST` request using the export-request fields to import the specified files.
