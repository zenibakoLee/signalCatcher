from __future__ import annotations

import logging
from datetime import datetime

import httpx

from pipeline.collectors.base import BaseCollector
from pipeline.models import RawItem
from pipeline.utils.rate_limiter import RateLimiter
from pipeline.utils.retry import with_retry

logger = logging.getLogger(__name__)

REDDIT_BASE = "https://www.reddit.com"
USER_AGENT = "SignalCatcher/1.0 (investment signal collector)"

DEFAULT_SUBREDDITS = [
    "technology",
    "artificial",
    "MachineLearning",
    "nvidia",
    "semiconductor",
    "wallstreetbets",
    "investing",
    "stocks",
    "singularity",
]


class RedditCollector(BaseCollector):
    def __init__(self, subreddits: list[str] | None = None, rate_limiter: RateLimiter | None = None):
        super().__init__(rate_limiter or RateLimiter(1.5))
        self.subreddits = subreddits or DEFAULT_SUBREDDITS

    async def collect(self, keywords: list[str], since: datetime, keyword_categories: dict[str, str] | None = None) -> list[RawItem]:
        items: list[RawItem] = []
        seen_ids: set[str] = set()
        since_ts = since.timestamp()

        async with httpx.AsyncClient(timeout=30, headers={"User-Agent": USER_AGENT}) as client:
            for sub in self.subreddits:
                await self.rate_limiter.acquire()
                try:
                    sub_items = await self._fetch_subreddit(client, sub, since_ts)
                    for item in sub_items:
                        if item.source_id not in seen_ids:
                            seen_ids.add(item.source_id)
                            items.append(item)
                except Exception:
                    logger.exception("Reddit: failed subreddit r/%s", sub)

        logger.info("Reddit: collected %d items from %d subreddits", len(items), len(self.subreddits))
        return items

    async def _fetch_subreddit(
        self, client: httpx.AsyncClient, subreddit: str, since_ts: float
    ) -> list[RawItem]:
        async def _do_request():
            r = await client.get(
                f"{REDDIT_BASE}/r/{subreddit}/hot.json",
                params={"limit": 25, "raw_json": 1},
            )
            r.raise_for_status()
            return r

        resp = await with_retry(_do_request)
        data = resp.json()
        items: list[RawItem] = []

        for child in data.get("data", {}).get("children", []):
            post = child.get("data", {})
            if post.get("stickied"):
                continue

            created = post.get("created_utc", 0)
            if created < since_ts:
                continue

            post_id = post.get("id", "")
            title = post.get("title", "")
            if not post_id or not title:
                continue

            selftext = (post.get("selftext") or "")[:500]
            score = post.get("score", 0)
            num_comments = post.get("num_comments", 0)
            permalink = post.get("permalink", "")

            try:
                pub_dt = datetime.fromtimestamp(created)
            except (ValueError, OSError):
                pub_dt = datetime.now()

            items.append(
                RawItem(
                    source="reddit",
                    source_id=f"reddit_{post_id}",
                    title=title,
                    url=f"https://www.reddit.com{permalink}" if permalink else None,
                    author=post.get("author"),
                    content_snippet=selftext or None,
                    published_at=pub_dt,
                    metadata={
                        "subreddit": subreddit,
                        "score": score,
                        "num_comments": num_comments,
                        "upvote_ratio": post.get("upvote_ratio"),
                        "link_flair_text": post.get("link_flair_text"),
                        "external_url": post.get("url") if not post.get("is_self") else None,
                    },
                )
            )

        return items
