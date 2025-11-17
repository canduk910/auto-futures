# binance_conn.py
# -----------------------------------------------------------
# 역할:
#  - python-binance Client 생성(테스트넷/실서버 스위치)
#  - 선물 시세/통계/호가/캔들/계정 등 '데이터 수집'을 전담
#  - 로직/지표/빌더와 분리하여 테스트/교체 용이
# -----------------------------------------------------------

import os
from typing import Tuple, Dict, Any, Optional, List
import pandas as pd
from binance.client import Client

# .env 파일에서 환경변수 로드
from dotenv import load_dotenv
load_dotenv()

# 공통 유틸 임포트 - 시간변환, 안전한 float 변환 등
from common_utils import safe_float, to_iso

# ================================
# [Client 생성/환경 스위치]
# ================================
# python-binance Client 생성 + 테스트넷/실서버 스위치.
# - env="paper": 선물 테스트넷 URL로 교체(조회/주문 둘 다 해당).
# - env="live": 기본 실서버 사용.

def create_binance_client(env: str = "paper") -> Client:
    api_key = os.getenv("BINANCE_TESTNET_API_KEY")
    api_secret = os.getenv("BINANCE_TESTNET_SECRET_KEY")

    client = Client(api_key=api_key, api_secret=api_secret, testnet=(env == "paper"))

    if env == "paper":
        client.FUTURES_URL = "https://testnet.binancefuture.com/fapi"
        client.FUTURES_DATA_URL = "https://testnet.binancefuture.com/futures/data"

    return client

# =====================================================================
#  python-binance 버전에 따라 고수준 메서드가 없을 때를 대비해
#   저수준 Futures REST 호출로 우회
# =====================================================================
# 선물 API 우회 호출 헬퍼
#  - GET https://fapi.binance.com/fapi/v1/{path}
#  - python-binance 내부 저수준 라우터: _request_futures_api
def _fapi_get(client: Client, path: str, params: Optional[Dict[str, Any]] = None):
    try:
        return client._request_futures_api("get", path, data=params or {})
    except Exception:
        return None

# 선물 Data API 우회 호출 헬퍼
#  - GET https://fapi.binance.com/futures/data/{path}
#  - python-binance 내부 저수준 라우터: _request_futures_data_api
def _futures_data_get(client: Client, path: str, params: Optional[Dict[str, Any]] = None):
    try:
        return client._request_futures_data_api("get", path, data=params or {})
    except Exception:
        return None


# =====================================
# [USDT-M 헬퍼들: 공식 라이브러리 호출]
# =====================================

# 1 심볼의 정밀도/틱/스텝/최소 노셔널 조회
# 주문 라운딩 근거 등
# 반환: (price_precision, qty_precision, tick_size, step_size, min_notional)
def futures_exchange_filters(client: Client, symbol: str) -> Tuple[int, int, Optional[str], Optional[str], Optional[str]]:
    ex = client.futures_exchange_info()
    s = next(x for x in ex["symbols"] if x["symbol"] == symbol)
    price_precision = s.get("pricePrecision")
    qty_precision = s.get("quantityPrecision")
    tick_size = step_size = min_notional = None
    for f in s["filters"]:
        if f["filterType"] == "PRICE_FILTER":
            tick_size = f.get("tickSize")
        elif f["filterType"] == "LOT_SIZE":
            step_size = f.get("stepSize")
        elif f["filterType"] in ("NOTIONAL", "MIN_NOTIONAL"):
            min_notional = f.get("notional") or f.get("minNotional")
    return price_precision, qty_precision, tick_size, step_size, min_notional

