#auto_future_trader.py (보강 버전: 실시간 체결확인)
# --------------------------------------------------------------------
# 핵심 변경:
#   - OrderStore 도입: 주문 등록 → WS 이벤트로 상태 갱신 → wait()로 종료 대기
#   - poll_fill()는 "백업"으로 남겨두고, 우선 WS 기반 확인 사용
#   - 상세 주석/로그 강화
# --------------------------------------------------------------------
# 주요 옵션:
# - DRY_RUN: 실제 주문 전송 안 함
# - WS_ENABLE: 웹소켓 사용 여부
# - ENV: paper | live
# 주요 의존 모듈:
# - common_utils: 안전한 float 변환, 가격/수량 스냅 등
# - input_builder: 바이낸스 데이터 수집 → 지표 계산 → INPUT JSON 조
# - call_openai: INPUT JSON 전달 → AI 조언(JSON) 수신
# - binance_conn: Client 생성, 심볼 필터, 계정/포지
# - ws_streams: 실시간 주문이벤트 수신
# - order_store: 주문 상태 추적
# # 주요 확장 포인트:
# - 입력 빌더/AI 호출 로직 교체 가능
# - 주문 전략 로직 교체 가능
# - 체결 확인 로직 교체 가능
# - 추가 주문 옵션 지원 가능
# - 다중 심볼 처리 가능
# - 고급 오류 처리 가능
# - 성능 최적화 가능
# - 테스트 커버리지 확장 가능
# - 로깅 세부조정 가능
# - 환경변수 추가 가능
# - 기타 등등
# 주요 참고사항:
# - DRY_RUN 모드로 충분히 테스트 후 실거래 적용 권장
# - 체결 확인은 WS 우선, 폴링 백업 순으로 시도
# - 금지시간대 설정 시 주의 필요
# - AI 결정 유효성 검증 필요
# - 주문 수량/가격 스냅 필요
# - 레버리지 변경 시도 필요
# - 바이낸스 API 호출 시 예외처리 필요
# - 로그를 통해 실행 흐름 추적 가능
# - 필요시 추가 기능 확장 가능
# - 기타 등등
# 주요 변경사항:
# - OrderStore 도입: 주문 등록 → WS 이벤트로 상태 갱신 → wait
# - poll_fill()는 "백업"으로 남겨두고, 우선 WS 기반 확인 사용
# - 상세 주석/로그 강화
# --------------------------------------------------------------------

import os, time, json, logging, queue
from typing import Dict, Any, Optional, List, Tuple

from binance.client import Client
from binance.enums import *
from binance.exceptions import BinanceAPIException

# 개별 모듈 임포트
#  - 공통 유틸: 안전한 float 변환, 가격/수량 스냅 등
from common_utils import safe_float, snap_price, snap_qty
#  - 입력 빌더: 바이낸스 데이터 수집 → 지표 계산 → INPUT JSON 조립
from input_builder import build_input_json
#  - OpenAI 호출: INPUT JSON 전달 → AI 조언(JSON) 수신
from call_openai import call_openai_for_advice
#  - 바이낸스 연결/데이터 수집: Client 생성, 심볼 필터, 계정/포지션 등
from binance_conn import (
    create_binance_client, futures_exchange_filters,
    fetch_account_and_positions
)

# 웹소켓 캐시
#  - WsCache: 마크프라이스/캔들/체결/주문 저장용
#  - get_global_cache: 글로벌 캐시 접근자
from ws_cache import get_global_cache, WsCache
#  - 주문 저장소 + 터미널 상태 상수
from order_store import OrderStore, TERMINAL_STATUSES
from ui.status_store import (
    update_status,
    append_event,
    set_latest_input,
    set_latest_advice,
    set_positions,
    append_order_history,
    append_ai_history,
    append_close_history,
)

from config_store import apply_runtime_settings_to_env
apply_runtime_settings_to_env()

# .env 파일에서 환경변수 로드
from dotenv import load_dotenv
load_dotenv()

# 로깅 설정
logging.basicConfig(
    level=os.getenv("LOG_LEVEL","INFO"),
    format="%(asctime)s [%(levelname)7s] %(filename)22s:%(lineno)4d [%(name)10s - %(funcName)12s] : %(message)s"
)
log = logging.getLogger("TRADER")

ENV = os.getenv("ENV", "paper")           # paper | live
DRY_RUN = os.getenv("DRY_RUN", "true").lower() in ("1","true","yes")
WS_ENABLE = os.getenv("WS_ENABLE", "true").lower() in ("1","true","yes")
USER_ON  = os.getenv("WS_USER_ENABLE",  "true").lower() in ("1","true","yes")
PRICE_ON = os.getenv("WS_PRICE_ENABLE", "true").lower() in ("1","true","yes")

