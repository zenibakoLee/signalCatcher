from __future__ import annotations

import logging
import time
from datetime import datetime

import feedparser

from pipeline.collectors.base import BaseCollector
from pipeline.models import RawItem
from pipeline.utils.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)


class RSSCollector(BaseCollector):
    def __init__(self, feeds: list[dict], rate_limiter: RateLimiter):
        super().__init__(rate_limiter)
        self.feeds = feeds

    async def collect(self, keywords: list[str], since: datetime) -> list[RawItem]:
        since_struct = since.timetuple()
        items: list[RawItem] = []

        for feed_cfg in self.feeds:
            self.rate_limiter.acquire_sync()
            try:
                feed_items = self._parse_feed(feed_cfg, since_struct)
                items.extend(feed_items)
            except Exception:
                logger.exception("RSS: failed to parse %s", feed_cfg.get("name"))

        logger.info("RSS: collected %d items from %d feeds", len(items), len(self.feeds))
        return items

    def _parse_feed(
        self, feed_cfg: dict, since_struct: time.struct_time
    ) -> list[RawItem]:
        feed = feedparser.parse(feed_cfg["url"])
        items: list[RawItem] = []

        for entry in feed.entries:
            published = entry.get("published_parsed") or entry.get("updated_parsed")
            if published and published < since_struct:
                continue

            pub_dt = (
                datetime(*published[:6]) if published else datetime.now()
            )

            title = entry.get("title", "").strip()
            if not title:
                continue

            link = entry.get("link", "")
            source_id = entry.get("id") or link or title

            summary = entry.get("summary", "")
            if hasattr(summary, "__len__") and len(summary) > 500:
                summary = summary[:500]

            items.append(
                RawItem(
                    source="rss",
                    source_id=source_id,
                    title=title,
                    url=link or None,
                    author=entry.get("author"),
                    content_snippet=summary or None,
                    published_at=pub_dt,
                    metadata={
                        "feed_name": feed_cfg.get("name"),
                        "feed_category": feed_cfg.get("category"),
                    },
                )
            )

        return items