# 2 심볼의 마크/인덱스/펀딩/다음 펀딩시각 조회
#    반환 예:
#    {
#      "mark_price": 3377.1291,
#      "index_price": 3376.8000,
#      "last_funding_rate": 0.0001,     # 0.01% → 0.0001
#      "next_funding_time": 1762664103825,  # ms
#      "interest_rate": 0.0001,         # 없으면 None
#      "server_time": 1762664103825
#    }
def fetch_premium_and_funding(client: Client, symbol: str) -> Dict[str, Any]:
    # 초기 반환값
    res = {
        "mark_price": None,
        "index_price": None,
        "last_funding_rate": None,
        "next_funding_time": None,
        "interest_rate": None,
        "server_time": None,
    }

    premium: Optional[Dict[str, Any]] = None
    # 1) 프리미엄/인덱스 조회 (버전별 호환)
    try:
        if hasattr(client, "futures_premium_index"):
            # 일부 버전에만 존재
            premium = client.futures_premium_index(symbol=symbol)
        else:
            # 저수준 엔드포인트 직접 호출: GET /fapi/v1/premiumIndex
            premium = client._request_futures_api("get", "premiumIndex", data={"symbol": symbol})
    except Exception:
        premium = None  # 조용히 폴백

    # 2) 마크프라이스 조회 (대부분 버전에 존재, funding/nextFundingTime 포함)
    mp: Optional[Dict[str, Any]] = None
    try:
        mp = client.futures_mark_price(symbol=symbol)
    except Exception:
        mp = None

    # 3) 최근 펀딩 이력(보강용) — 마지막 1개만
    last_rate = None
    try:
        hist = client.futures_funding_rate(symbol=symbol, limit=1)
        if isinstance(hist, list) and hist:
            last_rate = safe_float(hist[-1].get("fundingRate"), None)
    except Exception:
        pass

    # 4) 합성
    if premium:
        res["mark_price"]       = safe_float(premium.get("markPrice"), res["mark_price"])
        res["index_price"]      = safe_float(premium.get("indexPrice"), res["index_price"])
        res["last_funding_rate"]= safe_float(premium.get("lastFundingRate"), last_rate)
        res["next_funding_time"]= premium.get("nextFundingTime") or res["next_funding_time"]
        res["interest_rate"]    = safe_float(premium.get("interestRate"), res["interest_rate"])
        res["server_time"]      = premium.get("time") or res["server_time"]

    if mp:
        # premium이 비었거나 필드가 누락된 경우 보강
        if res["mark_price"]       is None: res["mark_price"] = safe_float(mp.get("markPrice"), None)
        if res["last_funding_rate"] is None: res["last_funding_rate"] = safe_float(mp.get("lastFundingRate"), last_rate)
        if res["next_funding_time"] is None: res["next_funding_time"] = mp.get("nextFundingTime")
        if res["server_time"]       is None: res["server_time"] = mp.get("time")
        # 일부 버전에선 indexPrice가 markPrice 응답에 포함되기도 함
        if res["index_price"] is None:
            res["index_price"] = safe_float(mp.get("indexPrice"), None)

    return res

# 3 미결제약정
# 반환: float | None
# 미결제약정: 시장 전체 계약 수량
def fetch_open_interest(client: Client, symbol: str) -> Optional[float]:
    try:
        d = client.futures_open_interest(symbol=symbol)
        return safe_float(d.get("openInterest"))
    except Exception:
        return None

# 4 24h 미결제약정 변화율 조회
def fetch_oi_change_24h_pct(client: Client, symbol: str) -> Optional[float]:
    """5m 간격 289개(≈24h) 히스토리의 처음/마지막 비교."""
    try:
        hist = client.futures_open_interest_hist(symbol=symbol, period="5m", limit=289)
        if isinstance(hist, list) and len(hist) >= 2:
            now = safe_float(hist[-1].get("sumOpenInterest"))
            prev = safe_float(hist[0].get("sumOpenInterest"))
            if prev and prev > 0:
                return (now - prev) / prev * 100.0
    except Exception:
        pass
    return None

