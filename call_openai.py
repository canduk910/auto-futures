# call_openai.py
# -----------------------------------------------------------
# 목적:
#  - INPUT JSON을 OpenAI에 전달
#  - 모델이 "오직 JSON"으로만 답하도록 response_format 지정
#  - 결과(JSON 문자열)를 dict로 파싱
# -----------------------------------------------------------

import os, json, logging
from openai import OpenAI
from input_builder import build_input_json

# .env 파일에서 환경변수 로드
from dotenv import load_dotenv
load_dotenv()

log = logging.getLogger("OPENAI")


def _stringify(value, limit: int = 100) -> str:
    """Compact representation for log output."""
    if value is None:
        return "None"
    if isinstance(value, (int, float, bool)):
        return str(value)
    text = str(value).replace("\n", " ")
    return text if len(text) <= limit else f"{text[:limit-3]}..."


def _summarize_mapping(title: str, payload: dict) -> str:
    if not isinstance(payload, dict):
        return f"{title}\n  - value: {_stringify(payload)}"

    lines = [title]
    for key in sorted(payload.keys()):
        value = payload[key]
        if isinstance(value, dict):
            lines.append(f"  - {key}: dict[{len(value)}]")
        elif isinstance(value, (list, tuple, set)):
            lines.append(f"  - {key}: {type(value).__name__}[{len(value)}]")
        else:
            lines.append(f"  - {key}: {_stringify(value)}")
    return "\n".join(lines)


def _summarize_advice(advice: dict) -> str:
    if not isinstance(advice, dict):
        return f"[OPENAI RESPONSE]\n  - raw: {_stringify(advice)}"

    position = advice.get("position") or {}
    entry = position.get("entry") or {}
    size = position.get("size") or {}
    stop_loss = position.get("stop_loss") or {}
    take_profits = position.get("take_profits") or []
    risk = advice.get("risk") or {}
    invalidations = advice.get("invalidations") or []

    lines = ["[OPENAI RESPONSE]"]
    lines.append(f"  - decision: {_stringify(advice.get('decision'))}")
    lines.append(f"  - timeframe: {_stringify(advice.get('timeframe'))}")
    lines.append(f"  - confidence: {_stringify(advice.get('confidence'))}")
    lines.append(f"  - rationale: {_stringify(advice.get('rationale'), limit=200)}")
    lines.append(
        f"  - entry: {_stringify(entry.get('order_type'))} @ {_stringify(entry.get('price'))}"
    )
    lines.append(
        f"  - size: {_stringify(size.get('contracts'))} contracts, {_stringify(size.get('quote_value_usdt'))} USDT"
    )
    lines.append(
        f"  - stop_loss: {_stringify(stop_loss.get('price'))} ({_stringify(stop_loss.get('trigger_on'))})"
    )
    lines.append(f"  - take_profits: {len(take_profits)} targets")
    lines.append(f"  - max_loss_usdt: {_stringify(risk.get('max_loss_usdt'))}")
    lines.append(f"  - notes: {_stringify(advice.get('notes'), limit=150)}")
    lines.append(f"  - invalidations: {len(invalidations)} entries")
    return "\n".join(lines)

