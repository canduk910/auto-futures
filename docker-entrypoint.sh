#!/bin/bash
set -euo pipefail

if [ -n "${GOOGLE_CLOUD_PROJECT:-}" ]; then
  echo "[ENTRYPOINT] Running in Cloud Run project ${GOOGLE_CLOUD_PROJECT}"
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
trap "echo '[ENTRYPOINT] Caught signal, stopping processes'; kill ${TRADER_PID}; wait ${TRADER_PID} 2>/dev/null" TERM INT

echo "[ENTRYPOINT] Launched auto_future_trader.py (pid=${TRADER_PID})"
echo "[ENTRYPOINT] Starting Streamlit dashboard on port ${PORT}"
"${STREAMLIT_CMD[@]}"

wait ${TRADER_PID}

#exec "$@"

