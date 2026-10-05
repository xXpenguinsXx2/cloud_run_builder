#!/usr/bin/env bash
set -euo pipefail

builder_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
apps_dir="$builder_root/apps"

select_deploy_environment() {
  case "${BRANCH_NAME:-}" in
    dev|stage|prod)
      deploy_environment="$BRANCH_NAME"
      ;;
    main)
      deploy_environment=prod
      ;;
    *)
      echo "Unsupported branch '${BRANCH_NAME:-<empty>}'; expected dev, stage, prod, or main." >&2
      return 1
      ;;
  esac

  : "${PROJECT_ID:?Cloud Build PROJECT_ID is required}"
  : "${DEPLOY_REGION:?Set the _REGION substitution}"
  : "${AR_REPOSITORY:?Set the _AR_REPOSITORY substitution}"
  image_tag="${SHORT_SHA:-${BUILD_ID:?Cloud Build BUILD_ID is required}}"
}

load_app_config() {
  local app_dir="$1"
  local app_name="${app_dir##*/}"
  local config_file="$app_dir/deploy/$deploy_environment.env"

  if [[ ! -f "$config_file" ]]; then
    echo "Missing deployment config: $config_file" >&2
    return 1
  fi

  unset SERVICE_NAME RUNTIME_SERVICE_ACCOUNT RUN_ENV_VARS RUN_SECRETS CLOUD_SQL_CONNECTION_NAME RUN_TIMEOUT_SECONDS
  # Config files are maintained alongside the app and contain shell assignments.
  # shellcheck disable=SC1090
  source "$config_file"

  SERVICE_NAME="${SERVICE_NAME:-${app_name//_/-}-$deploy_environment}"
  : "${RUNTIME_SERVICE_ACCOUNT:?Set RUNTIME_SERVICE_ACCOUNT in $config_file}"
  if [[ "$RUNTIME_SERVICE_ACCOUNT" == "__SET_IN_TRIGGER__" ]]; then
    echo "Set the Cloud Build _RUNTIME_SERVICE_ACCOUNT substitution to the existing non-default service account email." >&2
    return 1
  fi
  image_uri="$DEPLOY_REGION-docker.pkg.dev/$PROJECT_ID/$AR_REPOSITORY/$SERVICE_NAME:$image_tag"
}

discover_app_dirs() {
  local config_file
  local app_dir

  shopt -s nullglob
  app_dockerfiles=()
  for config_file in "$apps_dir"/*/deploy/"$deploy_environment".env; do
    app_dir="${config_file%/deploy/$deploy_environment.env}"
    if [[ ! -f "$app_dir/Dockerfile" ]]; then
      echo "Deployment config has no matching Dockerfile: $config_file" >&2
      return 1
    fi
    app_dockerfiles+=("$app_dir/Dockerfile")
  done

  if [[ "${#app_dockerfiles[@]}" -eq 0 ]]; then
    echo "No deployable apps configured for '$deploy_environment'; add apps/<app>/deploy/$deploy_environment.env." >&2
    return 1
  fi
}