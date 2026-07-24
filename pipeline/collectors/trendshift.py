"""Trendshift.io 트렌딩 GitHub 저장소 수집기.

GitHub 네이티브 trending의 대안으로, 모멘텀(멘션 속도 등) 기준 급부상 저장소를
순위화한다. RSS/API가 없어 Scrapling으로 메인 페이지에서 저장소명을 추출한 뒤
GitHub API로 설명·스타를 보강한다. 기존 GitHubCollector(topic·stars 검색)와 달리
'지금 뜨는' 모멘텀 신호를 잡는다 — 채택이 명백해지기 전의 선행 신호.

투자 무관 개발툴이 다수 섞이므로 스코어러가 걸러내도록 설계 (HN viral 패스와 동일 철학).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime

import httpx

from pipeline.collectors.base import BaseCollector
from pipeline.models import RawItem
from pipeline.utils.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

TRENDSHIFT_URL = "https://trendshift.io"
GITHUB_API = "https://api.github.com"
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
_REPO_RE = re.compile(r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)")
MAX_REPOS = 20


class TrendshiftCollector(BaseCollector):
    def __init__(self, rate_limiter: RateLimiter | None = None):
        super().__init__(rate_limiter or RateLimiter(0.5))

    async def collect(self, keywords: list[str], since: datetime, keyword_categories: dict[str, str] | None = None) -> list[RawItem]:
        repos = await asyncio.get_event_loop().run_in_executor(None, self._scrape_repos)
        if not repos:
            return []
        items = await self._enrich(repos)
        logger.info("Trendshift: collected %d trending repos", len(items))
        return items

    def _scrape_repos(self) -> list[str]:
        try:
            from scrapling.fetchers import Fetcher
        except ImportError:
            logger.warning("scrapling not installed — Trendshift collector skipped")
            return []
        try:
            page = Fetcher.get(TRENDSHIFT_URL, headers={"User-Agent": _UA}, timeout=20)
        except Exception:
            logger.exception("Trendshift: fetch failed")
            return []
        if page.status != 200:
            logger.warning("Trendshift: status %s", page.status)
            return []

        seen: set[str] = set()
        repos: list[str] = []
        for m in _REPO_RE.findall(page.html_content):
            repo = m.rstrip(".git")
            # trendshift 자체 링크·assets 제외, owner/repo 형태만
            if repo.count("/") == 1 and repo.lower() not in seen and not repo.startswith("trendshift"):
                seen.add(repo.lower())
                repos.append(repo)
            if len(repos) >= MAX_REPOS:
                break
        return repos

    async def _enrich(self, repos: list[str]) -> list[RawItem]:
        token = os.environ.get("GITHUB_TOKEN")
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"token {token}"

        items: list[RawItem] = []
        async with httpx.AsyncClient(timeout=20, headers=headers) as client:
            for rank, repo in enumerate(repos, 1):
                await self.rate_limiter.acquire()
                try:
                    r = await client.get(f"{GITHUB_API}/repos/{repo}")
                    if r.status_code != 200:
                        continue
                    data = r.json()
                except Exception:
                    logger.debug("Trendshift: GitHub enrich failed for %s", repo)
                    continue

                desc = (data.get("description") or "").strip()
                stars = data.get("stargazers_count", 0)
                lang = data.get("language") or ""
                topics = data.get("topics", [])
                title = f"{repo} — {desc}" if desc else repo

                items.append(
                    RawItem(
                        source="trendshift",
                        source_id=f"trendshift_{repo}",
                        title=title,
                        url=data.get("html_url") or f"https://github.com/{repo}",
                        author=data.get("owner", {}).get("login"),
                        content_snippet=desc[:500] or None,
                        published_at=datetime.now(),
                        metadata={
                            "trending_rank": rank,
                            "stars": stars,
                            "language": lang,
                            "topics": topics,
                        },
                    )
                )
        return items
