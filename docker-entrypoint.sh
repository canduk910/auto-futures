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
  echo "[ENTRYPOINT] Restoring runtime files from gs://${GCS_BUCKET}/${GCS_PREFIX:-runtime/}"
  python - <<'PY'
import logging
from runtime_sync import safe_download
logging.basicConfig(level=logging.INFO)
safe_download()
PY
}

upload_runtime() {
  if [ "${UPLOAD_ON_EXIT:-true}" != "true" ]; then
    return 0
  fi
  if [ -z "${GCS_BUCKET:-}" ]; then
    return 0
  fi
  echo "[ENTRYPOINT] Uploading runtime files to gs://${GCS_BUCKET}/${GCS_PREFIX:-runtime/}"
  python - <<'PY'
import logging
from runtime_sync import safe_upload
logging.basicConfig(level=logging.INFO)
safe_upload()
PY
}

restore_runtime
if [ "${UPLOAD_ON_START:-false}" = "true" ]; then
  echo "[ENTRYPOINT] UPLOAD_ON_START=true → runtime 즉시 업로드"
  python - <<'PY'
import logging
from runtime_sync import safe_upload
logging.basicConfig(level=logging.INFO)
safe_upload()
PY
fi

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
