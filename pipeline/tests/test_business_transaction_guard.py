from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from pipeline import db, llm, main
from pipeline.generators import company_analysis, conference_briefing, daily_digest, keyword_suggestions, thesis_scout
from pipeline.models import TrendAlert
from pipeline.processing import scorer, trend_detector


def connection(schema: str = "") -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    if schema:
        conn.executescript(schema)
    return conn


class CompletionSpy:
    def __init__(self, conn: sqlite3.Connection, parsed: dict):
        self.conn = conn
        self.parsed = parsed
        self.calls = 0

    def complete(self, **_kwargs):
        self.calls += 1
        assert not self.conn.in_transaction
        return SimpleNamespace(parsed=self.parsed, text="")


def assert_refuses_before_completion(conn, spy, invoke) -> None:
    conn.execute("BEGIN")
    with pytest.raises(llm.BusinessTransactionActiveError):
        invoke()
    assert spy.calls == 0
    assert conn.in_transaction
    conn.rollback()


def test_scorer_refuses_caller_transaction_before_completion(monkeypatch) -> None:
    conn = connection("""
        CREATE TABLE raw_items (id INTEGER PRIMARY KEY, source TEXT, title TEXT, url TEXT,
            content_snippet TEXT, metadata TEXT);
        CREATE TABLE scored_items (raw_item_id INTEGER UNIQUE, score INTEGER, score_reasoning TEXT,
            category TEXT, title_ko TEXT, related_tickers TEXT, model_used TEXT);
        INSERT INTO raw_items VALUES (1, 'rss', 'one', NULL, '', NULL);
    """)
    spy = CompletionSpy(conn, {"scores": []})
    monkeypatch.setattr(scorer, "get_connection", lambda: conn)
    monkeypatch.setattr(scorer, "_load_scoring_prompt", lambda: "prompt")
    monkeypatch.setattr(scorer.llm, "get_boundary", lambda: spy)
    assert_refuses_before_completion(conn, spy, lambda: scorer.score_items([1], run_id="run"))


def test_keyword_evaluation_refuses_supplied_transaction_before_completion(monkeypatch) -> None:
    conn = connection()
    spy = CompletionSpy(conn, {"keywords": []})
    monkeypatch.setattr(keyword_suggestions.llm, "get_boundary", lambda: spy)
    assert_refuses_before_completion(
        conn, spy,
        lambda: keyword_suggestions._evaluate_with_llm([("Known", 3)], set(), conn=conn, run_id="run"),
    )


def test_trend_interpretation_refuses_supplied_transaction_before_completion(monkeypatch) -> None:
    conn = connection("""
        CREATE TABLE raw_items (id INTEGER PRIMARY KEY, title TEXT);
        CREATE TABLE keyword_mentions (keyword TEXT, mention_date TEXT, sample_item_ids TEXT);
        CREATE TABLE trend_alerts (keyword TEXT, alert_date TEXT, llm_interpretation TEXT);
    """)
    alert = TrendAlert(keyword="AI", alert_date="2026-09-01", z_score=3, severity="urgent",
        moving_avg_7d=1, moving_avg_30d=1, std_dev_30d=1, today_count=4)
    spy = CompletionSpy(conn, {"interpretations": []})
    monkeypatch.setattr(trend_detector.llm, "get_boundary", lambda: spy)
    assert_refuses_before_completion(
        conn, spy, lambda: trend_detector._interpret_alerts([alert], conn=conn, run_id="run", persist=False)
    )


