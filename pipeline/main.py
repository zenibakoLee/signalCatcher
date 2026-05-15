from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

import click
import yaml
from dotenv import load_dotenv

from pipeline.db import (
    complete_pipeline_run,
    get_keyword_categories,
    init_db,
    load_keywords_from_yaml,
    get_active_keywords,
    start_pipeline_run,
)
from pipeline.processing.dedup import deduplicate_and_store
from pipeline.utils.logging_config import setup_logging

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def _load_sources_config() -> dict:
    with open(CONFIG_DIR / "sources.yaml") as f:
        return yaml.safe_load(f)


async def _collect_all(keywords: list[str], since: datetime) -> tuple[list, list[str]]:
    from pipeline.collectors.hackernews import HackerNewsCollector
    from pipeline.collectors.rss import RSSCollector
    from pipeline.collectors.arxiv import ArxivCollector
    from pipeline.collectors.github import GitHubCollector
    from pipeline.collectors.youtube import YouTubeCollector
    from pipeline.utils.rate_limiter import get_limiter

    sources_cfg = _load_sources_config()
    keyword_cats = get_keyword_categories()
    all_items = []
    errors = []

    collectors = [
        ("hackernews", HackerNewsCollector(get_limiter("hackernews"))),
        (
            "rss",
            RSSCollector(
                sources_cfg.get("rss_feeds", []),
                get_limiter("rss"),
            ),
        ),
        (
            "arxiv",
            ArxivCollector(
                sources_cfg.get("arxiv_categories", ["cs.AI", "cs.LG"]),
                get_limiter("arxiv"),
            ),
        ),
        (
            "github",
            GitHubCollector(
                sources_cfg.get("github_search", {}).get("extra_queries", []),
                get_limiter("github"),
            ),
        ),
        (
            "youtube",
            YouTubeCollector(
                sources_cfg.get("youtube_channels", []),
                get_limiter("youtube"),
            ),
        ),
    ]

    for name, collector in collectors:
        try:
            items = await collector.collect(keywords, since, keyword_categories=keyword_cats)
            all_items.extend(items)
        except Exception as e:
            logger.exception("Collector %s failed", name)
            errors.append(f"{name}: {e}")

    return all_items, errors


@click.group()
def cli():
    """Signal Catcher pipeline CLI."""
    load_dotenv()
    setup_logging()
    init_db()
    load_keywords_from_yaml(CONFIG_DIR / "keywords.yaml")


@cli.command()
@click.option("--hours", default=24, help="How many hours back to collect")
def daily(hours: int):
    """Run the daily collection pipeline."""
    start = time.time()
    run_id = start_pipeline_run("daily")
    since = datetime.now() - timedelta(hours=hours)
    keywords = get_active_keywords()

    logger.info("Starting daily pipeline: %d keywords, since %s", len(keywords), since.isoformat())

    try:
        # Step 1-3: Collect and deduplicate
        all_items, errors = asyncio.run(_collect_all(keywords, since))
        new_ids = deduplicate_and_store(all_items)

        # Step 4: Count keywords
        from pipeline.processing.keyword_counter import count_keywords_for_items
        count_keywords_for_items(new_ids)

        # Step 5: Detect trends (z-score acceleration)
        from pipeline.processing.trend_detector import detect_trends
        trend_alerts = detect_trends()

        # Step 6: Score with Claude Haiku
        from pipeline.processing.scorer import score_items
        items_scored = score_items(new_ids)

        # Step 7: Generate digest
        from pipeline.generators.daily_digest import generate_digest
        digest_data = generate_digest()

        # Step 8: Deliver to Discord
        if digest_data:
            from pipeline.delivery.discord_webhook import deliver_digest
            from datetime import date
            deliver_digest(digest_data, date.today().isoformat())

        accel_alerts = [a for a in trend_alerts if a.severity == "accelerating"]
        if accel_alerts:
            from pipeline.delivery.discord_webhook import deliver_acceleration_alerts
            deliver_acceleration_alerts(accel_alerts)

        duration = time.time() - start
        status = "completed" if not errors else "completed_with_errors"
        complete_pipeline_run(
            run_id,
            status=status,
            items_collected=len(new_ids),
            items_scored=items_scored,
            errors=errors or None,
            duration_secs=round(duration, 2),
        )
        logger.info(
            "Daily pipeline %s: %d collected, %d scored in %.1fs",
            status,
            len(new_ids),
            items_scored,
            duration,
        )
    except Exception as e:
        duration = time.time() - start
        complete_pipeline_run(
            run_id, status="failed", errors=[str(e)], duration_secs=round(duration, 2)
        )
        logger.exception("Daily pipeline failed")
        from pipeline.delivery.discord_webhook import deliver_error_alert
        deliver_error_alert("daily", str(e))
        raise