# ====== 시스템 프롬프트(정책/원칙) ======
SYSTEM_PROMPT = """역할: 당신은 암호화폐 선물트레이더이다.
원칙:
- 현재 바이낸스 선물 시장 상황과 사용자의 계정 상태를 고려하여 매매 제안을 한다.
- 선물거래임을 고려하여 숏 거래도 적극적으로 제안한다.
- 마크프라이스 기준으로 리스크를 산정한다.
- 펀딩비, 수수료, 레버리지, 청산가를 고려한다.
- 불확실하면 flat을 권고한다.
- 손실 우선 관리: 1회 손실율과 1일 손실금액의 한도를 넘기는 제안은 금지한다.
- 시장가 진입은 강한 돌파/변동성 확장 상황에서만 제안한다.
출력:
- 오직 JSON으로만 응답한다. 불필요한 텍스트 금지.
- 아래 "출력 JSON 스키마"를 엄격 준수한다.
품질:
- 근거(rationale)와 무효화 조건(invalidations)을 반드시 포함한다.
- 정밀도는 입력의 tick_size/step_size를 존중하되, 최종 스냅은 실행 엔진이 한다는 메모를 남긴다.
- 계산이 추정이면 estimated임을 분명히 명시한다.
면책:
- 최종 판단과 책임은 사용자에게 있음을 notes에 기재한다.
출력 JSON 스키마:
{
  "decision": "long|short|flat",
  "timeframe": "scalp|intraday|swing",
  "rationale": "string",
  "position": {
    "entry": {"order_type":"limit|market","price":0,"scale_in":[{"price":0,"qty_pct":0}], "invalid_after_minutes":0},
    "size": {"side":"buy|sell","contracts":0,"quote_value_usdt":0,"leverage":0,"margin_usdt":0,"risk_pct_of_equity":0},
    "stop_loss": {"trigger_on":"mark|last","price":0,"reason":"string"},
    "take_profits": [{"price":0,"size_pct":0}],
    "trailing_stop": {"activate_price":0,"callback_pct":0},
    "expected_fees": {"entry_usdt":0,"exit_usdt":0,"funding_8h_usdt":0},
    "estimated_liquidation_price": 0,
    "precision_note": "string"
  },
  "risk": {"r_multiple":0,"prob_upside_pct":0,"max_loss_usdt":0,"max_loss_pct":0,"kelly_fraction_pct":0,"daily_loss_limit_breached":false},
  "scenarios": {"bull":"string","base":"string","bear":"string"},
  "invalidations": ["string"],
  "confidence": 0,
  "next_check_after_min": 15,
  "compliance": {"reason_codes": ["RISK_OK"]},
  "notes": "string"
}"""

# 1 유저 프롬프트 빌더
# INPUT JSON을 인라인으로 포함
# 2 OpenAI 호출 함수
# 모델, 온도 설정 가능
# JSON 응답 강제
# 파싱 후 dict 반환
# 단독 실행 시: INPUT 생성 → OpenAI 호출 → 결과 출력
def build_user_prompt(input_json: dict) -> str:
    """유저 프롬프트: 실데이터(JSON)를 그대로 인라인 + 주의사항 명시."""
    return f"""아래는 바이낸스 선물 시세·유동성·기술지표·계정·제약 정보다.
이를 바탕으로 "출력 JSON 스키마"에 맞춰 제안하라.

[입력 JSON]
{json.dumps(input_json, ensure_ascii=False)}

주의:
- 제안은 한 번에 최대 1개 포지션.
- 손절가는 mark 기준.
- funding 직전 10분은 신규 진입 금지(입력 constraints 참고).
- daily_loss_limit_pct를 넘길 수 있는 제안은 금지.
- 출력은 반드시 JSON 한 덩어리만.
"""

def call_openai_for_advice(
    input_json: dict,
    model: str = "gpt-5-nano-2025-08-07",  # 속도/비용/품질 균형
    temperature: float = 1
) -> dict:
    log.info(_summarize_mapping("[OPENAI REQUEST] Input summary", input_json))
    log.info(
        "OpenAI request payload\n%s",
        json.dumps(input_json, ensure_ascii=False, indent=2)
    )

    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role":"system","content": SYSTEM_PROMPT},
            {"role":"user","content": build_user_prompt(input_json)}
        ],
        response_format={"type":"json_object"},  # JSON만 받도록 강제
        temperature=temperature,
    )
    advice_raw = resp.choices[0].message.content
    advice = json.loads(advice_raw)
    log.info(_summarize_advice(advice))
    log.info(
        "OpenAI response payload\n%s",
        json.dumps(advice, ensure_ascii=False, indent=2)
    )
    return advice

# 단독 실행 시: INPUT 생성 → OpenAI 호출 → 결과 출력
if __name__ == "__main__":
    if not logging.getLogger().handlers:
        level_name = os.getenv("LOG_LEVEL", "INFO").upper()
        level = getattr(logging, level_name, logging.INFO)
        logging.basicConfig(level=level)

    input_json = build_input_json(symbol="ETHUSDT", env="paper")
    call_openai_for_advice(input_json)
