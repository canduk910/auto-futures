# common_utils.py
# ---------------------------------------------
# 프로젝트 전반에서 쓰이는 공통 유틸 함수 모음
#  - 시간 포맷(UTC ISO8601)
#  - 안전한 float 변환
#  - 선택적 반올림(값이 None이면 None 유지)
#  - (향후 확장) 가격/수량 스냅 도우미 등
# ---------------------------------------------

from datetime import datetime, timezone
from dateutil import tz
from typing import Optional, Callable, Dict, Any

# ==============
# [도우미 함수] - 시간변환, 안전한 캐스팅 등
# ==============

# 1 현재 UTC 시간 ISO 포맷 반환
# 예: '2023-10-05T12:34:56+00:00'
# UTC 현재 시각을 ISO8601로 반환 (마이크로초 제거). 로깅/재현성.
def utc_now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

# 2 밀리초 타임스탬프를 ISO 포맷으로 변환
# 예: 1696503296000 -> '2023-10-05T12:34:56+00:00'
# 밀리초 epoch → ISO8601(UTC) 문자열로 변환.
def to_iso(ms):
    return datetime.fromtimestamp(ms/1000, tz=timezone.utc).replace(microsecond=0).isoformat()

# 3 안전한 float 변환
# 예 : '123.45' -> 123.45
#    어떤 값이 와도 안전하게 float로 변환.
#    - None, '' , 'NaN', 'null', 'None' → default
#    - 숫자형은 그대로 캐스팅
#    - 문자열은 strip 후 캐스팅
def safe_float(x, default=None) -> Optional[float]:
    try:
        if x is None:
            return default
        if isinstance(x, (int, float)):
            return float(x)
        s = str(x).strip()
        if s == "" or s.lower() in ("nan", "none", "null"):
            return default
        return float(s)
    except Exception:
        return default

def safe_int(x) -> Optional[int]:
        try:
            if x is None or x == "":
                return None
            return int(x)
        except Exception:
            return None

# 4 바이낸스 필터 찾기
# 바이낸스 심볼 정보(exchangeInfo) 내 필터리스트(filters)에서 특정 필터를 찾아 반환.
def find_filter(filters, name):
    for f in filters:
        if f.get("filterType") == name:
            return f
    return {}

# 5 타입방어 반올림 함수
# 예: round_or_none(123.4567, 2) -> 123.46
#     round_or_none(None, 2) -> None
def round_or_none(x, nd=2):
    return None if x is None else round(float(x), nd)

# 6 호가 가격 단위 설정
# 바이낸스의 tickSize에 맞게 가격을 조정합니다.
# 예: snap_price(123.4567, 0.01) -> 123.46
def snap_price(price: float, tick_size:float) -> float:
    if price is None or tick_size in (None, 0, "0", "0.0", "", "None"):
        return price
    try:
        step = float(tick_size)
    except Exception:
        return price
    decimals = len(str(step).split(".")[-1]) if "." in str(step) else 0
    return round(round(price / step) * step, decimals)

# 7 호가 수량 단위 설정
# 바이낸스의 stepSize에 맞게 수량을 조정합니다.
# 예: snap_qty(1.2345, 0.01) -> 1.23
def snap_qty(qty: float, step_size: float) -> float:
    if qty is None or step_size in (None, 0, "0", "0.0", "", "None"):
        return qty
    try:
        step = float(step_size)
    except Exception:
        return qty
    decimals = len(str(step).split(".")[-1]) if "." in str(step) else 0
    return round(round(qty / step) * step, decimals)