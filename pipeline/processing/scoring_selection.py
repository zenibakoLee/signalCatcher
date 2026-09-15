from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass(frozen=True)
class DailyScoringSelection:
    item_ids: list[int]
    eligible_count: int
    deferred_count: int
    malformed_count: int = 0


def _aware_iso_epoch(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.timestamp()


def _collected_iso_epoch(value: object) -> float | None:
    """Parse raw_items.collected_at under its storage contract.

    The schema's historical SQLite default is UTC ``strftime`` without an
    offset, so only this column may interpret a naive persisted value as UTC.
    Offset-bearing values are normalized by ``datetime.timestamp``.
    """
    if not isinstance(value, str) or len(value) < 19 or value[10] != "T":
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def select_daily_scoring_items(
    conn: sqlite3.Connection, *, since: datetime, until: datetime, limit: int
) -> DailyScoringSelection:
    """Select a bounded, deterministic, source-balanced daily scoring set."""
    if limit < 1:
        raise ValueError("daily scoring limit must be positive")
    if since.tzinfo is None or since.utcoffset() is None:
        raise ValueError("daily scoring window start must be timezone-aware")
    if until.tzinfo is None or until.utcoffset() is None:
        raise ValueError("daily scoring window end must be timezone-aware")
    if until <= since:
        raise ValueError("daily scoring window end must be after its start")

    conn.create_function("aware_iso_epoch", 1, _aware_iso_epoch, deterministic=True)
    conn.create_function(
        "collected_iso_epoch", 1, _collected_iso_epoch, deterministic=True
    )
    window_start = since.astimezone(UTC).timestamp()
    window_end = until.astimezone(UTC).timestamp()
    # ISO offsets can move the local calendar date by at most one day. This
    # indexed/coarse prefix bound keeps malformed historical rows out of the
    # candidate set while retaining every parseable timestamp in the window.
    coarse_start = (since.astimezone(UTC).date() - timedelta(days=1)).isoformat()
    coarse_end = (until.astimezone(UTC).date() + timedelta(days=1)).isoformat()
    malformed_start = since.astimezone(UTC).replace(tzinfo=None).isoformat()
    malformed_end = until.astimezone(UTC).replace(tzinfo=None).isoformat()
    timestamp_prefix = (
        "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*"
    )
    timestamp_locator = (
        "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T"
        "[0-9][0-9]:[0-9][0-9]:[0-9][0-9]*"
    )
    malformed_count = conn.execute(
        """SELECT COUNT(*)
           FROM raw_items r
           LEFT JOIN scored_items s ON s.raw_item_id = r.id
           WHERE s.raw_item_id IS NULL
             AND r.collected_at GLOB ?
             AND r.collected_at >= ?
             AND r.collected_at < ?
             AND collected_iso_epoch(r.collected_at) IS NULL""",
        (timestamp_locator, malformed_start, malformed_end),
    ).fetchone()[0]
    rows = conn.execute(
        """WITH ranked AS (
               SELECT r.id,
                      r.source,
                      COUNT(*) OVER () AS eligible_count,
                      ROW_NUMBER() OVER (
                          PARTITION BY r.source
                          ORDER BY COALESCE(
                                       aware_iso_epoch(r.published_at),
                                       collected_iso_epoch(r.collected_at)
                                   ) DESC,
                                   collected_iso_epoch(r.collected_at) DESC,
                                   r.id DESC
                      ) AS source_rank
               FROM raw_items r
               LEFT JOIN scored_items s ON s.raw_item_id = r.id
               WHERE s.raw_item_id IS NULL
                 AND r.collected_at GLOB ?
                 AND r.collected_at >= ?
                 AND r.collected_at < ?
                 AND collected_iso_epoch(r.collected_at) >= ?
                 AND collected_iso_epoch(r.collected_at) < ?
           )
           SELECT id, eligible_count
           FROM ranked
           ORDER BY source_rank ASC, source ASC
           LIMIT ?""",
        (timestamp_prefix, coarse_start, coarse_end, window_start, window_end, limit),
    ).fetchall()

    item_ids = [row["id"] for row in rows]
    eligible_count = rows[0]["eligible_count"] if rows else 0
    return DailyScoringSelection(
        item_ids=item_ids,
        eligible_count=eligible_count,
        deferred_count=eligible_count - len(item_ids),
        malformed_count=malformed_count,
    )
