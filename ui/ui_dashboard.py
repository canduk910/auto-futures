import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable

import altair as alt
import pandas as pd
import streamlit as st
from dotenv import dotenv_values, set_key
import time
import os

try:
    from streamlit_autorefresh import st_autorefresh as _autorefresh_component  # type: ignore
except ImportError:  # pragma: no cover
    _autorefresh_component = None

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))
PROJECT_ROOT = CURRENT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config_store import load_config, save_config, MANAGED_RUNTIME_KEYS  # noqa: E402

_DEF_FALLBACK = {
    key: meta.get("default") for key, meta in MANAGED_RUNTIME_KEYS.items()
}

def _resolve_setting(key: str, config_values: Dict[str, Any]) -> Any:
    value = config_values.get(key)
    if value is None:
        return _DEF_FALLBACK.get(key)
    return value

def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default

def _safe_dataframe(df, **kwargs):
    try:
        return st.dataframe(df, width="stretch", **kwargs)
    except TypeError:
        kwargs.pop("width", None)
        return st.dataframe(df, use_container_width=True, **kwargs)

def _safe_altair_chart(chart, **kwargs):
    try:
        return st.altair_chart(chart, width="stretch", **kwargs)
    except TypeError:
        kwargs.pop("width", None)
        return st.altair_chart(chart, use_container_width=True, **kwargs)

def _safe_container_altair(container, chart, **kwargs):
    try:
        return container.altair_chart(chart, width="stretch", **kwargs)
    except TypeError:
        kwargs.pop("width", None)
        return container.altair_chart(chart, use_container_width=True, **kwargs)

def _get_autorefresh_component() -> Optional[Callable[..., None]]:
    return _autorefresh_component

def _render_autorefresh(interval_seconds: int, label: str) -> None:
    if "auto_refresh_info" not in st.session_state:
        st.session_state["auto_refresh_info"] = None
    if interval_seconds <= 0:
        st.session_state.pop(AUTO_REFRESH_STATE_KEY, None)
        st.sidebar.caption("자동 새로고침 사용 안 함")
        return
    st.sidebar.caption(f"자동 새로고침: {label}")
    autorefresh_fn = _get_autorefresh_component()
    if autorefresh_fn is None:
        st.sidebar.warning("streamlit-autorefresh 모듈이 필요합니다.")
        st.sidebar.code("pip install streamlit-autorefresh")
        return
    autorefresh_fn(interval=interval_seconds * 1000, limit=None, key="auto_refresh_tick")


# 테이블 셀 색상 적용 헬퍼
def _style_trade_actions(df: 'pd.DataFrame') -> 'pd.io.formats.style.Styler':
    """Return a pandas Styler that colors BUY/Long red and SELL/Short blue for common action/direction columns.
    It detects common Korean column names used in the UI (동작, 사이드, 방향, 포지션, 진입유형).
    """
    try:
        styler = df.style
    except Exception:
        return df

    def color_val(v):
        try:
            if v is None:
                return ""
            s = str(v).strip().lower()
            if s in ("buy", "long", "long", "buy/long", "buy_long") or s.startswith("buy") or s == "long":
                return "color: #d64f3a"  # red-ish
            if s in ("sell", "short", "sell/short", "sell_short") or s.startswith("sell") or s == "short":
                return "color: #2563eb"  # blue
            # Korean labels
            if s in ("매수", "롱", "long" ):
                return "color: #d64f3a"
            if s in ("매도", "숏", "short"):
                return "color: #2563eb"
        except Exception:
            pass
        return ""

    # 후보 컬럼들
    # include '결정'/'decision' so AI decision column can be colored similarly
    candidate_cols = [c for c in df.columns if c in ("동작", "사이드", "방향", "포지션", "진입유형", "결정", "decision", "side", "action", "position")]
    if not candidate_cols:
        # try to find columns containing keywords
        candidate_cols = [c for c in df.columns if any(k in c.lower() for k in ("buy","sell","side","action","position","방향","동작","사이드","포지션"))]

    for col in candidate_cols:
        try:
            # Styler.map is preferred over applymap for element-wise CSS mapping
            styler = styler.map(lambda v: color_val(v), subset=[col])
        except Exception:
            # ignore styling failures for a column
            continue

    # 손익(PnL) 컬럼 색상화: 양수=빨강, 음수=파랑
    def color_pnl(v):
        try:
            if v is None:
                return ""
            # 이미 문자열일 수 있으므로 안전하게 float 변환
            fv = float(v)
            if fv > 0:
                return "color: #d64f3a"
            if fv < 0:
                return "color: #2563eb"
        except Exception:
            pass
        return ""

    pnl_candidates = [c for c in df.columns if c in ("손익", "realized_pnl_usdt", "손익(USDT)", "pnl", "profit")]
    for col in pnl_candidates:
        try:
            styler = styler.map(lambda v: color_pnl(v), subset=[col])
        except Exception:
            continue
    return styler

