"""시그널 기반 투자 대상 발굴 — 2차적 추론 엔진.

기존 '기업 모멘텀 분석'(시그널 누적 종목을 그대로 분석)을 대체한다.
단순히 시그널이 많은 종목을 재서술하는 대신, 가장 강력한 시그널들로부터
논리적 상상(2차·3차 파급)을 통해 시장이 아직 반영하지 못한 투자 대상을 발굴한다.

설계 원칙 (사용자 지시 — 실제 투자 유용성):
- 1차적 수혜주(엔비디아 등 명백한 이름)가 아니라, 그 이면의 대체 불가능한
  물리적 병목·구조적 희소성을 짚는다. 예: "AI 인프라 붐" → (X) 반도체 테마 전반,
  (O) 전력·리드타임 긴 중전기기처럼 좁고 대체 불가능한 병목.
- 매수 발굴뿐 아니라, 현재 프리미엄이 과도기적 산물이라 정상 회귀할
  '회피/청산' 대상도 함께 발굴 (딥시크 CEO의 반도체 이익률 회귀 논리).
- 미국·한국·일본 상장 종목 모두 대상.
- 모든 논지는 수집된 실제 시그널에 근거 (환각 방지) + 반증 조건 명시.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

import anthropic

from pipeline.db import get_connection

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"
SIGNAL_SCORE_FLOOR = 70
MAX_SIGNALS = 25
MAX_OUTPUT_TOKENS = 8000


def _gather_signals(conn, window_days: int) -> tuple[list[dict], dict | None]:
    since = (datetime.now() - timedelta(days=window_days)).isoformat()
    rows = conn.execute(
        """SELECT s.score, s.category, s.score_reasoning, s.title_ko,
                  r.title, r.source, r.published_at, s.raw_item_id
           FROM scored_items s JOIN raw_items r ON s.raw_item_id = r.id
           WHERE r.published_at >= ? AND s.score >= ?
           ORDER BY s.score DESC LIMIT ?""",
        (since, SIGNAL_SCORE_FLOOR, MAX_SIGNALS),
    ).fetchall()
    signals = [dict(r) for r in rows]

    digest = conn.execute(
        "SELECT digest_date, headline, summary_md FROM digests ORDER BY digest_date DESC LIMIT 1"
    ).fetchone()
    return signals, (dict(digest) if digest else None)


SCOUT_SYSTEM = """당신은 2차적(second-order) 사고에 능한 투자 발굴 분석가입니다.
남들이 다 아는 1차 수혜주가 아니라, 시그널의 논리적 파급을 끝까지 따라가 시장이
아직 값을 매기지 못한 대상을 찾습니다.

발굴 원칙:
1. 대체 불가능한 물리적 병목·구조적 희소성에 집중. "AI 인프라 붐" 같은 넓은 테마가
   아니라, 그 안에서 공급이 제한적이고 리드타임이 길며 대체 곤란한 지점(예: 전력,
   변압기·중전기기, 특정 소재, 냉각, 특수 장비)을 짚는다.
2. 2차·3차 추론: "A가 사실이면 → B가 병목이 되고 → 그 수혜는 C인데 시장은 아직 A만
   보고 있다"는 연결고리를 명시적으로 쓴다.
3. 회피/청산도 발굴: 현재의 높은 이익률·밸류에이션이 과도기적 산물이라 정상 회귀할
   대상(경쟁 격화, 병목 해소, 대체 등장)을 찾는다.
4. 미국·한국·일본 상장 종목 모두 대상. 종목명과 티커(미국=티커, 한국=한글정식명+6자리코드,
   일본=회사명+4자리코드)를 반드시 명시.
5. 모든 논지는 제공된 시그널에 근거해야 한다. 근거 시그널을 driving_signals에 인용.
   시그널에서 논리적으로 도출되지 않는 상상은 금지.
6. 각 논지에 반증 조건(falsifier)을 명시 — 이 논리가 틀렸음을 무엇으로 알 수 있는가.

**병목 깊이(depth_layer) — 매수 발굴에 필수. 대체 불가능성 기준 3개 층으로 분류:**
- 1층 (최심부): 다년 리드타임 + 과점/독점 + 물리적·규제적 해자. 신규 경쟁 진입이 사실상
  불가능 (예: 초고압 변압기 과점, EUV 노광, 특정 규제 자산). 사이클이 길어질수록 백로그 누적.
- 2층 (중간부): 유의미한 해자가 있으나 시간이 지나면 경쟁·대체 위험이 존재. 설계 승인·전환
  비용이 진입장벽이지만 절대적이지 않음.
- 3층 (표층): 지금 수혜를 받지만 진입장벽이 낮아 경쟁이 붙기 쉽고 결국 상품화(마진 하락).
위로 갈수록(1층) 대체 불가능, 아래로 갈수록(3층) 경쟁 진입 용이. 각 매수 항목에 1~3 부여.

**가격 반영 정도(pricing_status) — 모든 항목 필수. 논지가 이미 주가에 얼마나 반영됐는가:**
- unpriced: 시장이 아직 이 연결고리를 인식 못 함 (비대칭 기회 최대)
- partial: 일부 반영됐으나 여전히 상승 여지
- mostly: 상당 부분 반영 (컨센서스에 근접)
- overpriced: 과도하게 선반영 (회피 대상은 대부분 여기)
가장 가치 있는 매수는 '1층 병목 + unpriced/partial'의 조합이다.

