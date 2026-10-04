#!/bin/bash
set -euo pipefail

if [ -n "${GCS_BUCKET:-}" ]; then
    mount_path="${GCS_MOUNT_PATH:-/mnt/gcs}"
    mkdir -p "$mount_path"
    gcsfuse --foreground --implicit-dirs -o ro,allow_other "$GCS_BUCKET" "$mount_path" &
    gcsfuse_pid=$!

    for _ in $(seq 1 60); do
        if mountpoint -q "$mount_path"; then
            break
        fi
        if ! kill -0 "$gcsfuse_pid" 2>/dev/null; then
            wait "$gcsfuse_pid"
        fi
        sleep 1
    done

    if ! mountpoint -q "$mount_path"; then
        echo "Cloud Storage FUSE failed to mount gs://${GCS_BUCKET} at ${mount_path}" >&2
        exit 1
    fi
    echo "Mounted gs://${GCS_BUCKET} at ${mount_path}"
fi

exec docker-entrypoint.sh postgres