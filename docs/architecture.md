# Auto-Futures Architecture & Recent Design Notes

## 1. Runtime Topology (Current)
```
┌─────────────────────────────────────────────────────────────┐
│ auto_future_trader.py                                       │
│  └─ service_runner.run_service(symbol, run_once)            │
│      ├─ FuturesWS (mark + kline + user WS)                  │
│      │    └─ event_queue (queue.Queue)                      │
│      ├─ VolatilityDetector (mark/kline 급변 감지)           │
│      ├─ run_once(symbol)                                    │
│      │    ├─ input_builder → tech_indicators                │
│      │    ├─ call_openai                                     │
│      │    ├─ binance_conn (REST)                             │
│      │    ├─ order_store + ws_cache (체결 추적)             │
│      │    └─ ui.status_store + runtime/*.jsonl              │
│      └─ UI: Streamlit app polling runtime/ status files     │
└─────────────────────────────────────────────────────────────┘
```
- 단일 프로세스가 **웹소켓 수신, 급변 감지, AI 주문 실행, UI 상태 기록**을 모두 처리합니다.
- 내부 통신은 동일 프로세스 메모리를 통해 이뤄지며, `event_queue`는 표준 `queue.Queue` 입니다.

## 2. 상세 동작 플로우
1. `auto_future_trader.py` 실행 → `service_runner.run_service()` 진입.
2. `FuturesWS` 가 Binance WebSocket(마크프라이스, 1분봉, User Data)을 구독하고 이벤트를 `event_queue`와 `WsCache`/`OrderStore`에 전달.
3. 서비스 루프가 이벤트를 소비하며 트리거 모드에 따라 `VolatilityDetector` 평가 → 조건 충족 시 `run_once(symbol)` 실행.
4. `run_once`는 `input_builder`/`tech_indicators`로 데이터 구성 → `call_openai`로 AI 주문 지시 획득 → `binance_conn`을 통해 주문/계정 REST 호출.
5. 주문 체결 상태는 `OrderStore` + 실시간 User Data 이벤트로 감시되고, 결과는 `ui.status_store`와 `runtime/*.jsonl`에 기록.
6. Streamlit UI(`ui/ui_dashboard.py`)가 상태 파일을 주기적으로 읽어 화면을 갱신하고 `.env` 편집/업로드를 제공합니다.

## 3. 주요 모듈 책임 요약
| 모듈 | 주요 역할 |
| --- | --- |
| `service_runner.py` | 트리거 루프, WS 관리, 급변 감지, `run_once` 호출, 상태 업데이트 |
| `ws_streams.py` | Binance WS 연결, 이벤트 언래핑, `event_queue` / `WsCache` / `OrderStore` 업데이트 |
| `ws_cache.py` | 최근 마크프라이스, kline, 주문 이벤트 캐시 및 대기 편의 함수 |
| `order_store.py` | 주문 상태 머신, User Data 이벤트 기반으로 터미널 상태 알림 |
| `auto_future_trader.py` | `run_once` 구현, OpenAI 의사결정, 주문 전략, 포지션 관리, UI 기록 |
| `input_builder.py` & `tech_indicators.py` | 시세/지표 수집 및 계산 |
| `call_openai.py` | OpenAI API 호출, 응답 파싱/검증 |
| `ui/status_store.py` & `ui/ui_dashboard.py` | 런타임 상태/이력 파일 관리, Streamlit 대시보드 |

## 4. 최근 변경 사항 정리
| 변경 항목 | 내용 |
| --- | --- |
| `.env` 편집 UX | UI에서 `.env` 값을 수정/업로드할 때 자동 따옴표를 제거 (`set_key(..., quote_mode='never')`), 오타(`USE_QUOTE_VOLUME`) 정정 |
| 환경 변수 노출 | `.env` 직접 편집 시 수치형 값에 불필요한 문자열이 저장되지 않도록 필터 |
| 이벤트 진단 로깅 | `VolatilityDetector`의 mark/kline 진단 정보를 상세화. `delta_pct`, `threshold_pct`, `current/base price`, `range_pct`, `vol_ratio` 등이 조건 불충족 시 로그에 나타나도록 `_format_diag` 개선 |
| 조건별 로그 축소 | 이벤트 미발생 시 해당 이유에 필요한 값만 로그에 포함하여 가독성 개선 |

## 5. 최종 설계 스냅샷 (2025-11)
- **단일 프로세스** 구성을 유지하면서 WebSocket → `event_queue` → VolatilityDetector → `run_once` 흐름이 명확히 정리되었습니다.
- `.env` 조작과 로그 진단 등 운영 편의성이 강화되었습니다.
- UI는 Runtime JSONL/JSON 파일을 통해 서비스 상태, AI 히스토리, 포지션 정보를 조회합니다.
- 향후 확장을 위해 `VolatilityDetector` 진단 정보가 충분히 노출되므로 이벤트 트리거 기준 조정이 용이합니다.

