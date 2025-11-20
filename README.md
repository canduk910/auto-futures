# Auto-Futures Trading Service

Auto-Futures는 단일 프로세스에서 웹소켓 시세 수집, 급변 감지, AI 의사결정, 주문 집행, Streamlit UI 갱신까지 처리하는 자동 선물 트레이딩 실험 프로젝트입니다. 이 문서는 현재 구조와 주요 구성 요소, 배포 방법을 한눈에 볼 수 있도록 정리했습니다.

## 1. 런타임 토폴로지
```
┌─────────────────────────────────────────────────────────────┐
│ auto_future_trader.py                                       │
│  └─ service_runner.run_service(symbol, run_once)            │
│      ├─ FuturesWS (mark + kline + user WS)                  │
│      │    └─ event_queue (queue.Queue)                      │
│      ├─ VolatilityDetector (mark/kline 급변 감지)           │
│      ├─ run_once(symbol)                                    │
│      │    ├─ input_builder → tech_indicators                │
│      │    ├─ call_openai                                    │
│      │    ├─ binance_conn (REST)                            │
│      │    ├─ order_store + ws_cache (체결 추적)             │
│      │    └─ ui.status_store + runtime/*.jsonl              │
│      └─ UI: Streamlit app polling runtime/status files      │
└─────────────────────────────────────────────────────────────┘
```
- 하나의 Python 프로세스가 **웹소켓 수신, 이벤트 감지, 주문 실행, UI 상태 기록**을 모두 담당합니다.
- 내부 통신은 표준 `queue.Queue` 기반으로 메모리 내에서 수행됩니다.

## 2. 처리 플로우
1. `python auto_future_trader.py` 실행 시 `service_runner.run_service()`가 시작됩니다.
2. `FuturesWS`가 Binance 마크 프라이스/1분봉/User Data 스트림을 구독하고 `event_queue`, `WsCache`, `OrderStore`를 갱신합니다.
3. 서비스 루프가 이벤트를 소비하며 `VolatilityDetector`로 급변 조건을 판단합니다.
4. 조건을 만족하면 `run_once(symbol)`이 호출되어 입력 데이터(`input_builder`, `tech_indicators`)를 만들고 `call_openai`를 통해 AI 주문 지시를 받습니다.
5. `binance_conn`이 REST 주문/계정 호출을 담당하고 결과는 `order_store`와 User Data 이벤트를 통해 감시됩니다.
6. 상태/이력은 `ui.status_store`, `runtime/status.json`, `runtime/*.jsonl`에 기록됩니다.
7. Streamlit UI(`ui/ui_dashboard.py`)가 해당 파일을 주기적으로 읽어 화면을 갱신하고 `.env` 편집 기능을 제공합니다.

## 3. 소스 모듈 책임 요약
| 모듈 | 역할 |
| --- | --- |
| `auto_future_trader.py` | 엔트리 포인트. `run_once` 전략, OpenAI 의사결정, 포지션/로그 기록 |
| `service_runner.py` | 트리거 루프, 웹소켓 매니저, `VolatilityDetector`, 상태 업데이트 |
| `ws_streams.py` | Binance WS 연결 및 이벤트 분배 (`event_queue`, `ws_cache`, `order_store`) |
| `ws_cache.py` | 최신 시세/체결 캐시, 대기 편의 기능 |
| `order_store.py` | 주문 상태머신, User Data 이벤트 기반 체결 추적 |
| `input_builder.py` & `tech_indicators.py` | 시세/지표 수집 및 계산 |
| `call_openai.py` | OpenAI 호출 및 응답 파싱/검증 |
| `binance_conn.py` | Binance REST API 래퍼 (주문, 계정, 시세) |
| `ui/status_store.py` & `ui/ui_dashboard.py` | Runtime 상태 파일 관리 및 Streamlit UI |
| `docs/architecture.md` | 상세 설계 및 확장 아이디어 정리 |