from status_store import read_status, read_ai_history, read_close_history, update_status  # noqa: E402

st.set_page_config(page_title="자동 암호화폐 트레이딩", layout="wide")

ENV_PATH = CURRENT_DIR.parent / ".env"
RUNNING_ON_CLOUD = bool(os.getenv("K_SERVICE"))
BOOL_KEYS = {
    "DRY_RUN",
    "WS_ENABLE",
    "WS_USER_ENABLE",
    "WS_PRICE_ENABLE",
    "WS_TRACE",
    "LOOP_ENABLE",
    "USE_QUOTE_VOLUME",
}
FLOAT_KEYS = {
    "MP_DELTA_PCT",
    "KLINE_RANGE_PCT",
    "VOL_MULT",
    "AI_CONF_THRESHOLD",
}
INT_KEYS = {
    "LOOP_INTERVAL_SEC",
    "LOOP_COOLDOWN_SEC",
    "LOOP_BACKOFF_MAX_SEC",
    "MP_WINDOW_SEC",
    "VOL_LOOKBACK",
}
EDITABLE_KEYS = [
    "ENV",
    "DRY_RUN",
    "LOOP_TRIGGER",
    "LOOP_INTERVAL_SEC",
    "LOOP_COOLDOWN_SEC",
    "MP_WINDOW_SEC",
    "MP_DELTA_PCT",
    "KLINE_RANGE_PCT",
    "VOL_LOOKBACK",
    "VOL_MULT",
    "AI_CONF_THRESHOLD",
    "USE_QUOTE_VOLUME",
]

ENV_FIELD_INFO: Dict[str, Dict[str, str]] = {
    "ENV": {
        "label": "실행 환경",
        "description": "예: production, paper 등 서비스를 구분하는 환경 값",
    },
    "DRY_RUN": {
        "label": "드라이런 모드",
        "description": "체결 없이 시뮬레이션만 수행하려면 활성화하세요",
    },
    "LOOP_TRIGGER": {
        "label": "루프 트리거",
        "description": "주기 실행 조건(event/kline/timer)",
    },
    "LOOP_INTERVAL_SEC": {
        "label": "루프 실행 간격(초)",
        "description": "루프를 반복 실행할 최소 간격",
    },
    "LOOP_COOLDOWN_SEC": {
        "label": "루프 쿨다운(초)",
        "description": "루프 종료 후 대기할 시간",
    },
    "LOOP_BACKOFF_MAX_SEC": {
        "label": "오류 백오프 최대(초)",
        "description": "오류 발생 시 재시도까지 기다릴 최대 시간",
    },
    "MP_WINDOW_SEC": {
        "label": "가격 평균 창(초)",
        "description": "MP 계산에 사용할 데이터 창 길이",
    },
    "AI_CONF_THRESHOLD": {
        "label": "AI 신뢰도 임계값",
        "description": "AI confidence가 이 값 미만이면 주문 실행을 보류합니다 (0.0-1.0)",
    },
    "MP_DELTA_PCT": {
        "label": "가격 변동 임계치(%)",
        "description": "진입을 결정할 때 사용하는 MP 변화율",
    },
    "KLINE_RANGE_PCT": {
        "label": "캔들 범위 임계치(%)",
        "description": "지정된 캔들 범위를 벗어나는지 판단하는 값",
    },
    "VOL_LOOKBACK": {
        "label": "거래량 조회 길이",
        "description": "평균 거래량 비교에 사용할 과거 캔들 수",
    },
    "VOL_MULT": {
        "label": "거래량 배수",
        "description": "평균 대비 얼마나 많은 거래량일 때 알림을 줄지",
    },
    "USE_QUOTE_VOLUME": {
        "label": "거래량 기준",
        "description": "기본 거래량 대신 QUOTE 기준 거래량을 사용할지",
    },
}

REFRESH_OPTIONS = {
    "자동 새로고침 없음": 0,
    "15초": 15,
    "1분": 60,
    "5분": 300,
}
TRIGGER_OPTIONS = ["event", "kline", "timer"]
AUTO_REFRESH_STATE_KEY = "_auto_refresh_state"