@cli.command()
@click.option("--days", default=30, help="Number of days to backfill")
def backfill(days: int):
    """Backfill historical data for trend detection baseline."""
    start = time.time()
    run_id = start_pipeline_run("backfill")
    since = datetime.now() - timedelta(days=days)
    keywords = get_active_keywords()

    logger.info("Starting backfill: %d days, %d keywords", days, len(keywords))

    try:
        all_items, errors = asyncio.run(_collect_backfill(keywords, since))
        new_ids = deduplicate_and_store(all_items)

        from pipeline.processing.keyword_counter import count_keywords_for_items
        count_keywords_for_items(new_ids, use_item_dates=True)

        duration = time.time() - start
        complete_pipeline_run(
            run_id,
            status="completed",
            items_collected=len(new_ids),
            items_scored=0,
            errors=errors or None,
            duration_secs=round(duration, 2),
        )
        logger.info("Backfill completed: %d items in %.1fs", len(new_ids), duration)
    except Exception as e:
        duration = time.time() - start
        complete_pipeline_run(
            run_id, status="failed", errors=[str(e)], duration_secs=round(duration, 2)
        )
        logger.exception("Backfill failed")
        raise


async def _collect_backfill(keywords: list[str], since: datetime) -> tuple[list, list[str]]:
    from pipeline.collectors.hackernews import HackerNewsCollector
    from pipeline.collectors.github import GitHubCollector
    from pipeline.utils.rate_limiter import get_limiter

    sources_cfg = _load_sources_config()
    all_items = []
    errors = []

    collectors = [
        ("hackernews", HackerNewsCollector(get_limiter("hackernews"))),
        (
            "github",
            GitHubCollector(
                sources_cfg.get("github_search", {}).get("extra_queries", []),
                get_limiter("github"),
            ),
        ),
    ]

    for name, collector in collectors:
        try:
            items = await collector.collect(keywords, since)
            all_items.extend(items)
        except Exception as e:
            logger.exception("Backfill collector %s failed", name)
            errors.append(f"{name}: {e}")

    return all_items, errors


@cli.command()
def weekly():
    """Run weekly keyword suggestion pipeline."""
    from pipeline.generators.keyword_suggestions import suggest_keywords
    from pipeline.delivery.discord_webhook import deliver_keyword_suggestions

    start = time.time()
    run_id = start_pipeline_run("weekly")

    try:
        suggestions = suggest_keywords()
        if suggestions:
            deliver_keyword_suggestions(suggestions)

        duration = time.time() - start
        complete_pipeline_run(
            run_id,
            status="completed",
            items_collected=0,
            items_scored=0,
            duration_secs=round(duration, 2),
        )
        logger.info("Weekly pipeline completed: %d suggestions in %.1fs", len(suggestions), duration)
    except Exception as e:
        duration = time.time() - start
        complete_pipeline_run(
            run_id, status="failed", errors=[str(e)], duration_secs=round(duration, 2)
        )
        logger.exception("Weekly pipeline failed")
        raise


