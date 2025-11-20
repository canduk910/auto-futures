#!/bin/bash
set -euo pipefail

if [ -n "${GOOGLE_CLOUD_PROJECT:-}" ]; then
  echo "[ENTRYPOINT] Running in Cloud Run project ${GOOGLE_CLOUD_PROJECT}"
fi

restore_runtime() {
  if [ "${RESTORE_RUNTIME:-false}" != "true" ]; then
    return 0
  fi
  if [ -z "${GCS_BUCKET:-}" ]; then
    echo "[ENTRYPOINT] RESTORE_RUNTIME=true지만 GCS_BUCKET이 비어 있습니다. 복원을 건너뜁니다." >&2
    return 0
  fi
  PREFIX="${GCS_PREFIX:-runtime/}"
  echo "[ENTRYPOINT] Restoring runtime files from gs://${GCS_BUCKET}/${PREFIX}"
  if ! python scripts/gcs_sync.py --bucket "${GCS_BUCKET}" download --prefix "${PREFIX}"; then
    echo "[ENTRYPOINT] runtime 복원 실패 (무시하고 계속 진행)" >&2
  fi
}

restore_runtime

PORT=${PORT:-8080}
STREAMLIT_CMD=(
  streamlit run ui/ui_dashboard.py \
  --server.port "${PORT}" \
  --server.address 0.0.0.0 \
  --server.enableCORS false
)

python auto_future_trader.py &
TRADER_PID=$!
trap "echo '[ENTRYPOINT] Caught signal, stopping processes'; kill ${TRADER_PID}; wait ${TRADER_PID} 2>/dev/null" TERM INT

echo "[ENTRYPOINT] Launched auto_future_trader.py (pid=${TRADER_PID})"
echo "[ENTRYPOINT] Starting Streamlit dashboard on port ${PORT}"
"${STREAMLIT_CMD[@]}"

wait ${TRADER_PID}

#exec "$@"
