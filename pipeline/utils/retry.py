from __future__ import annotations

import asyncio
import logging
import random
from typing import TypeVar

import httpx

logger = logging.getLogger(__name__)

T = TypeVar("T")

NON_RETRYABLE = {401, 403, 404, 422}


async def with_retry(
    coro_factory,
    max_attempts: int = 3,
    base_delay: float = 1.0,
):
    for attempt in range(1, max_attempts + 1):
        try:
            return await coro_factory()
        except httpx.HTTPStatusError as e:
            if e.response.status_code in NON_RETRYABLE:
                raise
            if attempt == max_attempts:
                raise
            if e.response.status_code == 429:
                delay = 15.0 * attempt * (1 + random.random() * 0.25)
            else:
                delay = base_delay * (2 ** (attempt - 1)) * (1 + random.random() * 0.25)
            logger.warning("Attempt %d failed (%s), retrying in %.1fs", attempt, e, delay)
            await asyncio.sleep(delay)
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout) as e:
            if attempt == max_attempts:
                raise
            delay = base_delay * (2 ** (attempt - 1)) * (1 + random.random() * 0.25)
            logger.warning("Attempt %d failed (%s), retrying in %.1fs", attempt, e, delay)
            await asyncio.sleep(delay)
