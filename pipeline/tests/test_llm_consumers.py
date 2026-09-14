from __future__ import annotations

import sqlite3
from datetime import date
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from pipeline import db, llm, main
from pipeline.generators import company_analysis, conference_briefing, daily_digest, keyword_suggestions, thesis_scout
from pipeline.models import TrendAlert
from pipeline.processing import scorer, trend_detector


def scored_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE raw_items (
            id INTEGER PRIMARY KEY, source TEXT, title TEXT, url TEXT,
            content_snippet TEXT, metadata TEXT, published_at TEXT
        );
        CREATE TABLE scored_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            raw_item_id INTEGER NOT NULL UNIQUE,
            score INTEGER, score_reasoning TEXT, category TEXT, title_ko TEXT,
            related_tickers TEXT, model_used TEXT
        );
        """
    )
    return conn


def score(index: int) -> dict:
    return {
        "index": index,
        "score": 80,
        "reasoning": "reason",
        "category": "trend",
        "title_ko": "번역",
        "related_tickers": ["NVDA"],
    }


def test_scorer_consumes_parsed_structured_output_and_stages_without_inserts(monkeypatch) -> None:
    conn = scored_connection()
    conn.execute("INSERT INTO raw_items VALUES (1, 'rss', 'one', NULL, '', NULL, '2026-01-01')")
    conn.execute("INSERT INTO scored_items (raw_item_id) VALUES (1)")
    conn.commit()
    seen: list[dict] = []

    class Boundary:
        def complete(self, **kwargs):
            seen.append(kwargs)
            return SimpleNamespace(parsed={"scores": [score(1)]}, text="not json")

    monkeypatch.setattr(scorer.llm, "get_boundary", lambda: Boundary())
    staged = scorer._score_batch(
        "system", [conn.execute("SELECT * FROM raw_items WHERE id=1").fetchone()], run_id="test-run"
    )

    assert len(staged) == 1
    assert conn.execute("SELECT COUNT(*) FROM scored_items").fetchone()[0] == 1
    assert seen[0]["workload"] == "scoring"
    item_schema = seen[0]["output_schema"]["schema"]["properties"]["scores"]
    assert item_schema["minItems"] == item_schema["maxItems"] == 1
    assert item_schema["items"]["additionalProperties"] is False


def test_scorer_rejects_duplicate_or_missing_indices_without_writes(monkeypatch) -> None:
    conn = scored_connection()
    conn.executemany(
        "INSERT INTO raw_items VALUES (?, 'rss', ?, NULL, '', NULL, '2026-01-01')",
        [(1, "one"), (2, "two")],
    )
    conn.commit()

    class Boundary:
        def complete(self, **_kwargs):
            return SimpleNamespace(parsed={"scores": [score(1), score(1)]}, text="")

    monkeypatch.setattr(scorer.llm, "get_boundary", lambda: Boundary())
    rows = conn.execute("SELECT * FROM raw_items ORDER BY id").fetchall()
    with pytest.raises(llm.LLMParseError):
        scorer._score_batch("system", rows, run_id="test-run")
    assert conn.execute("SELECT COUNT(*) FROM scored_items").fetchone()[0] == 0


def test_multi_batch_scoring_rolls_back_first_batch_when_later_batch_fails(monkeypatch) -> None:
    conn = scored_connection()
    conn.executemany(
        "INSERT INTO raw_items VALUES (?, 'rss', ?, NULL, '', NULL, '2026-01-01')",
        [(1, "one"), (2, "two")],
    )
    conn.commit()
    calls = 0

    class Boundary:
        def complete(self, **_kwargs):
            nonlocal calls
            assert not conn.in_transaction
            calls += 1
            if calls == 2:
                raise llm.LLMProviderError("failed")
            return SimpleNamespace(parsed={"scores": [score(1)]}, text="")

    monkeypatch.setattr(scorer, "get_connection", lambda: conn)
    monkeypatch.setattr(scorer, "_load_scoring_prompt", lambda: "system")
    monkeypatch.setattr(scorer, "BATCH_SIZE", 1)
    monkeypatch.setattr(scorer.llm, "get_boundary", lambda: Boundary())

    with pytest.raises(llm.LLMProviderError):
        scorer.score_items([1, 2], run_id="test-run")
    assert conn.execute("SELECT COUNT(*) FROM scored_items").fetchone()[0] == 0


def test_scoring_rolls_back_all_staged_inserts_when_any_insert_is_ignored(monkeypatch) -> None:
    conn = scored_connection()
    conn.executemany(
        "INSERT INTO raw_items VALUES (?, 'rss', ?, NULL, '', NULL, '2026-01-01')",
        [(1, "one"), (2, "two")],
    )
    conn.commit()

    class Boundary:
        def complete(self, **_kwargs):
            conn.execute("INSERT INTO scored_items (raw_item_id) VALUES (2)")
            conn.commit()
            return SimpleNamespace(parsed={"scores": [score(1), score(2)]}, text="")

    monkeypatch.setattr(scorer, "get_connection", lambda: conn)
    monkeypatch.setattr(scorer, "_load_scoring_prompt", lambda: "system")
    monkeypatch.setattr(scorer.llm, "get_boundary", lambda: Boundary())

    with pytest.raises(llm.LLMOutputError, match="insert exactly one"):
        scorer.score_items([1, 2], run_id="test-run")
    assert [row[0] for row in conn.execute(
        "SELECT raw_item_id FROM scored_items ORDER BY raw_item_id"
    ).fetchall()] == [2]


def test_keyword_evaluator_uses_typed_parsed_output_and_rejects_unknown_candidates(monkeypatch) -> None:
    seen: list[dict] = []

    class Boundary:
        def complete(self, **kwargs):
            seen.append(kwargs)
            return SimpleNamespace(
                parsed={"keywords": [{"keyword": "Known", "category": "company", "reason": "근거"}]},
                text="not json",
            )

    monkeypatch.setattr(keyword_suggestions.llm, "get_boundary", lambda: Boundary())
    assert keyword_suggestions._evaluate_with_llm([("Known", 3)], set(), run_id="test-run") == [
        {"keyword": "Known", "category": "company", "reason": "근거"}
    ]
    assert seen[0]["workload"] == "keyword_discovery"
    schema = seen[0]["output_schema"]["schema"]["properties"]["keywords"]
    assert schema["maxItems"] == 1
    assert schema["items"]["properties"]["category"]["enum"]

    class BadBoundary:
        def complete(self, **_kwargs):
            return SimpleNamespace(
                parsed={"keywords": [{"keyword": "Invented", "category": "company", "reason": "근거"}]},
                text="",
            )

    monkeypatch.setattr(keyword_suggestions.llm, "get_boundary", lambda: BadBoundary())
    with pytest.raises(llm.LLMParseError):
        keyword_suggestions._evaluate_with_llm([("Known", 3)], set(), run_id="test-run")


def test_translation_rolls_back_all_batches_on_failure_and_uses_parsed(monkeypatch) -> None:
    conn = scored_connection()
    conn.executemany(
        "INSERT INTO raw_items VALUES (?, 'rss', ?, NULL, '', NULL, '2026-01-01')",
        [(i, f"title {i}") for i in range(1, 32)],
    )
    conn.executemany("INSERT INTO scored_items (raw_item_id) VALUES (?)", [(i,) for i in range(1, 32)])
    conn.commit()
    calls = 0

    class Boundary:
        def complete(self, **kwargs):
            nonlocal calls
            assert not conn.in_transaction
            calls += 1
            if calls == 2:
                raise llm.LLMProviderError("failed")
            assert kwargs["workload"] == "title_translation"
            assert kwargs["output_schema"]["schema"]["properties"]["translations"]["maxItems"] == 30
            return SimpleNamespace(
                parsed={"translations": [{"index": i, "title_ko": f"번역 {i}"} for i in range(1, 31)]},
                text="invalid",
            )

    monkeypatch.setattr(db, "get_connection", lambda: conn)
    monkeypatch.setattr(main.llm, "get_boundary", lambda: Boundary())
    result = CliRunner().invoke(main.translate_titles, ["--batch-limit", "31"])

    assert result.exception is not None
    assert conn.execute("SELECT COUNT(*) FROM scored_items WHERE title_ko IS NOT NULL").fetchone()[0] == 0


def test_translation_rolls_back_all_updates_when_any_target_disappears(monkeypatch) -> None:
    conn = scored_connection()
    conn.executemany(
        "INSERT INTO raw_items VALUES (?, 'rss', ?, NULL, '', NULL, '2026-01-01')",
        [(1, "one"), (2, "two")],
    )
    conn.executemany("INSERT INTO scored_items (raw_item_id) VALUES (?)", [(1,), (2,)])
    conn.commit()

    class Boundary:
        def complete(self, **_kwargs):
            conn.execute("DELETE FROM scored_items WHERE raw_item_id = 2")
            conn.commit()
            return SimpleNamespace(parsed={"translations": [
                {"index": 1, "title_ko": "하나"}, {"index": 2, "title_ko": "둘"},
            ]}, text="")

    monkeypatch.setattr(db, "get_connection", lambda: conn)
    monkeypatch.setattr(main.llm, "get_boundary", lambda: Boundary())
    result = CliRunner().invoke(main.translate_titles, ["--batch-limit", "2"])

    assert isinstance(result.exception, llm.LLMOutputError)
    assert conn.execute("SELECT title_ko FROM scored_items WHERE raw_item_id = 1").fetchone()[0] is None


def test_thesis_runtime_requires_both_directions_and_exact_unique_supplied_signals() -> None:
    def thesis(direction: str, number: int, driving: list[str]) -> dict:
        return {"direction": direction, "company": f"C{number}", "ticker": f"T{number}",
                "market": "US", "bottleneck": "b", "reasoning": "r", "depth_layer": 1,
                "pricing_status": "partial", "conviction": "high", "falsifier": "f",
                "driving_signals": driving}

    valid = [thesis("buy", i, ["signal-a"]) for i in range(3)] + [
        thesis("avoid", i + 3, ["signal-b"]) for i in range(3)
    ]
    thesis_scout._validate_theses(valid, {"signal-a", "signal-b"})

    with pytest.raises(llm.LLMParseError):
        thesis_scout._validate_theses(valid[:-1], {"signal-a", "signal-b"})
    with pytest.raises(llm.LLMParseError):
        thesis_scout._validate_theses(
            [*valid[:-1], thesis("avoid", 9, ["invented", "invented"])],
            {"signal-a", "signal-b"},
        )

    duplicate = thesis("buy", 99, ["signal-a"])
    duplicate.update(company=" c0 ", ticker=" t0 ", bottleneck=" B ")
    with pytest.raises(llm.LLMParseError, match="duplicate buy thesis"):
        thesis_scout._validate_theses([*valid, duplicate], {"signal-a", "signal-b"})


def test_trend_interpretation_uses_parsed_and_requires_exact_keyword_coverage(monkeypatch) -> None:
    alerts = [
        TrendAlert(
            keyword="AI", alert_date="2026-09-01", z_score=3, severity="urgent",
            moving_avg_7d=1, moving_avg_30d=1, std_dev_30d=1, today_count=4,
        ),
        TrendAlert(
            keyword="chips", alert_date="2026-09-01", z_score=2, severity="notable",
            moving_avg_7d=1, moving_avg_30d=1, std_dev_30d=1, today_count=3,
        ),
    ]

    class Connection:
        def __init__(self):
            self.updates = []
            self.in_transaction = False

        def execute(self, sql, params):
            if sql.lstrip().startswith("SELECT"):
                return SimpleNamespace(fetchall=lambda: [])
            self.updates.append(params)

    class Boundary:
        def complete(self, **kwargs):
            assert kwargs["workload"] == "trend_interpretation"
            return SimpleNamespace(parsed={"interpretations": [
                {"keyword": "AI", "interpretation": "a"},
                {"keyword": "chips", "interpretation": "b"},
            ]}, text="invalid")

    conn = Connection()
    monkeypatch.setattr(trend_detector.llm, "get_boundary", lambda: Boundary())
    trend_detector._interpret_alerts(alerts, conn=conn, run_id="test-run")
    assert [alert.llm_interpretation for alert in alerts] == ["a", "b"]
    assert len(conn.updates) == 2


def test_detect_trends_merges_duplicate_spike_and_acceleration_before_llm_and_db(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """CREATE TABLE keyword_daily_aggregates (
               keyword TEXT, mention_date TEXT, total_count INTEGER
           );
           CREATE TABLE trend_alerts (
               keyword TEXT, alert_date TEXT, z_score REAL, severity TEXT,
               moving_avg_7d REAL, moving_avg_30d REAL, std_dev_30d REAL,
               today_count INTEGER, llm_interpretation TEXT,
               UNIQUE(keyword, alert_date)
           );"""
    )
    conn.executemany(
        "INSERT INTO keyword_daily_aggregates VALUES ('AI', ?, ?)",
        [(f"2026-09-{day:02d}", count) for day, count in zip(range(1, 8), [1, 1, 1, 1, 1, 1, 10])],
    )
    conn.commit()
    acceleration = TrendAlert(
        keyword="AI", alert_date="2026-09-07", z_score=4, severity="accelerating",
        moving_avg_7d=3, moving_avg_30d=2, std_dev_30d=1, today_count=10,
    )
    interpreted: list[str] = []

    monkeypatch.setattr(trend_detector, "get_connection", lambda: conn)
    monkeypatch.setattr(trend_detector, "get_active_keywords", lambda _conn: ["AI"])
    monkeypatch.setattr(trend_detector, "detect_long_term_acceleration", lambda _date: [acceleration])

    def interpret(alerts, **_kwargs):
        interpreted.extend(alert.keyword for alert in alerts)
        return {"AI": "merged"}

    monkeypatch.setattr(trend_detector, "_interpret_alerts", interpret)
    alerts = trend_detector.detect_trends(date(2026, 9, 7), run_id="test-run")

    assert len(alerts) == 1
    assert interpreted == ["AI"]
    assert conn.execute("SELECT COUNT(*) FROM trend_alerts WHERE keyword='AI'").fetchone()[0] == 1


def test_all_generator_contracts_are_recursively_strict_and_typed() -> None:
    schemas = [
        scorer._score_schema(2),
        company_analysis._analysis_schema(),
        conference_briefing._pre_event_schema(),
        conference_briefing._post_event_schema(),
        daily_digest._digest_schema(3),
        {"name": "investment_theses", "schema": thesis_scout.SCOUT_TOOL["input_schema"]},
    ]
    for schema in schemas:
        formatted = llm.CodexOAuthBoundary._structured_format(schema)
        assert formatted["strict"] is True
