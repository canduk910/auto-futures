# tech_indicators.py
# ---------------------------------------------
# 기술적 지표 계산 모듈
#  - EMA/RSI/MACD/ATR/스토캐스틱/HV/VWAP
#  - 간단 S/R(프랙탈 기반)
# ---------------------------------------------

import math
import numpy as np
import pandas as pd

# =================
# [기술지표 계산기]
# =================
# ※ 초보자 설명:
#    - 지표는 '과매수/과매도', '추세', '변동성' 등을 정량화해서 의사결정에 도움을 줍니다.
#    - 여기서는 가장 기본적이고 널리 쓰이는 것들을 포함했습니다.

# 1 지수이동평균(EMA) 산출
# 최근 데이터에 더 큰 가중치를 주는 이동평균.
def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()

# 2 RSI 산출
# 상대강도지수: 상승압력과 하락압력의 비율로 과매수/과매도 상태를 추정.
def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    up = pd.Series(np.where(delta > 0, delta, 0.0), index=series.index)
    down = pd.Series(np.where(delta < 0, -delta, 0.0), index=series.index)
    roll_up = up.rolling(period).mean()
    roll_down = down.rolling(period).mean()
    rs = roll_up / (roll_down + 1e-12)
    return 100 - (100 / (1 + rs))

# 3 MACD 산출
# 이동평균수렴확산지수: 빠른 EMA - 느린 EMA, 두 개의 EMA 차이로 추세와 모멘텀을 파악.
def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[pd.Series, pd.Series, pd.Series]:
    m = ema(series, fast) - ema(series, slow)
    s = ema(m, signal)
    h = m - s  # 히스토그램
    return m, s, h

# 4 ATR(Average True Range) 산출
# 평균진폭: 변동성 지표로, 가격의 평균적인 변동폭을 측정.
def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["h"], df["l"], df["c"]
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low),                    # 현재 고-저
        (high - prev_close).abs(),      # 고가-전일종가
        (low - prev_close).abs()        # 저가-전일종가
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()

# 5 스토캐스틱 오실레이터 산출
# 스토캐스틱: 특정 기간 고저폭 대비 현재가의 위치(과열/침체)
def stochastic(df: pd.DataFrame, k_period: int = 14, d_period: int = 3) -> tuple[pd.Series, pd.Series]:
    low_min = df["l"].rolling(k_period).min()
    high_max = df["h"].rolling(k_period).max()
    k = (df["c"] - low_min) / (high_max - low_min + 1e-12) * 100.0
    d = k.rolling(d_period).mean()
    return k, d

# 6 역사적 변동성(Historical Volatility) 산출
# 로그수익률 기반 변동성 지표 (하루/연 단위 스케일)
# 기본값: 15분봉 기준 하루 96개, 10일 사용
def historical_volatility(closes: pd.Series, bars_per_day: int = 96, days: int = 10) -> float | None:
    n = bars_per_day * days
    if len(closes) <= 1:
        return None
    n = min(n, len(closes)-1)
    n = max(n, 2)
    logret = np.log(closes).diff().dropna()
    vol = logret.tail(n).std() * math.sqrt(365 * bars_per_day)
    return float(vol)

# 7 VWAP 산출
# 거래량 가중 평균가
def vwap_from_bars(df: pd.DataFrame) -> float:
    tp = (df["h"] + df["l"] + df["c"]) / 3.0  # Typical Price
    return float((tp * df["v"]).sum() / (df["v"].sum() + 1e-12))

# 8 간단한 피벗 포인트 기반 S/R 산출
# 프랙탈 방식으로 지역 최소/최대 지점을 찾아 S/R 후보로 사용
# 너무 복잡한 알고리즘 대신 직관적인 핫스팟을 찾는 용도
def pivots_sr(df: pd.DataFrame, lookback: int = 12, top_k: int = 2) -> tuple[list[float], list[float]]:
    lows, highs = [], []
    for i in range(lookback, len(df)-lookback):
        window = df.iloc[i-lookback:i+lookback+1]
        if df["l"].iloc[i] == window["l"].min():
            lows.append(df["l"].iloc[i])
        if df["h"].iloc[i] == window["h"].max():
            highs.append(df["h"].iloc[i])
    supports = sorted(lows)[:top_k] if lows else [float(df["l"].tail(lookback).min())]
    resistances = sorted(highs)[:top_k] if highs else [float(df["h"].tail(lookback).max())]
    return supports, resistances


