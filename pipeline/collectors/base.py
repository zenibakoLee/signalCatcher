from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from pipeline.models import RawItem
from pipeline.utils.rate_limiter import RateLimiter


class BaseCollector(ABC):
    def __init__(self, rate_limiter: RateLimiter):
        self.rate_limiter = rate_limiter

    @abstractmethod
    async def collect(
        self,
        keywords: list[str],
        since: datetime,
        keyword_categories: dict[str, str] | None = None,
    ) -> list[RawItem]:
        pass
