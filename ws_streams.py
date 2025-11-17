# ws_streams.py
# -------------------------------------------------------------------
# USDT-M 선물 웹소켓 연결 (User Data + Mark Price + 1m Kline)
# - 테스트넷/실서버 스위치(ENV=paper|live)
# - listenKey keepalive(45분)
# - 콜백 로깅 + 선택적 Queue 전달
# -------------------------------------------------------------------

import os, json, time, logging, queue, inspect
from typing import Optional, Dict, Any

import threading
import ssl
import certifi


def _build_ssl_context() -> Optional[ssl.SSLContext]:
    try:
        ctx = ssl.create_default_context(cafile=certifi.where())
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        return ctx
    except Exception:
        return None

# 내부 모듈
from common_utils import safe_float            # 안전한 float 변환용
from binance_conn import create_binance_client # REST 클라이언트 생성용
from order_store import OrderStore             # 주문 상태 추적용
from ws_cache import WsCache                   # 웹소켓 데이터 캐시용

from binance.client import Client
from binance.enums import KLINE_INTERVAL_1MINUTE
from binance import ThreadedWebsocketManager
try:
    from binance.ws.threaded_stream import ThreadedApiManager
except ImportError:
    ThreadedApiManager = None



log = logging.getLogger("WEBSOCKETS")

