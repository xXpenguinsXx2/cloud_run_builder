#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/common.sh"

select_deploy_environment
discover_app_dirs

for dockerfile in "${app_dockerfiles[@]}"; do
  app_dir="${dockerfile%/Dockerfile}"
  load_app_config "$app_dir"
  if [[ "${CLOUD_SQL_CONNECTION_NAME:-}" == "__SET_CLOUD_SQL_CONNECTION_NAME__" ]]; then
    echo "Set CLOUD_SQL_CONNECTION_NAME in $app_dir/deploy/$deploy_environment.env to the Cloud SQL instance connection name." >&2
    exit 1
  fi
  if [[ "${RUN_ENV_VARS:-}" == *"__SET_"* ]]; then
    echo "Replace deployment placeholders in $app_dir/deploy/$deploy_environment.env before deploying." >&2
    exit 1
  fi
  echo "Deploying $SERVICE_NAME to $deploy_environment in $DEPLOY_REGION"

  deploy_args=(
    run deploy "$SERVICE_NAME"
    "--project=$PROJECT_ID"
    "--region=$DEPLOY_REGION"
    "--image=$image_uri"
    "--service-account=$RUNTIME_SERVICE_ACCOUNT"
    --no-allow-unauthenticated
    --quiet
  )

  env_vars="${RUN_ENV_VARS:-}"
  if [[ -n "${CLOUD_SQL_CONNECTION_NAME:-}" ]]; then
    env_vars="${env_vars:+$env_vars,}CLOUD_SQL_CONNECTION_NAME=$CLOUD_SQL_CONNECTION_NAME"
  fi
  if [[ -n "$env_vars" ]]; then
    deploy_args+=("--set-env-vars=$env_vars")
  fi
  if [[ -n "${RUN_SECRETS:-}" ]]; then
    deploy_args+=("--set-secrets=$RUN_SECRETS")
  fi
  if [[ -n "${CLOUD_SQL_CONNECTION_NAME:-}" ]]; then
    deploy_args+=("--add-cloudsql-instances=$CLOUD_SQL_CONNECTION_NAME")
  fi
  if [[ -n "${RUN_TIMEOUT_SECONDS:-}" ]]; then
    deploy_args+=("--timeout=$RUN_TIMEOUT_SECONDS")
  fi

  gcloud "${deploy_args[@]}"
done