## 6. 메시지 전송 계층 대안 (큐 vs. Kafka vs. Pub/Sub)
### 6.1 현재(`queue.Queue`)
- **장점**: 구현이 단순하고 의존성이 없음. 단일 프로세스 메모리 공유라 지연이 거의 없음.
- **단점**: 같은 프로세스에서만 사용 가능. 서비스가 죽으면 이벤트도 함께 사라짐. 다중 소비자/스케일아웃 불가.

### 6.2 Kafka로 전환 시 고려사항
| 항목 | 영향 |
| --- | --- |
| 이벤트 생산 | `FuturesWS._emit()`에서 `queue.put_nowait` 대신 Kafka Producer(`topic: ws-events`) 전환 필요 |
| 이벤트 소비 | `service_runner`는 Kafka Consumer 그룹으로 전환. 파티션 설계를 통해 심볼별 순서 보장 가능 |
| Ordered delivery | Kafka 파티션 키를 심볼로 잡으면 동일 심볼의 이벤트 순서 보장 가능 |
| Backpressure | Kafka가 자체적으로 버퍼링하므로 로컬 큐 오버플로우 문제 완화 |
| 복잡도 | Kafka 클러스터/브로커 운영 필요. 네트워크/보안 구성 및 운영자가 필요 |
| 장애 대응 | Producer/Consumer 재시도 로직과 오프셋 관리 필요. 재처리 전략까지 설계해야 함 |

### 6.3 Google Cloud Pub/Sub 모델 적용 시 고려사항
| 항목 | 영향 |
| --- | --- |
| 이벤트 생산 | `FuturesWS._emit()`에서 Google Cloud Pub/Sub Publisher API 호출. 주제(topic) 예시: `projects/<project-id>/topics/ws-events`. 배치 퍼블리셔를 사용하면 네트워크 왕복과 비용 절감. |
| 이벤트 소비 | `service_runner`는 Pub/Sub Subscriber 클라이언트로 polling. Cloud Run / GCE 인스턴스 어디서든 subscriber 실행 가능하며 Ack 데드라인을 적절히 설정해야 함. |
| At-least-once | Pub/Sub은 기본적으로 **at-least-once** 전달이므로, `VolatilityDetector`/`run_once` 쪽에서 이벤트 ID 중복 처리 또는 캐시 기반 중복 제거 필요. |
| Ordered delivery | 구독자 당 "ordering key" 를 활용하면 심볼별 순서를 강제할 수 있으나, 토픽을 지역(Region) 고정으로 생성하고 ordering key(예: `symbol`)을 발행 시 함께 전달해야 함. |
| Backpressure | Pub/Sub이 자동으로 스케일하며 Ack 속도로 흐름 제어. 처리 지연 시 unacked 메시지 수를 모니터링하고 Ack 데드라인을 조정. |
| 운영 | Kafka 클러스터 운영 대신 GCP 관리형 서비스 사용. Service Account, Pub/Sub IAM, VPC-SC 등을 통해 보안을 구성. |
| 비용 | 메시지 수/데이터량 기반 과금. 급증 이벤트 대비 Budget Alert 설정 권장. |

**Pub/Sub 통합 시 워크플로우**
1. `gcloud pubsub topics create ws-events --message-retention-duration=10m` (심볼/환경별 추가 토픽 가능).
2. `gcloud pubsub subscriptions create trader-events --topic ws-events --ack-deadline=30 --enable-message-ordering`.
3. `ws_streams.py` 에서 `google-cloud-pubsub` 클라이언트 초기화 후 `_emit` 내부에서 `publisher.publish(topic_path, json.dumps(evt).encode(), ordering_key=symbol)` 호출.
4. `service_runner`에서는 로컬 큐 대신 Subscriber 스레드를 열어 메시지를 `event_queue`에 push하거나, 곧바로 `VolatilityDetector`를 호출.
5. 장애 시 Pub/Sub가 메시지를 재전송하므로 run_once는 멱등성을 고려하고, `event_id` 를 runtime 기록과 비교해 중복 실행을 방지.

### 6.4 선택 가이드
- **단일 호스트/소규모**: 기본 `queue.Queue` 유지.
- **사설망/자체 브로커 필요**: Kafka.
- **GCP 네이티브/관리형 원함**: Pub/Sub.
- **저비용 간단 확장**: Redis Stream.

