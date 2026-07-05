"""Generate company analysis for a specific ticker. Called by dashboard API."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

from pipeline.db import get_connection, init_db
from pipeline.generators.company_analysis import (
    TICKER_ALIASES,
    WINDOW_DAYS,
    generate_analysis,
)

KST = timezone(timedelta(hours=9))


def main() -> None:
    init_db()

    ticker = sys.argv[1].strip().upper()
    ticker = TICKER_ALIASES.get(ticker, ticker)

    conn = get_connection()
    cutoff = (datetime.now(KST) - timedelta(days=WINDOW_DAYS)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )

    rows = conn.execute(
        """
        SELECT s.related_tickers, s.score, s.score_reasoning, s.category,
               r.title, r.url, r.source, r.content_snippet, r.published_at
        FROM scored_items s
        JOIN raw_items r ON s.raw_item_id = r.id
        WHERE s.related_tickers IS NOT NULL
          AND s.related_tickers != '[]'
          AND r.collected_at >= ?
        ORDER BY r.published_at DESC
        """,
        (cutoff,),
    ).fetchall()

    signals: list[dict] = []
    for row in rows:
        try:
            tickers = json.loads(row["related_tickers"])
        except (json.JSONDecodeError, TypeError):
            continue
        normalized = [
            TICKER_ALIASES.get(t.strip().upper(), t.strip().upper()) for t in tickers
        ]
        if ticker in normalized:
            signals.append(
                {
                    "title": row["title"],
                    "url": row["url"],
                    "source": row["source"],
                    "score": row["score"],
                    "category": row["category"],
                    "reasoning": row["score_reasoning"],
                    "snippet": (row["content_snippet"] or "")[:300],
                    "published_at": row["published_at"],
                }
            )

    buzz_row = conn.execute(
        """
        SELECT SUM(mentions) as m, SUM(upvotes) as u
        FROM social_buzz
        WHERE ticker = ? AND collected_date >= ?
        """,
        (ticker, (datetime.now(KST) - timedelta(days=7)).strftime("%Y-%m-%d")),
    ).fetchone()

    candidate = {
        "ticker": ticker,
        "signal_count": len(signals),
        "avg_score": round(sum(s["score"] for s in signals) / len(signals), 1) if signals else 0,
        "high_score_count": sum(1 for s in signals if s["score"] >= 70),
        "buzz_mentions": (buzz_row["m"] or 0) if buzz_row else 0,
        "signals": sorted(signals, key=lambda s: s["published_at"], reverse=True),
    }

    data = generate_analysis(candidate)
    if data:
        json.dump({"success": True, "ticker": ticker}, sys.stdout, ensure_ascii=False)
    else:
        json.dump({"error": "generation_failed", "ticker": ticker}, sys.stdout)


if __name__ == "__main__":
    main()
