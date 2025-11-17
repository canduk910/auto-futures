# service_runner.py
import os, time, signal, logging, collections
from typing import Callable, Deque, Tuple, Optional

import ssl
import certifi

import threading

# 웹소켓 데이터 모듈
from ws_streams import FuturesWS
from ws_cache import WsCache, set_global_cache

# UI 상태저장소
from ui.status_store import update_status, append_event

# queue.Empty 타입 방어
import queue as pyqueue
from queue import Empty as PyQueueEmpty
try:
    import _queue as cqueue
    CQueueEmpty = cqueue.Empty      # C 확장 예외까지 캐치
except Exception:
    CQueueEmpty = PyQueueEmpty

# 공통 유틸
from common_utils import safe_float

# .env 파일에서 환경변수 로드
from dotenv import load_dotenv
load_dotenv()

log = logging.getLogger("SERVICE")
_STOP = threading.Event()

# Provide a certifi-driven SSL context for ThreadedWebsocketManager to avoid certificate errors.
ssl_context = ssl.create_default_context(cafile=certifi.where())
ssl_context.check_hostname = True
ssl_context.verify_mode = ssl.CERT_REQUIRED

def _handle(sig, frame): _STOP.set()
for s in ("SIGINT","SIGTERM"):
    if hasattr(signal, s):
        try: signal.signal(getattr(signal, s), _handle)
        except Exception: pass

def _invoke_without_ws(run_once_cb: Callable[[str], None], symbol: str):
    prev_enable = os.getenv("WS_ENABLE", "true")
    prev_user   = os.getenv("WS_USER_ENABLE", "true")
    prev_price  = os.getenv("WS_PRICE_ENABLE", "true")
    try:
        # 타이머/이벤트 호출 구간에선 WS 자체를 끈다(REST 폴백 전용)
        os.environ["WS_ENABLE"]       = "false"
        os.environ["WS_USER_ENABLE"]  = "false"
        os.environ["WS_PRICE_ENABLE"] = "false"
        log.info(f"run_once start: {symbol}")
        run_once_cb(symbol)
        log.info("run_once done")
    finally:
        os.environ["WS_ENABLE"] = prev_enable
        os.environ["WS_USER_ENABLE"]  = prev_user
        os.environ["WS_PRICE_ENABLE"] = prev_price


def _format_diag(diag: dict) -> str:
    if not diag:
        return ""
    parts = []
    typ = diag.get("type")
    if typ:
        parts.append(f"type={typ}")
    reason = diag.get("reason")
    if reason:
        parts.append(f"reason={reason}")
    reason_map = {
        "insufficient_samples": ("sample_count",),
        "delta_below_threshold": ("delta_pct", "threshold_pct", "current_price", "base_price"),
        "range_below_threshold": ("range_pct", "range_threshold_pct", "high", "low", "close"),
        "volume_below_threshold": ("vol", "avg_vol", "vol_ratio", "vol_mult"),
        "volume_history_unavailable": ("avg_vol", "vol_mult"),
        "no_trigger": ("range_pct", "range_threshold_pct", "vol", "avg_vol", "vol_mult"),
    }
    reason_tokens = [r.strip() for r in (reason or "").split(",") if r.strip()]
    key_order = (
        "delta_pct",
        "threshold_pct",
        "sample_count",
        "current_price",
        "base_price",
        "range_pct",
        "range_threshold_pct",
        "high",
        "low",
        "close",
        "vol",
        "avg_vol",
        "vol_ratio",
        "vol_mult",
    )
    allowed_keys = []
    for token in reason_tokens:
        allowed_keys.extend(reason_map.get(token, ()))
    if not allowed_keys:
        allowed_keys = key_order
    seen = set()
    for key in key_order:
        if key not in allowed_keys or key in seen:
            continue
        val = diag.get(key)
        if val is not None:
            parts.append(f"{key}={val}")
        seen.add(key)
    return ", ".join(parts)

