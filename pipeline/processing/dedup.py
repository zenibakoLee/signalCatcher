from __future__ import annotations

import logging

from pipeline.db import insert_raw_items
from pipeline.models import RawItem

logger = logging.getLogger(__name__)


def deduplicate_and_store(items: list[RawItem]) -> list[int]:
    new_ids = insert_raw_items(items)
    dupes = len(items) - len(new_ids)

    logger.info(
        "Dedup: %d new items stored, %d duplicates skipped",
        len(new_ids),
        dupes,
    )
    return new_ids