## 7. 향후 확장 아이디어
1. **프로세스 분리**: WebSocket 수신 전용 프로세스 + 트레이더 프로세스로 분리하여 장애를 격리하고 로그 책임을 명확히 할 수 있습니다.
2. **메시지 브로커 도입**: Redis Stream → Kafka → Pub/Sub 순으로 확장성을 늘릴 수 있습니다.
3. **테스트 하네스**: `run_once` 단위 테스트 및 WS 이벤트 리플레이 도구 추가로 회귀 검증 강화.
4. **UI 알림 강화**: 진단 정보(마크/kline)와 이벤트 미발생 사유를 Streamlit 대시보드에 노출해 파라미터 튜닝을 쉽게 만들 수 있습니다.

## 8. Cloud Deployment Checklist
1. **베이스 OS 준비**
   - Ubuntu 22.04 LTS(권장) 또는 Amazon Linux 2023에 최신 보안 패치 적용.
   - 필수 패키지: `sudo apt update && sudo apt install -y python3.11 python3.11-venv python3-pip git tmux unzip`
2. **코드/환경 설정**
   - Git/OneDrive 동기화 대신 `git clone` 으로 배포용 리포지토리 확보.
   - `python3.11 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`
   - `.env` 파일은 서버 전용으로 작성하고 **API Key를 절대 Git에 커밋하지 않도록** `chmod 600 .env` 권장.
3. **실행 구성**
   - `auto_future_trader.py`가 메인 엔트리이며, `.env` 의 `ENV`, `WS_ENABLE`, `LOOP_TRIGGER` 등을 서버 환경에 맞게 조정.
   - UI(Streamlit)는 필요 시 `streamlit run ui/ui_dashboard.py --server.port 8501 --server.enableCORS false` 로 별도 프로세스에서 실행.
4. **프로세스 관리**
   - 소규모: `tmux` 또는 `screen` 세션에 `python auto_future_trader.py` 실행.
   - 운영 권장: `systemd` 서비스 유닛 생성 또는 `supervisord`/`pm2`(procfile)로 자동 재시작 설정.
   - 로그: `journalctl -u auto-futures.service -f` 혹은 `tee /var/log/auto-futures.log` 로 스트리밍.
5. **모니터링 & 상태파일 경로**
   - `runtime/*.jsonl` 과 `runtime/status.json` 이 지속적으로 갱신되므로, 서버에서 해당 디렉터리를 로그 수집 또는 대시보드용 볼륨으로 마운트.
   - 필요 시 CloudWatch/Filebeat 등으로 `/var/log/auto-futures.log` + `runtime/` 를 수집.
6. **보안 & 네트워크**
   - 아웃바운드: Binance API(443), OpenAI API(443) 허용.
   - 인바운드: Streamlit/UI 포트(선택)를 VPN·보안그룹으로 제한.
   - `ufw`/Security Group 으로 SSH 와 필요 포트만 허용, Fail2ban 등 기본 보안조치.
   - GCP 사용 시: VPC 방화벽 규칙으로 22(SSH)·8501(UI)·상황별 포트만 허용하고, Cloud Armor/Cloud NAT 등 네트워크 정책 적용.
7. **데이터 지속성**
   - `runtime/` 디렉터리, 주문/AI 히스토리 파일이 중요하면 EBS 또는 NFS에 저장하여 인스턴스 재시작 시 유지.
   - 주기적으로 S3/Blob Backup 스크립트 실행.
8. **확장/분리 시 고려**
   - 트래픽 증가 시 WebSocket 수신 프로세스를 별도 인스턴스로 분리하고, `Kafka`, `Google Cloud Pub/Sub`, 또는 `Redis Stream` 을 메시지 버퍼로 사용 가능.
   - Kafka 사용 시: `FuturesWS._emit()` → Kafka Producer(`topic=ws-events`), `service_runner` → Kafka Consumer 그룹으로 변환. 심볼을 파티션 키로 사용하면 이벤트 순서가 보장됩니다.
   - **Pub/Sub 사용 시**: 퍼블리셔/서브스크라이버에 서비스 계정을 부여하고, `google-cloud-pubsub` SDK로 ACK/오더링 키를 처리. Cloud Run/Functions로 서브스크라이버를 실행하면 자동 스케일 인/아웃 가능.
   - 이 방식을 쓰면 여러 심볼 또는 여러 트레이더 인스턴스를 수평 확장할 수 있지만, Kafka/ Pub/Sub 운영/보안/모니터링 비용을 고려해야 합니다.
9. **배포 스크립트 예시**
   - CI/CD 에서 `scp` + `systemctl restart auto-futures` 혹은 Docker 이미지 빌드 후 ECS/Kubernetes 배포 등 조직 표준 파이프라인에 맞추어 자동화 가능합니다.

> **Quick start (수동 배포)**
```
ssh ubuntu@your-server
sudo apt update && sudo apt install -y python3.11 python3.11-venv git
git clone <repo-url> && cd auto-futures
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # 또는 secure copy
python auto_future_trader.py
```
