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

upload_runtime() {
  if [ "${UPLOAD_ON_EXIT:-true}" != "true" ]; then
    return 0
  fi
  if [ -z "${GCS_BUCKET:-}" ]; then
    return 0
  fi
  PREFIX="${GCS_PREFIX:-runtime/}"
  echo "[ENTRYPOINT] Uploading runtime files to gs://${GCS_BUCKET}/${PREFIX}"
  if ! python scripts/gcs_sync.py --bucket "${GCS_BUCKET}" upload --dest-prefix "${PREFIX}"; then
    echo "[ENTRYPOINT] runtime 업로드 실패 (무시하고 종료)" >&2
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
STREAMLIT_PID=""
CLEANED_UP=false

cleanup() {
  if [ "$CLEANED_UP" = "true" ]; then
    return
  fi
  CLEANED_UP=true
  echo "[ENTRYPOINT] Caught signal, performing cleanup"
  upload_runtime || true
  if [ -n "$STREAMLIT_PID" ]; then
    kill "$STREAMLIT_PID" 2>/dev/null || true
    wait "$STREAMLIT_PID" 2>/dev/null || true
  fi
  kill "$TRADER_PID" 2>/dev/null || true
  wait "$TRADER_PID" 2>/dev/null || true
}

trap cleanup TERM INT EXIT

"${STREAMLIT_CMD[@]}" &
STREAMLIT_PID=$!

echo "[ENTRYPOINT] Launched auto_future_trader.py (pid=${TRADER_PID})"
echo "[ENTRYPOINT] Starting Streamlit dashboard on port ${PORT} (pid=${STREAMLIT_PID})"

wait -n "$TRADER_PID" "$STREAMLIT_PID"
EXIT_CODE=$?
cleanup
exit $EXIT_CODE