## 4. 이벤트/로그 정책
- `VolatilityDetector`는 mark/kline 급변 판단 시 `delta_pct`, `threshold_pct`, `current/base price` 등 핵심 진단 값을 함께 기록합니다.
- 이벤트가 트리거되지 않은 경우에도 해당 조건에서 필요한 값만 로그에 포함해 가독성을 높였습니다.
- UI는 `.env` 편집 시 자동으로 따옴표를 제거하도록 구성되어 운영 편의성을 제공합니다.

## 5. 메시지 브로커 옵션
현재는 단순 `queue.Queue`를 사용하지만, 필요 시 아래처럼 확장할 수 있습니다.
- **Kafka**: `FuturesWS` → Kafka Producer, `service_runner` → Consumer 그룹. 파티션 키로 심볼 순서를 유지합니다.
- **Google Cloud Pub/Sub**: GCP 관리형 브로커. `publisher.publish(..., ordering_key=symbol)` 구조를 적용하면 멀티 프로세스 확장이 가능합니다. at-least-once 전달이므로 run_once 멱등성을 확보해야 합니다.
- **Redis Stream**: 경량 메시지 버퍼로서 큐보다 복원력을 얻을 수 있습니다.

## 6. 클라우드 배포 가이드
1. **인스턴스 준비**: Ubuntu 22.04 또는 GCE 인스턴스에 Python 3.11, venv, git 설치.
2. **코드 배포**:
   ```bash
   git clone git@github.com:canduk910/auto-futures.git
   cd auto-futures
   python3.11 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env  # 또는 별도 비밀 관리
   ```
3. **서비스 실행**:
   - 트레이더: `python auto_future_trader.py`
   - UI: `streamlit run ui/ui_dashboard.py --server.port 8501`
4. **프로세스 관리**: tmux/screen, systemd, 혹은 supervisord로 감시합니다.
5. **로그 & 상태**: `/var/log/auto-futures.log`, `runtime/status.json`, `runtime/*.jsonl`을 수집/백업.
6. **네트워크/보안**: Binance/OpenAI(443) 아웃바운드 허용, UI 포트는 VPN 또는 방화벽으로 제한.
7. **확장 전략**: 필요 시 웹소켓 수신과 주문 실행을 다른 인스턴스로 분리하고 Pub/Sub 또는 Kafka로 이벤트를 전달합니다.

## 7. 빠른 시작 명령 모음
```bash
# 1) 의존성 설치
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2) 서비스 실행
python auto_future_trader.py

# 3) Streamlit UI (선택)
streamlit run ui/ui_dashboard.py --server.port 8501 --server.enableCORS false
```

## 8. 환경 변수 샘플(.env)
서비스 실행 전 `.env`를 작성해 아래와 같이 핵심 키를 채워야 합니다.

```dotenv
# Binance API keys (fill in the values from your account)
BINANCE_TESTNET_API_KEY=
BINANCE_TESTNET_SECRET_KEY=
BINANCE_API_KEY=
BINANCE_SECRET_KEY=

# OPEN AI API Key
OPENAI_API_KEY=
LOG_LEVEL=INFO

# Default environment ("paper" for testnet, "live" for real trading)
ENV=paper
DRY_RUN=False   # 주문실행 테스트용 플래그 (True: 주문 실행 안함, False: 실제 주문 실행)
SYMBOL=ETHUSDT  # 거래 심볼 (예: BTCUSDT, ETHUSDT 등)
LEVERAGE=5      # 레버리지 설정 (예: 1, 5, 10, 20 등)

# 웹소켓 설정 (WebSocket settings)
WS_ENABLE=true          # 웹소켓 사용 여부
WS_USER_ENABLE=true     # 사용자 데이터 스트림 사용 여부
WS_PRICE_ENABLE=true    # 가격 데이터 스트림 사용 여부
WS_TRACE=true           # 웹소켓 디버그 트레이스 출력 여부

# Loop/runner options
LOOP_ENABLE=true
LOOP_TRIGGER=event        # kline  timer  event
LOOP_INTERVAL_SEC=60
LOOP_COOLDOWN_SEC=8
LOOP_BACKOFF_MAX_SEC=30

# Volatility detector parameters
MP_WINDOW_SEC=10      # 변동성 탐지기 창 길이 (초)
MP_DELTA_PCT=0.15     # 변동성 탐지기 델타 퍼센트 임계값
KLINE_RANGE_PCT=0.6   # 캔들 범위 퍼센트 임계값
VOL_LOOKBACK=20       # 거래량 조회 기간
VOL_MULT=3.0          # 거래량 배수 (조회기간의 평균거래량 대비 직전 거래량)
USE_QUOTE_VOLUME=true
```

