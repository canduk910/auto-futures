# order_store.py
# ----------------------------------------------------------------------
# 목적: 실시간(User Data Stream)으로 들어오는 주문이벤트를 안전하게 저장하고
#       "주문이 끝났는지(FILLED/CANCELED/REJECTED/EXPIRED)"를 기다릴 수 있게 함.
# 특징:
#   - 스레드 세이프(dict + Lock)
#   - 주문별 tracker(Event)로 다른 스레드에서 대기/깨어남
#   - Binance 선물의 'ORDER_TRADE_UPDATE' 이벤트(o.* 필드) 기준
# ----------------------------------------------------------------------

from __future__ import annotations
from threading import Event, Lock
import threading
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, Any, List
import time

# Binance 선물 주문의 "터미널(종료) 상태"
TERMINAL_STATUSES = {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}

# ------ 주문 추적기 ------
# 주문 한 건의 상태를 추적하고, 터미널 상태 도달 시 대기자를 깨우는 역할
@dataclass
class OrderTracker:
    symbol: str
    order_id: int
    side: Optional[str] = None           # 'BUY' / 'SELL'
    position_side: Optional[str] = None  # 'LONG' / 'SHORT' / None
    status: str = "NEW"                  # NEW / PARTIALLY_FILLED / FILLED / ...
    order_type: Optional[str] = None     # ot (LIMIT/MARKET/STOP_...)
    reduce_only: Optional[bool] = None   # R (True/False)
    price: Optional[float] = None        # p (주문가)
    stop_price: Optional[float] = None   # s (스탑가)
    quantity: Optional[float] = None     # q (주문수량)
    executed_qty: float = 0.0            # z (누적 체결수량)
    last_fill_qty: float = 0.0           # l (직전 체결수량)
    avg_price: Optional[float] = None    # ap (평균 체결가)
    last_fill_price: Optional[float] = None  # L (직전 체결가)
    update_time: Optional[int] = None    # E/T

    # 대기/신호 처리
    _event: Event = field(default_factory=Event, repr=False)
    # 동기화
    _lock: Lock = field(default_factory=Lock, repr=False)

    # ------ 상태 검사/대기 ------
    # 주문이 터미널 상태인지 검사
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    # 터미널 상태로 전환 시 대기자 깨우기
    def set_terminal(self):
        self._event.set()

    # 대기자용: 터미널 상태 될 때까지 대기
    # 터미널 상태 될 때까지 대기. True=종료 상태 도달, False=타임아웃.
    def wait(self, timeout: Optional[float] = None) -> bool:
        return self._event.wait(timeout=timeout)
    
    # 조회용 스냅샷
    # 주문 상태의 사본(dict) 반환
    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            d = asdict(self)
            # 내부 동기화 객체는 제외
            d.pop("_event", None); d.pop("_lock", None)
            return d

