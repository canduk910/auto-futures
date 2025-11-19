#!/usr/bin/env bash
# Helper to build & deploy auto-futures to Cloud Run with env/secrets.
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO_ROOT"

PROJECT_ID=${PROJECT_ID:?"set PROJECT_ID environment variable (e.g., PROJECT_ID=my-project)"}
REGION=${REGION:-asia-northeast3}
SERVICE=${SERVICE:-auto-futures}
IMAGE_NAME=${IMAGE_NAME:-auto-futures}
IMAGE=${IMAGE:-"asia-northeast3-docker.pkg.dev/${PROJECT_ID}/auto-futures/${IMAGE_NAME}"}
MEMORY=${MEMORY:-1Gi}
CPU=${CPU:-1}
MAX_INSTANCES=${MAX_INSTANCES:-1}
ALLOW_UNAUTH=${ALLOW_UNAUTH:-true}

ensure_secret() {
  local secret="$1"
  if ! gcloud secrets describe "$secret" >/dev/null 2>&1; then
    echo "[WARN] Secret $secret not found. Create it before deployment." >&2
  fi
}

main() {
  gcloud config set project "$PROJECT_ID" >/dev/null

  echo "[STEP] Building container image ${IMAGE}"
  gcloud builds submit --tag "$IMAGE"

  ensure_secret binance-testnet-api-key
  ensure_secret binance-testnet-secret-key
  ensure_secret openai-api-key

  echo "[STEP] Deploying ${SERVICE} to Cloud Run"
  deploy_args=(
    --image "$IMAGE"
    --region "$REGION"
    --platform managed
    --memory "$MEMORY"
    --cpu "$CPU"
    --max-instances "$MAX_INSTANCES"
    --set-env-vars ENV=paper,DRY_RUN=false,SYMBOL=ETHUSDT,LOG_LEVEL=INFO,WS_ENABLE=true,WS_USER_ENABLE=true,WS_PRICE_ENABLE=true,WS_TRACE=false,LOOP_ENABLE=true,LOOP_TRIGGER=event,LOOP_INTERVAL_SEC=60,LOOP_COOLDOWN_SEC=8,LOOP_BACKOFF_MAX_SEC=30,MP_WINDOW_SEC=10,MP_DELTA_PCT=0.35,KLINE_RANGE_PCT=0.6,VOL_LOOKBACK=20,VOL_MULT=3.0,USE_QUOTE_VOLUME=true,LEVERAGE=5,TZ=Asia/Seoul
    --set-secrets BINANCE_TESTNET_API_KEY=binance-testnet-api-key:latest,BINANCE_TESTNET_SECRET_KEY=binance-testnet-secret-key:latest,OPENAI_API_KEY=openai-api-key:latest
  )
  if [[ "${ALLOW_UNAUTH}" == "true" ]]; then
    deploy_args+=(--allow-unauthenticated)
  else
    deploy_args+=(--no-allow-unauthenticated)
  fi

  gcloud run deploy "$SERVICE" "${deploy_args[@]}"

  echo "[DONE] Deployment finished."
}

main "$@"
