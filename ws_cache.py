# ws_cache.py
# ---------------------------------------------
# 웹소켓 데이터 캐시 모듈
#  - 마크프라이스, 최근 캔들, 체결, 주문 이벤트 저장
#  - 스레드 안전
# ---------------------------------------------
from dataclasses import dataclass, field
from threading import RLock # for thread-safe operations
from collections import deque
from typing import Deque, Dict, Any, Optional
from copy import deepcopy
import time

@dataclass
class WsCache:
    symbol: str
    mark_price: Optional[float] = None
    last_mark_ts: float = 0.0
    last_kline_close: Dict[str, Any] = field(default_factory=dict)  # {"t","o","h","l","c","v","q"}
    trades: Deque[Dict[str,Any]] = field(default_factory=lambda: deque(maxlen=512))
    orders: Dict[str, Any] = field(default_factory=dict)  # orderId -> last event
    _lock: RLock = field(default_factory=RLock, repr=False)

    def __init__(self, symbol: str):
        self.symbol = symbol
        self._lock = RLock()
        self.mark_price: Optional[float] = None
        self.last_mark_ts: float = 0.0
        self.last_kline_close: Dict[str, Any] = {}

    # 1 마크프라이스 설정
    def set_mark(self, p: float, ts_ms: int):
        with self._lock:
            self.mark_price = p
            self.last_mark_ts = ts_ms / 1000.0

    # 2 최근 캔들 종가 설정
    def set_kline_close(self, k: Dict[str, Any]):
        with self._lock:
            self.last_kline_close = k

    # 3 체결거래내역 추가
    def add_trade(self, t: Dict[str, Any]):
        with self._lock:
            self.trades.append(t)

    # 4 주문 이벤트 설정
    def set_order_event(self, oid: str, ev: Dict[str, Any]):
        with self._lock:
            self.orders[str(oid)] = ev

    # 5 현재 스냅샷 반환
    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "symbol": self.symbol,
                "mark_price": self.mark_price,
                "last_mark_ts": self.last_mark_ts,
                "last_kline_close": dict(self.last_kline_close),
                "trades": list(self.trades),
                "orders": dict(self.orders),
                "ts": time.time()
            }
        

# ---- 글로벌 접근자(비침습 통합용) ----
_GLOBAL: Optional[WsCache] = None
def set_global_cache(c: WsCache):  # service_runner에서 1회 호출
    global _GLOBAL; _GLOBAL = c
def get_global_cache() -> Optional[WsCache]:
    return _GLOBAL