필요 시 `docs/architecture.md`의 체크리스트를 참고해 운영 환경 값을 조정하세요.

## 9. OpenAI JSON 입출력 구조
OpenAI 호출(`call_openai.py`)은 입력 JSON을 시스템 프롬프트와 함께 전달하고, 모델이 동일한 스키마로 응답하도록 강제합니다. 아래 표를 참고해 필수 필드를 맞춰 주세요.

**입력 JSON 주요 필드**

| 필드 | 설명 |
| --- | --- |
| `symbol`, `env`, `constraints` | 거래 심볼, 실행 환경(paper/live), 제약 조건(금지 시간 등) |
| `market.snapshots` | 마크프라이스, 캔들, 거래량 등 실시간 시세 스냅샷 |
| `indicators` | `tech_indicators.py`에서 계산한 기술 지표 모음 |
| `account` | 계정 잔고, 포지션, 사용 가능 마진 등 상태 정보 |
| `orders`, `positions` | 미체결 주문과 포지션 내역(WS/REST 기반) |
| `risk_limits` | 계정/전략별 손실 한도, 레버리지 한계 값 |

**출력 JSON 스키마 요약**

| 필드 | 설명 |
| --- | --- |
| `decision` | `long`/`short`/`flat` 중 하나의 최종 판단 |
| `timeframe` | `scalp`/`intraday`/`swing` 등 권장 보유 기간 |
| `rationale` | 진입 근거, 시장 해석, 위험 요약 |
| `position.entry` | 주문 유형(`limit`/`market`), 진입가, 스케일 인 전략 |
| `position.size` | 계약 수량, 사용 레버리지, 증거금, 리스크 비율 |
| `position.stop_loss` | 마크/라스트 기준 손절 위치 및 사유 |
| `position.take_profits` | 목표가 리스트(가격 + 청산 비율) |
| `position.trailing_stop` | 추적 손절 활성화 가격과 콜백 비율 |
| `position.expected_fees` | 진입/청산/펀딩 비용 추정 |
| `position.estimated_liquidation_price` | 예상 청산가 또는 추정치 |
| `risk` | R-multiple, 상방 확률, 최대 손실(금액/%) 등 |
| `scenarios` | bull/base/bear 시나리오 요약 |
| `invalidations` | 전략이 무효화되는 조건 목록 |
| `confidence` | 0~1 사이 신뢰도 스코어 |
| `next_check_after_min` | 다음 점검까지 대기 시간(분) |
| `compliance.reason_codes` | 내부 검증 코드(예: `RISK_OK`) |
| `notes` | 면책 문구 및 추가 메모 |

## 10. 클라우드 배포 가이드
1. Docker 이미지 빌드 (로컬 테스트 권장):
   ```bash
   docker build -t auto-futures:local .
   docker run --rm --env-file .env auto-futures:local
   ```
2. Artifact Registry/Container Registry에 업로드:
   ```bash
   gcloud auth configure-docker
   gcloud builds submit --tag gcr.io/PROJECT_ID/auto-futures
   ```
3. Secret Manager에 주요 비밀 저장 후 Cloud Run에 맵핑:
   ```bash
   gcloud secrets create binance-key --data-file=- <<'EOF'
   BINANCE_TESTNET_API_KEY=...
   BINANCE_TESTNET_SECRET_KEY=...
   EOF
   gcloud secrets create openai-key --data-file=- <<'EOF'
   OPENAI_API_KEY=...
   EOF
   ```