# ===== 이벤트 기반 급변 감지기 =====
# 마크프라이스/1분봉 이벤트로 급변 감지
#    - mark: 최근 MP_WINDOW_SEC 동안 퍼센트 변동이 MP_DELTA_PCT 이상이면 True
#    - kline: 봉 마감 시 range% 또는 거래대금/거래량 급증이면 True
# on_mark(payload) / on_kline(payload) 호출
# True=급변 감지, False=정상
# payload: WS 이벤트 페이로드
# 예: {'e':'markPriceUpdate','E':...,
#     's':'ETHUSDT', 'p':'3377.12', ...}
# 예: {'e':'kline','E':..., 's':'ETHUSDT',
#     'k':{'i':'1m','x':True,'h':'...','
#          'l':'...','c':'...','v':'...','q':'...'}}
# 설정 파라미터:
#   mp_window_sec: 마크프라이스 윈도우 (초)
#   mp_delta_pct:  마크프라이스 변동 임계치 (%)
#   kline_range_pct: 1분봉 변동폭 임계치 (%)
#   vol_lookback: 1분봉 거래대금/거래량 평균 산출용 과거 개수
#   vol_mult: 거래대금/거래량 급증 배수
#   use_quote_volume: 거래대금 기준 여부 (False=기본: 거래량 기준)    
class VolatilityDetector:
    def __init__(self,
                 mp_window_sec: int = 10,
                 mp_delta_pct: float = 0.35,
                 kline_range_pct: float = 0.6,
                 vol_lookback: int = 20,
                 vol_mult: float = 3.0,
                 use_quote_volume: bool = True):
        self.mp_window_sec = mp_window_sec
        self.mp_delta_pct = mp_delta_pct
        self.kline_range_pct = kline_range_pct
        self.vol_lookback = vol_lookback
        self.vol_mult = vol_mult
        self.use_quote_volume = use_quote_volume

        # (ts, mark_price)
        self._mp_hist: Deque[Tuple[float, float]] = collections.deque(maxlen=4096)
        # 최근 1분봉 거래대금/거래량(lookback 평균 계산용)
        self._vol_hist: Deque[float] = collections.deque(maxlen=max(5, vol_lookback))
        self._last_diag: dict = {}

    @property
    def last_diag(self) -> dict:
        return self._last_diag

    # ---- Mark Price 이벤트 처리 ----
    def on_mark(self, payload: dict) -> bool:
        # msg 예: {'e':'markPriceUpdate','E':..., 's':'ETHUSDT', 'p':'3377.12', ...}
        try:
            self._last_diag = {"type": "mark", "reason": "unknown"}
            ts = float(payload.get("E") or time.time() * 1000) / 1000.0
            p = safe_float(payload.get("p"))
            if p is None: 
                self._last_diag.update(reason="missing_price")
                return False
            self._mp_hist.append((ts, p))
            # 윈도우 밖 제거
            win_start = ts - self.mp_window_sec
            while self._mp_hist and self._mp_hist[0][0] < win_start:
                self._mp_hist.popleft()
            if len(self._mp_hist) < 2:
                self._last_diag.update(
                    reason="insufficient_samples",
                    sample_count=str(len(self._mp_hist))
                )
                return False
            p0 = self._mp_hist[0][1]
            if not p0:
                self._last_diag.update(reason="invalid_reference_price")
                return False
            delta_pct = abs((p / p0) - 1.0) * 100.0
            triggered = delta_pct >= self.mp_delta_pct
            diag_payload = {
                "delta_pct": f"{delta_pct:.4f}",
                "threshold_pct": f"{self.mp_delta_pct}",
                "current_price": f"{p:.2f}" if p is not None else None,
                "base_price": f"{p0:.2f}" if p0 is not None else None,
            } if not triggered else {}
            self._last_diag.update(
                reason="triggered" if triggered else "delta_below_threshold",
                **diag_payload,
            )
            return triggered
        except Exception as e:
            self._last_diag.update(reason="exception", error=str(e))
            return False

    # ---- Kline(1m) 이벤트 처리 ----
    def on_kline(self, payload: dict) -> bool:
        # msg 예: {'e':'kline','E':..., 's':'ETHUSDT', 'k':{'i':'1m','x':True,'h':'...','l':'...','c':'...','v':'...','q':'...'}}
        try:
            self._last_diag = {"type": "kline", "reason": "unknown"}
            k = payload.get("k", {}) or {}
            if not k.get("x"):  # 봉 마감시에만 판단
                self._last_diag.update(reason="candle_not_closed")
                return False

            h = safe_float(k.get("h"))
            l = safe_float(k.get("l"))
            c = safe_float(k.get("c"))
            if not c or not h or not l:
                self._last_diag.update(reason="missing_price_data")
                return False

            # 1) range% 급증
            range_pct = ((h - l) / c) * 100.0 if c else 0.0
            trig_range = (range_pct >= self.kline_range_pct)

            # 2) 거래대금/거래량 급증
            v_q = safe_float(k.get("q"))  # quote volume(USDT 기준)
            v_b = safe_float(k.get("v"))  # base volume(ETH 기준)
            vol = v_q if (self.use_quote_volume and v_q is not None) else v_b
            trig_vol = False
            avg = None
            vol_ratio = None
            if vol is not None:
                avg = (sum(self._vol_hist) / len(self._vol_hist)) if self._vol_hist else None
                if avg is not None and avg > 0 and vol >= (self.vol_mult * avg):
                    trig_vol = True
                if avg is not None and avg > 0:
                    vol_ratio = vol / avg
                # 최신 봉을 히스토리에 업데이트
                self._vol_hist.append(vol)

            triggered = trig_range or trig_vol
            reasons = []
            if not trig_range:
                reasons.append("range_below_threshold")
            if vol is None:
                reasons.append("volume_missing")
            elif avg is None or avg <= 0:
                reasons.append("volume_history_unavailable")
            elif not trig_vol:
                reasons.append("volume_below_threshold")

            diag_data = {
                "reason": "triggered" if triggered else ",".join(reasons) if reasons else "no_trigger",
                "range_pct": round(range_pct, 4),
                "range_threshold_pct": self.kline_range_pct,
                "high": round(h, 2) if h else None,
                "low": round(l, 2) if l else None,
                "close": round(c, 2) if c else None,
            }
            if vol is not None:
                diag_data["vol"] = round(vol, 2)
            if avg is not None:
                diag_data["avg_vol"] = round(avg, 2)
            if vol_ratio is not None:
                diag_data["vol_ratio"] = round(vol_ratio, 2)
            diag_data["vol_mult"] = self.vol_mult

            self._last_diag.update(diag_data)

            return triggered
        except Exception as e:
            self._last_diag.update(reason="exception", error=str(e))
            return False

