from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import quote

import httpx

from pipeline.collectors.base import BaseCollector
from pipeline.models import RawItem
from pipeline.utils.rate_limiter import RateLimiter
from pipeline.utils.retry import with_retry

logger = logging.getLogger(__name__)

ARXIV_BASE = "https://export.arxiv.org/api/query"
NS = {"atom": "http://www.w3.org/2005/Atom"}


SKIP_CATEGORIES = {"company", "infrastructure"}


class ArxivCollector(BaseCollector):
    def __init__(self, categories: list[str], rate_limiter: RateLimiter):
        super().__init__(rate_limiter)
        self.categories = categories

    async def collect(
        self,
        keywords: list[str],
        since: datetime,
        keyword_categories: dict[str, str] | None = None,
    ) -> list[RawItem]:
        if keyword_categories:
            keywords = [
                kw for kw in keywords
                if keyword_categories.get(kw) not in SKIP_CATEGORIES
            ]

        seen_ids: set[str] = set()
        items: list[RawItem] = []

        async with httpx.AsyncClient(timeout=60) as client:
            for kw in keywords:
                await self.rate_limiter.acquire()
                try:
                    kw_items = await self._search_keyword(client, kw, since, seen_ids)
                    items.extend(kw_items)
                except Exception:
                    logger.exception("arXiv: failed to search keyword '%s'", kw)

        logger.info("arXiv: collected %d items", len(items))
        return items

    async def _search_keyword(
        self,
        client: httpx.AsyncClient,
        keyword: str,
        since: datetime,
        seen: set[str],
    ) -> list[RawItem]:
        cat_query = "+OR+".join(f"cat:{c}" for c in self.categories)
        search_query = f"all:{quote(keyword)}+AND+({cat_query})"

        resp = await with_retry(
            lambda: client.get(
                ARXIV_BASE,
                params={
                    "search_query": search_query,
                    "sortBy": "submittedDate",
                    "sortOrder": "descending",
                    "max_results": "20",
                },
            )
        )
        resp.raise_for_status()

        return self._parse_atom(resp.text, since, seen)

    def _parse_atom(self, xml_text: str, since: datetime, seen: set[str]) -> list[RawItem]:
        items: list[RawItem] = []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            logger.warning("arXiv: failed to parse XML response")
            return items

        for entry in root.findall("atom:entry", NS):
            arxiv_id = (entry.findtext("atom:id", "", NS) or "").strip()
            if not arxiv_id:
                continue

            short_id = arxiv_id.split("/abs/")[-1] if "/abs/" in arxiv_id else arxiv_id
            if short_id in seen:
                continue

            title = (entry.findtext("atom:title", "", NS) or "").strip().replace("\n", " ")
            if not title:
                continue

            published_str = entry.findtext("atom:published", "", NS)
            try:
                published = datetime.fromisoformat(published_str.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue

            if published.replace(tzinfo=None) < since:
                continue

            seen.add(short_id)

            summary = (entry.findtext("atom:summary", "", NS) or "").strip().replace("\n", " ")

            authors = [
                a.findtext("atom:name", "", NS)
                for a in entry.findall("atom:author", NS)
            ]

            categories = [
                c.get("term", "")
                for c in entry.findall("atom:category", NS)
            ]

            pdf_link = ""
            for link in entry.findall("atom:link", NS):
                if link.get("title") == "pdf":
                    pdf_link = link.get("href", "")
                    break

            items.append(
                RawItem(
                    source="arxiv",
                    source_id=short_id,
                    title=title,
                    url=pdf_link or arxiv_id,
                    author=authors[0] if authors else None,
                    content_snippet=summary[:500] if summary else None,
                    published_at=published.replace(tzinfo=None),
                    metadata={
                        "categories": categories,
                        "primary_category": categories[0] if categories else None,
                        "authors": authors[:5],
                    },
                )
            )

        return items