4. Cloud Run 배포 예시:
   ```bash
   gcloud run deploy auto-futures \
     --image gcr.io/PROJECT_ID/auto-futures \
     --region asia-northeast3 \
     --platform managed \
     --memory 1Gi \
     --cpu 1 \
     --max-instances 1 \
     --set-env-vars ENV=paper,SYMBOL=ETHUSDT,DRY_RUN=false,LOG_LEVEL=INFO \
     --set-secrets BINANCE_TESTNET_API_KEY=binance-key:latest,BINANCE_TESTNET_SECRET_KEY=binance-key:latest,OPENAI_API_KEY=openai-key:latest \
     --no-allow-unauthenticated
   ```
5. 필요 시 Streamlit UI를 별도 서비스로 배포하거나 Cloud Run Jobs를 사용해 백오피스 배치를 실행하세요.
6. CLI 스크립트 활용: 프로젝트 루트의 `scripts/cloud_run_deploy.sh`는 컨테이너 빌드부터 배포, 환경 변수/시크릿 설정까지 자동화합니다.
   ```bash
   cd /Users/koscom/Projects/auto-futures
   PROJECT_ID=<YOUR_GCP_PROJECT> REGION=asia-northeast3 SERVICE=auto-futures ./scripts/cloud_run_deploy.sh
   ```
   Secrets(`binance-testnet-api-key`, `binance-testnet-secret-key`, `openai-api-key`)가 미리 생성되어 있어야 하며, 없으면 스크립트가 경고를 출력합니다.
7. IAM/Artifact Registry 선행 작업: Cloud Build/Compute 기본 서비스 계정이 이미지를 푸시하려면 Artifact Registry 쓰기 권한과 GCS object 권한이 필요합니다. 프로젝트 번호가 `216337086276`이라면 다음 명령을 한 번 실행하세요.
   ```bash
   PROJECT_ID=<YOUR_GCP_PROJECT>
   PROJECT_NUMBER=216337086276

   gcloud projects add-iam-policy-binding $PROJECT_ID \
     --member="serviceAccount:${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com" \
     --role="roles/artifactregistry.writer"

   gcloud projects add-iam-policy-binding $PROJECT_ID \
     --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
     --role="roles/artifactregistry.writer"

   gcloud projects add-iam-policy-binding $PROJECT_ID \
     --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
     --role="roles/storage.objectAdmin"
   ```
   또한 Artifact Registry를 사전 생성해야 합니다.
   ```bash
   gcloud artifacts repositories create auto-futures \
     --repository-format=docker \
     --location=asia-northeast3 \
     --project $PROJECT_ID
   ```
   API(`artifactregistry.googleapis.com`, `containerregistry.googleapis.com`, `cloudbuild.googleapis.com`, `secretmanager.googleapis.com`)를 활성화한 뒤 배포 스크립트를 실행하면 권한 오류 없이 진행됩니다.
8. gcr.io 대신 Artifact Registry 경로 사용: `gcr.io/PROJECT_ID/...` 레포는 기본적으로 존재하지 않으며, create-on-push 권한이 없다면 Cloud Build가 반복 실패합니다. 권장 방법은 Artifact Registry 경로를 직접 지정하는 것입니다.
   1) 위 7번 단계에서 레포(`auto-futures`)를 만들었다면 `scripts/cloud_run_deploy.sh`의 `IMAGE` 값을 아래처럼 바꿉니다.
      ```bash
      IMAGE="asia-northeast3-docker.pkg.dev/${PROJECT_ID}/auto-futures/auto-futures"
      ```
   2) gcr.io를 계속 쓰려면 Container Registry 버킷을 직접 생성하고(예: `gsutil mb -p $PROJECT_ID gs://artifacts.$PROJECT_ID.appspot.com`) 동일한 서비스 계정에 `roles/storage.objectAdmin` 권한을 부여해야 합니다.
   3) 배포 로그에 `denied: gcr.io repo does not exist`가 보이면 이 단계가 누락된 것이므로 IAM/레포 설정 후 `PROJECT_ID=... ./scripts/cloud_run_deploy.sh`를 다시 실행합니다.