# ------ 주문 저장소 ------
# 여러 주문의 상태를 스레드 세이프하게 관리
class OrderStore:

    #------ 초기화 ------
    # 주문ID → OrderTracker 맵
    # Lock으로 스레드 세이프 보장
    def __init__(self):
        self._lock = threading.Lock()
        self._orders: Dict[int, OrderTracker] = {}

    # ------ 등록/조회/삭제 ------
    # 새 주문을 등록. 이미 있으면 반환.
    # 주문ID는 고유해야 함.
    # 외부에서 주문 생성 시 호출
    # 주문이벤트가 먼저 도착하는 경우 대비
    # 외부에서 등록 안 했더라도 조회 후 등록 가능
    def register(self,
                symbol: str,
                order_id: int,
                side: Optional[str],
                position_side: Optional[str],
                order_type: Optional[str] = None,
                reduce_only: Optional[bool] = None,
                price: Optional[float] = None,
                stop_price: Optional[float] = None,
                quantity: Optional[float] = None,
        ) -> OrderTracker:
        with self._lock:
            ot = self._orders.get(order_id)
            if ot is None:
                ot = OrderTracker(symbol=symbol, order_id=order_id, side=side, position_side=position_side
                                  , order_type=order_type, reduce_only=reduce_only
                                  , price=price, stop_price=stop_price, quantity=quantity)
                self._orders[order_id] = ot
            return ot

    # 주문ID로 조회
    # None이면 미등록
    # 주문이벤트가 먼저 도착하는 경우 대비
    # 외부에서 등록 안 했더라도 조회 후 등록 가능
    def get(self, order_id: int) -> Optional[OrderTracker]:
        with self._lock:
            return self._orders.get(order_id)

    # 주문ID로 삭제
    # 존재하지 않아도 무방
    # 외부에서 주문 추적기를 더 이상 필요로 하지 않을 때 호출
    # 메모리 누수 방지
    def remove(self, order_id: int):
        with self._lock:
            self._orders.pop(order_id, None)

    # ------ 웹소켓 이벤트 반영 ------
    # Binance ORDER_TRADE_UPDATE 이벤트 페이로드를 받아 주문 상태 갱신
    # 외부에서 호출
    # 주문이 터미널 상태가 되면 대기자 깨우기
    # 주문이벤트가 먼저 도착하는 경우 대비
    # Binance ORDER_TRADE_UPDATE 형식:
    #      msg = {
    #        'e': 'ORDER_TRADE_UPDATE', 'E': serverTime, 'T': tradeTime,
    #        'o': { 's':symbol,'i':orderId,'S':side,'ps':positionSide,'X':orderStatus,
    #               'z':cumQty,'l':lastFillQty,'ap':avgPrice,'L':lastFillPrice, ...}
    #      }
    def update_from_user_event(self, payload: Dict[str, Any]):
        if not isinstance(payload, dict):
            return
        etype = (payload.get("e") or payload.get("eventType") or "").upper()

        # 주문이벤트 처리
        if etype == "ORDER_TRADE_UPDATE":
            # 주문 정보 추출
            o = payload.get("o", {})

            # 주문ID 필수
            order_id = o.get("i")
            if order_id is None:
                return
            
            # 심볼
            symbol = o.get("s")

            # 주문 추적기 가져오기/생성
            # 주문이벤트가 먼저 도착하는 경우 대비
            with self._lock:
                ot = self._orders.get(order_id)
                if ot is None:
                    # 외부에서 등록 안 했더라도, 체결 이벤트가 먼저 도착하는 드문 케이스 대비
                    ot = OrderTracker(symbol=o.get("s"), order_id=order_id)
                    self._orders[order_id] = ot
                self._merge_o_fields(ot, o, payload)

        # 터미널 상태 도달 시 대기자 깨우기
        elif etype == "EXECUTUIONREPORT":
            # executionReport는 payload 자체가 상세
            o = payload
            symbol = o.get("s")
            oid = self._safe_int(o.get("i") or o.get("orderId"))
            if oid is None:
                return
            with self._lock:
                ot = self._orders.get(oid)
                if ot is None:
                    ot = OrderTracker(symbol=symbol or "", order_id=oid)
                    self._orders[oid] = ot
                self._merge_o_fields(ot, o, payload)
        else:
            return
        
    # ------ 주문 터미널 대기 ------
    # 특정 주문ID가 터미널 상태가 될 때까지 대기
    # 타임아웃 가능
    # 외부에서 호출 (주문ID가 터미널 상태가 될 때까지 대기하고 최종 스냅샷을 반환.)
    def wait_until_terminal(self, order_id: int, timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
        with self._lock:
            ot = self._orders.get(order_id)
        if ot is None:
            # 주문이 등록되기 전에 기다리는 경우(드묾): 잠깐 폴링
            t0 = time.time()
            while time.time() - t0 < (timeout or 0):
                time.sleep(0.05)
                with self._lock:
                    ot = self._orders.get(order_id)
                if ot:
                    break
            if ot is None:
                return None
        ok = ot.wait_until_terminal(timeout=timeout)
        return ot.snapshot() if ok else None         
                
    def list_open(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [ot.snapshot() for ot in self._orders.values() if not ot.is_terminal()]
        
    # 주문이벤트 필드 병합
    # 내부용
    # 주문 추적기에 이벤트 필드 반영
    def _merge_o_fields(self, ot: OrderTracker, o: Dict[str, Any], payload: Dict[str, Any]) -> None:
        # 메타
        sym = o.get("s") or ot.symbol
        if sym:
            ot.symbol = sym

        # 상태/방향/유형
        st = (o.get("X") or ot.status or "").upper()
        ot.status = st
        ot.side = o.get("S") or ot.side
        ot.position_side = o.get("ps") or ot.position_side
        ot.order_type = o.get("ot") or o.get("o") or ot.order_type  # 일부 이벤트는 'o' 키에 타입

        # 수량/가격
        p  = self._safe_float(o.get("p"))  # price
        sp = self._safe_float(o.get("sp")) # stopPrice
        q  = self._safe_float(o.get("q"))  # quantity
        z  = self._safe_float(o.get("z"))  # executedQty
        lq = self._safe_float(o.get("l"))  # lastFillQty
        ap = self._safe_float(o.get("ap")) # avgPrice
        lp = self._safe_float(o.get("L"))  # lastFillPrice

        if p is not None:  ot.price = p
        if sp is not None: ot.stop_price = sp
        if q is not None:  ot.quantity = q
        if z is not None:  ot.executed_qty = z
        if lq is not None: ot.last_fill_qty = lq
        if lp is not None: ot.last_fill_price = lp
        if ap is not None: ot.avg_price = ap if ap is not None else ot.avg_price

        # reduceOnly
        ro = o.get("R")
        if isinstance(ro, bool):
            ot.reduce_only = ro
        elif isinstance(ro, str):
            ot.reduce_only = ro.lower() in ("1","true","yes")

        # 이벤트 시간
        ot.update_time = payload.get("E") or payload.get("T") or ot.update_time

        # 터미널이면 대기자 해제
        if ot.is_terminal():
            ot.set_terminal()
