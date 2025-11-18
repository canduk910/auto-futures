#!/bin/bash
set -euo pipefail

if [ -n "${GOOGLE_CLOUD_PROJECT:-}" ]; then
  echo "[ENTRYPOINT] Running in Cloud Run project ${GOOGLE_CLOUD_PROJECT}"
fi

exec "$@"

