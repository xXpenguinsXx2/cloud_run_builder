#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/common.sh"

select_deploy_environment
discover_app_dirs

for dockerfile in "${app_dockerfiles[@]}"; do
  app_dir="${dockerfile%/Dockerfile}"
  load_app_config "$app_dir"
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

  if [[ -n "${RUN_ENV_VARS:-}" ]]; then
    deploy_args+=("--set-env-vars=$RUN_ENV_VARS")
  fi
  if [[ -n "${RUN_SECRETS:-}" ]]; then
    deploy_args+=("--set-secrets=$RUN_SECRETS")
  fi

  gcloud "${deploy_args[@]}"
done