@pytest.mark.parametrize("generator", ["pre", "post"])
def test_conference_paths_refuse_supplied_transaction_before_completion(monkeypatch, generator) -> None:
    conn = connection("""
        CREATE TABLE conference_briefings (
            conference_name TEXT, conference_start TEXT, briefing_type TEXT, expected_items TEXT
        );
    """)
    conf = {"name": "Conf", "start_date": "2026-09-01", "end_date": "2026-09-02",
            "expected_topics": [], "search_terms": []}
    spy = CompletionSpy(conn, {})
    monkeypatch.setattr(conference_briefing.llm, "get_boundary", lambda: spy)
    monkeypatch.setattr(conference_briefing, "_gather_recent_signals", lambda *_args: "")
    monkeypatch.setattr(conference_briefing, "_gather_conference_items", lambda *_args: "")
    monkeypatch.setattr(conference_briefing, "_get_conference_item_ids", lambda *_args: [])
    if generator == "pre":
        invoke = lambda: conference_briefing.generate_pre_event(
            conf, conn=conn, persist=False, run_id="run"
        )
    else:
        invoke = lambda: conference_briefing.generate_post_event(
            conf, conn=conn, persist=False, run_id="run"
        )
    assert_refuses_before_completion(conn, spy, invoke)


def test_company_analysis_refuses_before_http_or_completion(monkeypatch) -> None:
    conn = connection()
    remote_calls = []
    spy = CompletionSpy(conn, {})

    class Client:
        def __init__(self, **_kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def get(self, *_args, **_kwargs): remote_calls.append("http")

    monkeypatch.setattr(company_analysis.httpx, "Client", Client)
    monkeypatch.setattr(company_analysis.llm, "get_boundary", lambda: spy)
    candidate = {"ticker": "NVDA", "signals": [], "buzz_mentions": 0, "avg_score": 0}
    assert_refuses_before_completion(
        conn, spy, lambda: company_analysis.generate_analysis(candidate, conn=conn, persist=False, run_id="run")
    )
    assert remote_calls == []


def test_title_translation_refuses_process_global_transaction_before_completion(monkeypatch) -> None:
    conn = connection("""
        CREATE TABLE raw_items (id INTEGER PRIMARY KEY, title TEXT);
        CREATE TABLE scored_items (id INTEGER PRIMARY KEY, raw_item_id INTEGER, title_ko TEXT);
        INSERT INTO raw_items VALUES (1, 'one');
        INSERT INTO scored_items VALUES (1, 1, NULL);
    """)
    spy = CompletionSpy(conn, {"translations": []})
    monkeypatch.setattr(db, "get_connection", lambda: conn)
    monkeypatch.setattr(main.llm, "get_boundary", lambda: spy)
    conn.execute("BEGIN")
    result = CliRunner().invoke(main.translate_titles, ["--batch-limit", "1"])
    assert isinstance(result.exception, llm.BusinessTransactionActiveError)
    assert spy.calls == 0
    assert conn.in_transaction
    conn.rollback()


def test_thesis_writer_refuses_process_global_transaction_before_completion(monkeypatch) -> None:
    conn = connection()
    signals = [{"score": 90, "source": "rss", "category": "trend", "title": f"signal-{i}",
                "score_reasoning": "reason"} for i in range(5)]
    spy = CompletionSpy(conn, {})
    monkeypatch.setattr(thesis_scout, "get_connection", lambda: conn)
    monkeypatch.setattr(thesis_scout, "_gather_signals", lambda *_args: (signals, None))
    monkeypatch.setattr(thesis_scout.llm, "get_boundary", lambda: spy)
    assert_refuses_before_completion(
        conn, spy, lambda: thesis_scout.run_thesis_scout(run_id="run")
    )


def test_digest_refuses_process_global_transaction_before_completion(monkeypatch) -> None:
    conn = connection("""
        CREATE TABLE digests (id INTEGER PRIMARY KEY, digest_date TEXT);
        CREATE TABLE raw_items (id INTEGER PRIMARY KEY, title TEXT, url TEXT, source TEXT,
            content_snippet TEXT, collected_at TEXT);
        CREATE TABLE scored_items (raw_item_id INTEGER, score INTEGER, score_reasoning TEXT, category TEXT);
        CREATE TABLE trend_alerts (keyword TEXT, z_score REAL, severity TEXT, today_count INTEGER,
            moving_avg_30d REAL, alert_date TEXT);
        INSERT INTO raw_items VALUES (1, 'one', '', 'rss', '', '2026-08-31T16:00:00');
        INSERT INTO scored_items VALUES (1, 90, 'reason', 'trend');
    """)
    spy = CompletionSpy(conn, {})
    monkeypatch.setattr(daily_digest, "get_connection", lambda: conn)
    monkeypatch.setattr(daily_digest, "_detect_topic_clusters", lambda *_args: [])
    monkeypatch.setattr(daily_digest.llm, "get_boundary", lambda: spy)
    assert_refuses_before_completion(
        conn, spy,
        lambda: daily_digest.generate_digest(__import__("datetime").date(2026, 9, 1), run_id="run"),
    )


def test_company_multi_call_batch_is_staged_and_write_failure_rolls_back(monkeypatch) -> None:
    conn = connection("CREATE TABLE saved (ticker TEXT)")
    candidates = [
        {"ticker": ticker, "signals": [], "signal_count": 0, "avg_score": 0, "buzz_mentions": 0}
        for ticker in ("ONE", "TWO")
    ]
    completed = []
    writes = 0

    def generate(candidate, **_kwargs):
        assert not conn.in_transaction
        assert conn.execute("SELECT COUNT(*) FROM saved").fetchone()[0] == 0
        completed.append(candidate["ticker"])
        return {"_web_articles": [], "company_name": candidate["ticker"]}

    def persist(_conn, candidate, _data, _articles):
        nonlocal writes
        assert completed == ["ONE", "TWO"]
        writes += 1
        _conn.execute("INSERT INTO saved VALUES (?)", (candidate["ticker"],))
        if writes == 2:
            raise sqlite3.IntegrityError("staged batch write failed")

    monkeypatch.setattr(company_analysis, "find_momentum_candidates", lambda: candidates)
    monkeypatch.setattr(company_analysis, "get_connection", lambda: conn)
    monkeypatch.setattr(company_analysis, "generate_analysis", generate)
    monkeypatch.setattr(company_analysis, "_persist_analysis", persist)
    with pytest.raises(sqlite3.IntegrityError):
        company_analysis.run_company_analyses(run_id="run")
    assert completed == ["ONE", "TWO"]
    assert conn.execute("SELECT COUNT(*) FROM saved").fetchone()[0] == 0


def test_conference_multi_call_batch_is_staged_and_write_failure_rolls_back(monkeypatch) -> None:
    conn = connection("CREATE TABLE saved (name TEXT)")
    conferences = [
        {"name": name, "start_date": "2026-09-01", "end_date": "2026-09-02"}
        for name in ("ONE", "TWO")
    ]
    completed = []
    writes = 0

    def generate(conf, **_kwargs):
        assert not conn.in_transaction
        assert conn.execute("SELECT COUNT(*) FROM saved").fetchone()[0] == 0
        completed.append(conf["name"])
        return {}

    def persist(_conn, conf, _data):
        nonlocal writes
        assert completed == ["ONE", "TWO"]
        writes += 1
        _conn.execute("INSERT INTO saved VALUES (?)", (conf["name"],))
        if writes == 2:
            raise sqlite3.IntegrityError("staged batch write failed")

    monkeypatch.setattr(conference_briefing, "get_actionable_conferences", lambda _target: {
        "pre_event": conferences, "post_event": [],
    })
    monkeypatch.setattr(conference_briefing, "generate_pre_event", generate)
    monkeypatch.setattr(conference_briefing, "_persist_pre_event", persist)
    monkeypatch.setattr(main, "get_connection", lambda: conn)
    result = CliRunner().invoke(main.event, ["--target-date", "2026-08-30"])
    assert isinstance(result.exception, sqlite3.IntegrityError)
    assert completed == ["ONE", "TWO"]
    assert conn.execute("SELECT COUNT(*) FROM saved").fetchone()[0] == 0
