"""Polymarket 예측시장 수집기.

AI·테크·매크로 이벤트에 대한 군중 확률을 수집한다. "연준 9월 인하 확률",
"OpenAI 연내 신모델" 같은 시장가는 기대와 현실의 괴리를 정량화하는 선행 심리 지표.
무료 gamma API 사용, 관련 키워드로 필터링.
"""

from __future__ import annotations

import logging
from datetime import datetime

import httpx

from pipeline.collectors.base import BaseCollector
from pipeline.models import RawItem
from pipeline.utils.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

GAMMA_URL = "https://gamma-api.polymarket.com/markets"
# 투자·기술 관련 마켓만 — 잡음(연예·스포츠) 제외
RELEVANT_KW = (
    "ai ", "openai", "anthropic", "nvidia", "gpt", "llm", "chip", "semiconductor",
    "fed ", "interest rate", "rate cut", "rate hike", "inflation", "recession", "gdp", "tariff",
    "tsmc", "google", "microsoft", "meta ", "tesla", "deepseek", "agi",
    "data center", "quantum", "stock market", "s&p", "nasdaq",
)
# 스포츠·이스포츠·연예 등 잡음 — 키워드가 걸려도 거부
EXCLUDE_KW = (
    "counter-strike", "csgo", "cs2", "nba", "nfl", "mlb", "soccer", "ufc", "boxing",
    "league of legends", "dota", "valorant", "map ", "bo3", "bo5", "vs ", "winner",
    "album", "movie", "grammy", "oscar", "super bowl", "world cup", "election odds",
)


class PolymarketCollector(BaseCollector):
    def __init__(self, rate_limiter: RateLimiter | None = None):
        super().__init__(rate_limiter or RateLimiter(2.0))

    async def collect(self, keywords: list[str], since: datetime, keyword_categories: dict[str, str] | None = None) -> list[RawItem]:
        await self.rate_limiter.acquire()
        params = {
            "active": "true", "closed": "false",
            "order": "volume24hr", "ascending": "false", "limit": 120,
        }
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(GAMMA_URL, params=params)
                r.raise_for_status()
                markets = r.json()
        except Exception:
            logger.exception("Polymarket: fetch failed")
            return []

        items: list[RawItem] = []
        for m in markets:
            q = (m.get("question") or "").strip()
            ql = q.lower()
            if not q or not any(kw in ql for kw in RELEVANT_KW):
                continue
            if any(bad in ql for bad in EXCLUDE_KW):
                continue
            prob = _implied_prob(m)
            if prob is None:
                continue
            vol = _num(m.get("volume24hr"))
            if vol < 5000:  # 유동성 낮은 마켓 제외
                continue

            title = f"[예측시장] {q} — 시장가 {prob:.0f}%"
            items.append(
                RawItem(
                    source="polymarket",
                    source_id=f"poly_{m.get('id') or m.get('slug')}",
                    title=title,
                    url=f"https://polymarket.com/event/{m.get('slug', '')}",
                    author=None,
                    content_snippet=f"24시간 거래량 ${vol:,.0f}. 종료 {m.get('endDate', '')[:10]}.",
                    published_at=datetime.now(),
                    metadata={"probability": round(prob, 1), "volume_24h": round(vol)},
                )
            )
        logger.info("Polymarket: collected %d relevant markets", len(items))
        return items[:25]


def _implied_prob(m: dict) -> float | None:
    """Yes 결과의 시장가(0~1) → %."""
    import json
    raw = m.get("outcomePrices")
    try:
        prices = json.loads(raw) if isinstance(raw, str) else raw
        if prices:
            return float(prices[0]) * 100
    except (json.JSONDecodeError, TypeError, ValueError, IndexError):
        pass
    return None


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0