# 5 롱/숏 비율 조회
#     글로벌 롱/숏 계정 비율(USDT-M).
#    - 호출부 호환 유지: dict 반환(기존 키 그대로)
#    - python-binance 버전에 client.futures_global_long_short_account_ratio가 없을 때
#      저수준 GET /fapi/v1/futures/data/globalLongShortAccountRatio 로 우회
#    - 데이터가 없으면 {} 반환(기존 동작 유지)
#     반환 예:
#    {
#      "ratio": 1.12,                # longShortRatio(최근)
#      "prev_ratio": 1.05,           # 직전 캔들
#      "delta": 0.07,                # 변화량(최근-직전)
#      "long_accounts": 0.51,        # longAccount (비율)
#      "short_accounts": 0.49,       # shortAccount (비율)
#      "period": "5m",
#      "timestamp": 1762664100000
#    }
def fetch_long_short_ratio(client: Client, symbol: str) -> Dict[str, Any]:
    # ── (기존 호출부는 인자 없이 쓴다) 내부 기본값만 설정 ──
    period = "5m"   # 허용: 5m,15m,30m,1h,2h,4h,6h,12h,1d
    limit  = 30

    # ── 고수준 메서드 시도 ──
    data: Optional[List[Dict[str, Any]]] = None
    try:
        if hasattr(client, "futures_global_long_short_account_ratio"):
            data = client.futures_global_long_short_account_ratio(symbol=symbol, period=period, limit=limit)
    except Exception:
        data = None

    # ── 없거나 실패하면 저수준 호출로 우회 ──
    if not data:
        data = _futures_data_get(client, "globalLongShortAccountRatio",
                         {"symbol": symbol, "period": period, "limit": limit})

    # ── 파싱(빈 데이터 방어: 기존처럼 {}) ──
    if not data or not isinstance(data, list) or len(data) == 0:
        return {}

    last = data[-1]
    prev = data[-2] if len(data) >= 2 else None

    last_ratio = safe_float(last.get("longShortRatio"))
    prev_ratio = safe_float(prev.get("longShortRatio")) if prev else None
    delta      = (last_ratio - prev_ratio) if (last_ratio is not None and prev_ratio is not None) else None
    long_acc   = safe_float(last.get("longAccount"))
    short_acc  = safe_float(last.get("shortAccount"))
    ts         = last.get("timestamp")

    return {
        "ratio": last_ratio,
        "prev_ratio": prev_ratio,
        "delta": delta,
        "long_accounts": long_acc,
        "short_accounts": short_acc,
        "period": period,
        "timestamp": ts
    }

# 6 호가 스프레드/최우선 호가/주문서 편향 조회
# 반환: dict
def fetch_orderbook_metrics(client: Client, symbol: str) -> Dict[str, Any]:
    try:
        ob = client.futures_order_book(symbol=symbol, limit=50)
        bids = ob.get("bids", [])
        asks = ob.get("asks", [])
        bid_price = float(bids[0][0]) if bids else None
        bid_qty   = float(bids[0][1]) if bids else None
        ask_price = float(asks[0][0]) if asks else None
        ask_qty   = float(asks[0][1]) if asks else None
        spread = abs(ask_price - bid_price) if (bid_price and ask_price) else None

        sb = sum(float(q) for _, q in bids) if bids else 0.0
        sa = sum(float(q) for _, q in asks) if asks else 0.0
        imb = (sb - sa) / (sb + sa) if (sb + sa) > 0 else 0.0

        return {
            "bid_ask_spread": spread,
            "top1": {"bid_price": bid_price, "bid_qty": bid_qty, "ask_price": ask_price, "ask_qty": ask_qty},
            "topN_imbalance": imb
        }
    except Exception:
        return {"bid_ask_spread": None, "top1": {}, "topN_imbalance": 0.0}

# 7 24h 거래량/고가/저가 조회
#     24시간 가격변동 통계 (GET /fapi/v1/ticker/24hr) — 버전 호환 구현
#    - client.futures_ticker_24hr()가 없으면 _request_futures_api로 우회
#    - lastPrice가 누락될 경우 /ticker/price로 보강
# 반환: dict
def fetch_ticker_24h(client: Client, symbol: str) -> Dict[str, Any]:
    d = None
    # 1) 고수준 메서드가 있으면 사용
    try:
        if hasattr(client, "futures_ticker_24hr"):
            d = client.futures_ticker_24hr(symbol=symbol)
    except Exception:
        d = None

    # 2) 없거나 실패하면 저수준으로 직접 호출
    if not d:
        d = _fapi_get(client, "ticker/24hr", {"symbol": symbol})
        if not d:
            return {}

    out = {
        "last_price":        safe_float(d.get("lastPrice")),
        "price_change":      safe_float(d.get("priceChange")),
        "price_change_pct":  safe_float(d.get("priceChangePercent")),
        "open_price":        safe_float(d.get("openPrice")),
        "high_price":        safe_float(d.get("highPrice")),
        "low_price":         safe_float(d.get("lowPrice")),
        "volume_base":       safe_float(d.get("volume")),        # 24h 거래량(기초자산)
        "volume_quote":      safe_float(d.get("quoteVolume")),   # 24h 거래대금(USDT)
        "open_time":         d.get("openTime"),
        "close_time":        d.get("closeTime"),
        "count":             int(safe_float(d.get("count"), 0) or 0),
    }

    # 3) last_price 누락 보강: GET /fapi/v1/ticker/price
    if out["last_price"] is None:
        t = _fapi_get(client, "ticker/price", {"symbol": symbol})
        if t:
            out["last_price"] = safe_float(t.get("price"))

    return out