class FuturesWS:
    def __init__(self,
                 env: str = "paper",
                 symbol: str = "ETHUSDT",
                 # 캐시: 마크프라이스/캔들/체결/주문 저장용
                 event_queue: Optional[queue.Queue] = None,
                 order_store: Optional[OrderStore] = None,
                 enable_user: bool = True,
                 enable_price: bool = True,
                 cache: Optional[WsCache] = None
                 ):
        self.env = env
        self.symbol = symbol
        self.event_queue = event_queue or queue.Queue(maxsize=1000)
        self.order_store = order_store or OrderStore() # 주문 상태 추적용
        self.enable_user = enable_user     # ★ 유저 데이터 소켓 on/off
        self.enable_price = enable_price   # ★ 마크/클라인 소켓 on/off
        # 이벤트체크용
        self._ev_count = 0
        self._ev_drop = 0
        self._last_emit_ts = 0.0
        self._trace = os.getenv("WS_TRACE", "false").lower() in ("1", "true", "yes")

        # REST 클라이언트: listenKey 발급/갱신 용
        self.client: Client = create_binance_client(env=env)

        # WS 웹소켓 매니저 (testnet 스위치)
        ssl_context = _build_ssl_context()
        self.twm = ThreadedWebsocketManager(
            api_key=os.getenv("BINANCE_TESTNET_API_KEY"),
            api_secret=os.getenv("BINANCE_TESTNET_SECRET_KEY"),
            testnet=(env == "paper"),
        )
        if ThreadedApiManager and ssl_context is not None:
            try:
                ThreadedApiManager.ssl_context = ssl_context  # type: ignore[attr-defined]
            except Exception:
                pass
        self._listen_key = None
        self._keepalive_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._started = False

        # WS 웹소켓 캐시
        self.cache = cache

    # 내부용 헬퍼
    # ws_streams.py 내부, FuturesWS 클래스에 헬퍼 추가
    # ----------------------------------------------------- 
    # 0 내부 메시지 언래핑
    # 메시지가 래핑형인지 직접형인지 구분하여 내부 dict 반환
    # 래핑형: {stream:..., data:{...}}
    # 직접형: {e:..., ...}
    # 항상 내부 dict를 반환하도록 통일
    # 내부 전용 메서드
    # ----------------------------------------------------- 
    def _unwrap(self, msg: dict) -> dict:
        if isinstance(msg, dict) and isinstance(msg.get("data"), dict):
            return msg["data"]
        return msg

    # -----------------------------------------------------    
    # 1 이벤트 큐에 이벤트 삽입 + 계측/로깅
    # WS 콜백 → 서비스루프 큐로 이벤트 전달
    # 콜백에서 직접 호출
    # typ: 이벤트 타입 (예: "mark", "kline", "user")
    # payload: 이벤트 페이로드(dict)
    # 현재 미사용되어 추후 활용할 수도 있음. 일단은 삭제대상으로 삼음.
    # -----------------------------------------------------
    def _emit(self, typ: str, payload: dict):
        if not self.event_queue:
            log.warning(f"event_queue is None; drop {typ}")
            self._ev_drop += 1
            return
        evt = {"type": typ, "payload": payload}
        try:
            # 비차단 put: 꽉 차면 즉시 예외
            self.event_queue.put_nowait(evt)
            self._ev_count += 1
            self._last_emit_ts = time.time()
            # 과도 로그 방지: 50개마다 요약
            if self._ev_count % 50 == 0:
                qsize = getattr(self.event_queue, "qsize", lambda: "?")()
                log.info(f"emit ok: type={typ}, total={self._ev_count}, qsize={qsize}")
            if self._trace:
                log.debug(f"emit {typ}: {str(payload)[:200]}")
        except Exception as e:
            self._ev_drop += 1
            log.warning(f"emit FAIL: type={typ}, drop={self._ev_drop}, err={e}")


    # -----------------------------------------------------    
    # 2 버전별 시그니처 차이를 흡수해서 mark price 소켓을 안전하게 시작한다.
    # - 대표 서명1: start_symbol_mark_price_socket(callback, symbol, fast=False)
    # - 대표 서명2: start_symbol_mark_price_socket(symbol, callback, fast=False)
    # - 빠른 옵션(fast) 인자 유무도 동적으로 처리
    # - 함수명이 구버전인 start_mark_price_socket 인 경우도 시도
    # -----------------------------------------------------    
    def _start_mark_price_socket_safe(self):
        fn = getattr(self.twm, "start_symbol_mark_price_socket", None)
        if fn is None:
            # 일부 구버전 함수명
            fn = getattr(self.twm, "start_mark_price_socket", None)
        if fn is None:
            log.error("mark price socket 시작 함수가 없습니다.")
            return

        try:
            sig = inspect.signature(fn)
            names = list(sig.parameters.keys())  # 예: ['callback','symbol','fast']

            has_fast = "fast" in sig.parameters

            # 1) (callback, symbol, fast)
            if len(names) >= 2 and names[0] == "callback" and names[1] == "symbol":
                if has_fast:
                    return fn(self.on_mark_price, self.symbol, fast=True)
                else:
                    return fn(self.on_mark_price, self.symbol)

            # 2) (symbol, callback, fast)
            if len(names) >= 2 and names[0] == "symbol" and names[1] == "callback":
                if has_fast:
                    return fn(self.symbol, self.on_mark_price, fast=True)
                else:
                    return fn(self.symbol, self.on_mark_price)

            # 3) 기타: 키워드로 시도
            kwargs = {}
            if "callback" in sig.parameters: kwargs["callback"] = self.on_mark_price
            if "symbol"   in sig.parameters: kwargs["symbol"]   = self.symbol
            if has_fast:                     kwargs["fast"]     = True
            return fn(**kwargs)

        except Exception as e:
            log.error(f"mark price socket 시작 실패: {e}")

    # -----------------------------------------------------    
    # 3 버전별 시그니처 차이를 흡수해서 kline 소켓을 안전하게 시작한다.
    # - 대표 서명1: start_kline_futures_socket(callback, symbol, interval)
    # - 대표 서명2: start_kline_futures_socket(symbol, callback, interval)
    # - 함수명이 구버전인 start_kline_socket 인 경우도 시도
    # - interval 인자 유무도 동적으로 처리
    # -----------------------------------------------------    
    def _start_kline_socket_safe(self):
        fn = getattr(self.twm, "start_kline_futures_socket", None)
        if fn is None:
            # 구버전 명칭
            fn = getattr(self.twm, "start_kline_socket", None)
        if fn is None:
            log.error("kline socket 함수가 없습니다.")
            return

        try:
            sig = inspect.signature(fn)
            names = list(sig.parameters.keys())  # 예: ['callback','symbol','interval']

            # 1) (callback, symbol, interval)
            if len(names) >= 3 and names[0]=="callback" and names[1]=="symbol":
                return fn(self.on_kline, self.symbol, KLINE_INTERVAL_1MINUTE)

            # 2) (symbol, callback, interval)
            if len(names) >= 3 and names[0]=="symbol" and names[1]=="callback":
                return fn(self.symbol, self.on_kline, KLINE_INTERVAL_1MINUTE)

            # 3) 키워드 시도
            kwargs = {}
            if "callback" in sig.parameters: kwargs["callback"] = self.on_kline
            if "symbol"   in sig.parameters: kwargs["symbol"]   = self.symbol
            if "interval" in sig.parameters: kwargs["interval"] = KLINE_INTERVAL_1MINUTE
            return fn(**kwargs)

        except Exception as e:
            log.error(f"kline socket 시작 실패: {e}")

    # ---------------------------
    # 콜백(로그 + 큐 전송)
    # ---------------------------
    # 내부용: 큐에 이벤트 삽입
    def _push(self, typ: str, payload: Dict[str, Any]):
        evt = {"type": typ, "payload": payload, "ts": time.time()}
        log.debug(f"EVENT<{typ}>: {json.dumps(payload, ensure_ascii=False)[:800]}")
        try:
            self.event_queue.put_nowait(evt)
        except queue.Full:
            log.warning("WS 이벤트 큐가 가득 찼습니다. 가장 오래된 항목을 버립니다.")
            try:
                self.event_queue.get_nowait()
                self.event_queue.put_nowait(evt)
            except Exception:
                pass

    # User Data 이벤트 처리
    def on_user(self, msg: Dict[str, Any]):
        # 대표 이벤트: ACCOUNT_UPDATE, ORDER_TRADE_UPDATE, MARGIN_CALL
        etype = msg.get("e") or msg.get("eventType")
        if etype in ("ORDER_TRADE_UPDATE", "executionReport"):
            o = msg.get("o", {})
            # 1) 주문 스토어 갱신
            if self.order_store:
                try:
                    self.order_store.update_from_user_event(msg)  # ← 추가
                except Exception:
                    pass
            # 2) 캐시에도 최근 주문 이벤트 기록
            try:
                if self.cache:
                    oid = o.get("i") or o.get("orderId")
                    if oid is not None:
                        self.cache.set_order_event(str(oid), o)  # ← 추가
            except Exception:
                pass
            log.info(f"[USER] ORDER {o.get('s')} #{o.get('i')} {o.get('X')} "
                     f"side={o.get('S')} ps={o.get('ps')} "
                     f"lastFill={o.get('l')} avgPx={o.get('ap')} cumQty={o.get('z')}")
        elif etype == "ACCOUNT_UPDATE":
            a = msg.get("a", {})
            log.info(f"[USER] ACCOUNT balanceUpd={len(a.get('B',[]))} posUpd={len(a.get('P',[]))}")
        else:
            log.info(f"[USER] {etype}")
        self._emit("user", msg)

    # Mark Price 이벤트 처리
    # {'e':'markPriceUpdate','s':'ETHUSDT','p':'3380.12', ...}
    def on_mark_price(self, msg: Dict[str, Any]):
        try:
            m = self._unwrap(msg)
            # 심볼/가격/타임스탬프 키를 폭넓게 지원
            sym = m.get("s") or m.get("symbol")
            ts  = int(m.get("E") or m.get("eventTime") or 0)
            p   = safe_float(m.get("p") or m.get("markPrice"))            
            
            # 캐시에 마크프라이스 설정
            if self.cache and sym == self.symbol and p is not None:
                self.cache.set_mark(p, ts)
                if self._trace:
                    log.debug(f"[MARK] {msg.get('s')} mark={msg.get('p')} funding={msg.get('r')}")
        except Exception:
            pass
            
        self._emit("mark", self._unwrap(msg))

    # Kline 이벤트 처리
    # {'e':'kline', 's':'ETHUSDT', 'k': {... 'i':'1m','o':'','c':'', ...}}
    def on_kline(self, msg: Dict[str, Any]):
        try:
            m = self._unwrap(msg)
            log.debug(f"[KLINE-RAW] {json.dumps(m, ensure_ascii=False)[:500]}")
            k   = m.get("k") or {}

            # 1) 심볼 정규화
            #  - 일반 futures kline: top-level 's' 또는 k['s']
            #  - continuous_kline: top-level 'ps'(pair symbol)
            if m.get("e") == "continuous_kline":
                sym = (m.get("ps") or "").upper()
            else:
                sym = (m.get("s") or k.get("s") or "").upper()
            
            if not sym or sym != self.symbol.upper():
                self._emit("kline", m)
                return

            # interval과 close 여부
            interval = k.get("i") or m.get("i")
            is_closed_raw = k.get("x")
            is_closed = (is_closed_raw is True) or (str(is_closed_raw).lower() == "true")

            if interval == "1m" and is_closed:
                # 봉 마감이 아니어도 최신 close를 캐시에 반영
                payload = {
                    "i": k.get("i"),     # interval
                    "s": sym,            # symbol
                    "t": k.get("T"),
                    "o": k.get("o"),
                    "h": k.get("h"),
                    "l": k.get("l"),
                    "c": k.get("c"),
                    "v": k.get("v"),
                    "q": k.get("q"),
                    "closed": is_closed,     # ← 추가
                }
                if self.cache:
                    self.cache.set_kline_close(payload)

                if self._trace:
                    log.debug(f"[KLINE] {self.symbol} 1m close={k.get('c')}")
        except Exception:
            pass
            
        self._emit("kline", self._unwrap(msg))

    # ---------------------------
    # keepalive (45분마다)
    # ---------------------------
    def _keepalive_loop(self):
        # 상수선언
        keepalive_time_seconds = 45 * 60

        while not self._stop.is_set():
            try:
                time.sleep(keepalive_time_seconds)
                if self._listen_key and hasattr(self.client, "futures_stream_keepalive"):
                    self.client.futures_stream_keepalive(self._listen_key)
                    log.info("listenKey keepalive")
            except Exception as e:
                log.warning(f"keepalive 실패: {e}")

    # ---------------------------
    # 시작/종료
    # ---------------------------

    # 시작
    def start(self):
        # 타임 측정용
        t0 = time.monotonic()
        def _t(msg): log.info(f"[WS][{msg}] +{time.monotonic()-t0:.3f}s")

        # 1) WS 시작
        if self._started:
            log.warning("start() called while already started — ignore")
            return
        self._started = True

        log.info(f"flags user={self.enable_user} price={self.enable_price}")
        self.twm.start(); _t("twm.start")
        
        # ---------- 유저 소켓 ----------
        # listenKey 관리 버전에 따라 분기
        # 지원 버전: 명시적 listen_key 인자
        # 미지원 버전: 콜백만 전달
        if self.enable_user:
             use_listen_key = False
             try:
                 sig = inspect.signature(self.twm.start_futures_user_socket)
                 use_listen_key = ("listen_key" in sig.parameters)
             except Exception:
                 use_listen_key = False

             if use_listen_key:
                # 우리가 listenKey 직접 관리
                self._listen_key = self.client.futures_stream_get_listen_key(); _t("get_listen_key")
                log.info(f"listenKey 발급: {str(self._listen_key)[:8]}...")
                # 지원 버전: 명시적 listen_key 인자
                start_kwargs = {"callback": self.on_user}
                if "listen_key" in getattr(inspect.signature(self.twm.start_futures_user_socket), "parameters", {}):
                    start_kwargs["listen_key"] = self._listen_key
                self.twm.start_futures_user_socket(**start_kwargs); _t("user_socket")
                # keepalive 스레드 시작
                self._keepalive_thread = threading.Thread(target=self._keepalive_loop, daemon=True)
                self._keepalive_thread.start()
             else:
                 # 콜백만 넘김 (TWM이 내부적으로 listenKey 생성/관리)
                 self._listen_key = None
                 self.twm.start_futures_user_socket(callback=self.on_user); _t("user_socket(no key)")
                 log.info("futures user socket 시작(listen_key 인자 미지원 버전)")

        # ---------- 가격 소켓 ----------
        if self.enable_price:
            # 2-2) Mark Price (ETHUSDT 단일)
            # fast 인자 지원 버전과 미지원 버전 분기
            # 지원 버전: 빠른 시작
            # 미지원 버전: 기본값
            # 참고:
            #  python-binance 1.0.16부터 fast 인자 지원
            self._start_mark_price_socket_safe(); _t("mark_price_socket")

            # 2-3) 1m Kline (ETHUSDT)
            # fast 인자 없음
            # python-binance 1.0.16부터 kline_futures_socket 지원
            self._start_kline_socket_safe(); _t("kline_socket")

        log.info("started")

    # 종료
    def stop(self):
        self._stop.set()

        # listenKey close 시도
        try:
            # 우리가 listen_key를 직접 쓴 버전에서만 close 시도
            if self._listen_key and hasattr(self.client, "futures_stream_close"):
                self.client.futures_stream_close(self._listen_key)
        except Exception as e:
            log.warning(f"listenKey close 실패: {e}")

        # TWM 종료
        try:
            self.twm.stop()
        except Exception as e:
            log.warning(f"TWM stop 실패: {e}")
        finally:
            self._started = False

        log.info("stopped")