# 1 헷지 모드 체크
# 바이낸스 선물 헷지 모드 여부 반환
# 헷지모드란 롱/숏 포지션을 별도로 보유 가능한 모드
# 반환: True=헷지모드, False=원모드
def is_hedge_mode(client: Client) -> bool:
    try:
        d = client.futures_get_position_mode()  # {'dualSidePosition': True|False}
        v = str(d.get("dualSidePosition","")).lower()
        return v in ("true","1","yes")
    except Exception as e:
        log.warning(f"futures_get_position_mode() 실패: {e}")
        return False

# 2 금지시간대 체크
# 현재 시간이 constraints의 forbidden_times_utc에 속하는지 여부 반환
# constraints: {"forbidden_times_utc": ["HH:MM-HH:MM",...]}
# 반환: True=금지시간대, False=허용시간대
def now_forbidden(constraints: Dict[str, Any]) -> bool:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    hhmm = now.strftime("%H:%M")
    for span in constraints.get("forbidden_times_utc", []):
        try:
            a,b = span.split("-")
            if a <= hhmm <= b:
                return True
        except Exception:
            continue
    return False

# 3 가격/수량 스냅
# 주어진 가격/수량을 tick_size/step_size에 맞게 스냅
def round_price_qty(price: Optional[float], qty: float, tick_size: Optional[str], step_size: Optional[str]) -> Tuple[Optional[float], float]:
    p = snap_price(price, float(tick_size)) if (price is not None and tick_size) else price
    q = snap_qty(qty, float(step_size)) if (qty is not None and step_size) else qty
    return p, q

# 4 기존 포지션 추출
# 주어진 심볼과 방향에 대해 기존 오픈포지션 수량 추출
# 반환: (같은방향수량, 반대방향수량)
# hedge: 헷지모드 여부
# account: fetch_account_and_positions() 반환값
# target_side: "long" | "short"
# hedge 모드인 경우 같은방향/반대방향 포지션을 별도로 집계
def extract_existing_position(account: Dict[str, Any], symbol: str, target_side: str, hedge: bool) -> Tuple[float, float]:
    same, opp = 0.0, 0.0
    for p in account.get("open_positions", []):
        if p.get("symbol") != symbol:
            continue
        side = p.get("side")
        qty  = safe_float(p.get("qty"), 0.0) or 0.0
        if hedge:
            if side == target_side: same += qty
            else: opp += qty
        else:
            if side == target_side: same += qty
            else: opp += qty
    return same, opp

# 5 심볼별 레버리지 보장
# 주어진 심볼에 대해 원하는 레버리지로 설정
# desired: 원하는 레버리지 (1 이상)
# client: 바이낸스 Client 객체
# 심볼: 거래 심볼
# 레버리지 변경 실패 시 경고 로그 기록
def ensure_symbol_leverage(client: Client, symbol: str, desired: int):
    if not desired or desired < 1:
        return
    try:
        if DRY_RUN:
            log.info(f"[DRY] 레버리지 변경 요청: {symbol} → x{desired}")
        else:
            r = client.futures_change_leverage(symbol=symbol, leverage=int(desired))
            log.info(f"레버리지 변경 결과: {r}")
    except Exception as e:
        log.warning(f"레버리지 변경 실패: {e}")

# ---------- 주문 송신/체결확인 ----------
# 1 공통 주문 송신
# DRY_RUN이면 전송하지 않고 파라미터만 반환
# 반환: 바이낸스 주문 응답 딕셔너리 또는 DRY_RUN 정보
# params: 바이낸스 주문 파라미터 딕셔너리
# client: 바이낸스 Client 객체
# 예외 발생 시 에러 정보 딕셔너리 반환
# 주문 전송 성공 시 로그 기록
# 주문 전송 실패 시 에러 로그 기록
def place_order(client: Client, params: Dict[str, Any]) -> Dict[str, Any]:
    if DRY_RUN:
        log.info(f"[DRY] 주문 전송: {params}")
        return {"status":"DRY_RUN","params":params}
    try:
        resp = client.futures_create_order(**params)
        log.info(f"주문 전송 완료: #{resp.get('orderId')} type={params.get('type')} reduceOnly={params.get('reduceOnly')}")
        return resp
    except Exception as e:
        log.error(f"주문 전송 실패: {e}")
        return {"status":"ERROR","error":str(e),"params":params}

# 2 웹소켓 기반 체결 대기
# - order_store.register()로 생성된 tracker가 ORDER_TRADE_UPDATE로 갱신될 때까지 대기
# order_store: OrderStore 객체
# order_id: 주문 ID
# timeout_sec: 타임아웃(초)
# 반환: 체결 요약 딕셔너리 또는 None(타임아웃)
def wait_fill_with_ws(order_store: OrderStore, order_id: int, timeout_sec: int = 30) -> Optional[Dict[str, Any]]:
    tr = order_store.get(order_id)
    if tr is None:
        return None
    ok = tr.wait(timeout=timeout_sec)
    if not ok:
        return None
    return {
        "order_id": tr.order_id,
        "status": tr.status,
        "executed_qty": tr.executed_qty,
        "avg_price": tr.avg_price,
        "last_fill_price": tr.last_fill_price,
        "side": tr.side,
        "position_side": tr.position_side,
        "update_time": tr.update_time
    }

