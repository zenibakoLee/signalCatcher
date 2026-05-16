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

YT_API = "https://www.googleapis.com/youtube/v3"


class YouTubeCollector(BaseCollector):
    def __init__(self, channels: list[dict], rate_limiter: RateLimiter):
        super().__init__(rate_limiter)
        self.channels = channels
        self._quota_used = 0

    async def collect(self, keywords: list[str], since: datetime, keyword_categories: dict[str, str] | None = None) -> list[RawItem]:
        api_key = os.environ.get("YOUTUBE_API_KEY")
        if not api_key:
            logger.warning("YouTube: YOUTUBE_API_KEY not set, skipping")
            return []

        items: list[RawItem] = []
        effective_since = since - timedelta(days=6)
        since_iso = effective_since.strftime("%Y-%m-%dT%H:%M:%SZ")

        async with httpx.AsyncClient(timeout=30) as client:
            for ch in self.channels:
                await self.rate_limiter.acquire()
                try:
                    ch_items = await self._fetch_channel(
                        client, api_key, ch, since_iso
                    )
                    items.extend(ch_items)
                except Exception:
                    logger.exception("YouTube: failed channel '%s'", ch.get("name"))

        logger.info(
            "YouTube: collected %d items from %d channels (%d quota units used)",
            len(items),
            len(self.channels),
            self._quota_used,
        )
        return items

    async def _fetch_channel(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        channel: dict,
        since_iso: str,
    ) -> list[RawItem]:
        channel_id = channel.get("channel_id", "")
        playlist_id = "UU" + channel_id[2:]

        async def _do_request():
            r = await client.get(
                f"{YT_API}/playlistItems",
                params={
                    "part": "snippet",
                    "playlistId": playlist_id,
                    "maxResults": 10,
                    "key": api_key,
                },
            )
            r.raise_for_status()
            return r

        resp = await with_retry(_do_request)
        self._quota_used += 1

        data = resp.json()
        items: list[RawItem] = []

        for item in data.get("items", []):
            snippet = item.get("snippet", {})
            published = snippet.get("publishedAt", "")

            if published < since_iso:
                continue

            video_id = snippet.get("resourceId", {}).get("videoId", "")
            if not video_id:
                continue

            title = snippet.get("title", "")
            if not title or title == "Private video" or title == "Deleted video":
                continue

            try:
                pub_dt = datetime.fromisoformat(published.replace("Z", "+00:00")).replace(tzinfo=None)
            except (ValueError, AttributeError):
                pub_dt = datetime.now()

            description = (snippet.get("description") or "")[:500]

            items.append(
                RawItem(
                    source="youtube",
                    source_id=video_id,
                    title=title,
                    url=f"https://www.youtube.com/watch?v={video_id}",
                    author=snippet.get("channelTitle"),
                    content_snippet=description or None,
                    published_at=pub_dt,
                    metadata={
                        "channel_id": channel_id,
                        "channel_name": channel.get("name"),
                        "thumbnail": snippet.get("thumbnails", {}).get("high", {}).get("url"),
                    },
                )
            )

        return items