9. Secret Manager 권한 부여: Cloud Run 리비전이 비밀을 읽으려면 실행 SA(기본값: `${PROJECT_NUMBER}-compute@developer.gserviceaccount.com`)에게 `roles/secretmanager.secretAccessor` 권한이 있어야 합니다.
   ```bash
   PROJECT_ID=<YOUR_GCP_PROJECT>
   PROJECT_NUMBER=<PROJECT_NUMBER>
   SERVICE_ACCOUNT=${PROJECT_NUMBER}-compute@developer.gserviceaccount.com

   gcloud secrets add-iam-policy-binding binance-testnet-api-key \
     --member="serviceAccount:${SERVICE_ACCOUNT}" \
     --role="roles/secretmanager.secretAccessor" \
     --project $PROJECT_ID

   gcloud secrets add-iam-policy-binding binance-testnet-secret-key \
     --member="serviceAccount:${SERVICE_ACCOUNT}" \
     --role="roles/secretmanager.secretAccessor" \
     --project $PROJECT_ID

   gcloud secrets add-iam-policy-binding openai-api-key \
     --member="serviceAccount:${SERVICE_ACCOUNT}" \
     --role="roles/secretmanager.secretAccessor" \
     --project $PROJECT_ID
   ```
   프로젝트 전체에 동일 권한을 주려면 `gcloud projects add-iam-policy-binding ... --role roles/secretmanager.secretAccessor`를 사용하세요. 권한이 없으면 `spec.template...Permission denied on secret` 오류가 발생합니다.

## 11. 참고 문서
- `docs/architecture.md`: 더 자세한 구조, 이벤트 플로우, Pub/Sub 설계 및 배포 체크리스트가 수록되어 있습니다.

Auto-Futures는 실험용 코드로 제공되며, 실거래에 사용하기 전에 반드시 시뮬레이션과 리스크 검증을 진행하세요.

## 12. 런타임 데이터 영구보존
이 서비스는 `runtime/` 디렉터리에 AI 자문, 거래/청산 내역을 JSONL 형태로 쌓습니다. Cloud Run처럼 휘발성 컨테이너 환경에서는 재시작 시 이 파일들이 사라지므로, GCS 버킷으로 주기적으로 백업/복원하는 절차를 함께 운영하세요.

### 12.1 GCS 동기화 스크립트
`scripts/gcs_sync.py`는 `runtime/**/*.jsonl|json|ndjson` 파일을 GCS 버킷과 동기화합니다. 실행 전 `pip install google-cloud-storage`로 의존성을 추가하세요.

환경 변수:
- `GCS_BUCKET` (또는 `BUCKET_NAME`): 대상 버킷 이름

사용 예시:
```bash
# 종료 직전에 백업
GCS_BUCKET=my-bucket python3 scripts/gcs_sync.py upload

# 기동 직후 복원
GCS_BUCKET=my-bucket python3 scripts/gcs_sync.py download
```

### 12.2 자동화 팁
- Cloud Run 컨테이너의 `docker-entrypoint.sh`에서 기동 시 `download`, 종료 Signal 처리에서 `upload`를 호출하면 무정지로 복원할 수 있습니다.
- 정기 백업이 필요하면 Cloud Scheduler → Cloud Run Job 조합으로 `upload`를 실행하세요.

### 12.3 권한과 보안
- 업로드에는 `roles/storage.objectCreator` 이상, 다운로드에는 `roles/storage.objectViewer` 이상 권한이 필요합니다.
- 서비스 계정 권한 부여 예시:
  ```bash
  PROJECT_ID=...
  SERVICE_ACCOUNT=...@${PROJECT_ID}.iam.gserviceaccount.com

  gcloud projects add-iam-policy-binding ${PROJECT_ID} \
    --member="serviceAccount:${SERVICE_ACCOUNT}" \
    --role="roles/storage.objectAdmin"
  ```
- GCS 버킷에도 수명주기(Lifecycle) 규칙을 설정해 오래된 JSONL을 자동 정리할 수 있습니다.

런타임 데이터를 꾸준히 백업하면 AI 자문 히스토리/체결 로그를 재학습 자료나 감사 목적으로 활용하기 쉬워집니다.
