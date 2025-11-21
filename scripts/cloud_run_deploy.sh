#!/usr/bin/env bash
# Helper to build & deploy auto-futures to Cloud Run with env/secrets.
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO_ROOT"

ENV_FILE=${ENV_FILE:-.env}
LOAD_ENV_FILE=${LOAD_ENV_FILE:-true}
if [[ "$LOAD_ENV_FILE" == "true" && -f "$ENV_FILE" ]]; then
  echo "[STEP] Loading environment variables from $ENV_FILE"
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

PROJECT_ID=${PROJECT_ID:?"set PROJECT_ID environment variable (e.g., PROJECT_ID=my-project)"}
REGION=${REGION:-asia-northeast3}
SERVICE=${SERVICE:-auto-futures}
IMAGE_NAME=${IMAGE_NAME:-auto-futures}
IMAGE=${IMAGE:-"asia-northeast3-docker.pkg.dev/${PROJECT_ID}/auto-futures/${IMAGE_NAME}"}
BUILD_CONFIG=${BUILD_CONFIG:-cloudbuild.yaml}
MEMORY=${MEMORY:-1Gi}
CPU=${CPU:-1}
MAX_INSTANCES=${MAX_INSTANCES:-1}
ALLOW_UNAUTH=${ALLOW_UNAUTH:-true}
BUILD_LOGGING=${BUILD_LOGGING:-cloud-logging-only}
GRANT_SECRET_ROLES=${GRANT_SECRET_ROLES:-true}
SECRET_ROLE=${SECRET_ROLE:-roles/secretmanager.admin}
SUPPORTS_BUILD_LOGGING_FLAG=""
SYNC_RUNTIME=${SYNC_RUNTIME:-false}
SYNC_MODE=${SYNC_MODE:-upload}
GCS_BUCKET=${GCS_BUCKET:-}
GCS_PREFIX=${GCS_PREFIX:-runtime/}

add_env_var() {
  local key="$1"
  local value="$2"
  for i in "${!env_vars[@]}"; do
    if [[ "${env_vars[$i]%%=*}" == "$key" ]]; then
      env_vars[$i]="$key=$value"
      return
    fi
  done
  env_vars+=("$key=$value")
}

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

sync_runtime() {
  [[ "$SYNC_RUNTIME" == "true" ]] || return 0
  if [[ -z "$GCS_BUCKET" ]]; then
    echo "[WARN] SYNC_RUNTIME=true지만 GCS_BUCKET이 설정되지 않았습니다. runtime 동기화를 건너뜁니다." >&2
    return 0
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    echo "[WARN] python3를 찾을 수 없어 runtime 동기화를 건너뜁니다." >&2
    return 0
  fi
  echo "[STEP] Running runtime sync (${SYNC_MODE}) with bucket ${GCS_BUCKET}"
  if [[ "$SYNC_MODE" == "upload" ]]; then
    python3 scripts/gcs_sync.py --bucket "$GCS_BUCKET" "$SYNC_MODE" --dest-prefix "$GCS_PREFIX" || echo "[WARN] runtime 동기화 실패 (무시하고 계속 진행)" >&2
  else
    python3 scripts/gcs_sync.py --bucket "$GCS_BUCKET" "$SYNC_MODE" --prefix "$GCS_PREFIX" || echo "[WARN] runtime 동기화 실패 (무시하고 계속 진행)" >&2
  fi
}