def _rerun_app() -> None:
    """Trigger a Streamlit rerun, compatible with new and old APIs."""
    rerun_fn = getattr(st, "rerun", None) or getattr(st, "experimental_rerun", None)
    if rerun_fn:
        rerun_fn()


def _format_ts(ts: Any) -> str:
    if ts is None:
        return "-"
    try:
        value = float(ts)
    except (TypeError, ValueError):
        return "-"
    if value > 1_000_000_000_000:  # ms → s
        value = value / 1000.0
    try:
        return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return "-"


def _format_time(ts: Any) -> str:
    formatted = _format_ts(ts)
    if formatted == "-":
        return "-"
    return formatted.split(" ")[-1]


def _format_scenario_value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                try:
                    rendered = json.dumps(item, ensure_ascii=False)
                except TypeError:
                    rendered = str(item)
            else:
                rendered = str(item)
            parts.append(f"{key}: {rendered}")
        return "\n".join(parts) if parts else "-"
    if isinstance(value, list):
        rendered_items = []
        for item in value:
            if isinstance(item, (dict, list)):
                try:
                    rendered_items.append(json.dumps(item, ensure_ascii=False))
                except TypeError:
                    rendered_items.append(str(item))
            else:
                rendered_items.append(str(item))
        return "\n".join(rendered_items) if rendered_items else "-"
    return str(value)


st.sidebar.markdown(
    """
    <style>
    @keyframes sidebarPulse {
        0% { opacity: 0.3; }
        50% { opacity: 1; }
        100% { opacity: 0.3; }
    }
    [data-testid="stSidebar"] .sidebar-live-title {
        display: flex;
        align-items: center;
        gap: 0.55rem;
        font-weight: 700;
        font-size: 1.4rem;
        margin-bottom: 0.25rem;
    }
    [data-testid="stSidebar"] .sidebar-live-title .dot {
        width: 11px;
        height: 11px;
        border-radius: 50%;
        background: #22c55e;
        box-shadow: 0 0 8px rgba(34, 197, 94, 0.6);
        animation: sidebarPulse 1.8s ease-in-out infinite;
    }
    </style>
    <div class="sidebar-live-title"><span class="dot"></span><span>자동 암호화폐 트레이딩</span></div>
    """,
    unsafe_allow_html=True,
)
st.sidebar.caption("제어판")
if st.sidebar.button("지금 새로고침"):
    _rerun_app()

refresh_options = list(REFRESH_OPTIONS.keys())
if "auto_refresh_select" not in st.session_state:
    st.session_state["auto_refresh_select"] = refresh_options[0]

selected_option = st.sidebar.selectbox(
    "자동 새로고침 간격",
    refresh_options,
    index=refresh_options.index(st.session_state["auto_refresh_select"]),
    key="auto_refresh_select",
)
interval_seconds = REFRESH_OPTIONS[selected_option]

refresh_notice = st.sidebar.container()

_render_autorefresh(interval_seconds, selected_option)

NAV_OPTIONS = ["모니터링", "AI 자문", "거래 내역", "청산 분석", "설정"]
if "nav_menu" not in st.session_state:
    st.session_state["nav_menu"] = NAV_OPTIONS[0]

