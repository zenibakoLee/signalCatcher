from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

import httpx

from pipeline.collectors.base import BaseCollector
from pipeline.models import RawItem
from pipeline.utils.rate_limiter import RateLimiter
from pipeline.utils.retry import with_retry

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


class GitHubCollector(BaseCollector):
    def __init__(self, extra_queries: list[str], rate_limiter: RateLimiter):
        super().__init__(rate_limiter)
        self.extra_queries = extra_queries

    async def collect(self, keywords: list[str], since: datetime) -> list[RawItem]:
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            logger.warning("GitHub: GITHUB_TOKEN not set, skipping")
            return []

        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
        }

        seen: set[str] = set()
        items: list[RawItem] = []
        since_date = since.strftime("%Y-%m-%d")
        week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

        async with httpx.AsyncClient(timeout=30, headers=headers) as client:
            for kw in keywords:
                await self.rate_limiter.acquire()
                try:
                    query = f"{kw} created:>{week_ago} stars:>5"
                    kw_items = await self._search(client, query, seen)
                    items.extend(kw_items)
                except Exception:
                    logger.exception("GitHub: failed keyword '%s'", kw)

            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            for extra in self.extra_queries:
                await self.rate_limiter.acquire()
                try:
                    query = extra.replace("{yesterday}", yesterday)
                    extra_items = await self._search(client, query, seen)
                    items.extend(extra_items)
                except Exception:
                    logger.exception("GitHub: failed extra query '%s'", extra)

            await self.rate_limiter.acquire()
            try:
                trending = await self._search(
                    client,
                    f"stars:>100 pushed:>{yesterday}",
                    seen,
                    sort="stars",
                    per_page=30,
                )
                items.extend(trending)
            except Exception:
                logger.exception("GitHub: failed trending query")

        logger.info("GitHub: collected %d items", len(items))
        return items

    async def _search(
        self,
        client: httpx.AsyncClient,
        query: str,
        seen: set[str],
        sort: str = "stars",
        per_page: int = 20,
    ) -> list[RawItem]:
        resp = await with_retry(
            lambda: client.get(
                f"{GITHUB_API}/search/repositories",
                params={
                    "q": query,
                    "sort": sort,
                    "order": "desc",
                    "per_page": per_page,
                },
            )
        )
        resp.raise_for_status()
        data = resp.json()

        items: list[RawItem] = []
        for repo in data.get("items", []):
            full_name = repo.get("full_name", "")
            if not full_name or full_name in seen:
                continue
            seen.add(full_name)

            desc = repo.get("description") or ""
            created = repo.get("created_at", "")
            try:
                published = datetime.fromisoformat(created.replace("Z", "+00:00")).replace(tzinfo=None)
            except (ValueError, AttributeError):
                published = datetime.now()

            items.append(
                RawItem(
                    source="github",
                    source_id=full_name,
                    title=f"{full_name}: {desc[:200]}" if desc else full_name,
                    url=repo.get("html_url"),
                    author=repo.get("owner", {}).get("login"),
                    content_snippet=desc[:500] if desc else None,
                    published_at=published,
                    metadata={
                        "stars": repo.get("stargazers_count", 0),
                        "forks": repo.get("forks_count", 0),
                        "language": repo.get("language"),
                        "topics": repo.get("topics", []),
                    },
                )
            )

        return items
