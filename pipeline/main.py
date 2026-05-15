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


async def _collect_all(keywords: list[str], since: datetime) -> list:
    from pipeline.collectors.hackernews import HackerNewsCollector
    from pipeline.collectors.rss import RSSCollector
    from pipeline.utils.rate_limiter import get_limiter

    sources_cfg = _load_sources_config()
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
    ]

    for name, collector in collectors:
        try:
            items = await collector.collect(keywords, since)
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

        # Step 5: Score with Claude Haiku
        from pipeline.processing.scorer import score_items
        items_scored = score_items(new_ids)

        # Step 6: Generate digest
        from pipeline.generators.daily_digest import generate_digest
        digest_data = generate_digest()

        # Step 7: Deliver to Discord
        if digest_data:
            from pipeline.delivery.discord_webhook import deliver_digest
            from datetime import date
            deliver_digest(digest_data, date.today().isoformat())

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
        raise


@cli.command()
@click.option("--days", default=30, help="Number of days to backfill")
def backfill(days: int):
    """Backfill historical data for trend detection baseline."""
    click.echo(f"Backfill for {days} days — not yet implemented")


@cli.command()
def weekly():
    """Run weekly keyword suggestion pipeline."""
    click.echo("Weekly pipeline — not yet implemented")


@cli.command()
def event():
    """Check for conferences needing pre/post-event briefings."""
    click.echo("Event pipeline — not yet implemented")


if __name__ == "__main__":
    cli()
