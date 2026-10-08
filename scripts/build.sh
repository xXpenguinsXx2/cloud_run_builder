
#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/common.sh"

select_deploy_environment
discover_app_dirs

for dockerfile in "${app_dockerfiles[@]}"; do
  app_dir="${dockerfile%/Dockerfile}"
  load_app_config "$app_dir"
  echo "Building and pushing $SERVICE_NAME from $app_dir for $deploy_environment"
  docker build --tag "$image_uri" "$app_dir"
  docker push "$image_uri"
done