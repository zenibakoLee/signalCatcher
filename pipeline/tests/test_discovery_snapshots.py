from __future__ import annotations

import json
import sqlite3

from pipeline import db
from pipeline.main import write_current_discovery_snapshots
from pipeline.processing.discovery_snapshots import (
    build_candidate_snapshot,
    build_theme_snapshot,
    persist_discovery_snapshots,
    write_discovery_snapshots,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db._SCHEMA)
    return conn


def test_schema_is_additive_and_snapshot_tables_have_provenance() -> None:
    conn = _conn()
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {"themes", "theme_snapshots", "candidate_snapshots"} <= tables
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(candidate_snapshots)")
    }
    assert {"as_of", "pipeline_run_id", "feature_version", "feature_values", "status"} <= columns
    assert "investment_theses" in tables


def test_theme_score_is_deterministic_with_sufficient_independent_evidence() -> None:
    source = {
        "keyword": "advanced cooling",
        "z_score": 4.0,
        "raw_item_ids": [11, 12, 13],
        "sources": ["rss", "arxiv", "github"],
        "as_of": "2026-08-01",
    }
    first = build_theme_snapshot(source, pipeline_run_id=7)
    second = build_theme_snapshot(source, pipeline_run_id=7)

    assert first == second
    assert first["status"] == "scored"
    assert first["score"] is not None
    assert first["signal_kind"] == "emerging_signal"
    assert first["feature_values"]["source_breadth"] == 3
    assert first["evidence_raw_item_ids"] == [11, 12, 13]


def test_theme_has_no_score_when_evidence_coverage_is_insufficient() -> None:
    snapshot = build_theme_snapshot(
        {
            "keyword": "unproven theme",
            "z_score": 4.0,
            "raw_item_ids": [11],
            "sources": ["rss"],
            "as_of": "2026-08-01",
        },
        pipeline_run_id=7,
    )

    assert snapshot["status"] == "insufficient_evidence"
    assert snapshot["score"] is None
    assert snapshot["rank"] is None


def test_candidate_requires_verified_us_listing_and_complete_evidence() -> None:
    base = {
        "ticker": "ACME",
        "market": "US",
        "market_verified": True,
        "hypothesis_category": "buy",
        "thesis_id": 4,
        "company_analysis_id": 8,
        "raw_item_ids": [1, 2, 3],
        "sources": ["rss", "arxiv", "github"],
        "as_of": "2026-08-01",
        "theme_exposure": 80,
        "revenue_evidence": 70,
        "bottleneck_evidence": 60,
        "pricing_unreflected": 50,
        "attention_acceleration": 40,
        "source_confirmation": 75,
        "liquidity_listing": 100,
    }
    scored = build_candidate_snapshot(base, pipeline_run_id=7)
    assert scored["status"] == "scored"
    assert scored["score"] is not None
    assert scored["rank"] is None

    unverified = build_candidate_snapshot({**base, "market_verified": False}, pipeline_run_id=7)
    assert unverified["status"] == "unverified_us_listing"
    assert unverified["score"] is None

    non_us = build_candidate_snapshot({**base, "market": "KR"}, pipeline_run_id=7)
    assert non_us["status"] == "ineligible_market"
    assert non_us["score"] is None


def test_persisted_snapshots_keep_evidence_links_and_no_rank_for_unscored() -> None:
    conn = _conn()
    conn.execute("INSERT INTO pipeline_runs (run_type, started_at) VALUES ('daily', '2026-08-01T00:00:00+00:00')")
    conn.execute("INSERT INTO raw_items (source, source_id, title, published_at) VALUES ('rss', 'a', 'A', '2026-08-01T00:00:00+00:00')")
    conn.execute("INSERT INTO raw_items (source, source_id, title, published_at) VALUES ('arxiv', 'b', 'B', '2026-08-01T00:00:00+00:00')")
    conn.execute("INSERT INTO raw_items (source, source_id, title, published_at) VALUES ('github', 'c', 'C', '2026-08-01T00:00:00+00:00')")
    conn.commit()
    theme = build_theme_snapshot({"keyword": "cooling", "z_score": 3.0, "raw_item_ids": [1, 2, 3], "sources": ["rss", "arxiv", "github"], "as_of": "2026-08-01"}, pipeline_run_id=1)
    candidate = build_candidate_snapshot({"ticker": "ACME", "market": "US", "market_verified": False, "hypothesis_category": "avoid", "thesis_id": None, "company_analysis_id": None, "raw_item_ids": [1, 2], "sources": ["rss", "arxiv"], "as_of": "2026-08-01"}, pipeline_run_id=1)

    persist_discovery_snapshots(conn, [theme], [candidate])
    row = conn.execute("SELECT score, rank, evidence_raw_item_ids, hypothesis_category FROM candidate_snapshots").fetchone()
    assert row["score"] is None
    assert row["rank"] is None
    assert json.loads(row["evidence_raw_item_ids"]) == [1, 2]
    assert row["hypothesis_category"] == "avoid"


def test_daily_flow_snapshot_helper_uses_the_current_run_connection(monkeypatch) -> None:
    import pipeline.main as main

    conn = _conn()
    captured = {}

    def fake_writer(actual_conn, pipeline_run_id, as_of=None):
        captured.update(conn=actual_conn, pipeline_run_id=pipeline_run_id, as_of=as_of)
        return {"themes": 1, "candidates": 0}

    monkeypatch.setattr(main, "get_connection", lambda: conn)
    monkeypatch.setattr("pipeline.processing.discovery_snapshots.write_discovery_snapshots", fake_writer)
    assert write_current_discovery_snapshots(7) == {"themes": 1, "candidates": 0}
    assert captured == {"conn": conn, "pipeline_run_id": 7, "as_of": None}


def test_database_writer_preserves_available_thesis_company_and_raw_links() -> None:
    conn = _conn()
    conn.execute("INSERT INTO pipeline_runs (run_type, started_at) VALUES ('daily', '2026-08-01T00:00:00+00:00')")
    conn.execute("INSERT INTO raw_items (source, source_id, title, published_at) VALUES ('rss', 'evidence', 'Contract evidence', '2026-08-01T00:00:00+00:00')")
    conn.execute("""INSERT INTO investment_theses (thesis_date, direction, company, ticker, market, reasoning, model_used, driving_signals)
                    VALUES ('2026-08-01', 'buy', 'Acme', 'ACME', 'US', 'reason', 'legacy-model', '[\"Contract evidence\"]')""")
    conn.execute("""INSERT INTO company_analyses (ticker, company_name, momentum_score, verdict, verdict_summary, five_questions, signal_timeline, risk_factors, key_signals_json, model_used)
                    VALUES ('ACME', 'Acme', 10, 'watch', 'summary', '[]', '[]', '[]', '{}', 'legacy-model')""")
    conn.commit()

    assert write_discovery_snapshots(conn, pipeline_run_id=1, as_of="2026-08-01") == {"themes": 0, "candidates": 1}
    row = conn.execute("SELECT thesis_id, company_analysis_id, evidence_raw_item_ids, status FROM candidate_snapshots").fetchone()
    assert row["thesis_id"] == 1
    assert row["company_analysis_id"] == 1
    assert json.loads(row["evidence_raw_item_ids"]) == [1]
    assert row["status"] == "unverified_us_listing"


def test_database_reader_is_read_only_when_no_pipeline_run_is_provided() -> None:
    conn = _conn()
    before = conn.total_changes
    assert write_discovery_snapshots(conn, pipeline_run_id=None, as_of="2026-08-01") == {"themes": 0, "candidates": 0}
    assert conn.total_changes == before
