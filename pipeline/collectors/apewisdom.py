from __future__ import annotations

import logging
from datetime import date, datetime

import httpx

from pipeline.db import get_connection

logger = logging.getLogger(__name__)

API_BASE = "https://apewisdom.io/api/v1.0"

AI_TICKERS = {
    "NVDA", "AMD", "SMCI", "TSM", "AVGO", "MRVL", "MU", "INTC",
    "MSFT", "GOOGL", "GOOG", "META", "AMZN", "AAPL", "ORCL",
    "CRM", "NOW", "PLTR", "AI", "PATH", "SNOW", "DDOG",
    "ARM", "QCOM", "ASML", "AMAT", "LRCX", "KLAC",
    "SPY", "QQQ", "SMH", "SOXX",
}

MAX_PAGES = 3


async def collect_social_buzz(target_date: date | None = None) -> int:
    if target_date is None:
        target_date = date.today()

    date_str = target_date.isoformat()
    conn = get_connection()

    existing = conn.execute(
        "SELECT COUNT(*) FROM social_buzz WHERE collected_date = ?", (date_str,)
    ).fetchone()[0]
    if existing > 0:
        logger.info("Social buzz already collected for %s (%d rows)", date_str, existing)
        return existing

    all_results = []
    async with httpx.AsyncClient(timeout=30) as client:
        for page in range(1, MAX_PAGES + 1):
            try:
                url = f"{API_BASE}/filter/all-stocks/page/{page}" if page > 1 else f"{API_BASE}/filter/all-stocks"
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
                results = data.get("results", [])
                if not results:
                    break
                all_results.extend(results)
            except Exception:
                logger.exception("ApeWisdom fetch failed (page %d)", page)
                break

    if not all_results:
        logger.warning("ApeWisdom: no results fetched")
        return 0

    stored = 0
    for r in all_results:
        ticker = r.get("ticker", "").upper()
        if not ticker:
            continue

        conn.execute(
            """INSERT OR IGNORE INTO social_buzz
               (ticker, name, mentions, upvotes, rank, rank_24h_ago, mentions_24h_ago, source_filter, collected_date)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'all-stocks', ?)""",
            (
                ticker,
                r.get("name"),
                int(r.get("mentions", 0)),
                int(r.get("upvotes", 0)),
                int(r.get("rank", 0)),
                int(r.get("rank_24h_ago", 0)) if r.get("rank_24h_ago") else None,
                int(r.get("mentions_24h_ago", 0)) if r.get("mentions_24h_ago") else None,
                date_str,
            ),
        )
        stored += 1

    conn.commit()
    logger.info("ApeWisdom: stored %d tickers for %s", stored, date_str)
    return stored


def get_ai_buzz_summary(target_date: date | None = None, days: int = 1) -> list[dict]:
    if target_date is None:
        target_date = date.today()

    conn = get_connection(readonly=True)
    date_str = target_date.isoformat()

    rows = conn.execute(
        """SELECT ticker, name, mentions, upvotes, rank, rank_24h_ago, mentions_24h_ago
           FROM social_buzz
           WHERE collected_date = ? AND ticker IN ({})
           ORDER BY mentions DESC""".format(",".join("?" * len(AI_TICKERS))),
        (date_str, *AI_TICKERS),
    ).fetchall()

    return [dict(r) for r in rows]


def get_ai_buzz_total(target_date: date | None = None) -> dict:
    """Get aggregate AI ticker buzz for risk scoring."""
    if target_date is None:
        target_date = date.today()

    conn = get_connection(readonly=True)
    date_str = target_date.isoformat()
    placeholders = ",".join("?" * len(AI_TICKERS))

    row = conn.execute(
        f"""SELECT SUM(mentions) as total_mentions, SUM(upvotes) as total_upvotes,
                   COUNT(*) as ticker_count
            FROM social_buzz
            WHERE collected_date = ? AND ticker IN ({placeholders})""",
        (date_str, *AI_TICKERS),
    ).fetchone()

    history = conn.execute(
        f"""SELECT collected_date, SUM(mentions) as total_mentions
            FROM social_buzz
            WHERE ticker IN ({placeholders})
              AND collected_date <= ?
              AND collected_date >= date(?, '-90 days')
            GROUP BY collected_date
            ORDER BY collected_date""",
        (*AI_TICKERS, date_str, date_str),
    ).fetchall()

    totals = [r["total_mentions"] for r in history if r["total_mentions"]]

    import statistics
    mean_30d = statistics.mean(totals[-30:]) if len(totals) >= 7 else None
    std_30d = statistics.stdev(totals[-30:]) if len(totals) >= 7 else None

    current = row["total_mentions"] or 0
    z_score = None
    if mean_30d is not None and std_30d and std_30d > 0:
        z_score = (current - mean_30d) / std_30d

    return {
        "date": date_str,
        "total_mentions": current,
        "total_upvotes": row["total_upvotes"] or 0,
        "ticker_count": row["ticker_count"] or 0,
        "mean_30d": round(mean_30d, 1) if mean_30d else None,
        "std_30d": round(std_30d, 1) if std_30d else None,
        "z_score": round(z_score, 3) if z_score is not None else None,
    }
