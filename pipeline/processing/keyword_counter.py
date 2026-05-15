from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import date

from pipeline.db import get_connection, get_active_keywords

logger = logging.getLogger(__name__)


def count_keywords_for_items(
    item_ids: list[int],
    target_date: date | None = None,
    use_item_dates: bool = False,
) -> None:
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

    placeholders = ",".join("?" for _ in item_ids)
    rows = conn.execute(
        f"SELECT id, source, title, content_snippet, date(published_at) as item_date FROM raw_items WHERE id IN ({placeholders})",
        item_ids,
    ).fetchall()

    if use_item_dates:
        date_groups: dict[str, list] = {}
        for row in rows:
            d = row["item_date"]
            if d not in date_groups:
                date_groups[d] = []
            date_groups[d].append(row)

        for date_str, day_rows in sorted(date_groups.items()):
            _count_rows(conn, patterns, day_rows, date_str)
            _aggregate_daily(conn, date_str)

        logger.info("Keyword counter: %d items across %d dates", len(rows), len(date_groups))
    else:
        if target_date is None:
            target_date = date.today()
        date_str = target_date.isoformat()
        _count_rows(conn, patterns, rows, date_str)
        _aggregate_daily(conn, date_str)


def _count_rows(conn, patterns: dict, rows: list, date_str: str) -> None:
    counts: dict[tuple[str, str], list[int]] = {}
    item_keywords: dict[int, list[str]] = {}

    for row in rows:
        text = (row["title"] or "") + " " + (row["content_snippet"] or "")
        matched_kws = []
        for kw, pattern in patterns.items():
            if pattern.search(text):
                key = (kw, row["source"])
                if key not in counts:
                    counts[key] = []
                counts[key].append(row["id"])
                matched_kws.append(kw)
        if len(matched_kws) >= 2:
            item_keywords[row["id"]] = matched_kws

    for (kw, source), matched_ids in counts.items():
        sample = json.dumps(matched_ids[:5])
        conn.execute(
            """INSERT INTO keyword_mentions (keyword, source, mention_date, mention_count, sample_item_ids)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(keyword, source, mention_date)
               DO UPDATE SET mention_count = excluded.mention_count,
                             sample_item_ids = excluded.sample_item_ids""",
            (kw, source, date_str, len(matched_ids), sample),
        )

    _count_cooccurrences(conn, item_keywords, date_str)

    conn.commit()
    logger.info("Keyword counter: %d keyword-source pairs for %s", len(counts), date_str)


def _count_cooccurrences(conn, item_keywords: dict[int, list[str]], date_str: str) -> None:
    pair_counts: dict[tuple[str, str], list[int]] = {}

    for item_id, kws in item_keywords.items():
        kws_sorted = sorted(kws)
        for i in range(len(kws_sorted)):
            for j in range(i + 1, len(kws_sorted)):
                pair = (kws_sorted[i], kws_sorted[j])
                if pair not in pair_counts:
                    pair_counts[pair] = []
                pair_counts[pair].append(item_id)

    for (kw_a, kw_b), item_ids in pair_counts.items():
        sample = json.dumps(item_ids[:5])
        conn.execute(
            """INSERT INTO keyword_cooccurrences (keyword_a, keyword_b, mention_date, co_count, sample_item_ids)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(keyword_a, keyword_b, mention_date)
               DO UPDATE SET co_count = excluded.co_count,
                             sample_item_ids = excluded.sample_item_ids""",
            (kw_a, kw_b, date_str, len(item_ids), sample),
        )

    if pair_counts:
        logger.info("Co-occurrences: %d pairs for %s", len(pair_counts), date_str)


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
