from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import click
import yaml

from pipeline import llm
from pipeline.db import (
    complete_pipeline_run,
    get_active_keywords,
    get_connection,
    get_keyword_categories,
    init_db,
    load_keywords_from_yaml,
    start_pipeline_run,
)
from pipeline.processing.dedup import deduplicate_and_store
from pipeline.utils.logging_config import setup_logging

logger = logging.getLogger(__name__)

TRANSLATION_BATCH_SIZE = 100
TRANSLATION_MAX_OUTPUT_TOKENS = 8_000

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def _new_llm_run_id(kind: str, pipeline_run_id: int) -> str:
    return f"{kind}-{pipeline_run_id}-{uuid.uuid4()}"


@dataclass(frozen=True)
class DailyScoringResult:
    items_scored: int
    deferred_count: int
    malformed_count: int = 0
    validation_error: str | None = None


def _score_daily_window(
    *, since: datetime, until: datetime, run_id: str | int
) -> DailyScoringResult:
    from pipeline.processing.scorer import MAX_SCORED_ITEMS_PER_RUN, score_items
    from pipeline.processing.scoring_selection import select_daily_scoring_items

    selection = select_daily_scoring_items(
        get_connection(), since=since, until=until, limit=MAX_SCORED_ITEMS_PER_RUN
    )
    items_scored = score_items(selection.item_ids, run_id=run_id)
    validation_error = None
    if items_scored != len(selection.item_ids):
        validation_error = (
            "daily scorer persisted "
            f"{items_scored} of {len(selection.item_ids)} selected items"
        )
    return DailyScoringResult(
        items_scored=items_scored,
        deferred_count=selection.deferred_count,
        malformed_count=selection.malformed_count,
        validation_error=validation_error,
    )


def _load_sources_config() -> dict:
    with open(CONFIG_DIR / "sources.yaml") as f:
        return yaml.safe_load(f)


def write_current_theme_snapshots(run_id: int) -> int:
    """Retain daily theme monitoring while candidate discovery stays weekly."""
    from pipeline.processing.discovery_snapshots import write_theme_snapshots

    return write_theme_snapshots(get_connection(), run_id)


async def _collect_all(keywords: list[str], since: datetime) -> tuple[list, list[str]]:
    from pipeline.collectors.arxiv import ArxivCollector
    from pipeline.collectors.dcinside import DCInsideCollector
    from pipeline.collectors.github import GitHubCollector
    from pipeline.collectors.hackernews import HackerNewsCollector
    from pipeline.collectors.openrouter import OpenRouterCollector
    from pipeline.collectors.polymarket import PolymarketCollector
    from pipeline.collectors.rss import RSSCollector
    from pipeline.collectors.sec_form4 import SECForm4Collector
    from pipeline.collectors.trendshift import TrendshiftCollector
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
                search_queries=sources_cfg.get("youtube_search_queries", []),
                trending_queries=sources_cfg.get("youtube_trending_queries", []),
            ),
        ),
        ("dcinside", DCInsideCollector(get_limiter("dcinside"))),
        ("trendshift", TrendshiftCollector(get_limiter("trendshift"))),
        ("sec_form4", SECForm4Collector(get_limiter("sec_form4"))),
        ("polymarket", PolymarketCollector(get_limiter("polymarket"))),
        ("openrouter", OpenRouterCollector(get_limiter("openrouter"))),
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
    setup_logging()
    init_db()
    load_keywords_from_yaml(CONFIG_DIR / "keywords.yaml")


