from __future__ import annotations

import logging
from datetime import datetime

import httpx

from pipeline.collectors.base import BaseCollector
from pipeline.models import RawItem
from pipeline.utils.rate_limiter import RateLimiter
from pipeline.utils.retry import with_retry

logger = logging.getLogger(__name__)

ALGOLIA_BASE = "https://hn.algolia.com/api/v1"


class HackerNewsCollector(BaseCollector):
    def __init__(self, rate_limiter: RateLimiter):
        super().__init__(rate_limiter)

    async def collect(self, keywords: list[str], since: datetime, keyword_categories: dict[str, str] | None = None) -> list[RawItem]:
        since_ts = int(since.timestamp())
        seen_ids: set[str] = set()
        items: list[RawItem] = []

        async with httpx.AsyncClient(timeout=30) as client:
            items.extend(
                await self._fetch_front_page(client, seen_ids)
            )

            for kw in keywords:
                await self.rate_limiter.acquire()
                kw_items = await self._search_keyword(client, kw, since_ts, seen_ids)
                items.extend(kw_items)

        logger.info("HackerNews: collected %d items", len(items))
        return items

    async def _fetch_front_page(
        self, client: httpx.AsyncClient, seen: set[str]
    ) -> list[RawItem]:
        await self.rate_limiter.acquire()
        resp = await with_retry(
            lambda: client.get(
                f"{ALGOLIA_BASE}/search",
                params={"tags": "front_page", "hitsPerPage": 30},
            )
        )
        resp.raise_for_status()
        return self._parse_hits(resp.json().get("hits", []), seen)

    async def _search_keyword(
        self,
        client: httpx.AsyncClient,
        keyword: str,
        since_ts: int,
        seen: set[str],
    ) -> list[RawItem]:
        resp = await with_retry(
            lambda: client.get(
                f"{ALGOLIA_BASE}/search",
                params={
                    "query": keyword,
                    "tags": "story",
                    "numericFilters": f"created_at_i>{since_ts}",
                    "hitsPerPage": 50,
                },
            )
        )
        resp.raise_for_status()
        return self._parse_hits(resp.json().get("hits", []), seen)

    def _parse_hits(self, hits: list[dict], seen: set[str]) -> list[RawItem]:
        items: list[RawItem] = []
        for hit in hits:
            oid = str(hit.get("objectID", ""))
            if not oid or oid in seen:
                continue
            seen.add(oid)

            title = hit.get("title") or ""
            if not title:
                continue

            items.append(
                RawItem(
                    source="hackernews",
                    source_id=oid,
                    title=title,
                    url=hit.get("url"),
                    author=hit.get("author"),
                    content_snippet=(hit.get("story_text") or "")[:500],
                    published_at=datetime.fromtimestamp(hit.get("created_at_i", 0)),
                    metadata={
                        "points": hit.get("points", 0),
                        "num_comments": hit.get("num_comments", 0),
                    },
                )
            )
        return items
