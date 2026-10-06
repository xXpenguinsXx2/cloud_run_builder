# dev_test_apps

Local development and testing tools for the apps under `../apps/`. Nothing here is deployed by Cloud Build; it exists purely to let a developer build, run, and smoke-test the production containers locally before pushing a change.

```text
dev_test_apps/
  Makefile                       # top-level wrapper: build/run arbitrary dev/test containers
  gcs_to_postgresql.compose.yaml # docker compose stack for local Postgres import testing
  smoke_tests/
    Makefile                     # export + import smoke test targets
    smoke_test.py                # BigQuery export smoke test runner (local or remote)
    cloud_run_smoke_test.py      # remote export smoke test (invokes deployed Cloud Run via gcloud identity token)
    local_import_smoke_test.py   # posts import requests to the local importer container
    export-request.json          # CSV BigQuery export request body
    export-request-parquet.json  # Parquet/SNAPPY BigQuery export request body
    import-request-csv.json      # CSV GCS -> PostgreSQL import request body
    import-request-parquet.json  # Parquet GCS -> PostgreSQL import request body
    requirements.txt              # test-runner Python deps (not the app's own deps)
    test_smoke_test.py            # unit tests for the smoke test helpers
```

This README is the single source of truth for the day-to-day test workflow; the root [`../README.md`](../README.md) only summarizes it. All commands below assume you're running from the repository root (`cloud_run_builder`).

> **Naming note:** this directory's `smoke_tests/` is unrelated to the repository-root [`../smoke_tests/`](../smoke_tests/) directory. This one holds live local/remote export and import smoke-test tooling (talks to real BigQuery/GCS/Cloud Run); the root one holds fully mocked, no-network unit tests for the apps' HTTP handlers, run with `make test` from the repository root (see [../README.md#local-wrapper-commands](../README.md#local-wrapper-commands)).

## Prerequisites

- Docker (and Docker Compose) installed and running.
- A Python virtual environment at `.venv` in the repository root (see root README for setup), or set `PYTHON` to point at any interpreter with the packages from `smoke_tests/requirements.txt` installed.
- Authenticate with Google Cloud before running any smoke test that talks to BigQuery, GCS, or Cloud Run:

  ```bash
  make -C dev_test_apps/smoke_tests auth
  ```

  This runs both `gcloud auth login` (your user identity, used for `gcloud run services describe` / `gcloud auth print-identity-token`) and `gcloud auth application-default login` (Application Default Credentials, used by the Python clients inside the containers and smoke test scripts). Run `make -C dev_test_apps/smoke_tests auth-google-adc` alone if you only need to refresh ADC.

## Running unit tests

Fast, no-network tests for the smoke test helper functions:

```bash
make -C dev_test_apps/smoke_tests test-deps   # first time only, or after requirements.txt changes
make -C dev_test_apps/smoke_tests test
```

## Export smoke tests (`bq_to_gcs_helpers`)

These exercise the BigQuery-to-GCS export function, validating the configured dataset/table/location before triggering CSV and Parquet exports.

**Local** — run the `bq_to_gcs_helpers` function locally first (see [../apps/bq_to_gcs_helpers/README.md](../apps/bq_to_gcs_helpers/README.md#run-locally-with-docker) for the `docker build`/`docker run` commands), then:

```bash
make -C dev_test_apps smoke-test-export-local
```

This checks `http://127.0.0.1:8080` is responding, then triggers the CSV (`export-request.json`) and Parquet/SNAPPY (`export-request-parquet.json`) exports in turn. Override the endpoint with `LOCAL_FUNCTION_URL` if your local function listens elsewhere.

**Remote** — exercises the already-deployed Cloud Run service directly, with no local process or proxy required:

```bash
make -C dev_test_apps smoke-test-export-remote
```

This resolves the Cloud Run URL via `gcloud run services describe`, authenticates with a `gcloud auth print-identity-token` bearer token, and runs the same CSV/Parquet preflight-and-export checks against `example/cloud-run-export-*.csv` and `example/cloud-run-export-*.parquet` objects. Override `PROJECT_ID`, `REGION`, `SERVICE_NAME`, or `SOURCE_PROJECT` to match the environment you deployed to (see `smoke_tests/Makefile` for all overridable variables).

## Local import workflow (`gcs_to_postgresql_helpers`)

Spins up a disposable PostgreSQL container plus a locally built copy of the importer, then imports the GCS objects produced by the export smoke tests:

```bash
make -C dev_test_apps local-import
```

This runs `docker compose -f gcs_to_postgresql.compose.yaml up -d --build --wait` to start PostgreSQL on `localhost:5433` and the importer on `localhost:8081`, posts the CSV and Parquet import requests (`smoke_tests/import-request-csv.json` / `import-request-parquet.json`), and prints the resulting row counts for `public.example` and `public.parq_example`. The import requests point at the `example/cloud-run-export-*` objects written by `make -C dev_test_apps smoke-test-export-remote`; run that first if those objects don't exist yet.

The PostgreSQL volume (`gcs_to_postgresql_postgres_data`) persists across runs so you can re-import without losing other local data. Tear the stack down with:

```bash
make -C dev_test_apps local-import-postgres-down
```

The importer container mounts your host's `${APPDATA}/gcloud` directory read-only so it can use your Application Default Credentials — run `make -C dev_test_apps/smoke_tests auth-google-adc` first if the importer fails to authenticate to GCS/BigQuery.

## Building/running an arbitrary dev/test container

The top-level `dev_test_apps/Makefile` can build and run any subdirectory that has its own `Dockerfile` (currently just `smoke_tests/`):

```bash
make -C dev_test_apps list              # list available DEV_TEST_APP names
make -C dev_test_apps build-all         # build every dev/test image
make -C dev_test_apps run DEV_TEST_APP=smoke_tests
```

`run` builds the image first, then runs it in the foreground with `--rm`; use Ctrl+C to stop it. Override `DEV_TEST_RUN_ARGS` to pass extra Docker flags or environment values to the container.

## Environment variable overrides

| Variable | Default | Used by |
| --- | --- | --- |
| `PROJECT_ID` | `jag-pgsql-gke` | `smoke_tests/Makefile` (export + auth targets) |
| `REGION` | `us-central1` | `smoke_tests/Makefile` (remote export) |
| `SERVICE_NAME` | `bigquery-gcs-export` | `smoke_tests/Makefile` (remote export) |
| `SOURCE_PROJECT` | `jag-pgsql-gke` | `smoke_tests/Makefile` (export) |
| `FORMAT` | `CSV` | `smoke_tests/Makefile` (`export` target only; the `smoke-test-export-*` targets set this per-format automatically) |
| `LOCAL_FUNCTION_URL` | `http://127.0.0.1:8080` | `smoke_tests/Makefile` (local export) |
| `PYTHON` | `../../.venv/Scripts/python.exe` (Windows) or `../../.venv/bin/python` (POSIX) | all Makefiles |
| `DEV_TEST_RUN_ARGS` | unset (smoke_tests sets a default `host.docker.internal` + `FUNCTION_URL` mapping) | `dev_test_apps/Makefile` `run` target |

For details on what each smoke test actually validates, see the comments at the top of `smoke_tests/smoke_test.py` and `smoke_tests/cloud_run_smoke_test.py`, and the `.env` files under `../apps/*/deploy/` for how production/staging configuration maps to these same environment variables.
