from __future__ import annotations

import asyncio
import sqlite3
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from click.testing import CliRunner

from pipeline import db, main
from pipeline.collectors.github import GitHubCollector
from pipeline.processing import scorer
from pipeline.processing.scoring_selection import select_daily_scoring_items
from pipeline.utils.rate_limiter import RateLimiter


def _selection_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE raw_items (
            id INTEGER PRIMARY KEY,
            source TEXT NOT NULL,
            published_at TEXT NOT NULL,
            collected_at TEXT NOT NULL
        );
        CREATE TABLE scored_items (
            id INTEGER PRIMARY KEY,
            raw_item_id INTEGER NOT NULL UNIQUE
        );
        """
    )
    return conn


def test_selector_bounds_857_item_backlog_and_reports_exact_deferred_count(
    monkeypatch,
) -> None:
    conn = _selection_connection()
    since = datetime(2026, 9, 14, tzinfo=UTC)
    sources = ["arxiv"] * 400 + ["github"] * 300 + ["youtube"] * 157
    conn.executemany(
        "INSERT INTO raw_items (id, source, published_at, collected_at) VALUES (?, ?, ?, ?)",
        [
            (
                item_id,
                source,
                (since + timedelta(minutes=item_id)).isoformat(),
                (since + timedelta(seconds=item_id)).isoformat(),
            )
            for item_id, source in enumerate(sources, 1)
        ],
    )

    until = since + timedelta(days=2)
    selection = select_daily_scoring_items(
        conn, since=since, until=until, limit=400
    )

    assert len(selection.item_ids) == 400
    assert len(set(selection.item_ids)) == 400
    assert selection.eligible_count == 857
    assert selection.deferred_count == 457
    assert Counter(sources[item_id - 1] for item_id in selection.item_ids) == {
        "arxiv": 134,
        "github": 133,
        "youtube": 133,
    }

    score_calls: list[list[int]] = []
    monkeypatch.setattr(main, "get_connection", lambda: conn)
    monkeypatch.setattr(
        scorer,
        "score_items",
        lambda item_ids, *, run_id: score_calls.append(item_ids) or len(item_ids),
    )
    result = main._score_daily_window(
        since=since, until=until, run_id="857-test"
    )

    assert scorer.MAX_SCORED_ITEMS_PER_RUN == 400
    assert score_calls == [selection.item_ids]
    assert result == main.DailyScoringResult(items_scored=400, deferred_count=457)


def test_selector_is_newest_first_per_source_with_deterministic_ties() -> None:
    conn = _selection_connection()
    since = datetime(2026, 9, 14, tzinfo=UTC)
    fresh = (since + timedelta(hours=1)).isoformat()
    old = (since - timedelta(seconds=1)).isoformat()
    conn.executemany(
        "INSERT INTO raw_items (id, source, published_at, collected_at) VALUES (?, ?, ?, ?)",
        [
            (1, "arxiv", fresh, fresh),
            (2, "arxiv", fresh, fresh),
            (3, "github", fresh, fresh),
            (4, "github", old, old),
            (5, "youtube", fresh, fresh),
        ],
    )
    conn.execute("INSERT INTO scored_items (raw_item_id) VALUES (5)")

    until = since + timedelta(days=1)
    first = select_daily_scoring_items(conn, since=since, until=until, limit=10)
    second = select_daily_scoring_items(conn, since=since, until=until, limit=10)

    assert first.item_ids == [2, 3, 1]
    assert second == first
    assert first.eligible_count == 3
    assert first.deferred_count == 0


def test_selector_orders_mixed_offset_timestamps_by_absolute_time() -> None:
    conn = _selection_connection()
    since = datetime(2026, 9, 15, tzinfo=UTC)
    conn.executemany(
        "INSERT INTO raw_items (id, source, published_at, collected_at) VALUES (?, ?, ?, ?)",
        [
            (1, "arxiv", "2026-09-15T09:30:00+09:00", "2026-09-15T10:00:00+09:00"),
            (2, "arxiv", "2026-09-15T01:00:00+00:00", "2026-09-15T01:00:00+00:00"),
        ],
    )

    selection = select_daily_scoring_items(
        conn, since=since, until=since + timedelta(days=1), limit=10
    )

    assert selection.item_ids == [2, 1]


def test_selector_accepts_legacy_naive_collected_at_as_utc() -> None:
    conn = _selection_connection()
    conn.execute(
        "INSERT INTO raw_items (id, source, published_at, collected_at) VALUES (?, ?, ?, ?)",
        (1, "github", "2026-09-15T01:00:00", "2026-09-15T01:00:00"),
    )

    selection = select_daily_scoring_items(
        conn,
        since=datetime(2026, 9, 15, 0, 30, tzinfo=UTC),
        until=datetime(2026, 9, 16, tzinfo=UTC),
        limit=10,
    )

    assert selection.item_ids == [1]


def test_selector_isolates_malformed_rows_to_the_candidate_window() -> None:
    conn = _selection_connection()
    conn.executemany(
        "INSERT INTO raw_items (id, source, published_at, collected_at) VALUES (?, ?, ?, ?)",
        [
            (1, "github", "not-a-published-time", "2026-09-15T01:00:00"),
            (2, "arxiv", "2026-09-15T09:30:00+09:00", "2026-09-15T10:00:00+09:00"),
            (3, "rss", "broken", "2020-01-01Tbroken"),
            (4, "youtube", "broken", "2026-09-15T01:00:00broken"),
            (5, "rss", "broken", "2026-09-15"),
            (6, "github", "future", "2026-09-17T00:00:00+00:00"),
            (7, "rss", "broken", "2026-09-17T01:00:00broken"),
            (8, "rss", "broken", "2026-09-14T01:00:00broken"),
        ],
    )

    selection = select_daily_scoring_items(
        conn,
        since=datetime(2026, 9, 15, tzinfo=UTC),
        until=datetime(2026, 9, 16, tzinfo=UTC),
        limit=10,
    )

    assert selection.item_ids == [2, 1]
    assert selection.eligible_count == 2
    assert selection.deferred_count == 0
    assert selection.malformed_count == 1


def test_real_schema_collector_item_writes_aware_utc_timestamps(monkeypatch) -> None:
    class Response:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {
                "items": [
                    {
                        "full_name": "nous/research",
                        "created_at": "2026-09-15T01:00:00Z",
                        "owner": {"login": "nous"},
                    }
                ]
            }

    class Client:
        async def get(self, *_args, **_kwargs) -> Response:
            return Response()

    collector = GitHubCollector([], RateLimiter(1_000_000))
    item = asyncio.run(collector._search(cast(Any, Client()), "test", set()))[0]
    assert item.published_at == datetime(2026, 9, 15, 1, tzinfo=UTC)

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db._SCHEMA)
    monkeypatch.setattr(db, "_connection", conn)
    item_id = db.insert_raw_item(item)

    row = conn.execute(
        "SELECT published_at, collected_at FROM raw_items WHERE id = ?", (item_id,)
    ).fetchone()
    assert row["published_at"] == "2026-09-15T01:00:00+00:00"
    collected = datetime.fromisoformat(row["collected_at"])
    assert collected.tzinfo is UTC

    conn.execute(
        """INSERT INTO raw_items (source, source_id, title, published_at)
           VALUES ('rss', 'schema-default', 'default', '2026-09-15T01:00:00+00:00')"""
    )
    default_collected = conn.execute(
        "SELECT collected_at FROM raw_items WHERE source_id = 'schema-default'"
    ).fetchone()[0]
    assert datetime.fromisoformat(default_collected).tzinfo is UTC

    conn.execute("DELETE FROM raw_items WHERE source_id = 'schema-default'")
    conn.execute(
        "UPDATE raw_items SET collected_at = '2026-09-15T02:00:00+00:00' WHERE id = ?",
        (item_id,),
    )
    conn.executemany(
        """INSERT INTO raw_items
           (source, source_id, title, published_at, collected_at)
           VALUES (?, ?, ?, ?, ?)""",
        [
            ("github", "legacy", "legacy", "2026-09-15T00:30:00", "2026-09-15T00:30:00"),
            ("arxiv", "offset", "offset", "2026-09-15T09:30:00+09:00", "2026-09-15T10:00:00+09:00"),
            ("rss", "old-broken", "old", "broken", "2020-01-01Tbroken"),
            ("youtube", "current-broken", "current", "broken", "2026-09-15T01:00:00broken"),
        ],
    )

    selection = select_daily_scoring_items(
        conn,
        since=datetime(2026, 9, 15, tzinfo=UTC),
        until=datetime(2026, 9, 16, tzinfo=UTC),
        limit=10,
    )
    selected_source_ids = {
        conn.execute("SELECT source_id FROM raw_items WHERE id = ?", (selected_id,)).fetchone()[0]
        for selected_id in selection.item_ids
    }
    assert selected_source_ids == {"nous/research", "legacy", "offset"}
    assert selection.malformed_count == 1


def test_daily_scoring_recovers_current_window_items_when_dedupe_has_no_new_ids(
    monkeypatch,
) -> None:
    conn = _selection_connection()
    since = datetime(2026, 9, 14, tzinfo=UTC)
    fresh = (since + timedelta(hours=1)).isoformat()
    conn.executemany(
        "INSERT INTO raw_items (id, source, published_at, collected_at) VALUES (?, ?, ?, ?)",
        [(1, "arxiv", fresh, fresh), (2, "github", fresh, fresh)],
    )
    scored_calls: list[list[int]] = []

    monkeypatch.setattr(main, "get_connection", lambda: conn)
    monkeypatch.setattr(
        "pipeline.processing.scorer.score_items",
        lambda item_ids, *, run_id: scored_calls.append(item_ids) or len(item_ids),
    )

    result = main._score_daily_window(
        since=since, until=since + timedelta(days=1), run_id="retry-run"
    )

    assert scored_calls == [[1, 2]]
    assert result.items_scored == 2
    assert result.deferred_count == 0


def test_daily_records_exact_deferred_coverage_and_continues_downstream(
    monkeypatch,
) -> None:
    completed: list[dict] = []
    downstream: list[str] = []
    collection_finished_at: list[datetime] = []

    async def collect(_keywords, _since):
        collection_finished_at.append(datetime.now(UTC))
        return [object()] * 857, []

    def score_window(**kwargs):
        assert kwargs["until"] >= collection_finished_at[0]
        return main.DailyScoringResult(
            items_scored=400, deferred_count=457, malformed_count=3
        )

    monkeypatch.setattr(main, "start_pipeline_run", lambda _kind: 97)
    monkeypatch.setattr(main, "get_active_keywords", list)
    monkeypatch.setattr(main, "_collect_all", collect)
    monkeypatch.setattr(main, "deduplicate_and_store", lambda _items: list(range(1, 858)))
    monkeypatch.setattr(
        main,
        "_score_daily_window",
        score_window,
    )
    monkeypatch.setattr(
        "pipeline.processing.scorer.score_items",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unbounded scorer path used")),
    )
    monkeypatch.setattr(
        "pipeline.generators.thesis_scout.run_thesis_scout",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("daily must not generate candidates")
        ),
    )
    monkeypatch.setattr(main, "complete_pipeline_run", lambda _run_id, **values: completed.append(values))
    monkeypatch.setattr("pipeline.collectors.apewisdom.collect_social_buzz", lambda: _async_value(0))
    monkeypatch.setattr("pipeline.processing.transcript.enrich_youtube_transcripts", lambda _ids: 0)
    monkeypatch.setattr("pipeline.processing.keyword_counter.count_keywords_for_items", lambda _ids: None)
    monkeypatch.setattr("pipeline.generators.keyword_suggestions.auto_manage_keywords", lambda **_kwargs: {})
    monkeypatch.setattr("pipeline.processing.trend_detector.detect_trends", lambda **_kwargs: [])
    monkeypatch.setattr(
        "pipeline.generators.daily_digest.generate_digest",
        lambda **_kwargs: downstream.append("digest") or None,
    )
    monkeypatch.setattr(
        main,
        "write_current_theme_snapshots",
        lambda _run_id: downstream.append("themes") or 0,
    )

    monkeypatch.setattr("pipeline.delivery.discord_webhook.deliver_error_alert", lambda *_args: None)

    result = CliRunner().invoke(main.daily, ["--hours", "24"])

    assert result.exception is None
    assert downstream == ["digest", "themes"]
    assert len(completed) == 1
    assert completed[0]["status"] == "completed_with_errors"
    assert completed[0]["items_collected"] == 857
    assert completed[0]["items_scored"] == 400
    assert completed[0]["errors"] == [
        "scoring coverage gap: deferred 457 current-window items due to 400-item daily cap",
        "scoring coverage gap: deferred 3 current-window items due to invalid collected_at timestamps",
    ]


def test_daily_digest_failure_preserves_collector_and_deferred_errors(monkeypatch) -> None:
    completed: list[dict] = []

    async def collect(_keywords, _since):
        return [object()] * 857, ["rss: collector failed"]

    monkeypatch.setattr(main, "start_pipeline_run", lambda _kind: 100)
    monkeypatch.setattr(main, "get_active_keywords", list)
    monkeypatch.setattr(main, "_collect_all", collect)
    monkeypatch.setattr(main, "deduplicate_and_store", lambda _items: list(range(1, 858)))
    monkeypatch.setattr(
        main,
        "_score_daily_window",
        lambda **_kwargs: main.DailyScoringResult(items_scored=400, deferred_count=457),
    )
    monkeypatch.setattr(
        main,
        "complete_pipeline_run",
        lambda _run_id, **values: completed.append(values),
    )
    monkeypatch.setattr("pipeline.collectors.apewisdom.collect_social_buzz", lambda: _async_value(0))
    monkeypatch.setattr("pipeline.processing.transcript.enrich_youtube_transcripts", lambda _ids: 0)
    monkeypatch.setattr("pipeline.processing.keyword_counter.count_keywords_for_items", lambda _ids: None)
    monkeypatch.setattr("pipeline.generators.keyword_suggestions.auto_manage_keywords", lambda **_kwargs: {})
    monkeypatch.setattr("pipeline.processing.trend_detector.detect_trends", lambda **_kwargs: [])
    monkeypatch.setattr(
        "pipeline.generators.daily_digest.generate_digest",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("digest failed")),
    )
    monkeypatch.setattr("pipeline.delivery.discord_webhook.deliver_error_alert", lambda *_args: None)

    result = CliRunner().invoke(main.daily, ["--hours", "24"])

    assert isinstance(result.exception, RuntimeError)
    assert completed[0]["items_collected"] == 857
    assert completed[0]["items_scored"] == 400
    assert completed[0]["errors"] == [
        "rss: collector failed",
        "scoring coverage gap: deferred 457 current-window items due to 400-item daily cap",
        "digest failed",
    ]


def test_daily_failure_deduplicates_identical_accumulated_terminal_error(
    monkeypatch,
) -> None:
    completed: list[dict] = []

    async def collect(_keywords, _since):
        return [object()], ["dedup failed"]

    monkeypatch.setattr(main, "start_pipeline_run", lambda _kind: 102)
    monkeypatch.setattr(main, "get_active_keywords", list)
    monkeypatch.setattr(main, "_collect_all", collect)
    monkeypatch.setattr(
        main,
        "deduplicate_and_store",
        lambda _items: (_ for _ in ()).throw(RuntimeError("dedup failed")),
    )
    monkeypatch.setattr(
        main,
        "complete_pipeline_run",
        lambda _run_id, **values: completed.append(values),
    )
    monkeypatch.setattr("pipeline.delivery.discord_webhook.deliver_error_alert", lambda *_args: None)

    result = CliRunner().invoke(main.daily, ["--hours", "24"])

    assert isinstance(result.exception, RuntimeError)
    assert completed[0]["errors"] == ["dedup failed"]


def test_daily_failed_scorer_records_12_collected_and_zero_scored(monkeypatch) -> None:
    completed: list[dict] = []

    async def collect(_keywords, _since):
        return [object()] * 12, []

    monkeypatch.setattr(main, "start_pipeline_run", lambda _kind: 98)
    monkeypatch.setattr(main, "get_active_keywords", list)
    monkeypatch.setattr(main, "_collect_all", collect)
    monkeypatch.setattr(main, "deduplicate_and_store", lambda _items: list(range(1, 13)))
    monkeypatch.setattr(
        main,
        "_score_daily_window",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("scorer failed")),
    )
    monkeypatch.setattr(
        main,
        "complete_pipeline_run",
        lambda _run_id, **values: completed.append(values),
    )
    monkeypatch.setattr("pipeline.collectors.apewisdom.collect_social_buzz", lambda: _async_value(0))
    monkeypatch.setattr("pipeline.processing.transcript.enrich_youtube_transcripts", lambda _ids: 0)
    monkeypatch.setattr("pipeline.processing.keyword_counter.count_keywords_for_items", lambda _ids: None)
    monkeypatch.setattr("pipeline.generators.keyword_suggestions.auto_manage_keywords", lambda **_kwargs: {})
    monkeypatch.setattr("pipeline.processing.trend_detector.detect_trends", lambda **_kwargs: [])
    monkeypatch.setattr("pipeline.delivery.discord_webhook.deliver_error_alert", lambda *_args: None)

    result = CliRunner().invoke(main.daily, ["--hours", "24"])

    assert isinstance(result.exception, RuntimeError)
    assert completed == [
        {
            "status": "failed",
            "items_collected": 12,
            "items_scored": 0,
            "errors": ["scorer failed"],
            "duration_secs": completed[0]["duration_secs"],
        }
    ]


def test_daily_scorer_selection_race_records_returned_persisted_count(monkeypatch) -> None:
    conn = _selection_connection()
    now = datetime.now(UTC)
    conn.executemany(
        "INSERT INTO raw_items (id, source, published_at, collected_at) VALUES (?, ?, ?, ?)",
        [
            (1, "arxiv", now.isoformat(), now.isoformat()),
            (2, "github", now.isoformat(), now.isoformat()),
            (3, "youtube", now.isoformat(), now.isoformat()),
        ],
    )
    completed: list[dict] = []

    async def collect(_keywords, _since):
        return [], []

    monkeypatch.setattr(main, "start_pipeline_run", lambda _kind: 101)
    monkeypatch.setattr(main, "get_active_keywords", list)
    monkeypatch.setattr(main, "get_connection", lambda: conn)
    monkeypatch.setattr(main, "_collect_all", collect)
    monkeypatch.setattr(main, "deduplicate_and_store", lambda _items: [])
    monkeypatch.setattr(
        scorer,
        "score_items",
        lambda item_ids, *, run_id: 2,
    )
    monkeypatch.setattr(
        main,
        "complete_pipeline_run",
        lambda _run_id, **values: completed.append(values),
    )
    monkeypatch.setattr("pipeline.collectors.apewisdom.collect_social_buzz", lambda: _async_value(0))
    monkeypatch.setattr("pipeline.processing.transcript.enrich_youtube_transcripts", lambda _ids: 0)
    monkeypatch.setattr("pipeline.processing.keyword_counter.count_keywords_for_items", lambda _ids: None)
    monkeypatch.setattr("pipeline.generators.keyword_suggestions.auto_manage_keywords", lambda **_kwargs: {})
    monkeypatch.setattr("pipeline.processing.trend_detector.detect_trends", lambda **_kwargs: [])
    monkeypatch.setattr("pipeline.delivery.discord_webhook.deliver_error_alert", lambda *_args: None)

    result = CliRunner().invoke(main.daily, ["--hours", "24"])

    assert isinstance(result.exception, RuntimeError)
    assert completed[0]["status"] == "failed"
    assert completed[0]["items_collected"] == 0
    assert completed[0]["items_scored"] == 2
    assert completed[0]["errors"] == ["daily scorer persisted 2 of 3 selected items"]


def test_daily_failure_before_collection_records_zero_counts(monkeypatch) -> None:
    completed: list[dict] = []

    async def collect(_keywords, _since):
        raise RuntimeError("collection failed")

    monkeypatch.setattr(main, "start_pipeline_run", lambda _kind: 99)
    monkeypatch.setattr(main, "get_active_keywords", list)
    monkeypatch.setattr(main, "_collect_all", collect)
    monkeypatch.setattr(
        main,
        "complete_pipeline_run",
        lambda _run_id, **values: completed.append(values),
    )
    monkeypatch.setattr("pipeline.delivery.discord_webhook.deliver_error_alert", lambda *_args: None)

    result = CliRunner().invoke(main.daily, ["--hours", "24"])

    assert isinstance(result.exception, RuntimeError)
    assert completed[0]["status"] == "failed"
    assert completed[0]["items_collected"] == 0
    assert completed[0]["items_scored"] == 0


async def _async_value(value):
    return value


def test_scorer_rejects_duplicate_ids_before_any_llm_call(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE raw_items (
            id INTEGER PRIMARY KEY,
            source TEXT NOT NULL,
            title TEXT NOT NULL,
            url TEXT,
            content_snippet TEXT,
            metadata TEXT
        );
        CREATE TABLE scored_items (
            id INTEGER PRIMARY KEY,
            raw_item_id INTEGER NOT NULL UNIQUE
        );
        INSERT INTO raw_items (id, source, title) VALUES (1, 'arxiv', 'one');
        """
    )
    monkeypatch.setattr(scorer, "get_connection", lambda: conn)
    monkeypatch.setattr(
        scorer.llm,
        "get_boundary",
        lambda: (_ for _ in ()).throw(AssertionError("LLM must not be called")),
    )

    with pytest.raises(ValueError, match="unique"):
        scorer.score_items([1, 1], run_id="duplicate-test")