@cli.command()
@click.option("--target-date", default=None, help="Override target date (YYYY-MM-DD) for testing")
def event(target_date: str | None):
    """Check for conferences needing pre/post-event briefings."""
    from datetime import date as date_type
    from pipeline.generators.conference_briefing import (
        get_actionable_conferences,
        generate_pre_event,
        generate_post_event,
    )
    from pipeline.delivery.discord_webhook import deliver_conference_briefing

    target = date_type.fromisoformat(target_date) if target_date else None
    actionable = get_actionable_conferences(target)

    if not actionable["pre_event"] and not actionable["post_event"]:
        logger.info("Event pipeline: no conferences to process today")
        return

    for conf in actionable["pre_event"]:
        logger.info("Generating pre-event briefing for %s", conf["name"])
        data = generate_pre_event(conf)
        if data:
            deliver_conference_briefing(data, conf, "pre_event")

    for conf in actionable["post_event"]:
        logger.info("Generating post-event briefing for %s", conf["name"])
        data = generate_post_event(conf)
        if data:
            deliver_conference_briefing(data, conf, "post_event")


@cli.command("score-all")
@click.option("--batch-limit", default=200, help="Max items to score in one run")
def score_all(batch_limit: int):
    """Score all unscored items across all sources."""
    from pipeline.db import get_connection
    from pipeline.processing.scorer import score_items

    conn = get_connection()
    unscored = conn.execute(
        """SELECT r.id FROM raw_items r
           LEFT JOIN scored_items s ON s.raw_item_id = r.id
           WHERE s.id IS NULL
           ORDER BY r.published_at DESC
           LIMIT ?""",
        (batch_limit,),
    ).fetchall()

    ids = [r["id"] for r in unscored]
    if not ids:
        logger.info("score-all: no unscored items")
        return

    logger.info("score-all: scoring %d items", len(ids))
    scored = score_items(ids)
    logger.info("score-all: %d items scored", scored)


@cli.command("translate-titles")
@click.option("--batch-limit", default=500, help="Max items to translate in one run")
def translate_titles(batch_limit: int):
    """Backfill title_ko for scored items missing Korean translations."""
    from pipeline.db import get_connection

    conn = get_connection()
    rows = conn.execute(
        """SELECT s.id, r.title
           FROM scored_items s
           JOIN raw_items r ON s.raw_item_id = r.id
           WHERE s.title_ko IS NULL
           ORDER BY s.id DESC
           LIMIT ?""",
        (batch_limit,),
    ).fetchall()

    if not rows:
        logger.info("translate-titles: all items already translated")
        return

    logger.info("translate-titles: %d items to translate", len(rows))

    import anthropic
    client = anthropic.Anthropic()
    batch_size = 30
    total = 0

    for batch_start in range(0, len(rows), batch_size):
        batch = rows[batch_start : batch_start + batch_size]
        titles_block = "\n".join(
            f'{i+1}. "{row["title"]}"' for i, row in enumerate(batch)
        )

        prompt = (
            f"아래 영어 제목들을 자연스러운 한국어로 번역하세요.\n"
            f"고유명사(회사명, 제품명, 기술명)는 원어 그대로 유지하세요.\n\n"
            f"{titles_block}\n\n"
            f'JSON 배열로 반환: [{{"index": 1, "title_ko": "번역된 제목"}}, ...]\n'
            f"유효한 JSON만 반환하세요."
        )

        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=3000,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text.strip()
            if text.startswith("```"):
                lines = text.split("\n")
                text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

            import json
            translations = json.loads(text)

            for entry in translations:
                idx = entry.get("index", 0) - 1
                title_ko = entry.get("title_ko")
                if idx < 0 or idx >= len(batch) or not title_ko:
                    continue
                conn.execute(
                    "UPDATE scored_items SET title_ko = ? WHERE id = ?",
                    (title_ko, batch[idx]["id"]),
                )
                total += 1
            conn.commit()
        except Exception:
            logger.exception("translate-titles: batch failed at offset %d", batch_start)

    logger.info("translate-titles: %d items translated", total)


if __name__ == "__main__":
    cli()