main() {
  gcloud config set project "$PROJECT_ID" >/dev/null

  sync_runtime

  maybe_grant_secret_roles

  echo "[STEP] Building container image ${IMAGE}"
  detect_build_logging_support
  if [[ -f "$BUILD_CONFIG" ]]; then
    if [[ "$SUPPORTS_BUILD_LOGGING_FLAG" == "yes" ]]; then
      gcloud builds submit --config "$BUILD_CONFIG" --substitutions=_IMAGE="$IMAGE" --logging="$BUILD_LOGGING"
    else
      echo "[INFO] gcloud builds submit --logging not supported on this version; using default logging behavior"
      gcloud builds submit --config "$BUILD_CONFIG" --substitutions=_IMAGE="$IMAGE"
    fi
  else
    if [[ "$SUPPORTS_BUILD_LOGGING_FLAG" == "yes" ]]; then
      gcloud builds submit --tag "$IMAGE" --logging="$BUILD_LOGGING"
    else
      echo "[INFO] gcloud builds submit --logging not supported on this version; using default logging behavior"
      gcloud builds submit --tag "$IMAGE"
    fi
  fi

  ensure_secret binance-testnet-api-key
  ensure_secret binance-testnet-secret-key
  ensure_secret openai-api-key

  echo "[STEP] Deploying ${SERVICE} to Cloud Run"
  env_vars=()

  add_env_var "ENV" "${ENV:-paper}"
  add_env_var "DRY_RUN" "${DRY_RUN:-false}"
  add_env_var "SYMBOL" "${SYMBOL:-ETHUSDT}"
  add_env_var "LOG_LEVEL" "${LOG_LEVEL:-INFO}"
  add_env_var "WS_ENABLE" "${WS_ENABLE:-true}"
  add_env_var "WS_USER_ENABLE" "${WS_USER_ENABLE:-true}"
  add_env_var "WS_PRICE_ENABLE" "${WS_PRICE_ENABLE:-true}"
  add_env_var "WS_TRACE" "${WS_TRACE:-false}"
  add_env_var "LOOP_ENABLE" "${LOOP_ENABLE:-true}"
  add_env_var "LOOP_TRIGGER" "${LOOP_TRIGGER:-event}"
  add_env_var "LOOP_INTERVAL_SEC" "${LOOP_INTERVAL_SEC:-60}"
  add_env_var "LOOP_COOLDOWN_SEC" "${LOOP_COOLDOWN_SEC:-8}"
  add_env_var "LOOP_BACKOFF_MAX_SEC" "${LOOP_BACKOFF_MAX_SEC:-30}"
  add_env_var "MP_WINDOW_SEC" "${MP_WINDOW_SEC:-10}"
  add_env_var "MP_DELTA_PCT" "${MP_DELTA_PCT:-0.35}"
  add_env_var "KLINE_RANGE_PCT" "${KLINE_RANGE_PCT:-0.6}"
  add_env_var "VOL_LOOKBACK" "${VOL_LOOKBACK:-20}"
  add_env_var "VOL_MULT" "${VOL_MULT:-3.0}"
  add_env_var "USE_QUOTE_VOLUME" "${USE_QUOTE_VOLUME:-true}"
  add_env_var "LEVERAGE" "${LEVERAGE:-5}"
  add_env_var "TZ" "${TZ:-Asia/Seoul}"
  add_env_var "PROJECT_ID" "${PROJECT_ID}"
  add_env_var "GOOGLE_CLOUD_PROJECT" "${PROJECT_ID}"

  secret_keys_regex='^(BINANCE_TESTNET_API_KEY|BINANCE_TESTNET_SECRET_KEY|OPENAI_API_KEY)$'
  if [[ "$LOAD_ENV_FILE" == "true" && -f "$ENV_FILE" ]]; then
    while IFS= read -r line || [[ -n "$line" ]]; do
      line="${line#${line%%[![:space:]]*}}"
      line="${line%${line##*[![:space:]]}}"
      if [[ "$line" =~ ^([^#]*[^[:space:]])[[:space:]]+#.*$ ]]; then
        line="${BASH_REMATCH[1]}"
      fi
      [[ -z "$line" || "${line:0:1}" == "#" ]] && continue
      [[ "$line" == export* ]] && line="${line#export }"
      key="${line%%=*}"
      value="${line#*=}"
      key="${key#${key%%[![:space:]]*}}"
      key="${key%${key##*[![:space:]]}}"
      [[ -z "$key" || "$key" =~ $secret_keys_regex ]] && continue
      add_env_var "$key" "$value"
    done < "$ENV_FILE"
  fi

  [[ -n "$GCS_BUCKET" ]] && add_env_var "GCS_BUCKET" "$GCS_BUCKET"
  [[ -n "$GCS_PREFIX" ]] && add_env_var "GCS_PREFIX" "$GCS_PREFIX"
  [[ -n "${RESTORE_RUNTIME:-}" ]] && add_env_var "RESTORE_RUNTIME" "$RESTORE_RUNTIME"

  env_vars_payload=""
  for kv in "${env_vars[@]}"; do
    env_vars_payload+="${kv}|"
  done
  env_vars_payload="${env_vars_payload%|}"

  deploy_args=(
    --image "$IMAGE"
    --region "$REGION"
    --platform managed
    --memory "$MEMORY"
    --cpu "$CPU"
    --max-instances "$MAX_INSTANCES"
    --set-env-vars "^|^${env_vars_payload}"
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
