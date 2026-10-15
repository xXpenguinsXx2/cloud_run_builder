# Cloud Run Builder

Cloud Build wrapper for building configured app containers, pushing immutable images to Artifact Registry, and deploying them as private Cloud Run services. Production containers live under `apps/`; local Postgres and test containers live separately under `dev_test_apps/` and are never included in deploy discovery.

## App layout

Add each independently deployable container like this:

```text
cloud_run_builder/
  apps/
    reports-api/
      Dockerfile
      deploy/
        dev.env
        stage.env
        prod.env
```

Each environment file is sourced by the Bash build scripts and included by the root Makefile for deployment. Use plain, unquoted `KEY=value` assignments compatible with both Bash and Make (no shell expansions); keep comma-separated runtime settings in a single `RUN_ENV_VARS` assignment rather than using `+=`. The file may set the runtime identity, override the service name, or configure runtime environment variables and Secret Manager references:

```bash
SERVICE_NAME=reports-api-stage
RUNTIME_SERVICE_ACCOUNT=reports-api-stage@my-project.iam.gserviceaccount.com
RUN_ENV_VARS=LOG_LEVEL=info
RUN_SECRETS=API_TOKEN=reports-api-token:latest
```

Only apps with both a `Dockerfile` and the selected environment file are included in automated builds/deployments. Create `dev.env`, `stage.env`, and `prod.env` for every app you want Cloud Build to deploy. `SERVICE_NAME` defaults to the app name with underscores replaced by hyphens, plus the environment name. Configure different runtime service accounts and secrets per environment as needed. Secret references use Cloud Run's `KEY=SECRET:VERSION` format. Do not put secret values or service-account keys in these files.

## Local wrapper commands

Run these commands from `cloud_run_builder`. `APP` defaults to `gcs_to_postgresql`, `PROJECT_ID` defaults to `jag-pgsql-gke`, and `build-all` builds each app under `apps/` that has a Dockerfile. Dev/test container operations are handled only by the standalone Makefile under `dev_test_apps/`.

```bash
make list-apps
make build
make build APP=bq_to_gcs IMAGE_TAG=local
make build-all IMAGE_TAG=local
make run APP=gcs_to_postgresql DEPLOY_ENV=dev
make push APP=bq_to_gcs IMAGE_TAG=dev
make deploy APP=bq_to_gcs REGION=us-central1 \
  IMAGE_TAG=dev RUNTIME_SERVICE_ACCOUNT=bq-export@jag-pgsql-gke.iam.gserviceaccount.com \
  RUN_SECRETS=API_TOKEN=api-token:latest
```

`deploy` builds and pushes the selected image before deploying it. `run`, `deploy`, `release`, and `check-deploy` automatically load `apps/<APP>/deploy/<DEPLOY_ENV>.env` when it exists, including `RUN_ENV_VARS`, `RUN_SECRETS`, `SERVICE_NAME`, and `RUN_TIMEOUT_SECONDS`. Explicit Make command-line assignments override the file. If no file exists, command-line values and defaults are used. The config is applied at runtime, not baked into the image; editing it requires restarting the local container or deploying a new Cloud Run revision. `make build` alone does not apply runtime settings. Use `make -n deploy APP=gcs_to_postgresql DEPLOY_ENV=dev` to preview the deployment without building or changing cloud resources.

`make run` builds the selected app, then runs it in the foreground with `--rm`, passing each comma-separated `RUN_ENV_VARS` entry as a Docker environment variable. Values must not contain commas. `CLOUD_SQL_CONNECTION_NAME`, when configured, is also passed as an environment variable; this does not start a Cloud SQL proxy or attach an instance locally. The default port mapping is `127.0.0.1:8080:8080`. Override `RUN_ARGS` to change ports or add Docker options such as read-only Application Default Credentials mounts; include a port mapping in the override if needed. Apps that access Google Cloud still need local credentials and the appropriate permissions. Cloud Run-only settings (`RUN_SECRETS`, runtime service accounts, and request timeouts) are not applied locally, and Secret Manager references are not automatically resolved by Docker. For a local secret, export it in your shell and pass its name, for example `RUN_ARGS="-p 127.0.0.1:8080:8080 --env DB_PASSWORD"`. Stop the container with Ctrl+C. Use `make -n run APP=gcs_to_postgresql DEPLOY_ENV=dev` to preview the build and run commands.

The service stays private. The runtime service account comes from `RUNTIME_SERVICE_ACCOUNT`, or `CLOUD_RUN_SERVICE_ACCOUNT` when no explicit account is configured; if neither is set, Cloud Run uses its default identity. The `RUN_SECRETS` value contains Secret Manager references, not secret values. The runtime service account must have access to those secrets. `DEPLOY_ENV` defaults to `dev`; set `DEPLOY_ENV=stage` or `DEPLOY_ENV=prod` for manual stage/prod deployments.

