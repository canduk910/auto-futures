# input_builder.py
# -----------------------------------------------------------
# 역할:
#  - binance_conn에서 '수집'된 데이터들을 호출
#  - tech_indicators로 지표 계산
#  - common_utils로 포맷/라운딩
#  - 최종 INPUT JSON을 조립/반환
# -----------------------------------------------------------

import os, json
from typing import Tuple, Dict, Any
import numpy as np
import pandas as pd
from binance.client import Client

# 개별 모듈 임포트
#  - 공통 유틸: 시간변환, 안전한 float 변환, 반올림, 가격/수량 조정 등
from common_utils import utc_now_iso, round_or_none
#  - 기술지표: EMA, RSI, MACD, ATR, HV, VWAP, S/R 등
from tech_indicators import (
    ema, rsi, macd, atr, stochastic,
    historical_volatility, vwap_from_bars, pivots_sr
)
# - 바이낸스 연결/데이터 수집: Client 생성, 심볼 필터, 시세/펀딩/미결제약정/롱숏비율/호가/캔들/계정 등
from binance_conn import (
    create_binance_client, futures_exchange_filters,
    fetch_premium_and_funding, fetch_open_interest, fetch_oi_change_24h_pct,
    fetch_long_short_ratio, fetch_orderbook_metrics, fetch_ticker_24h,
    fetch_klines, fetch_daily_klines, fetch_account_and_positions
)

# ================================
# [INPUT JSON 빌더]
# ================================
# 바이낸스 선물 데이터 수집 → 지표 계산 → INPUT JSON 조립
def build_input_json(
    symbol: str = "BTCUSDT",
    env: str = "paper",
    tz_str: str = "Asia/Seoul",
    app_version: str = "1.0.0",
    override_mark: float | None = None,
    override_kline_close: dict | None = None
) -> Dict[str, Any]:

    client = create_binance_client(env=env)

    # 1) 계약/정밀도
    pp, qp, tick, step, min_notional = futures_exchange_filters(client, symbol)
    contract = {
        "market": "USDT-M",
        "type": "perpetual",
        "price_precision": pp,
        "quantity_precision": qp,
        "tick_size": tick,
        "step_size": step,
        "min_notional": min_notional
    }

    # 2) 시세/통계
    prem = fetch_premium_and_funding(client, symbol) or {}
    d24  = fetch_ticker_24h(client, symbol)
    oi   = fetch_open_interest(client, symbol)
    oi_chg = fetch_oi_change_24h_pct(client, symbol)
    lsr    = fetch_long_short_ratio(client, symbol)
    fr = float(prem.get("funding_rate", 0.0) or 0.0)
    basis_ann_pct = fr * 3 * 365 * 100.0

    market_obj = {
        "last_price": prem.get("mark_price"),
        "mark_price": prem.get("mark_price"),
        "index_price": prem.get("index_price"),
        "premium_index": prem.get("premium_index"),
        "funding_rate": fr,
        "next_funding_time": prem.get("next_funding_time"),
        "open_interest": oi,
        "oi_change_24h_pct": oi_chg,
        "long_short_ratio": lsr,
        "basis_annualized_pct": basis_ann_pct,
        "twentyfour_h": d24
    }

    # 3) 유동성
    liquidity = fetch_orderbook_metrics(client, symbol)

    # 4) 캔들(15m, 96개) + 지표
    bars15 = fetch_klines(client, symbol, interval="15m", limit=96)
    closes = bars15["c"]

    ema20 = float(ema(closes, 20).iloc[-1]) if len(closes) >= 20 else None
    ema50 = float(ema(closes, 50).iloc[-1]) if len(closes) >= 50 else None
    ema200 = float(ema(closes, 200).iloc[-1]) if len(closes) >= 200 else None
    rsi14 = float(rsi(closes, 14).iloc[-1]) if len(closes) >= 15 else None

    m_val, m_sig, m_hist = macd(closes)
    macd_obj = {"value": float(m_val.iloc[-1]), "signal": float(m_sig.iloc[-1]), "hist": float(m_hist.iloc[-1])}

    atr14 = float(atr(bars15[["h","l","c"]], 14).iloc[-1]) if len(bars15) >= 15 else None
    k, d = stochastic(bars15[["h","l","c"]], 14, 3)
    stoch_obj = {"k": float(k.iloc[-1]), "d": float(d.iloc[-1])}
    hv10 = historical_volatility(closes, bars_per_day=96, days=10) if len(closes) >= 20 else None

    # 5) 전일 변동폭(변동성 돌파 참고)
    ddf = fetch_daily_klines(client, symbol, limit=3)
    range_prev = float(ddf["h"].iloc[-2] - ddf["l"].iloc[-2]) if len(ddf) >= 2 else None

    technicals = {
        "atr_14": atr14,
        "rsi_14": rsi14,
        "macd": macd_obj,
        "ma": {"ema20": ema20, "ema50": ema50, "ema200": ema200},
        "hv_10": hv10,
        "stoch": stoch_obj,
        "volatility_breakout": {"range_prev": range_prev, "k": 0.5}
    }

    # 6) 레벨(VWAP / 간단 S/R)
    supports, resistances = pivots_sr(bars15, lookback=12, top_k=2)
    vwap_val = vwap_from_bars(bars15)
    levels = {
        "support": [round_or_none(s, 2) for s in supports],
        "resistance": [round_or_none(r, 2) for r in resistances],
        "vwap": round_or_none(vwap_val, 2)
    }

    # 7) 최근 바 배열
    recent_bars_15m = [
        {"t": bars15["ts"].iloc[i].isoformat(),
         "o": float(bars15["o"].iloc[i]),
         "h": float(bars15["h"].iloc[i]),
         "l": float(bars15["l"].iloc[i]),
         "c": float(bars15["c"].iloc[i]),
         "v": float(bars15["v"].iloc[i])}
        for i in range(len(bars15))
    ]

    # 8) 계정/포지션(키 있으면 실제 반영)
    account = fetch_account_and_positions(client, symbol_filter=symbol)

    # 9) 전략 제약(운영 가드)
    constraints = {
        "cooldown_minutes": 15,
        "max_orders": 1,
        "forbidden_sides": [],
        "forbidden_times_utc": ["15:55-16:05"]
    }

    # 10) 최종 INPUT JSON
    input_json: Dict[str, Any] = {
        "meta": {
            "env": env,
            "timestamp_utc": utc_now_iso(),
            "timezone": tz_str,
            "lang": "ko",
            "app_version": app_version
        },
        "symbol": symbol,
        "contract": contract,
        "account": account,
        "market": {**market_obj},
        "liquidity": liquidity,
        "technicals": technicals,
        "levels": levels,
        "recent_bars_15m": recent_bars_15m,
        "constraints": constraints,
        "question": "다음 4시간 인트라데이 전략 조언과 포지션 제안",
        "history": []
    }
    return input_json

if __name__ == "__main__":
    js = build_input_json(symbol="BTCUSDT", env="paper")
    print(json.dumps(js, ensure_ascii=False, indent=2))