품질 기준: 명백한 이름(엔비디아·TSMC·삼성전자 그 자체)을 1차적으로 나열하지 말 것.
그 이면의 덜 알려진, 그러나 논리적으로 필연적인 대상을 우선한다. 확신이 약하면 적게
내되, 매 항목이 '왜 지금 이게 비대칭적 기회인가'에 답해야 한다."""

SCOUT_TOOL = {
    "name": "investment_theses",
    "description": "시그널 기반 투자 대상 발굴 결과",
    "input_schema": {
        "type": "object",
        "properties": {
            "market_read": {"type": "string", "description": "현재 시그널들이 그리는 큰 그림 2-3문장 (한국어)"},
            "theses": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "direction": {"type": "string", "enum": ["buy", "avoid"], "description": "buy=발굴, avoid=회피/청산"},
                        "company": {"type": "string"},
                        "ticker": {"type": "string", "description": "미국=티커, 한국=6자리코드, 일본=4자리코드"},
                        "market": {"type": "string", "enum": ["US", "KR", "JP"]},
                        "bottleneck": {"type": "string", "description": "핵심 병목/논거 한 줄 (한국어)"},
                        "reasoning": {"type": "string", "description": "2차·3차 추론 체인. A→B→C 연결을 명시 (한국어, 3-5문장)"},
                        "depth_layer": {"type": "integer", "enum": [1, 2, 3], "description": "병목 깊이. 1=최심(대체불가), 2=중간, 3=표층(진입쉬움). 매수 발굴 필수, 회피는 생략 가능"},
                        "pricing_status": {"type": "string", "enum": ["unpriced", "partial", "mostly", "overpriced"], "description": "주가 반영 정도"},
                        "conviction": {"type": "string", "enum": ["high", "medium", "low"]},
                        "falsifier": {"type": "string", "description": "이 논리가 틀렸음을 알 수 있는 조건 (한국어)"},
                        "driving_signals": {"type": "array", "items": {"type": "string"}, "description": "근거가 된 시그널 제목/요지 1-3개"},
                    },
                    "required": ["direction", "company", "market", "bottleneck", "reasoning", "pricing_status", "conviction", "falsifier", "driving_signals"],
                },
            },
        },
        "required": ["market_read", "theses"],
    },
}


def run_thesis_scout(window_days: int = 7) -> list[dict]:
    conn = get_connection()
    signals, digest = _gather_signals(conn, window_days)
    if len(signals) < 5:
        logger.info("Thesis scout: too few strong signals (%d) — skipping", len(signals))
        return []

    signal_block = "\n".join(
        f'- [{s["score"]}|{s["source"]}|{s["category"]}] {s["title"]}'
        f'\n  근거: {(s["score_reasoning"] or "")[:140]}'
        for s in signals
    )
    digest_block = ""
    if digest:
        digest_block = f'\n\n오늘의 다이제스트 헤드라인: {digest["headline"]}\n요약: {(digest["summary_md"] or "")[:500]}'

    user_msg = (
        f"최근 {window_days}일 강력 시그널 {len(signals)}개 (점수 {SIGNAL_SCORE_FLOOR}+):\n\n"
        f"{signal_block}{digest_block}\n\n"
        "위 시그널들로부터 2차·3차 추론을 통해 투자 대상을 발굴하세요. "
        "매수 발굴과 회피/청산을 모두 포함하되, 명백한 1차 수혜주는 피하고 대체 불가능한 "
        "병목에 집중하세요. 확신 있는 것만, 각 3-7개 이내."
    )

    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=MODEL,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=SCOUT_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
            tools=[SCOUT_TOOL],
            tool_choice={"type": "tool", "name": "investment_theses"},
        )
    except Exception:
        logger.exception("Thesis scout: LLM call failed")
        return []

    data = None
    for block in resp.content:
        if block.type == "tool_use":
            data = block.input
            break
    if not data or not data.get("theses"):
        logger.warning("Thesis scout: no theses returned")
        return []

    thesis_date = datetime.now().strftime("%Y-%m-%d")
    market_read = data.get("market_read", "")
    saved = []
    for t in data["theses"]:
        conn.execute(
            """INSERT INTO investment_theses
               (thesis_date, direction, company, ticker, market, bottleneck, reasoning,
                depth_layer, pricing_status, conviction, falsifier, driving_signals, model_used)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                thesis_date, t.get("direction", "buy"), t.get("company", ""),
                t.get("ticker", ""), t.get("market", ""), t.get("bottleneck", ""),
                t.get("reasoning", ""), t.get("depth_layer"), t.get("pricing_status", ""),
                t.get("conviction", "medium"),
                t.get("falsifier", ""), json.dumps(t.get("driving_signals", []), ensure_ascii=False),
                MODEL,
            ),
        )
        saved.append(t)
    conn.commit()
    logger.info("Thesis scout: %d theses saved (%s)", len(saved), thesis_date)
    return [{"market_read": market_read, **t} for t in saved]