# ===== 서비스 러너 =====
# 지정된 트리거(1분봉 또는 타이머)로 run_once_cb(symbol) 호출 루프 실행
# 외부에서 signal로 중단 가능
# run_once_cb: (symbol:str) -> None
#    trigger 모드:
#      - timer : N초마다 실행
#      - kline : 1분봉 종가마다 실행
#      - event : 급변 이벤트(마크프라이스/1분봉) 발생 시 실행

def run_service(symbol: str, run_once_cb: Callable[[str], None]):
    trigger   = os.getenv("LOOP_TRIGGER", "kline").lower()   # kline | timer
    interval  = int(os.getenv("LOOP_INTERVAL_SEC", "60"))
    cooldown  = int(os.getenv("LOOP_COOLDOWN_SEC", "8"))
    backoff_m = int(os.getenv("LOOP_BACKOFF_MAX_SEC", "30"))
    env_name  = os.getenv("ENV", "paper")

    # 이벤트 모드 파라미터
    mp_win  = int(os.getenv("MP_WINDOW_SEC", "10"))
    mp_pct  = float(os.getenv("MP_DELTA_PCT", "0.35"))
    rng_pct = float(os.getenv("KLINE_RANGE_PCT", "0.6"))
    vol_lb  = int(os.getenv("VOL_LOOKBACK", "20"))
    vol_mul = float(os.getenv("VOL_MULT", "3.0"))
    use_qv  = os.getenv("USE_QUOTE_VOLUME", "true").lower() in ("1","true","yes")    

    log.info(f"start trigger={trigger} interval={interval}s cooldown={cooldown}s")
    update_status("service", {
        "state": "starting",
        "trigger": trigger,
        "interval": interval,
        "cooldown": cooldown,
        "env": env_name,
    })
    evt_q: "pyqueue.Queue[dict]" = pyqueue.Queue(maxsize=4000)
    cache = WsCache(symbol=symbol)
    set_global_cache(cache)
    detector = None

    # WS는 kline/event 트리거에서 사용
    ws_instance: Optional[FuturesWS] = None
    try:
        ws = FuturesWS(env=env_name, symbol=symbol, event_queue=evt_q,
                       enable_user=False, enable_price=True, cache=cache)
        ws_instance = ws
        ws.start()
        log.info(f"웹소켓 접속 ({trigger} 트리거 모드)")
        update_status("service", {
            "state": "running",
            "trigger": trigger,
            "symbol": symbol,
            "last_event": None,
        })
    except Exception as e:
        log.exception(f"웹소켓 시작 실패: {e!s} (type={type(e).__name__}) → REST 폴백 모드로 동작")
        trigger = "timer"
        update_status("service", {
            "state": "fallback",
            "trigger": trigger,
            "symbol": symbol,
            "error": str(e),
        })

    if trigger == "event":
        detector = VolatilityDetector(
            mp_window_sec=mp_win, mp_delta_pct=mp_pct,
            kline_range_pct=rng_pct, vol_lookback=vol_lb, vol_mult=vol_mul,
            use_quote_volume=use_qv
        )

    last_run = 0.0
    backoff  = 1.0
    try:
        mark_cnt = 0; kline_cnt = 0; last_stat = time.time()
        while not _STOP.is_set():
            try:
                if trigger == "timer":
                    now = time.time()
                    if now - last_run >= max(1, interval):
                        last_run = now
                        #_invoke_without_ws(run_once_cb, symbol)
                        run_once_cb(symbol)
                        # 완료 시각으로 변경 (재진입 방지)
                        finished = time.time()
                        last_run = finished
                        update_status("service", {
                            "state": "running",
                            "trigger": trigger,
                            "symbol": symbol,
                            "last_event": "timer",
                            "last_run_ts": finished,
                        }, ts=finished)
                        append_event({
                            "source": "service",
                            "symbol": symbol,
                            "event_type": "timer_cycle",
                            "result": "executed",
                            "ts": finished,
                        })
                        backoff = 1.0
                    _STOP.wait(0.5)
                    continue

                # kline/event 트리거: 이벤트 큐 처리 (신뢰도 높은 블로킹 get)
                try:
                    ev = evt_q.get(timeout=1.0)   # ← 타임아웃 블로킹
                except (PyQueueEmpty, CQueueEmpty):
                    _STOP.wait(0.2)
                    continue
                except Exception as e:
                    if e.__class__.__name__ == "Empty":
                        _STOP.wait(0.2)
                        continue
                    log.warning(f"evt_q.get 예외: {e}")
                    _STOP.wait(0.5)
                    continue

                if trigger == "kline":
                    if ev.get("type") != "kline": 
                        continue
                    k = (ev.get("payload") or {}).get("k", {})

                    # 1분봉 종가 이벤트만 처리
                    if ((ev.get("payload") or {}).get("s") or "").upper() != symbol.upper():
                        continue
                    if k.get("i") != "1m" or not k.get("x"): 
                        continue  # 봉 마감
                    now = time.time()
                    if now - last_run < cooldown: 
                        continue
                    last_run = now
                    #_invoke_without_ws(run_once_cb, symbol)
                    run_once_cb(symbol)
                    # 완료시각으로 변경 (재진입 방지)
                    finished = time.time()
                    last_run = finished
                    update_status("service", {
                        "state": "running",
                        "trigger": trigger,
                        "symbol": symbol,
                        "last_event": "kline",
                        "last_run_ts": finished,
                        "cooldown": cooldown,
                    }, ts=finished)
                    append_event({
                        "source": "service",
                        "symbol": symbol,
                        "event_type": "kline_close",
                        "result": "executed",
                        "interval": k.get("i"),
                        "ts": finished,
                    })
                    backoff = 1.0
                    continue

                if trigger == "event":
                    typ = ev.get("type")
                    payload = ev.get("payload", {})

                    if ((ev.get("payload") or {}).get("s") or "").upper() != symbol.upper():
                        continue
                    fired = False
                    if typ == "mark":
                        fired = detector.on_mark(payload)
                        mark_cnt += 1
                    elif typ == "kline":
                        fired = detector.on_kline(payload)
                        kline_cnt += 1

                    # 통계 로깅
                    if time.time() - last_stat >= 10:
                        qsz = getattr(evt_q, "qsize", lambda: "?")()
                        log.info(f"30s stats: mark={mark_cnt}, kline={kline_cnt}, qsize={qsz}")
                        update_status("service", {
                            "state": "running",
                            "trigger": trigger,
                            "symbol": symbol,
                            "mark_events": mark_cnt,
                            "kline_events": kline_cnt,
                            "last_qsize": qsz,
                        })
                        mark_cnt = kline_cnt = 0; last_stat = time.time()

                    if not fired:
                        diag_msg = _format_diag(detector.last_diag if detector else {})
                        if diag_msg:
                            log.info(f"[EVENT] Vol-spike trigger not fired ({diag_msg})")
                        else:
                            log.info("[EVENT] Vol-spike trigger not fired")
                        continue

                    now = time.time()
                    if now - last_run < cooldown:
                        continue
                    last_run = now
                    log.info("[EVENT] Vol-spike trigger fired → run_once()")
                    #_invoke_without_ws(run_once_cb, symbol)
                    run_once_cb(symbol)
                    last_run = time.time()
                    backoff = 1.0
                    continue

            except Exception as e:
                log.exception(f"cycle error: {e}")
                time.sleep(backoff)
                backoff = min(backoff*2, backoff_m)
        log.info("stop signal, cleaning up...")
        update_status("service", {"state": "stopping", "symbol": symbol})
    finally:
        try:
            if ws_instance:
                ws_instance.stop()
        except Exception as e:
            log.warning(f"WebSocket stop failed: {e}")
        update_status("service", {"state": "stopped", "symbol": symbol})