`make test` runs the handler-level unit tests for every app (`smoke_tests/test_*.py`, fully mocked, no Google Cloud access required); run `make test-deps` first to install their dependencies. Don't confuse this top-level `smoke_tests/` directory with `dev_test_apps/smoke_tests/` below, which holds the live local/remote smoke test tooling.

The tester containers have their own Makefile in `dev_test_apps/`; see [dev_test_apps/README.md](dev_test_apps/README.md) for the full local/remote smoke test and local import workflow. Quick reference:

```bash
make -C dev_test_apps list
make -C dev_test_apps build-all
make -C dev_test_apps run DEV_TEST_APP=postgres
make -C dev_test_apps run DEV_TEST_APP=smoke_tests
make -C dev_test_apps smoke-test-export-local
make -C dev_test_apps smoke-test-export-remote
make -C dev_test_apps local-import
```

`run` builds the selected image first, then runs it in the foreground with `--rm`; use Ctrl+C to stop it. Override `DEV_TEST_RUN_ARGS` to pass Docker options or environment values to the selected container. The smoke runner expects its HTTP handler at `localhost:8080` by default.

## GCS to PostgreSQL importer

`apps/gcs_to_postgresql/` contains a private Cloud Run HTTP function that imports CSV or Parquet GCS exports into PostgreSQL. It loads the schema from the `<table_id>_schema.json` file that `bq_to_gcs` exports (falling back to BigQuery), imports the latest `services/exports/<table_id>/<YYYYMMDD>/` folder (or `export_date` / `source_object` when given), and transactionally replaces `public.<table_id>` for CSV or `public.parq_<table_id>` for Parquet. It connects directly to PostgreSQL (no SSL) using `DB_HOST`, `DB_PORT`, `DB_NAME`, and `DB_USER`, with the password taken from `DB_PASSWORD`, a Secret Manager secret (`DB_SECRET_NAME`), or the runtime identity's access token.

For local testing (disposable PostgreSQL + importer via Docker Compose), authenticate with Google Application Default Credentials and see [dev_test_apps/README.md](dev_test_apps/README.md#local-import-workflow-gcs_to_postgresql) for the full `local-import` workflow.

Before deploying, replace the `DB_HOST`/`DB_USER` placeholders (and optionally set `DB_SECRET_NAME`/`DB_SECRET_PROJECT`) in the selected `apps/gcs_to_postgresql/deploy/<environment>.env` file; see [the app README](apps/gcs_to_postgresql/README.md#database-connection-and-deployment). Grant the runtime identity BigQuery Data Viewer and Storage Object Viewer on the source table and bucket, Secret Manager Secret Accessor on the password secret when used, and grant the database user the schema/table privileges needed to replace the import tables. The deployment sets a one-hour request timeout.

## Cloud Build trigger

Create a Cloud Build trigger with:

- Repository source set to this repository's root.
- Configuration file `cloudbuild.yaml` at the repository root.
- Branch filters for `dev`, `stage`, `prod`, and `main` (or a broader filter if desired).
- Substitutions `_REGION` and `_AR_REPOSITORY` set to the Artifact Registry location and Docker repository. Defaults are `us-central1` and `cloud-run-source-deploy`.
- Substitution `_RUNTIME_SERVICE_ACCOUNT` optionally selects the Cloud Run runtime identity. It defaults to empty, in which case `--service-account` is omitted and Cloud Run uses its default identity; set it on the trigger to use a specific account.
- A dedicated Cloud Build service account as the trigger's build identity.

Branches `dev`, `stage`, and `prod` select the same-named app config. `main` selects `prod`. Other branches fail closed. The BigQuery-to-GCS production profile uses project `jag-pgsql-gke`, region `us-central1`, bucket `jag_bq_export_bucket`, and service `bigquery-gcs-export`; dataset and table remain request fields. Apps without a matching environment config are ignored; if no configured app is found, the build fails with a message. Images are tagged with the commit SHA and the deployed revision uses that exact tag.

## IAM and secrets

The Cloud Build service account needs permission to push to the Artifact Registry repository, deploy Cloud Run services, and act as each configured runtime service account. Grant it Artifact Registry Writer, Cloud Run Developer (or the narrower permissions your org uses), and Service Account User on the runtime accounts.

The Cloud Run runtime service account is set with `RUNTIME_SERVICE_ACCOUNT`; no service-account JSON key is needed. Grant that runtime identity access to the Secret Manager secrets referenced by `RUN_SECRETS` (typically Secret Manager Secret Accessor on each secret). Cloud Run resolves those secrets when the container runs. Keep build-time credentials out of the container image and repository.

Create the Artifact Registry Docker repository and enable Cloud Build, Artifact Registry, and Cloud Run APIs before the first trigger run. The pipeline deploys private services; grant invocation access separately if an app needs callers outside the project.