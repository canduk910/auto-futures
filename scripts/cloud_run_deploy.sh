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
BUILD_LOGGING=${BUILD_LOGGING:-cloud-logging-only}
GRANT_SECRET_ROLES=${GRANT_SECRET_ROLES:-true}
SECRET_ROLE=${SECRET_ROLE:-roles/secretmanager.admin}
SUPPORTS_BUILD_LOGGING_FLAG=""

ensure_secret() {
  local secret="$1"
  if ! gcloud secrets describe "$secret" >/dev/null 2>&1; then
    echo "[WARN] Secret $secret not found. Create it before deployment." >&2
  fi
}

detect_build_logging_support() {
  [[ -n "$SUPPORTS_BUILD_LOGGING_FLAG" ]] && return 0
  if gcloud builds submit --help 2>/dev/null | grep -q "--logging"; then
    SUPPORTS_BUILD_LOGGING_FLAG="yes"
  else
    SUPPORTS_BUILD_LOGGING_FLAG="no"
  fi
}

maybe_grant_secret_roles() {
  [[ "$GRANT_SECRET_ROLES" == "true" ]] || return 0
  local project_number
  project_number=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
  local build_sa="${project_number}@cloudbuild.gserviceaccount.com"
  local run_sa="${project_number}-compute@developer.gserviceaccount.com"
  echo "[STEP] Ensuring Secret Manager role $SECRET_ROLE for $build_sa and $run_sa"
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${build_sa}" \
    --role="$SECRET_ROLE" >/dev/null
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${run_sa}" \
    --role="$SECRET_ROLE" >/dev/null
}

main() {
  gcloud config set project "$PROJECT_ID" >/dev/null

  maybe_grant_secret_roles

  echo "[STEP] Building container image ${IMAGE}"
  detect_build_logging_support
  if [[ "$SUPPORTS_BUILD_LOGGING_FLAG" == "yes" ]]; then
    gcloud builds submit --tag "$IMAGE" --logging="$BUILD_LOGGING"
  else
    echo "[INFO] gcloud builds submit --logging not supported on this version; using default logging behavior"
    gcloud builds submit --tag "$IMAGE"
  fi

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
    --set-env-vars ENV=paper,DRY_RUN=false,SYMBOL=ETHUSDT,LOG_LEVEL=INFO,WS_ENABLE=true,WS_USER_ENABLE=true,WS_PRICE_ENABLE=true,WS_TRACE=false,LOOP_ENABLE=true,LOOP_TRIGGER=event,LOOP_INTERVAL_SEC=60,LOOP_COOLDOWN_SEC=8,LOOP_BACKOFF_MAX_SEC=30,MP_WINDOW_SEC=10,MP_DELTA_PCT=0.2,KLINE_RANGE_PCT=0.5,VOL_LOOKBACK=20,VOL_MULT=3.0,USE_QUOTE_VOLUME=true,LEVERAGE=5,TZ=Asia/Seoul,PROJECT_ID=${PROJECT_ID},GOOGLE_CLOUD_PROJECT=${PROJECT_ID}
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