st.sidebar.markdown(
    """
    <style>
    [data-testid=\"stSidebar\"] .stButton button {
        width: 100%;
        margin-bottom: 0.35rem;
        border-radius: 8px;
        font-weight: 600;
        display: inline-flex;
        justify-content: flex-start;
    }
    [data-testid=\"stSidebar\"] .stButton button:focus {
        outline: none;
        box-shadow: none;
    }
    [data-testid=\"stSidebar\"] [data-testid=\"baseButton-secondary\"] {
        background-color: #f5f7fb;
        color: #314057;
        border-color: #d7dce5;
    }
    [data-testid=\"stSidebar\"] [data-testid=\"baseButton-secondary\"]:hover {
        background-color: #e6eaf2;
        color: #111827;
        border-color: #c1c9d6;
    }
    [data-testid=\"stSidebar\"] [data-testid=\"baseButton-primary\"] {
        background-color: #2563eb;
        color: #ffffff;
        border-color: #1d4ed8;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

selected_tab = st.session_state["nav_menu"]

st.sidebar.markdown("### 대시보드")
nav_container = st.sidebar.container()
pending_selection = None
for nav_option in NAV_OPTIONS:
    is_selected = selected_tab == nav_option
    button_type = "primary" if is_selected else "secondary"
    if nav_container.button(
        nav_option,
        key=f"nav_btn_{nav_option.replace(' ', '_')}",
        use_container_width=True,
        type=button_type,
    ):
        pending_selection = nav_option

if pending_selection and pending_selection != selected_tab:
    st.session_state["nav_menu"] = pending_selection
    _rerun_app()

status_data = read_status()
last_ts = status_data.get("last_update_ts")
st.sidebar.write(f"최근 갱신: {_format_ts(last_ts)}")

service = status_data.get("service") or {}
trader = status_data.get("trader") or {}
events = status_data.get("events") or []

latest_input_block = status_data.get("latest_input") or {}
latest_input = latest_input_block.get("payload") or {}
latest_input_ts = latest_input_block.get("ts")

latest_advice_block = status_data.get("latest_advice") or {}
latest_advice_payload = latest_advice_block.get("payload") or {}
advice_symbol = None
advice_data: Dict[str, Any] = {}
latest_advice_ts = latest_advice_block.get("ts")
if isinstance(latest_advice_payload, dict):
    advice_symbol = latest_advice_payload.get("symbol")
    potential = latest_advice_payload.get("advice")
    if isinstance(potential, dict):
        advice_data = potential
    else:
        advice_data = latest_advice_payload

orders_block = status_data.get("orders") or {}
if isinstance(orders_block, dict):
    orders_list: List[Dict[str, Any]] = orders_block.get("items") or []
    orders_snapshot_ts = orders_block.get("ts")
elif isinstance(orders_block, list):
    orders_list = orders_block
    orders_snapshot_ts = None
else:
    orders_list = []
    orders_snapshot_ts = None

positions_block = status_data.get("positions") or {}
if isinstance(positions_block, dict):
    positions_list: List[Dict[str, Any]] = positions_block.get("items") or []
    positions_snapshot_ts = positions_block.get("ts")
elif isinstance(positions_block, list):
    positions_list = positions_block
    positions_snapshot_ts = None
else:
    positions_list = []
    positions_snapshot_ts = None

ai_history = read_ai_history(limit=120)

if selected_tab == "모니터링":
    st.subheader("서비스 상태")
    col_a, col_b, col_c = st.columns(3)
    col_a.metric("트리거", service.get("trigger", "-"))
    col_b.metric("최근 실행", _format_time(latest_advice_ts))
    col_c.metric("대기열", service.get("last_qsize", "-"))
    col_a.metric("트레이더 상태", trader.get("state", "-"))
    col_b.metric("최신 결정", trader.get("last_decision", "-"))
    col_c.metric("신뢰도", trader.get("last_confidence", "-"))

    # 재로딩 관련 정보
    reload_ts = service.get("last_reload_applied_ts")
    reload_res = service.get("last_reload_result")
    if reload_ts:
        col_a.caption(f"마지막 재로딩 시각: {_format_ts(reload_ts)}")
    else:
        col_a.caption("마지막 재로딩 시각: -")
    if reload_res:
        col_b.caption(f"마지막 재로딩 결과: {reload_res}")
    else:
        col_b.caption("마지막 재로딩 결과: -")

    st.divider()
    st.subheader("최근 이벤트")
    if events:
        trimmed = events[-5:]
        df_events = pd.DataFrame(trimmed[::-1])
        if "ts" in df_events.columns:
            df_events["ts"] = df_events["ts"].apply(_format_ts)
        _safe_dataframe(df_events, hide_index=True)
    else:
        st.info("이벤트 기록이 없습니다.")

elif selected_tab == "AI 자문":
    st.subheader("OpenAI 최신 응답")
    bars = latest_input.get("recent_bars_15m") or []

    scenario_rows: List[Dict[str, str]] = []
    if advice_data:
        scenarios = advice_data.get("scenarios")
        if isinstance(scenarios, dict) and scenarios:
            label_map = {"bull": "강세", "base": "중립", "bear": "약세"}
            for key, label in label_map.items():
                if key in scenarios:
                    scenario_rows.append({
                        "시나리오": label,
                        "내용": _format_scenario_value(scenarios.get(key)),
                    })
            for key, value in scenarios.items():
                if key not in label_map:
                    scenario_rows.append({
                        "시나리오": key,
                        "내용": _format_scenario_value(value),
                    })

    metrics_col, chart_col = st.columns([1.6, 1], gap="large")
    chart_container = chart_col.container()

    with metrics_col:
        if advice_data:
            metric_cols = metrics_col.columns(2)
            metric_cols[0].metric("결정", advice_data.get("decision", "-"))
            metric_cols[1].metric("신뢰도", advice_data.get("confidence", "-"))
            metric_cols = metrics_col.columns(2)
            metric_cols[0].metric("타임프레임", advice_data.get("timeframe", "-"))
            metric_cols[1].metric("심볼", advice_symbol or latest_input.get("symbol", "-"))
            metrics_col.caption(f"응답 수신 시각: {_format_ts(latest_advice_block.get('ts'))}")
            rationale = advice_data.get("rationale")
            if rationale:
                metrics_col.markdown(f"**결정 근거**\n\n{rationale}")
        else:
            metrics_col.info("OpenAI 응답 기록이 없습니다.")

    if advice_data and scenario_rows:
        st.markdown("#### 시나리오 요약")
        scenario_df = pd.DataFrame(scenario_rows)
        scenario_df["내용"] = scenario_df["내용"].astype(str)
        _safe_dataframe(
             scenario_df,
             hide_index=True,
             column_config={
                 "시나리오": st.column_config.TextColumn("시나리오", width="small", max_chars=4),
                 "내용": st.column_config.TextColumn("내용", width="large"),
             },
        )

    with chart_container:
        chart_container.markdown("#### 시장 차트 (OpenAI 입력 기반)")
        if bars:
            bars_df = pd.DataFrame(bars)
            if not bars_df.empty:
                bars_df = bars_df.rename(columns={
                    "t": "time",
                    "o": "open",
                    "h": "high",
                    "l": "low",
                    "c": "close",
                    "v": "volume",
                })
                try:
                    bars_df["time"] = pd.to_datetime(bars_df["time"])
                    bars_df = bars_df.sort_values("time")
                    low_series = bars_df["low"].dropna()
                    high_series = bars_df["high"].dropna()
                    if low_series.empty or high_series.empty:
                        raise ValueError("가격 데이터가 부족합니다.")
                    min_low = float(low_series.min()) * 0.9
                    max_high = float(high_series.max()) * 1.1
                    if min_low == max_high:
                        pad = max(min_low * 0.01, 1e-6)
                        price_domain = [min_low - pad, max_high + pad]
                    else:
                        price_domain = [min_low, max_high]
                    color_scale = alt.condition(
                        "datum.close >= datum.open",
                        alt.value("#d64f3a"),
                        alt.value("#2e8b57"),
                    )
                    candle_rules = alt.Chart(bars_df).mark_rule(color="#a0a0a0").encode(
                        x=alt.X("time:T", title="시간"),
                        y=alt.Y("low:Q", title="가격", scale=alt.Scale(domain=price_domain)),
                        y2="high:Q",
                    )
                    candle_bars = alt.Chart(bars_df).mark_bar().encode(
                        x="time:T",
                        y=alt.Y("open:Q", scale=alt.Scale(domain=price_domain)),
                        y2="close:Q",
                        color=color_scale,
                    )
                    candle = (candle_rules + candle_bars).properties(height=220)
                    volume = alt.Chart(bars_df).mark_bar(opacity=0.45).encode(
                        x="time:T",
                        y=alt.Y("volume:Q", title="거래량"),
                        color=color_scale,
                    ).properties(height=60)
                    combo_chart = alt.vconcat(candle, volume).resolve_scale(x="shared").properties(spacing=8)
                    _safe_container_altair(chart_container, combo_chart)
                except Exception:
                    chart_container.line_chart(bars_df.set_index("time")["close"], height=280)
            else:
                chart_container.info("차트에 사용할 데이터가 부족합니다.")
        else:
            chart_container.info("차트에 표시할 입력 데이터가 아직 없습니다.")

    if advice_data:
        with st.expander("전체 응답 JSON", expanded=False):
            st.json(advice_data)

    st.divider()
    st.subheader("OpenAI 의사결정 기록")
    if ai_history:
        history_df = pd.DataFrame(ai_history)
        if not history_df.empty:
            history_df = history_df.sort_values(by="ts", ascending=False)
            history_df["시간"] = history_df["ts"].apply(_format_ts)
            if "position" in history_df.columns:
                history_df["진입유형"] = history_df["position"].apply(
                    lambda v: v.get("entry_type") if isinstance(v, dict) else None
                )
                history_df["진입가"] = history_df["position"].apply(
                    lambda v: v.get("entry_price") if isinstance(v, dict) else None
                )
                history_df["계약수량"] = history_df["position"].apply(
                    lambda v: v.get("contracts") if isinstance(v, dict) else None
                )
                history_df["손절가"] = history_df["position"].apply(
                    lambda v: v.get("stop_loss_price") if isinstance(v, dict) else None
                )
            history_df["근거"] = history_df["rationale"].fillna("").apply(
                lambda v: v if len(v) <= 120 else v[:117] + "..."
            )
            display_df = history_df.rename(columns={
                "symbol": "심볼",
                "decision": "결정",
                "confidence": "신뢰도",
                "timeframe": "타임프레임",
            })
            cols_to_show = [c for c in [
                "시간",
                "심볼",
                "결정",
                "신뢰도",
                "타임프레임",
                "진입유형",
                "진입가",
                "계약수량",
                "손절가",
                "근거",
            ] if c in display_df.columns]
            try:
                styled = _style_trade_actions(display_df[cols_to_show])
                _safe_dataframe(styled, hide_index=True)
            except Exception:
                _safe_dataframe(display_df[cols_to_show], hide_index=True)
        else:
            st.info("기록이 비어 있습니다.")
    else:
        st.info("의사결정 기록이 아직 없습니다.")

elif selected_tab == "거래 내역":
    st.subheader("주문 실행 내역")
    if orders_list:
        orders_df = pd.DataFrame(orders_list)
        if not orders_df.empty:
            orders_df = orders_df.sort_values(by="ts", ascending=False, na_position="last")
            if "ts" in orders_df.columns:
                orders_df["시간"] = orders_df["ts"].apply(_format_ts)
            else:
                orders_df["시간"] = "-"
            if "update_time" in orders_df.columns:
                orders_df["체결시각"] = orders_df["update_time"].apply(_format_ts)
            display_df = orders_df.rename(columns={
                "action": "동작",
                "side": "사이드",
                "position_side": "포지션",
                "order_type": "주문유형",
                "quantity": "수량",
                "price": "가격",
                "status": "상태",
                "executed_qty": "체결수량",
                "avg_price": "평균가",
                "reduce_only": "감축전용",
                "order_id": "주문ID",
                "client_order_id": "클라이언트ID",
                "dry_run": "모의주문",
            })
            cols_to_show = [c for c in [
                "시간",
                "동작",
                "사이드",
                "포지션",
                "주문유형",
                "수량",
                "가격",
                "상태",
                "체결수량",
                "평균가",
                "감축전용",
                "주문ID",
                "체결시각",
                "모의주문",
            ] if c in display_df.columns]
            try:
                styled = _style_trade_actions(display_df[cols_to_show])
                _safe_dataframe(styled, hide_index=True)
            except Exception:
                _safe_dataframe(display_df[cols_to_show], hide_index=True)
            if orders_snapshot_ts:
                st.caption(f"내역 업데이트 기준 시각: {_format_ts(orders_snapshot_ts)}")
        else:
            st.info("표시할 주문 내역이 없습니다.")
    else:
        st.info("주문 실행 내역이 없습니다.")

    st.divider()
    st.subheader("현재 포지션")
    if positions_list:
        pos_df = pd.DataFrame(positions_list)
        if not pos_df.empty:
            display_df = pos_df.rename(columns={
                "symbol": "심볼",
                "side": "방향",
                "qty": "수량",
                "entry_price": "진입가",
                "unrealized_pnl_usdt": "평가손익(USDT)",
                "liquidation_price": "청산가",
                "break_even_price": "손익분기점",
                "margin_mode": "마진모드",
                "leverage": "레버리지",
            })
            cols_to_show = [c for c in [
                "심볼",
                "방향",
                "수량",
                "진입가",
                "손익분기점",
                "평가손익(USDT)",
                "청산가",
                "마진모드",
                "레버리지",
            ] if c in display_df.columns]
            try:
                styled = _style_trade_actions(display_df[cols_to_show])
                _safe_dataframe(styled, hide_index=True)
            except Exception:
                _safe_dataframe(display_df[cols_to_show], hide_index=True)
            if positions_snapshot_ts:
                st.caption(f"포지션 스냅샷 시각: {_format_ts(positions_snapshot_ts)}")
        else:
            st.info("포지션 데이터가 비어 있습니다.")
    else:
        st.info("보유 중인 포지션이 없습니다.")

elif selected_tab == "청산 분석":
    st.subheader("포지션 청산 분석")
    close_history = read_close_history(limit=400)
    if close_history:
        close_df = pd.DataFrame(close_history)
        if close_df.empty:
            st.info("청산 내역이 아직 없습니다.")
        else:
            numeric_columns = {
                "realized_pnl_usdt": "USDT 손익",
                "return_pct": "수익률",
                "qty": "수량",
                "entry_price": "진입가",
                "exit_price": "청산가",
            }
            for col in numeric_columns:
                if col not in close_df.columns:
                    close_df[col] = pd.NA
                close_df[col] = pd.to_numeric(close_df[col], errors="coerce")

            sort_key = "closed_ts" if "closed_ts" in close_df.columns else ("ts" if "ts" in close_df.columns else None)
            if sort_key:
                close_df = close_df.sort_values(by=sort_key, ascending=True, na_position="last")

            pnl_total = float(close_df["realized_pnl_usdt"].sum() or 0)
            qty_total = float(close_df["qty"].sum() or 0)
            if qty_total != 0:
                close_df["진입가"] = close_df["entry_price"]
                close_df["청산가"] = close_df["exit_price"]
                close_df["손익"] = close_df["realized_pnl_usdt"]
                close_df["수익률"] = close_df["return_pct"]
                display_df = close_df.rename(columns={
                    "symbol": "심볼",
                    "side": "방향",
                    "진입가": "진입가",
                    "청산가": "청산가",
                    "손익": "손익",
                    "수익률": "수익률",
                })

                # 차트: 시간 기준 누적 손익(선 그래프) 및 손익 분포(히스토그램)
                try:
                    # 시간 컬럼 결정 (closed_ts 우선, 없으면 ts)
                    time_col = "closed_ts" if "closed_ts" in close_df.columns else ("ts" if "ts" in close_df.columns else None)
                    chart_cols = st.columns([2, 1])
                    if time_col is not None:
                        tmp = close_df[[time_col, "realized_pnl_usdt"]].copy()
                        # try seconds -> if ms, detect and convert
                        try:
                            tmp["time"] = pd.to_datetime(tmp[time_col], unit='s', errors='coerce')
                            if tmp["time"].isna().all():
                                tmp["time"] = pd.to_datetime(tmp[time_col], unit='ms', errors='coerce')
                        except Exception:
                            tmp["time"] = pd.to_datetime(tmp[time_col], errors='coerce')
                        tmp = tmp.dropna(subset=["time"])
                        tmp = tmp.sort_values(by="time")
                        tmp["cum_pnl"] = tmp["realized_pnl_usdt"].cumsum()

                        with chart_cols[0]:
                            st.markdown("#### 누적 청산 손익 (시간)")
                            try:
                                # line for cumulative, points colored by recent realized pnl sign
                                line = alt.Chart(tmp).mark_line(color="#6b7280").encode(
                                    x=alt.X("time:T", title="시간"),
                                    y=alt.Y("cum_pnl:Q", title="누적 손익(USDT)"),
                                )
                                points = alt.Chart(tmp).mark_circle(size=40).encode(
                                    x=alt.X("time:T"),
                                    y=alt.Y("cum_pnl:Q"),
                                    color=alt.condition(alt.datum.realized_pnl_usdt > 0, alt.value("#d64f3a"), alt.value("#2563eb")),
                                    tooltip=[alt.Tooltip("time:T", title="시간"), alt.Tooltip("realized_pnl_usdt:Q", title="손익(USDT)")]
                                )
                                chart = (line + points).properties(height=260)
                                _safe_altair_chart(chart)
                            except Exception:
                                st.line_chart(tmp.set_index("time")["cum_pnl"], height=260)

                        with chart_cols[1]:
                            st.markdown("#### 청산 손익 분포")
                            try:
                                vals = tmp["realized_pnl_usdt"].dropna()
                                if not vals.empty:
                                    # pandas로 binning 후 각 bin의 중간값에 따라 색 지정
                                    bins = pd.cut(vals, bins=40)
                                    counts = bins.value_counts().sort_index()
                                    intervals = counts.index
                                    lefts = [iv.left for iv in intervals]
                                    rights = [iv.right for iv in intervals]
                                    mids = [(l + r) / 2.0 for l, r in zip(lefts, rights)]
                                    hist_df = pd.DataFrame({
                                        "left": lefts,
                                        "right": rights,
                                        "bin_mid": mids,
                                        "count": counts.values,
                                    })
                                    chart = alt.Chart(hist_df).mark_bar(opacity=0.9).encode(
                                        x=alt.X("bin_mid:Q", title="손익(USDT)"),
                                        y=alt.Y("count:Q", title="건수"),
                                        color=alt.condition(alt.datum.bin_mid > 0, alt.value("#d64f3a"), alt.value("#2563eb")),
                                        tooltip=[alt.Tooltip("left:Q", title="left"), alt.Tooltip("right:Q", title="right"), alt.Tooltip("count:Q", title="건수")],
                                    ).properties(height=260)
                                    _safe_altair_chart(chart)
                                else:
                                    st.info("차트를 생성할 데이터가 없습니다.")
                            except Exception:
                                try:
                                    counts = pd.cut(tmp["realized_pnl_usdt"].dropna(), bins=30).value_counts().sort_index()
                                    st.bar_chart(counts, height=260)
                                except Exception:
                                    st.write("차트를 생성할 수 없습니다.")
                    else:
                        # 시간 정보가 없으면 히스토그램만 표시
                        with chart_cols[0]:
                            st.markdown("#### 청산 손익 분포")
                            try:
                                vals = close_df["realized_pnl_usdt"].dropna()
                                if not vals.empty:
                                    bins = pd.cut(vals, bins=40)
                                    counts = bins.value_counts().sort_index()
                                    intervals = counts.index
                                    lefts = [iv.left for iv in intervals]
                                    rights = [iv.right for iv in intervals]
                                    mids = [(l + r) / 2.0 for l, r in zip(lefts, rights)]
                                    hist_df = pd.DataFrame({
                                        "left": lefts,
                                        "right": rights,
                                        "bin_mid": mids,
                                        "count": counts.values,
                                    })
                                    chart = alt.Chart(hist_df).mark_bar(opacity=0.9).encode(
                                        x=alt.X("bin_mid:Q", title="손익(USDT)"),
                                        y=alt.Y("count:Q", title="건수"),
                                        color=alt.condition(alt.datum.bin_mid > 0, alt.value("#d64f3a"), alt.value("#2563eb")),
                                        tooltip=[alt.Tooltip("left:Q", title="left"), alt.Tooltip("right:Q", title="right"), alt.Tooltip("count:Q", title="건수")],
                                    ).properties(height=260)
                                    _safe_altair_chart(chart)
                                else:
                                    st.info("차트를 생성할 데이터가 없습니다.")
                            except Exception:
                                st.write("차트를 생성할 수 없습니다.")
                except Exception:
                    st.warning("청산 차트 표시 중 오류가 발생했습니다.")

                cols_to_show = [c for c in [
                    "심볼",
                    "방향",
                    "진입가",
                    "청산가",
                    "손익",
                    "수익률",
                ] if c in display_df.columns]
                try:
                    styled = _style_trade_actions(display_df[cols_to_show])
                    _safe_dataframe(styled, hide_index=True)
                except Exception:
                    _safe_dataframe(display_df[cols_to_show], hide_index=True)
            else:
                st.info("청산 내역이 비어 있습니다.")
    else:
        st.info("청산 분석 데이터를 불러올 수 없습니다.")

else:  # 설정 탭
    st.subheader("환경 설정")
    config = load_config()
    settings = {k: config.values.get(k) for k in EDITABLE_KEYS if k in config.values}
    for key in EDITABLE_KEYS:
        meta = ENV_FIELD_INFO.get(key, {"label": key, "description": key})
        st.caption(meta["description"])
        if key == "LOOP_TRIGGER":
            default_index = TRIGGER_OPTIONS.index(settings.get(key, TRIGGER_OPTIONS[0])) if settings.get(key) in TRIGGER_OPTIONS else 0
            new_val = st.selectbox(meta["label"], TRIGGER_OPTIONS, index=default_index)
        elif key in BOOL_KEYS:
            new_val = st.checkbox(meta["label"], value=str(settings.get(key, "false")).lower() in ("1", "true", "yes"))
        elif key in FLOAT_KEYS:
            new_val = st.number_input(meta["label"], value=float(settings.get(key, 0)))
        elif key in INT_KEYS:
            new_val = st.number_input(meta["label"], value=int(settings.get(key, 0)), format="%d")
        else:
            new_val = st.text_input(meta["label"], value=settings.get(key, ""))
        if st.button(f"{meta['label']} 저장", key=f"save_{key}"):
            save_config({key: str(new_val)})
            st.success("저장되었습니다.")
            _rerun_app()

    # 설정 탭에서 서비스에 즉시 재로딩을 요청할 수 있는 버튼
    if st.button("서비스에 설정 재로딩 요청 (지금)"):
        try:
            ts = time.time()
            update_status("service", {"reload_requested_ts": ts})
            st.success("서비스에 재로딩 요청을 보냈습니다.")
            _rerun_app()
        except Exception as e:
            st.error(f"재로딩 요청 오류: {e}")

st.sidebar.markdown(
    """
    <style>
    [data-testid="stSidebar"] {
        animation: none;
    }
    </style>
    """,
    unsafe_allow_html=True,
)
