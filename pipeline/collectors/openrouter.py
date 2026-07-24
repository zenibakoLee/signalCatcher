"""OpenRouter 모델 사용량 랭킹 수집기.

OpenRouter에서 실제로 어떤 AI 모델이 얼마나 쓰이는지(토큰 사용량)는 채택·시장점유율의
선행 지표다. 논문·벤치마크가 아니라 '프로덕션에서 실사용되는 모델'을 보여준다.
공식 rankings API(무키)로 최근 사용량 상위 모델을 집계해 요약 신호로 낸다.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime

import httpx

from pipeline.collectors.base import BaseCollector
from pipeline.models import RawItem
from pipeline.utils.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

RANKINGS_URL = "https://openrouter.ai/api/frontend/v1/rankings/models"
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


class OpenRouterCollector(BaseCollector):
    def __init__(self, rate_limiter: RateLimiter | None = None):
        super().__init__(rate_limiter or RateLimiter(1.0))

    async def collect(self, keywords: list[str], since: datetime, keyword_categories: dict[str, str] | None = None) -> list[RawItem]:
        await self.rate_limiter.acquire()
        try:
            async with httpx.AsyncClient(timeout=20, headers={"User-Agent": _UA}) as client:
                r = await client.get(RANKINGS_URL)
                r.raise_for_status()
                rows = r.json().get("data", [])
        except Exception:
            logger.exception("OpenRouter: fetch failed")
            return []
        if not rows:
            return []

        dates = sorted({row["date"][:10] for row in rows})
        recent = set(dates[-3:])
        agg: dict[str, dict] = defaultdict(lambda: {"tokens": 0, "count": 0})
        for row in rows:
            if row["date"][:10] not in recent:
                continue
            model = row["model_permaslug"].split(":")[0]
            agg[model]["tokens"] += row.get("total_completion_tokens", 0) + row.get("total_prompt_tokens", 0)
            agg[model]["count"] += row.get("count", 0)
        if not agg:
            return []

        ranked = sorted(agg.items(), key=lambda kv: -kv[1]["tokens"])
        total_tokens = sum(v["tokens"] for _, v in ranked) or 1
        top = ranked[:10]

        lines = []
        for rank, (model, v) in enumerate(top, 1):
            share = v["tokens"] / total_tokens * 100
            lines.append(f"{rank}. {model} ({share:.1f}%)")

        # 진영 집계 (중국 오픈소스 vs 미국 프론티어) — 상품화 내러티브 정량화
        cn_labs = ("deepseek", "moonshot", "z-ai", "qwen", "alibaba", "minimax", "tencent", "xiaomi", "stepfun", "baidu", "01-ai", "zhipu", "glm")
        us_labs = ("anthropic", "openai", "google", "meta", "x-ai", "xai", "mistral", "nvidia")
        cn_share = sum(v["tokens"] for m, v in ranked if any(l in m for l in cn_labs)) / total_tokens * 100
        us_share = sum(v["tokens"] for m, v in ranked if any(l in m for l in us_labs)) / total_tokens * 100

        title = f"[OpenRouter 실사용 랭킹] 상위 모델 토큰 점유율 (최근 3일) — 중국계 {cn_share:.0f}% vs 미국계 {us_share:.0f}%"
        snippet = " · ".join(lines)

        item = RawItem(
            source="openrouter",
            source_id=f"openrouter_{dates[-1]}",
            title=title,
            url="https://openrouter.ai/rankings",
            author=None,
            content_snippet=snippet[:500],
            published_at=datetime.now(),
            metadata={
                "top_models": [m for m, _ in top],
                "cn_share": round(cn_share, 1),
                "us_share": round(us_share, 1),
            },
        )
        logger.info("OpenRouter: top model=%s, CN %.0f%% / US %.0f%%", top[0][0], cn_share, us_share)
        return [item]