# 3 REST 폴링 백업 체결 대기
# 웹소켓이 없거나 타임아웃인 경우 보조로 사용
# client: 바이낸스 Client 객체
# symbol: 거래 심볼
# order_id: 주문 ID
# timeout_sec: 타임아웃(초)
# interval: 폴링 간격(초)
# 반환: 체결 요약 딕셔너리 또는 None(타임아웃)
# 주문이 터미널 상태가 되면 즉시 반환
# 폴링 실패 시 경고 로그 기록
# 주문 상태/체결 정보 반환
# 예: {"order_id": 12345678, "status": "FILLED", "executed_qty": 0.1, "avg_price": 12345.67, "last_fill_price": 12345.67}
def poll_fill_backup(client: Client, symbol: str, order_id: int, timeout_sec: int = 15, interval: float = 0.8) -> Optional[Dict[str, Any]]:
    t0 = time.time()
    while time.time() - t0 < timeout_sec:
        try:
            od = client.futures_get_order(symbol=symbol, orderId=order_id)
            st = od.get("status")
            log.info(f"[POLL] id={order_id} status={st} executedQty={od.get('executedQty')}")
            if st in TERMINAL_STATUSES:
                return {
                    "order_id": order_id,
                    "status": st,
                    "executed_qty": safe_float(od.get("executedQty"), 0.0),
                    "avg_price": safe_float(od.get("avgPrice"), None),
                    "last_fill_price": safe_float(od.get("price"), None)
                }
        except Exception as e:
            log.warning(f"[POLL] 주문 조회 실패: {e}")
        time.sleep(interval)
    return None


# ---------- 버전 호환 헬퍼 ----------
# 1 선물 오픈오더 조회 우회
# python-binance 버전에 따라 고수준 메서드가 없을 때를 대비해
# 저수준 Futures REST 호출로 우회
# 반환: 오픈오더 리스트
# client: 바이낸스 Client 객체
# symbol: 거래 심볼
def _fapi_get_open_orders(client, symbol):
    try:
        if hasattr(client, "futures_get_open_orders"):
            return client.futures_get_open_orders(symbol=symbol)
    except Exception:
        pass
    # 저수준 우회
    try:
        return client._request_futures_api("get", "openOrders", data={"symbol": symbol})
    except Exception:
        return []

# 2 보호주문 취소
# 포지션 기준으로 더는 유효하지 않은 reduceOnly/closePosition 보호주문을 취소한다.
# - 원웨이: 총 포지션이 0이면 해당 심볼의 보호주문 전부 취소
# - 헤지  : LONG 수량 0이면 LONG측만, SHORT 수량 0이면 SHORT측만 취소
# 반환: 취소된 orderId 리스트
# client: 바이낸스 Client 객체
# symbol: 거래 심볼
# hedge_mode: 헷지모드 여부
# acct_snapshot: fetch_account_and_positions() 반환값
# dry_run: True이면 실제 취소하지 않고 로그만 기록
def cancel_stale_protection_orders(client, symbol: str, hedge_mode: bool, acct_snapshot: dict, dry_run: bool) -> list:
    # 1) 현재 포지션 수량 집계
    long_qty = 0.0
    short_qty = 0.0
    for p in acct_snapshot.get("open_positions", []):
        if p.get("symbol") != symbol:
            continue
        q = float(p.get("qty") or 0.0)
        if p.get("side") == "long":
            long_qty += q
        elif p.get("side") == "short":
            short_qty += q

    # 2) 오픈오더 조회
    orders = _fapi_get_open_orders(client, symbol) or []
    canceled = []

    # 취소대상 타입(문자열 비교로 버전 의존성 최소화)
    PROTECT_TYPES = {
        "STOP", "TAKE_PROFIT",
        "STOP_MARKET", "TAKE_PROFIT_MARKET",
        "TRAILING_STOP_MARKET",
        "LIMIT"  # reduceOnly 익절 리밋
    }

    for od in orders:
        try:
            typ = str(od.get("type") or "")
            if typ not in PROTECT_TYPES:
                continue

            # reduceOnly 또는 closePosition 플래그
            ro = od.get("reduceOnly")
            if isinstance(ro, str):
                ro = ro.lower() == "true"
            cp = od.get("closePosition")
            if isinstance(cp, str):
                cp = cp.lower() == "true"
            is_protection = bool(ro) or bool(cp)
            if not is_protection:
                continue

            # 헤지/원웨이에 따라 취소 여부 판단
            should_cancel = False
            if hedge_mode:
                ps = od.get("positionSide")  # 'LONG' / 'SHORT' / None
                if ps == "LONG" and long_qty <= 0:
                    should_cancel = True
                if ps == "SHORT" and short_qty <= 0:
                    should_cancel = True
            else:
                # 원웨이: 총량 0이면 싹 정리
                if long_qty <= 0 and short_qty <= 0:
                    should_cancel = True

            if not should_cancel:
                continue

            oid = od.get("orderId")
            if dry_run:
                log.info(f"[DRY] 취소 대상 보호주문 감지: #{oid} type={typ} posSide={od.get('positionSide')} ro={ro} cp={cp}")
                canceled.append(oid)
                continue

            try:
                resp = client.futures_cancel_order(symbol=symbol, orderId=oid)
                log.info(f"[CANCEL] 보호주문 취소 완료: #{oid} → {json.dumps(resp)[:200]}")
                canceled.append(oid)
            except Exception as e:
                log.warning(f"[CANCEL] 보호주문 취소 실패 #{oid}: {e}")

        except Exception as e:
            log.warning(f"[CANCEL] 검사 중 예외: {e}")

    if canceled:
        log.info(f"[CANCEL] 취소된 보호주문: {canceled}")
    else:
        log.info("[CANCEL] 취소할 보호주문 없음.")

    return canceled