# 8 캔들 조회(15분봉 기본)
# 반환: pd.DataFrame with columns(ts,o,h,l,c,v)
def fetch_klines(client: Client, symbol: str, interval: str = "15m", limit: int = 96) -> pd.DataFrame:
    raw = client.futures_klines(symbol=symbol, interval=interval, limit=limit)
    df = pd.DataFrame(raw, columns=[
        "open_time","o","h","l","c","v","close_time","qv","n","taker_base","taker_quote","ignore"
    ])
    df = df.astype({"open_time": "int64", "o":"float64","h":"float64","l":"float64","c":"float64","v":"float64"})
    df["ts"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df[["ts","o","h","l","c","v"]]

# 9 일간 캔들 조회(3일치 기본)
# 반환: pd.DataFrame with columns(ts,o,h,l,c,v)
def fetch_daily_klines(client: Client, symbol: str, limit: int = 3) -> pd.DataFrame:
    raw = client.futures_klines(symbol=symbol, interval="1d", limit=limit)
    df = pd.DataFrame(raw, columns=[
        "open_time","o","h","l","c","v","close_time","qv","n","taker_base","taker_quote","ignore"
    ])
    df = df.astype({"open_time": "int64", "o":"float64","h":"float64","l":"float64","c":"float64","v":"float64"})
    df["ts"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df[["ts","o","h","l","c","v"]]

# 10 계정 정보 및 오픈 포지션 조회
# 반환: dict
# 키 없음/권한 이슈 → 기본값을 유지하고 실패는 조용히 무시.
def fetch_account_and_positions(client: Client, symbol_filter: Optional[str] = None) -> Dict[str, Any]:

    # ---------------------------------------------------------
    # 헬퍼 (함수의 내부함수)
    # ---------------------------------------------------------
    def _is_hedge_mode() -> bool:
        try:
            d = client.futures_get_position_mode()  # {'dualSidePosition': True|False}
            return str(d.get("dualSidePosition", "")).lower() in ("true", "1", "yes")
        except Exception:
            return False

    def _calc_upnl_usdt(mark: float, entry: float, qty: float) -> float:
        if mark is None or entry is None or not qty:
            return 0.0
        return (mark - entry) * qty if qty > 0 else (entry - mark) * abs(qty)

    def _infer_margin_mode(p: Dict[str, Any]) -> str:
        # position_information 응답에는 'isolatedMargin'/'isolatedWallet'이 들어오는 경우가 많음
        if str(p.get("marginType", "")).lower() == "isolated":
            return "isolated"
        if (safe_float(p.get("isolatedMargin"), 0.0) or 0) > 0:
            return "isolated"
        if (safe_float(p.get("isolatedWallet"), 0.0) or 0) > 0:
            return "isolated"
        return "cross"

    account = {
        "equity_usdt": 5000.0,    # 기본값 5,000 USDT
        "margin_mode": "isolated", # 기본값 격리
        "leverage": 5,       # 기본값 5배
        "max_leverage": 20,  # 일반적으로 USDT-M 선물은 최대 20배
        "fee_tier": 2,       # 기본값 2 (0~9)
        "maker_fee": 0.0002, # 일반적으로 USDT-M 선물은 0.02%
        "taker_fee": 0.0004, # 일반적으로 USDT-M 선물은 0.04%
        "risk_limits": {
            "max_position_usdt": 5000.0, # 포지션당 최대 허용 USDT
            "risk_pct_of_equity": 0.5,   # 계정자산 대비 최대 리스크 허용 비율(%)
            "daily_loss_limit_pct": 3.0, # 일일 최대 손실 허용 비율(%)
            "max_drawdown_pct": 10.0     # 최대 누적 손실 허용 비율(%)
        },
        "open_positions": [],
        "open_orders": []
    }


    # ---------------------------------------------------------
    # 1) 지갑/레버리지: futures_account()로 에쿼티와 심볼별 레버리지 맵 확보
    # ---------------------------------------------------------
    symbol_leverage: Dict[str, int] = {}
    try:
        acc = client.futures_account()
        for a in acc.get("assets", []):
            if a.get("asset") == "USDT":
                account["equity_usdt"] = safe_float(a.get("walletBalance"), account["equity_usdt"])
                break
        for p in acc.get("positions", []):
            lv = int(safe_float(p.get("leverage"), 0) or 0)
            if p.get("symbol") and lv:
                symbol_leverage[p["symbol"]] = lv
    except Exception:
        pass  # 키/권한 문제 시 기본값 유지

    # ---------------------------------------------------------
    # 2) 포지션: futures_position_information() 기준으로 표준화
    # ---------------------------------------------------------
    try:
        raw = client.futures_position_information(symbol=symbol_filter) if symbol_filter \
              else client.futures_position_information()
    except Exception:
        raw = []

    hedge_mode = _is_hedge_mode()
    positions: List[Dict[str, Any]] = []

    for p in raw:
        sym = p.get("symbol")
        if symbol_filter and sym != symbol_filter:
            continue

        qty = safe_float(p.get("positionAmt"), 0.0) or 0.0
        if abs(qty) == 0:
            continue  # 수량 0 라인 제거

        entry = safe_float(p.get("entryPrice"))
        # position_information 응답에는 markPrice가 포함되는 경우가 많음
        mark = safe_float(p.get("markPrice"))
        upnl_raw = safe_float(p.get("unRealizedProfit"), None)
        upnl = upnl_raw if upnl_raw is not None else _calc_upnl_usdt(mark, entry, qty)

        # 청산가: "0"/0.0 이면 의미 없는 값으로 보아 None 처리
        liq = safe_float(p.get("liquidationPrice"), None)
        if liq == 0.0:
            liq = None

        # 헤지 모드면 positionSide를 우선
        if hedge_mode:
            ps = str(p.get("positionSide", "BOTH")).upper()
            if ps in ("LONG", "SHORT"):
                side = ps.lower()
            else:
                side = "long" if qty > 0 else "short"
        else:
            side = "long" if qty > 0 else "short"

        # 마진 모드/레버리지
        margin_mode = _infer_margin_mode(p)

        lev_raw = p.get("leverage")
        lev_val = safe_float(lev_raw, None)   # None, '', 'NaN' 등 모두 방어

        # 기본값 처리
        # 계정 전체 기본 레버리지로 폴백
        # 심볼별 레버리지 맵에 없으면 계정 기본값 사용
        # 심볼별 레버리지 맵에 있으면 그 값을 우선
        # 심볼별 레버리지 맵에 없고, p.get("leverage")가 None/0/음수 등 이상치인 경우 계정 기본값 사용
        # 계정 기본값은 위에서 futures_account()로 미리 채워둠
        # 최종적으로도 이상치인 경우 5배로 디폴트
        if lev_val is None or lev_val <= 0:
            lev = int(account.get("leverage", 5))  # 계정 디폴트로 폴백
        else:
            lev = int(lev_val)

        leverage = symbol_leverage.get(sym, lev)

        positions.append({
            "symbol": sym,
            "side": side,
            "qty": abs(qty),
            "entry_price": entry,
            "unrealized_pnl_usdt": upnl,
            "liquidation_price": liq,  # None이면 UI에서 '미제공/무의미' 표기 권장
            "break_even_price": safe_float(p.get("breakEvenPrice")),
            "margin_mode": margin_mode,
            "leverage": leverage
        })

    account["open_positions"] = positions
    # 계정 전역 margin_mode/leverge 표시는 첫 포지션의 값 또는 디폴트로 유지
    if positions:
        account["margin_mode"] = positions[0]["margin_mode"]
        account["leverage"] = positions[0]["leverage"]

    return account