def test_scorer_rejects_nonexistent_ids_before_any_llm_call(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE raw_items (
            id INTEGER PRIMARY KEY,
            source TEXT NOT NULL,
            title TEXT NOT NULL,
            url TEXT,
            content_snippet TEXT,
            metadata TEXT
        );
        CREATE TABLE scored_items (
            id INTEGER PRIMARY KEY,
            raw_item_id INTEGER NOT NULL UNIQUE
        );
        """
    )
    monkeypatch.setattr(scorer, "get_connection", lambda: conn)
    monkeypatch.setattr(
        scorer.llm,
        "get_boundary",
        lambda: (_ for _ in ()).throw(AssertionError("LLM must not be called")),
    )

    with pytest.raises(ValueError, match="do not exist"):
        scorer.score_items([999], run_id="missing-test")


def test_scorer_rejects_500001_unique_ids_before_opening_database(monkeypatch) -> None:
    monkeypatch.setattr(
        scorer,
        "get_connection",
        lambda: (_ for _ in ()).throw(AssertionError("database must not be queried")),
    )

    with pytest.raises(scorer.llm.RequestLimitExceededError, match="500001 items"):
        scorer.score_items(list(range(1, 500_002)), run_id="oversized-test")


@pytest.mark.parametrize("item_ids", [(1,), [1, "2"], [True]])
def test_scorer_rejects_invalid_item_id_input_types_before_database(
    monkeypatch, item_ids
) -> None:
    monkeypatch.setattr(
        scorer,
        "get_connection",
        lambda: (_ for _ in ()).throw(AssertionError("database must not be queried")),
    )

    with pytest.raises(TypeError, match="list of integers"):
        scorer.score_items(item_ids, run_id="type-test")
