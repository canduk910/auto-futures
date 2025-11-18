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
LOOP_TRIGGER=event        # kline | timer | event
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

## 9. Google Cloud Run 준비 작업
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

## 10. 참고 문서
- `docs/architecture.md`: 더 자세한 구조, 이벤트 플로우, Pub/Sub 설계 및 배포 체크리스트가 수록되어 있습니다.

Auto-Futures는 실험용 코드로 제공되며, 실거래에 사용하기 전에 반드시 시뮬레이션과 리스크 검증을 진행하세요.