# 3 캐시 스냅샷 안전 반환   
def _safe_snapshot(cache) -> dict:
    if not cache:
        return {}
    try:
        snap = cache.snapshot()
        return snap if isinstance(snap, dict) else {}
    except Exception:
        return {}

# ---------- 메인 실행 ----------
# 단일 실행
# symbol: 거래 심볼
# 반환: 없음
def run_once(symbol: str = "ETHUSDT"):
    start_ts = time.time()
    update_status("trader", {
        "state": "running",
        "symbol": symbol,
        "started_ts": start_ts,
    }, ts=start_ts)
    append_event({
        "source": "trader",
        "symbol": symbol,
        "event_type": "cycle",
        "result": "start",
        "ts": start_ts,
    })

    def set_state(state: str, **extra: Any) -> None:
        now_ts = time.time()
        payload = {
            "state": state,
            "symbol": symbol,
            "last_run_ts": now_ts,
            "last_duration": round(now_ts - start_ts, 3),
        }
        payload.update(extra)
        update_status("trader", payload, ts=now_ts)

    decision: Optional[str] = None
    confidence: Optional[float] = None

    def record_order(action: str, params: Dict[str, Any], response: Dict[str, Any], fill: Optional[Dict[str, Any]] = None) -> None:
        """Persist order execution summary for UI consumption."""
        try:
            entry: Dict[str, Any] = {
                "action": action,
                "symbol": symbol,
                "side": params.get("side"),
                "position_side": params.get("positionSide"),
                "order_type": params.get("type"),
                "quantity": safe_float(params.get("quantity"), None),
                "price": safe_float(params.get("price"), None),
                "reduce_only": params.get("reduceOnly"),
                "status": response.get("status"),
                "order_id": response.get("orderId") or response.get("order_id"),
                "client_order_id": response.get("clientOrderId"),
                "dry_run": bool(response.get("status") == "DRY_RUN"),
            }
            if fill:
                entry["status"] = fill.get("status") or entry.get("status")
                entry["executed_qty"] = safe_float(fill.get("executed_qty"), None)
                entry["avg_price"] = safe_float(fill.get("avg_price"), None)
                if fill.get("update_time"):
                    entry["update_time"] = fill.get("update_time")
            append_order_history(entry)
        except Exception:
            log.debug("order history 기록 실패", exc_info=True)

    def record_close_trade(action: str, position_snapshot: Optional[Dict[str, Any]], fill: Optional[Dict[str, Any]]) -> None:
        """Persist realized PnL analytics for position exits."""
        if not position_snapshot or not fill:
            return
        try:
            status = str(fill.get("status") or "").upper()
            dry_run = status == "DRY_RUN"
            if not dry_run and status and status not in TERMINAL_STATUSES:
                return

            qty = safe_float(fill.get("executed_qty"), None)
            if qty is None or qty <= 0:
                qty = safe_float(position_snapshot.get("qty"), None)
            if qty is None or qty <= 0:
                return

            entry_price = safe_float(position_snapshot.get("entry_price"), None)
            exit_price = safe_float(fill.get("avg_price"), None)
            if exit_price is None:
                exit_price = safe_float(fill.get("last_fill_price"), None)
            if exit_price is None and dry_run:
                exit_price = entry_price

            side = position_snapshot.get("side")
            pnl_value: Optional[float] = None
            pnl_pct: Optional[float] = None
            if entry_price is not None and exit_price is not None and qty:
                direction = 1.0 if side == "long" else -1.0
                pnl_value = (exit_price - entry_price) * qty * direction
                denom = entry_price * qty
                if denom:
                    pnl_pct = (pnl_value / denom) * 100.0

            append_close_history({
                "action": action,
                "symbol": position_snapshot.get("symbol"),
                "side": side,
                "qty": qty,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "realized_pnl_usdt": pnl_value,
                "return_pct": pnl_pct,
                "closed_ts": time.time(),
                "decision": decision,
                "confidence": confidence,
                "dry_run": dry_run,
                "status": status or None,
            })
        except Exception:
            log.debug("close history 기록 실패", exc_info=True)

    # 클라이언트/심볼 필터 준비
    client = create_binance_client(env=ENV)
    hedge = is_hedge_mode(client)
    pp, qp, tick, step, _ = futures_exchange_filters(client, symbol)
    log.info(f"ENV={ENV} hedge_mode={hedge} symbol={symbol} tick={tick} step={step}, pp={pp} qp={qp}")

    # 웹소켓 & 주문저장소 준비
    order_store = OrderStore()
    #ws = None
    #if WS_ENABLE and (USER_ON or PRICE_ON) and not DRY_RUN:
    #    ws = FuturesWS(env=ENV, symbol=symbol, event_queue=queue.Queue(maxsize=4000), order_store=order_store,
    #                   enable_user=USER_ON, enable_price=PRICE_ON)
    #    ws.start()

    # 웹소켓 캐시 준비
    cache = get_global_cache()
    if not cache:
        log.warning("WsCache가 설정되지 않았습니다. 이번턴은 스킵합니다다")
        set_state("skipped", reason="ws_cache_missing")
        append_event({
            "source": "trader",
            "symbol": symbol,
            "event_type": "prerun_check",
            "result": "skipped",
            "details": {"reason": "ws_cache_missing"},
        })
        return

    # 1 실시간 체결확인용 스냅샷 획득
    snap = _safe_snapshot(cache)
    mark = cache.mark_price
    kbar = cache.last_kline_close
    last_age = (time.time() - cache.last_mark_ts) if cache.last_mark_ts > 0 else None
    last_close = safe_float(kbar.get("c"), None) if kbar else None

    log.info("[SNAP] 웹소켓 연결 완료 (User/Mark/Kline) — 실시간 체결확인 캐시 획득")

    # 2 현재 마크프라이스/캔들 정보 로그
    is_closed = bool(kbar.get("closed")) if kbar else False
    log.info(f"[SNAP] mark={mark} age={None if last_age is None else round(last_age,2)}s last_close={last_close}  closed={is_closed}")

    # WS 프라이밍: 첫 데이터 수신 전이면 한 턴 스킵(원하면 여기서 REST 폴백)
    if mark is None or last_close is None or cache.last_mark_ts <= 0:
        log.info("WS 데이터 프라이밍 대기중 — 이번 턴 스킵.")
        set_state("skipped", reason="ws_priming")
        append_event({
            "source": "trader",
            "symbol": symbol,
            "event_type": "prerun_check",
            "result": "skipped",
            "details": {"reason": "ws_priming"},
        })
        return

    try:
        # 1) 입력/제한
        src = build_input_json(symbol=symbol, env=ENV, override_mark=mark, override_kline_close=kbar)
        input_snapshot = {
            "symbol": src.get("symbol"),
            "meta": src.get("meta"),
            "market": src.get("market"),
            "technicals": src.get("technicals"),
            "levels": src.get("levels"),
            "recent_bars_15m": src.get("recent_bars_15m"),
            "constraints": src.get("constraints"),
        }
        set_latest_input(input_snapshot)
        if now_forbidden(src.get("constraints", {})):
            log.warning("금지된 시간대(UTC) — 신규 진입 보류")
            update_status("trader", {
                "state": "running",
                "symbol": symbol,
                "notice": "forbidden_window",
            })
            append_event({
                "source": "trader",
                "symbol": symbol,
                "event_type": "constraint",
                "result": "blocked",
                "details": {"constraint": "forbidden_window"},
            })

        # 2) AI 조언(JSON)
        advice = call_openai_for_advice(src)
        decision = advice.get("decision")
        confidence = safe_float(advice.get("confidence"), None)
        set_latest_advice({
            "symbol": symbol,
            "advice": advice,
        })
        position_info = advice.get("position") or {}
        entry_info = position_info.get("entry") or {}
        size_info = position_info.get("size") or {}
        stop_loss_info = position_info.get("stop_loss") or {}
        append_ai_history({
            "symbol": symbol,
            "decision": decision,
            "confidence": confidence,
            "timeframe": advice.get("timeframe"),
            "rationale": (advice.get("rationale") or "")[:400],
            "notes": (advice.get("notes") or "")[:300],
            "position": {
                "entry_type": entry_info.get("order_type"),
                "entry_price": safe_float(entry_info.get("price"), None),
                "contracts": safe_float(size_info.get("contracts"), None),
                "quote_value_usdt": safe_float(size_info.get("quote_value_usdt"), None),
                "stop_loss_price": safe_float(stop_loss_info.get("price"), None),
            },
        })
        log.info(f"AI 결정: {decision} conf={confidence}")
        set_state("running", last_decision=decision, last_confidence=confidence)
        append_event({
            "source": "trader",
            "symbol": symbol,
            "event_type": "ai_decision",
            "result": "received",
            "details": {
                "decision": decision,
                "confidence": confidence,
            },
        })
        if decision not in ("long","short","flat"):
            log.warning("유효하지 않은 결정. 종료.")
            set_state("invalid", last_decision=decision, last_confidence=confidence, reason="invalid_decision")
            append_event({
                "source": "trader",
                "symbol": symbol,
                "event_type": "ai_decision",
                "result": "invalid",
                "details": {"decision": decision},
            })
            return

        # 3) 계정/오픈포지션 확인
        acct = fetch_account_and_positions(client, symbol_filter=symbol)
        set_positions(acct.get("open_positions", []))
        target_side = "long" if decision == "long" else ("short" if decision == "short" else None)
        same_qty, opp_qty = (0.0,0.0) if not target_side else extract_existing_position(acct, symbol, target_side, hedge)

        # 신뢰도 임계값을 runtime 설정에서 가져와 적용 (기본 0.5)
        try:
            ai_conf_thr = float(os.getenv("AI_CONF_THRESHOLD", os.getenv("AI_CONF_THRESHOLD", "0.5")))
        except Exception:
            ai_conf_thr = 0.5
        ai_conf_thr = max(0.0, min(1.0, ai_conf_thr))
        if confidence is not None and 0.0 < confidence < ai_conf_thr:
            log.warning("낮은 신뢰도(confidence) — 주문 실행 보류 (threshold=%s)", ai_conf_thr)
            set_state("skipped", last_decision=decision, last_confidence=confidence, reason="low_confidence")
            append_event({
                "source": "trader",
                "symbol": symbol,
                "event_type": "ai_decision",
                "result": "skipped",
                "details": {"confidence": confidence},
            })
            return

        # 3) 주문 수량/유형 해석 및 스냅
        pos = advice.get("position", {}) or {}
        size = pos.get("size", {}) or {}
        entry = pos.get("entry", {}) or {}
        last_price = safe_float(src["market"]["mark_price"], None) or safe_float(src["market"]["last_price"], None)
        qty = safe_float(size.get("contracts"), 0.0) or (safe_float(size.get("quote_value_usdt"), 0.0) / last_price if last_price else 0.0)
        qty = float(qty or 0.0)
        entry_price = safe_float(entry.get("price"), None)
        entry_type = str(entry.get("order_type","market")).upper()
        entry_type = "MARKET" if entry_type == "MARKET" else "LIMIT"
        entry_price, qty = round_price_qty(entry_price, qty, tick, step)
        if target_side and qty <= 0 and decision != "flat":
            log.warning("유효하지 않은 주문 수량. 종료.")
            set_state("invalid", last_decision=decision, last_confidence=confidence, reason="zero_quantity")
            append_event({
                "source": "trader",
                "symbol": symbol,
                "event_type": "order_prep",
                "result": "invalid",
                "details": {"qty": qty},
            })
            return

        # 4) 레버리지 조정(옵션)
        ensure_symbol_leverage(client, symbol, int(safe_float(size.get("leverage"), 0) or 0))

        # ---- 사이드 매핑 ----
        side_map = {"long":"BUY", "short":"SELL"}
        pos_side_map = {"long":"LONG", "short":"SHORT"}

        # 5) flat(전량 청산) 처리
        if decision == "flat":
            if acct.get("open_positions"):
                log.info("AI=flat → 모든 포지션 reduceOnly 시장가 청산")
                for p in acct["open_positions"]:
                    if p.get("symbol") != symbol or safe_float(p.get("qty"),0)<=0: 
                        continue
                    pos_snapshot = dict(p)
                    reduce_side = "SELL" if p.get("side")=="long" else "BUY"
                    params = {
                        "symbol": symbol,
                        "side": reduce_side,
                        "type": FUTURE_ORDER_TYPE_MARKET,
                        "quantity": snap_qty(p["qty"], float(step) if step else 0.0),
                        "reduceOnly": True
                    }
                    if hedge:
                        params["positionSide"] = pos_side_map[p.get("side")]
                    resp = place_order(client, params)
                    # DRY_RUN이면 여기서 끝
                    if resp.get("status") == "DRY_RUN":
                        fill_result = {
                            "status": "DRY_RUN",
                            "executed_qty": safe_float(params.get("quantity"), None),
                            "avg_price": safe_float(params.get("price"), None),
                        }
                        record_order("flat_exit", params, resp, fill_result)
                        record_close_trade("flat_exit", pos_snapshot, fill_result)
                        continue
                    fill_result: Optional[Dict[str, Any]] = None
                    oid = resp.get("orderId")
                    if oid:
                        order_store.register(symbol, oid, reduce_side, params.get("positionSide"))
                        res = wait_fill_with_ws(order_store, oid, timeout_sec=30) or \
                              poll_fill_backup(client, symbol, oid, timeout_sec=10)
                        fill_result = res
                        log.info(f"[청산 결과] {res}")
                    record_order("flat_exit", params, resp, fill_result)
                    record_close_trade("flat_exit", pos_snapshot, fill_result)

            else:
                log.info("보유 포지션 없음 — 아무 것도 하지 않음.")
            
            # 청산 체결 확인 직후 클린업
            chk_acct = fetch_account_and_positions(client, symbol_filter=symbol)
            cancel_stale_protection_orders(client, symbol, hedge, chk_acct, DRY_RUN)
            set_positions(chk_acct.get("open_positions", []))
            set_state("flat", last_decision=decision, last_confidence=confidence, last_action="flat_exit")
            append_event({
                "source": "trader",
                "symbol": symbol,
                "event_type": "flat_execution",
                "result": "completed",
            })
            return

        # 6) 반대방향 청산
        if opp_qty > 0:
            log.info(f"반대방향({('short' if target_side=='long' else 'long')}) 포지션 {opp_qty} 청산")
            reduce_side = "BUY" if target_side=="short" else "SELL"
            opp_position = None
            for pos in acct.get("open_positions", []):
                if pos.get("symbol") == symbol and pos.get("side") == ("short" if target_side=="long" else "long"):
                    opp_position = dict(pos)
                    break
            params = {
                "symbol": symbol,
                "side": reduce_side,
                "type": FUTURE_ORDER_TYPE_MARKET,
                "quantity": snap_qty(opp_qty, float(step) if step else 0.0),
                "reduceOnly": True
            }
            if hedge:
                params["positionSide"] = pos_side_map["short" if target_side=="long" else "long"]
            resp = place_order(client, params)
            if resp.get("status") == "DRY_RUN":
                fill_result = {
                    "status": "DRY_RUN",
                    "executed_qty": safe_float(params.get("quantity"), None),
                    "avg_price": safe_float(params.get("price"), None),
                }
                record_order("hedge_close", params, resp, fill_result)
                record_close_trade("hedge_close", opp_position, fill_result)
            else:
                fill_result: Optional[Dict[str, Any]] = None
                oid = resp.get("orderId")
                if oid:
                    order_store.register(symbol, oid, reduce_side, params.get("positionSide"))
                    res = wait_fill_with_ws(order_store, oid, timeout_sec=30) or \
                          poll_fill_backup(client, symbol, oid, timeout_sec=10)
                    fill_result = res
                    log.info(f"[반대방향 청산 결과] {res}")

                    # 청산 체결 확인 직후 클린업
                    chk_acct = fetch_account_and_positions(client, symbol_filter=symbol)
                    cancel_stale_protection_orders(client, symbol, hedge, chk_acct, DRY_RUN)
                    set_positions(chk_acct.get("open_positions", []))
                record_order("hedge_close", params, resp, fill_result)
                record_close_trade("hedge_close", opp_position, fill_result)

        # 8) 신규 진입(또는 스케일인)
        entry_side = side_map[target_side]
        params = {
            "symbol": symbol,
            "side": entry_side,
            "type": (FUTURE_ORDER_TYPE_MARKET if entry_type=="MARKET" else FUTURE_ORDER_TYPE_LIMIT),
            "quantity": qty
        }
        if entry_type == "LIMIT":
            params["price"] = entry_price
            params["timeInForce"] = "GTC"
        if hedge:
            params["positionSide"] = pos_side_map[target_side]

        log.info(f"신규 진입: side={target_side} qty={qty} type={entry_type} price={entry_price}")
        resp = place_order(client, params)

        filled_qty = 0.0
        entry_fill: Optional[Dict[str, Any]] = None
        if resp.get("status") == "DRY_RUN":
            filled_qty = qty  # 모의
            entry_fill = {
                "status": "DRY_RUN",
                "executed_qty": qty,
                "avg_price": entry_price,
            }
        else:
            oid = resp.get("orderId")
            if oid:
                # ★ 주문 저장소에 등록하고, 웹소켓으로 완료 대기
                order_store.register(symbol, oid, entry_side, params.get("positionSide"))
                res = wait_fill_with_ws(order_store, oid, timeout_sec=30)
                if res is None:
                    log.warning("WS 타임아웃 — REST 폴링 백업 수행")
                    res = poll_fill_backup(client, symbol, oid, timeout_sec=10)
                log.info(f"[신규 진입 결과] {res}")
                entry_fill = res
                if res and res.get("status") in TERMINAL_STATUSES:
                    filled_qty = safe_float(res.get("executed_qty"), 0.0) or qty

                # 신규 진입 체결 확인 직후 클린업
                chk_acct = fetch_account_and_positions(client, symbol_filter=symbol)
                cancel_stale_protection_orders(client, symbol, hedge, chk_acct, DRY_RUN)
                set_positions(chk_acct.get("open_positions", []))
        record_order("entry", params, resp, entry_fill)

        # 9) 손절/익절/트레일링(감축 전용)
        if filled_qty and filled_qty > 0:
            pos_conf = advice.get("position", {}) or {}
            sl = pos_conf.get("stop_loss", {}) or {}
            tp_list = pos_conf.get("take_profits", []) or []
            trail = pos_conf.get("trailing_stop", {}) or {}

            # 손절(Stop-Market)
            sl_price = safe_float(sl.get("price"), None)
            trigger_on = (sl.get("trigger_on") or "mark").lower()
            working_type = "MARK_PRICE" if trigger_on == "mark" else "CONTRACT_PRICE"
            if sl_price:
                sl_price, _ = round_price_qty(sl_price, filled_qty, tick, step)
                params = {
                    "symbol": symbol,
                    "side": ("SELL" if target_side=="long" else "BUY"),
                    "type": FUTURE_ORDER_TYPE_STOP_MARKET,
                    "quantity": filled_qty,
                    "stopPrice": sl_price,
                    "reduceOnly": True,
                    "workingType": working_type
                }
                if hedge:
                    params["positionSide"] = pos_side_map[target_side]
                log.info(f"손절 주문: stop={sl_price} workingType={working_type}")
                resp = place_order(client, params)
                record_order("stop_loss", params, resp, None)
                if resp.get("status") != "DRY_RUN" and resp.get("orderId"):
                    order_store.register(symbol, resp["orderId"], params["side"], params.get("positionSide"))

            # 익절(분할 LIMIT reduceOnly)
            for i, tp in enumerate(tp_list, start=1):
                tp_price = safe_float(tp.get("price"), None)
                tp_szpct = safe_float(tp.get("size_pct"), 0.0)
                if not tp_price or tp_szpct <= 0: 
                    continue
                tp_qty = filled_qty * (tp_szpct/100.0)
                tp_price, tp_qty = round_price_qty(tp_price, tp_qty, tick, step)
                params = {
                    "symbol": symbol,
                    "side": ("SELL" if target_side=="long" else "BUY"),
                    "type": FUTURE_ORDER_TYPE_LIMIT,
                    "quantity": tp_qty,
                    "price": tp_price,
                    "timeInForce": "GTC",
                    "reduceOnly": True
                }
                if hedge: params["positionSide"] = pos_side_map[target_side]
                log.info(f"익절#{i}: price={tp_price} qty={tp_qty}")
                resp = place_order(client, params)
                record_order(f"take_profit_{i}", params, resp, None)
                if resp.get("status") != "DRY_RUN" and resp.get("orderId"):
                    order_store.register(symbol, resp["orderId"], params["side"], params.get("positionSide"))

            # 트레일링(감축 전용)
            act = safe_float(trail.get("activate_price"), None)
            cb  = safe_float(trail.get("callback_pct"), None)
            if act and cb:
                act, _ = round_price_qty(act, filled_qty, tick, step)
                params = {
                    "symbol": symbol,
                    "side": ("SELL" if target_side=="long" else "BUY"),
                    "type": "FUTURE_ORDER_TYPE_TRAILING_STOP_MARKET",
                    "quantity": filled_qty,
                    "stopPrice": act,
                    "callbackRate": cb,
                    "reduceOnly": True,
                    "workingType": "MARK_PRICE"
                }
                if hedge: params["positionSide"] = pos_side_map[target_side]
                log.info(f"트레일링: activate={act} cb%={cb}")
                resp = place_order(client, params)
                record_order("trailing_stop", params, resp, None)
                if resp.get("status") != "DRY_RUN" and resp.get("orderId"):
                    order_store.register(symbol, resp["orderId"], params["side"], params.get("positionSide"))

        # 10) 요약
        log.info("=== 실행 완료 ===")
        log.info(f"AI 결정: {advice.get('decision')} conf={advice.get('confidence')}")
        log.info(f"진입 주문: side={target_side} qty={qty} type={entry_type} price={entry_price}")
        log.info(f"기존 포지션: 같은방향={same_qty} 반대방향={opp_qty}")
        log.info(f"실제 체결량: {filled_qty}")
        log.info("================")
        set_state(
            "completed",
            last_decision=decision,
            last_confidence=confidence,
            filled_qty=filled_qty,
            last_action="entry" if decision in ("long", "short") else "flat",
        )
        append_event({
            "source": "trader",
            "symbol": symbol,
            "event_type": "execution",
            "result": "completed",
            "details": {
                "decision": decision,
                "filled_qty": filled_qty,
            },
        })

    except Exception as exc:
        log.exception("run_once 실패: %s", exc)
        set_state(
            "error",
            last_decision=decision,
            last_confidence=confidence,
            error=str(exc),
        )
        append_event({
            "source": "trader",
            "symbol": symbol,
            "event_type": "execution",
            "result": "error",
            "details": {"error": str(exc)},
        })
        raise

    finally:
        try:
            acct_snapshot = fetch_account_and_positions(client, symbol_filter=symbol)
            set_positions(acct_snapshot.get("open_positions", []))
        except Exception:
            pass
        try:
            #if ws:
            #    ws.stop()
            time.sleep(1) # TWM 종료 안정화 (재진입 충돌방지)
        except Exception as e:
            log.warning("WS stop 실패: %s", e)

if __name__ == "__main__":
    sym = os.getenv("SYMBOL", "ETHUSDT")

    loop_on = os.getenv("LOOP_ENABLE","false").lower() in ("1","true","yes")
    if loop_on:
        from service_runner import run_service
        run_service(sym, run_once_cb=run_once)   # ← 콜백 주입
    else:
        run_once(sym)