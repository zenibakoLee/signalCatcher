from __future__ import annotations

import logging

from pipeline.db import insert_raw_item
from pipeline.models import RawItem

logger = logging.getLogger(__name__)


def deduplicate_and_store(items: list[RawItem]) -> list[int]:
    new_ids: list[int] = []
    dupes = 0
    for item in items:
        row_id = insert_raw_item(item)
        if row_id is not None:
            new_ids.append(row_id)
        else:
            dupes += 1

    logger.info(
        "Dedup: %d new items stored, %d duplicates skipped",
        len(new_ids),
        dupes,
    )
    return new_ids