@cli.command()
@click.option("--hours", default=24, help="How many hours back to collect")
def daily(hours: int):
    """Run the daily collection pipeline."""
    start = time.time()
    run_id = start_pipeline_run("daily")
    llm_run_id = _new_llm_run_id("daily", run_id)
    since = datetime.now() - timedelta(hours=hours)  # noqa: DTZ005
    scoring_since = datetime.now(UTC) - timedelta(hours=hours)
    keywords = get_active_keywords()
    new_ids: list[int] = []
    items_scored = 0
    errors: list[str] = []

    logger.info("Starting daily pipeline: %d keywords, since %s", len(keywords), since.isoformat())

    try:
        # Step 1-3: Collect and deduplicate
        all_items, errors = asyncio.run(_collect_all(keywords, since))
        new_ids = deduplicate_and_store(all_items)

        # Step 3.5: Collect social buzz (apewisdom — independent of keyword search)
        from pipeline.collectors.apewisdom import collect_social_buzz
        buzz_count = asyncio.run(collect_social_buzz())
        logger.info("Social buzz: %d tickers stored", buzz_count)

        # Step 3.7: Enrich YouTube items with transcripts
        from pipeline.processing.transcript import enrich_youtube_transcripts
        yt_enriched = enrich_youtube_transcripts(new_ids)
        logger.info("Transcript enrichment: %d items", yt_enriched)

        # Step 4: Count keywords
        from pipeline.processing.keyword_counter import count_keywords_for_items
        count_keywords_for_items(new_ids)

        # Step 4.5: Auto-manage keywords (discover/spike/retire)
        from pipeline.generators.keyword_suggestions import auto_manage_keywords
        kw_result = auto_manage_keywords(run_id=llm_run_id)
        logger.info("Keyword management: %s", kw_result)
        if kw_result.get("added") or kw_result.get("spiked") or kw_result.get("resurged") or kw_result.get("retired"):
            from pipeline.delivery.discord_webhook import deliver_keyword_management
            deliver_keyword_management(kw_result)

        # Step 5: Detect trends (z-score acceleration)
        from pipeline.processing.trend_detector import detect_trends
        trend_alerts = detect_trends(run_id=llm_run_id)

        # Step 6: Score a bounded, recoverable current-window selection.
        scoring_until = datetime.now(UTC)
        scoring_result = _score_daily_window(
            since=scoring_since, until=scoring_until, run_id=llm_run_id
        )
        items_scored = scoring_result.items_scored
        if scoring_result.deferred_count:
            from pipeline.processing.scorer import MAX_SCORED_ITEMS_PER_RUN

            errors.append(
                "scoring coverage gap: deferred "
                f"{scoring_result.deferred_count} current-window items due to "
                f"{MAX_SCORED_ITEMS_PER_RUN}-item daily cap"
            )
        if scoring_result.malformed_count:
            errors.append(
                "scoring coverage gap: deferred "
                f"{scoring_result.malformed_count} current-window items due to "
                "invalid collected_at timestamps"
            )
        if scoring_result.validation_error:
            raise RuntimeError(scoring_result.validation_error)

        # Step 7: Generate digest
        from pipeline.generators.daily_digest import generate_digest
        digest_data = generate_digest(run_id=llm_run_id)

        # Step 7.5: Generate comic for digest
        comic_path = None
        if digest_data:
            from datetime import date

            from pipeline.generators.comic import generate_digest_comic
            comic_path = generate_digest_comic(
                digest_data, date.today().isoformat()  # noqa: DTZ011
            )

        # Step 8: Deliver to Discord
        if digest_data:
            from datetime import date

            from pipeline.delivery.discord_webhook import deliver_digest
            deliver_digest(
                digest_data,
                date.today().isoformat(),  # noqa: DTZ011
                comic_path=comic_path,
            )

        accel_alerts = [a for a in trend_alerts if a.severity == "accelerating"]
        if accel_alerts:
            from pipeline.delivery.discord_webhook import deliver_acceleration_alerts
            deliver_acceleration_alerts(accel_alerts)

        theme_count = write_current_theme_snapshots(run_id)
        logger.info("Daily theme snapshots: %d", theme_count)

        if errors:
            from datetime import date

            from pipeline.delivery.discord_webhook import deliver_collector_errors
            deliver_collector_errors(
                errors, date.today().isoformat()  # noqa: DTZ011
            )

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
        terminal_error = str(e)
        if terminal_error not in errors:
            errors.append(terminal_error)
        complete_pipeline_run(
            run_id,
            status="failed",
            items_collected=len(new_ids),
            items_scored=items_scored,
            errors=errors,
            duration_secs=round(duration, 2),
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
    since = datetime.now() - timedelta(days=days)  # noqa: DTZ005
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
    from pipeline.collectors.github import GitHubCollector
    from pipeline.collectors.hackernews import HackerNewsCollector
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
@click.option("--target-date", default=None, help="Override target date (YYYY-MM-DD) for testing")
def event(target_date: str | None):
    """Check for conferences needing pre/post-event briefings."""
    from datetime import date as date_type

    from pipeline.delivery.discord_webhook import deliver_conference_briefing
    from pipeline.generators.conference_briefing import (
        _persist_post_event,
        _persist_pre_event,
        generate_post_event,
        generate_pre_event,
        get_actionable_conferences,
    )

    target = date_type.fromisoformat(target_date) if target_date else None
    run_id = f"event-{uuid.uuid4()}"
    actionable = get_actionable_conferences(target)

    if not actionable["pre_event"] and not actionable["post_event"]:
        logger.info("Event pipeline: no conferences to process today")
        return

    MAX_CONFERENCE_BRIEFINGS_PER_RUN = 4
    pre_event = actionable["pre_event"][:MAX_CONFERENCE_BRIEFINGS_PER_RUN]
    post_event = actionable["post_event"][:MAX_CONFERENCE_BRIEFINGS_PER_RUN - len(pre_event)]
    if len(pre_event) + len(post_event) < len(actionable["pre_event"]) + len(actionable["post_event"]):
        raise llm.RequestLimitExceededError("conference briefing cap exceeded")

    conn = get_connection()
    generated: list[tuple[dict, dict, str]] = []
    for conf in pre_event:
        logger.info("Generating pre-event briefing for %s", conf["name"])
        data = generate_pre_event(conf, conn=conn, commit=False, run_id=run_id, persist=False)
        generated.append((data, conf, "pre_event"))

    for conf in post_event:
        logger.info("Generating post-event briefing for %s", conf["name"])
        data = generate_post_event(conf, conn=conn, commit=False, run_id=run_id, persist=False)
        generated.append((data, conf, "post_event"))
    try:
        conn.execute("BEGIN")
        for data, conf, briefing_type in generated:
            if briefing_type == "pre_event":
                _persist_pre_event(conn, conf, data)
            else:
                expected_items = data.pop("_expected_items")
                source_ids = data.pop("_source_ids")
                _persist_post_event(conn, conf, data, expected_items, source_ids)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    for data, conf, briefing_type in generated:
        deliver_conference_briefing(data, conf, briefing_type)


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
    scored = score_items(ids, run_id=f"score-all-{uuid.uuid4()}")
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

    batch_size = TRANSLATION_BATCH_SIZE
    total = 0
    run_id = f"translate-titles-{uuid.uuid4()}"
    staged: list[tuple[object, dict]] = []
    for batch_start in range(0, len(rows), batch_size):
        batch = rows[batch_start : batch_start + batch_size]
        titles_block = "\n".join(f'{i+1}. "{row["title"]}"' for i, row in enumerate(batch))
        prompt = (
            "아래 영어 제목들을 자연스러운 한국어로 번역하세요.\n"
            "고유명사(회사명, 제품명, 기술명)는 원어 그대로 유지하세요.\n\n"
            f"{titles_block}\n\n"
            'JSON 객체로 반환: {"translations": [{"index": 1, "title_ko": "번역된 제목"}, ...]}\n'
            "유효한 JSON만 반환하세요."
        )
        translation_entry = {
            "type": "object", "properties": {
                "index": {"type": "integer", "minimum": 1, "maximum": len(batch)},
                "title_ko": {"type": "string", "minLength": 1, "maxLength": 500},
            }, "required": ["index", "title_ko"], "additionalProperties": False,
        }
        llm.require_no_business_transaction(conn)
        result = llm.get_boundary().complete(
            instructions="Translate titles faithfully into natural Korean.", input_text=prompt,
            max_output_tokens=TRANSLATION_MAX_OUTPUT_TOKENS, model=llm.LUNA_MODEL, workload="title_translation",
            run_id=run_id,
            output_schema=llm.strict_object_schema("title_translations", {"translations": {
                "type": "array", "minItems": len(batch), "maxItems": len(batch), "items": translation_entry,
            }}),
        )
        translations = result.parsed["translations"]
        if sorted(entry["index"] for entry in translations) != list(range(1, len(batch) + 1)):
            raise llm.LLMParseError("translation indices must be unique and complete")
        staged.extend((batch[entry["index"] - 1], entry) for entry in translations)
    try:
        conn.execute("BEGIN")
        for row, entry in staged:
            cursor = conn.execute("UPDATE scored_items SET title_ko = ? WHERE id = ?", (entry["title_ko"], row["id"]))
            if cursor.rowcount != 1:
                raise llm.LLMOutputError("title update did not update exactly one staged row")
            total += 1
        conn.commit()
    except Exception:
        conn.rollback()
        logger.exception("translate-titles: transaction failed")
        raise

    logger.info("translate-titles: %d items translated", total)


@cli.command("superstar-weekly")
@click.option("--lookback-days", type=click.IntRange(1, 90), default=30, show_default=True)
@click.option("--evidence-limit", type=click.IntRange(1, 120), default=120, show_default=True)
@click.option("--candidate-limit", type=click.IntRange(1, 8), default=8, show_default=True)
@click.option("--as-of", "as_of_date", default=None, help="KST audit date (YYYY-MM-DD)")
def superstar_weekly(
    lookback_days: int,
    evidence_limit: int,
    candidate_limit: int,
    as_of_date: str | None,
) -> None:
    """Run the bounded weekly source-backed Superstar audit."""
    from datetime import date as date_type

    from pipeline.generators import superstar_weekly as weekly

    started = time.time()
    pipeline_run_id = start_pipeline_run("superstar_weekly")
    llm_run_id = _new_llm_run_id("superstar-weekly", pipeline_run_id)
    input_items = considered = published = 0
    errors: list[str] = []
    try:
        now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
        if as_of_date is not None:
            audit_date = date_type.fromisoformat(as_of_date)
            cutoff = datetime.combine(
                audit_date, datetime.max.time(), tzinfo=ZoneInfo("Asia/Seoul")
            )
        else:
            audit_date = now_kst.date()
            cutoff = now_kst
        conn = get_connection()
        candidates, input_items = weekly.discover_candidates(
            conn,
            as_of=cutoff,
            lookback_days=lookback_days,
            evidence_limit=evidence_limit,
            candidate_limit=candidate_limit,
            run_id=llm_run_id,
        )
        security_records, sec_errors = weekly.verify_candidate_list(
            conn, candidates, as_of=audit_date.isoformat()
        )
        errors.extend(sec_errors)
        counts = weekly.persist_weekly_audit(
            conn,
            pipeline_run_id=pipeline_run_id,
            as_of=audit_date.isoformat(),
            candidates=candidates,
            security_records=security_records,
        )
        considered = counts["considered"]
        published = counts["published"]
        complete_pipeline_run(
            pipeline_run_id,
            status="completed_with_errors" if errors else "completed",
            errors=errors or None,
            duration_secs=round(time.time() - started, 2),
            input_items=input_items,
            candidates_considered=considered,
            candidates_published=published,
        )
    except Exception as exc:
        message = str(exc)
        if message not in errors:
            errors.append(message)
        complete_pipeline_run(
            pipeline_run_id,
            status="failed",
            errors=errors,
            duration_secs=round(time.time() - started, 2),
            input_items=input_items,
            candidates_considered=considered,
            candidates_published=published,
        )
        logger.exception("Weekly Superstar pipeline failed")
        raise


if __name__ == "__main__":
    cli()
