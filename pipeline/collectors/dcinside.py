"""DCInside 특이점(Singularity) 마이너 갤러리 개념글 수집기.

RSS를 제공하지 않아 Scrapling(Fetcher)으로 recommend(개념글) 목록을 스크래핑한다.
한국 AI 커뮤니티의 큐레이션된 화제 글 — 해외 소스보다 먼저 도는 번역·정리·찌라시
성격의 신호를 포착한다.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from pipeline.collectors.base import BaseCollector
from pipeline.models import RawItem
from pipeline.utils.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

RECOMMEND_URL = (
    "https://gall.dcinside.com/mgallery/board/lists/"
    "?id=thesingularity&exception_mode=recommend"
)
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


class DCInsideCollector(BaseCollector):
    def __init__(self, rate_limiter: RateLimiter | None = None, pages: int = 2):
        super().__init__(rate_limiter or RateLimiter(0.5))
        self.pages = pages

    async def collect(self, keywords: list[str], since: datetime, keyword_categories: dict[str, str] | None = None) -> list[RawItem]:
        items = await asyncio.get_event_loop().run_in_executor(None, self._scrape, since)
        logger.info("DCInside: collected %d concept posts", len(items))
        return items

    def _scrape(self, since: datetime) -> list[RawItem]:
        try:
            from scrapling.fetchers import Fetcher
        except ImportError:
            logger.warning("scrapling not installed — DCInside collector skipped")
            return []

        items: list[RawItem] = []
        seen: set[str] = set()
        for page in range(1, self.pages + 1):
            url = f"{RECOMMEND_URL}&page={page}"
            try:
                resp = Fetcher.get(url, headers={"User-Agent": _UA}, timeout=20)
            except Exception:
                logger.exception("DCInside: fetch failed (page %d)", page)
                continue
            if resp.status != 200:
                logger.warning("DCInside: status %s (page %d)", resp.status, page)
                continue
            items.extend(self._parse_rows(resp, since, seen))
        return items

    def _parse_rows(self, page, since: datetime, seen: set[str]) -> list[RawItem]:
        items: list[RawItem] = []
        for row in page.css("tr.ub-content"):
            num_el = row.css("td.gall_num")
            num = num_el[0].text.strip() if num_el else ""
            if not num.isdigit():  # 공지·광고 행 제외
                continue

            anchors = row.css("td.gall_tit a")
            if not anchors:
                continue
            title = anchors[0].get_all_text().strip()
            href = anchors[0].attrib.get("href", "")
            if not title or not href:
                continue
            if href.startswith("/"):
                href = "https://gall.dcinside.com" + href

            if num in seen:
                continue
            seen.add(num)

            date_el = row.css("td.gall_date")
            pub_dt = datetime.now()
            if date_el:
                raw_date = date_el[0].attrib.get("title", "")
                try:
                    pub_dt = datetime.strptime(raw_date[:16], "%Y-%m-%d %H:%M")
                except (ValueError, TypeError):
                    pass
            if pub_dt < since:
                continue

            rec_el = row.css("td.gall_recommend")
            recommend = 0
            if rec_el:
                try:
                    recommend = int(rec_el[0].text.strip())
                except (ValueError, TypeError):
                    pass

            items.append(
                RawItem(
                    source="dcinside",
                    source_id=f"dcinside_{num}",
                    title=title,
                    url=href,
                    author=None,
                    content_snippet=None,
                    published_at=pub_dt,
                    metadata={"gallery": "thesingularity", "recommend": recommend, "post_no": num},
                )
            )
        return items
