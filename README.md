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

Each environment file is sourced as a Bash file by the build scripts. It must set the runtime identity, and may override the service name or configure runtime environment variables and Secret Manager references:

```bash
SERVICE_NAME=reports-api-stage
RUNTIME_SERVICE_ACCOUNT=reports-api-stage@my-project.iam.gserviceaccount.com
RUN_ENV_VARS=LOG_LEVEL=info
RUN_SECRETS=API_TOKEN=reports-api-token:latest
```

Only apps with both a `Dockerfile` and the selected environment file are included in automated builds/deployments. Create `dev.env`, `stage.env`, and `prod.env` for every app you want Cloud Build to deploy. `SERVICE_NAME` defaults to the app name with underscores replaced by hyphens, plus the environment name. Configure different runtime service accounts and secrets per environment as needed. Secret references use Cloud Run's `KEY=SECRET:VERSION` format. Do not put secret values or service-account keys in these files.

## Local wrapper commands

Run these commands from `cloud_run_builder`. `APP` defaults to `bq_to_gcs_helpers`, `PROJECT_ID` defaults to `jag-pgsql-gke`, and `build-all` builds each app under `apps/` that has a Dockerfile. Dev/test container operations are handled only by the standalone Makefile under `dev_test_apps/`.

```bash
make list-apps
make build
make build APP=bq_to_gcs_helpers IMAGE_TAG=local
make build-all IMAGE_TAG=local
make push APP=bq_to_gcs_helpers IMAGE_TAG=dev
make deploy APP=bq_to_gcs_helpers REGION=us-central1 \
  IMAGE_TAG=dev RUNTIME_SERVICE_ACCOUNT=bq-export@jag-pgsql-gke.iam.gserviceaccount.com \
  RUN_SECRETS=API_TOKEN=api-token:latest
```

`deploy` builds and pushes the selected image before deploying it. It keeps the service private and sets the runtime service account on Cloud Run. The `RUN_SECRETS` value contains Secret Manager references, not secret values. The runtime service account must have access to those secrets. `DEPLOY_ENV` defaults to `dev` and controls the default Cloud Run service name. Set `DEPLOY_ENV=stage` or `DEPLOY_ENV=prod` for manual stage/prod deployments.

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

`apps/gcs_to_postgresql_helpers/` contains a private Cloud Run HTTP function that imports CSV or Parquet GCS exports into PostgreSQL. It reads the source BigQuery schema, imports objects matching `destination_object`, and transactionally replaces `public.<table_id>` for CSV or `public.parq_<table_id>` for Parquet. It uses Cloud SQL IAM database authentication when `CLOUD_SQL_CONNECTION_NAME` is configured, and a direct TCP connection for local development.

For local testing (disposable PostgreSQL + importer via Docker Compose), authenticate with Google Application Default Credentials and see [dev_test_apps/README.md](dev_test_apps/README.md#local-import-workflow-gcs_to_postgresql_helpers) for the full `local-import` workflow.

Before deploying, replace the Cloud SQL connection name and database-name placeholders in the selected `apps/gcs_to_postgresql_helpers/deploy/<environment>.env` file. Enable the Cloud SQL Admin API and IAM database authentication, add the Cloud Run runtime service account as a Cloud SQL IAM database user, grant it Cloud SQL Client and Cloud SQL Instance User access, and grant that database user the schema/table privileges needed to replace the import tables. The deployment derives `DB_IAM_USER` from `RUNTIME_SERVICE_ACCOUNT` by stripping its `.gserviceaccount.com` suffix (the IAM database username format Cloud SQL expects), attaches the configured instance to Cloud Run, passes its connection name to the service, and sets a one-hour request timeout. Select `CLOUD_SQL_IP_TYPE=PRIVATE` and configure Cloud Run VPC connectivity if the instance is private-IP-only. Grant the runtime identity BigQuery Data Viewer and Storage Object Viewer on the source table and bucket.

## Cloud Build trigger

Create a Cloud Build trigger with:

- Repository source set to this repository's root.
- Configuration file `cloudbuild.yaml` at the repository root.
- Branch filters for `dev`, `stage`, `prod`, and `main` (or a broader filter if desired).
- Substitutions `_REGION` and `_AR_REPOSITORY` set to the Artifact Registry location and Docker repository. Defaults are `us-central1` and `cloud-run-source-deploy`.
- Substitution `_RUNTIME_SERVICE_ACCOUNT` selects the Cloud Run runtime identity. It defaults to the project's Compute Engine default service account; change this single substitution in `cloudbuild.yaml` or the trigger to switch to a non-default account.
- A dedicated Cloud Build service account as the trigger's build identity.

Branches `dev`, `stage`, and `prod` select the same-named app config. `main` selects `prod`. Other branches fail closed. The BigQuery-to-GCS production profile uses project `jag-pgsql-gke`, region `us-central1`, bucket `jag_bq_export_bucket`, and service `bigquery-gcs-export`; dataset and table remain request fields. Apps without a matching environment config are ignored; if no configured app is found, the build fails with a message. Images are tagged with the commit SHA and the deployed revision uses that exact tag.

## IAM and secrets

The Cloud Build service account needs permission to push to the Artifact Registry repository, deploy Cloud Run services, and act as each configured runtime service account. Grant it Artifact Registry Writer, Cloud Run Developer (or the narrower permissions your org uses), and Service Account User on the runtime accounts.

The Cloud Run runtime service account is set with `RUNTIME_SERVICE_ACCOUNT`; no service-account JSON key is needed. Grant that runtime identity access to the Secret Manager secrets referenced by `RUN_SECRETS` (typically Secret Manager Secret Accessor on each secret). Cloud Run resolves those secrets when the container runs. Keep build-time credentials out of the container image and repository.

Create the Artifact Registry Docker repository and enable Cloud Build, Artifact Registry, and Cloud Run APIs before the first trigger run. The pipeline deploys private services; grant invocation access separately if an app needs callers outside the project.