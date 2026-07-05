from __future__ import annotations

import asyncio
import time


class RateLimiter:
    def __init__(self, calls_per_second: float):
        self._min_interval = 1.0 / calls_per_second
        self._last_call = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._min_interval - (now - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()

    def acquire_sync(self) -> None:
        now = time.monotonic()
        wait = self._min_interval - (now - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()


LIMITERS: dict[str, RateLimiter] = {
    "hackernews": RateLimiter(2.0),
    "arxiv": RateLimiter(0.1),
    "github": RateLimiter(0.5),
    "youtube": RateLimiter(1.0),
    "rss": RateLimiter(2.0),
}


def get_limiter(source: str) -> RateLimiter:
    return LIMITERS.get(source, RateLimiter(1.0))
