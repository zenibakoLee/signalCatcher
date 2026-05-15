from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import date

from pipeline.db import get_connection, get_active_keywords

logger = logging.getLogger(__name__)


def count_keywords_for_items(item_ids: list[int], target_date: date | None = None) -> None:
    if not item_ids:
        return

    conn = get_connection()
    keywords = get_active_keywords(conn)
    if not keywords:
        logger.warning("No active keywords to count")
        return

    patterns = {
        kw: re.compile(r"\b" + re.escape(kw) + r"\b", re.IGNORECASE)
        for kw in keywords
    }

    if target_date is None:
        target_date = date.today()
    date_str = target_date.isoformat()

    placeholders = ",".join("?" for _ in item_ids)
    rows = conn.execute(
        f"SELECT id, source, title, content_snippet FROM raw_items WHERE id IN ({placeholders})",
        item_ids,
    ).fetchall()

    counts: dict[tuple[str, str], list[int]] = {}

    for row in rows:
        text = (row["title"] or "") + " " + (row["content_snippet"] or "")
        for kw, pattern in patterns.items():
            if pattern.search(text):
                key = (kw, row["source"])
                if key not in counts:
                    counts[key] = []
                counts[key].append(row["id"])

    for (kw, source), matched_ids in counts.items():
        sample = json.dumps(matched_ids[:5])
        conn.execute(
            """INSERT INTO keyword_mentions (keyword, source, mention_date, mention_count, sample_item_ids)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(keyword, source, mention_date)
               DO UPDATE SET mention_count = mention_count + excluded.mention_count,
                             sample_item_ids = excluded.sample_item_ids""",
            (kw, source, date_str, len(matched_ids), sample),
        )

    conn.commit()
    logger.info("Keyword counter: %d keyword-source pairs updated", len(counts))

    _aggregate_daily(conn, date_str)


def _aggregate_daily(conn: sqlite3.Connection, date_str: str) -> None:
    rows = conn.execute(
        """SELECT keyword, SUM(mention_count) as total,
                  json_group_object(source, mention_count) as breakdown
           FROM keyword_mentions
           WHERE mention_date = ?
           GROUP BY keyword""",
        (date_str,),
    ).fetchall()

    for row in rows:
        conn.execute(
            """INSERT INTO keyword_daily_aggregates (keyword, mention_date, total_count, source_breakdown)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(keyword, mention_date)
               DO UPDATE SET total_count = excluded.total_count,
                             source_breakdown = excluded.source_breakdown""",
            (row["keyword"], date_str, row["total"], row["breakdown"]),
        )

    conn.commit()
    logger.info("Daily aggregates: %d keywords aggregated for %s", len(rows), date_str)
