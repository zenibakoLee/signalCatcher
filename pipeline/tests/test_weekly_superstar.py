from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from pipeline import db, llm, main
from pipeline.generators import superstar_weekly


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db._SCHEMA)
    db._migrate_weekly_superstar(conn)
    return conn


def _seed_evidence(conn: sqlite3.Connection) -> None:
    rows = [
        (1, "rss", "wedge", "2026-09-10T00:00:00+00:00", 91),
        (2, "sec_form4", "adjacent", "2026-09-11T00:00:00+00:00", 90),
        (3, "arxiv", "workflow data", "2026-09-12T00:00:00+00:00", 89),
        (4, "github", "developer ecosystem", "2026-09-13T00:00:00+00:00", 88),
        (5, "youtube", "switching costs rise", "2026-09-14T00:00:00+00:00", 87),
        (6, "standards_body", "de facto standard", "2026-09-15T00:00:00+00:00", 86),
    ]
    for raw_id, source, title, published_at, score in rows:
        conn.execute(
            """INSERT INTO raw_items
               (id, source, source_id, title, url, content_snippet, published_at, collected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (raw_id, source, str(raw_id), title, f"https://example.com/{raw_id}", title, published_at, published_at),
        )
        conn.execute(
            """INSERT INTO scored_items
               (raw_item_id, score, score_reasoning, category, model_used)
               VALUES (?, ?, 'reason', 'platform', 'fixture')""",
            (raw_id, score),
        )
    conn.commit()


def _citations(*ids: int) -> list[dict[str, object]]:
    return [
        {"raw_item_id": raw_id, "source_date": f"2026-09-{raw_id + 9:02d}"}
        for raw_id in ids
    ]


def _terra_candidate(*, missing: str | None = None, duplicate_id: bool = False) -> dict:
    ids = [1, 2, 3, 4, 5, 6]
    stages = []
    for stage, raw_id in zip(superstar_weekly.PLATFORM_STAGES, ids, strict=True):
        status = "missing" if stage == missing else "proven"
        citation_id = 1 if duplicate_id and stage == "adjacent_products_attach" else raw_id
        stages.append({
            "stage": stage,
            "status": status,
            "claim": "" if status == "missing" else f"exactly bounded claim {stage}",
            "citations": [] if status == "missing" else _citations(citation_id),
        })
    return {
        "ticker": "ETN",
        "stages": stages,
        "filter_audit": {
            "industry_track": "other_unclassified",
            "classification": "unclassified",
            "filter_findings": [],
        },
    }


def _validate_synthesis(payload: dict, evidence: list[dict], **kwargs) -> list[dict]:
    return superstar_weekly.validate_synthesis(
        payload,
        evidence,
        company_by_ticker={"ETN": "Eaton Corp plc"},
        **kwargs,
    )


def _verified_security() -> dict[str, object]:
    return {
        "ticker": "ETN",
        "company_name": "Eaton Corp plc",
        "cik": 1551182,
        "exchange": "NYSE",
        "listing_status": "verified_operating_issuer_sole_exchange_ticker_periodic",
        "verification_source_url": "https://data.sec.gov/submissions/CIK0001551182.json",
        "verification_as_of": "2026-08-01T12:00:00.000Z",
        "source_url": "https://www.sec.gov/files/company_tickers_exchange.json",
        "source_as_of": "Fri, 11 Sep 2026 12:34:56 GMT",
        "fetched_at": "2026-09-14T00:00:00+00:00",
        "source_status": "current",
    }


def test_weekly_schema_is_additive_and_records_stage_provenance() -> None:
    conn = _conn()
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"weekly_superstar_snapshots", "weekly_superstar_stage_evidence"} <= tables
    run_columns = {row[1] for row in conn.execute("PRAGMA table_info(pipeline_runs)")}
    assert {"input_items", "candidates_considered", "candidates_published"} <= run_columns
    snapshot_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(weekly_superstar_snapshots)")
    }
    assert {"coverage_ratio", "missing_evidence"} <= snapshot_columns

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO weekly_superstar_stage_evidence
               (snapshot_id, stage, status, claim)
               VALUES (999, 'switching_costs_standard', 'missing', '')"""
        )


def test_weekly_migration_upgrades_five_stage_schema_idempotently() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript("""
        CREATE TABLE pipeline_runs (id INTEGER PRIMARY KEY);
        CREATE TABLE raw_items (id INTEGER PRIMARY KEY);
        CREATE TABLE security_master (id INTEGER PRIMARY KEY);
        CREATE TABLE weekly_superstar_snapshots (
            id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL,
            company TEXT NOT NULL,
            as_of TEXT NOT NULL,
            pipeline_run_id INTEGER NOT NULL,
            feature_version TEXT NOT NULL,
            status TEXT NOT NULL,
            security_master_id INTEGER,
            missing_stages TEXT NOT NULL,
            source_count INTEGER NOT NULL,
            score REAL,
            rank INTEGER,
            created_at TEXT NOT NULL,
            UNIQUE(ticker, as_of, pipeline_run_id, feature_version)
        );
        CREATE TABLE weekly_superstar_stage_evidence (
            id INTEGER PRIMARY KEY,
            snapshot_id INTEGER NOT NULL,
            stage TEXT NOT NULL CHECK(stage IN (
                'wedge_product', 'adjacent_products_attach',
                'customer_data_workflow_accumulates', 'third_party_developers_join',
                'switching_costs_standard'
            )),
            status TEXT NOT NULL,
            claim TEXT NOT NULL,
            raw_item_id INTEGER,
            source TEXT,
            source_date TEXT
        );
    """)

    conn.execute("INSERT INTO pipeline_runs (id) VALUES (1)")
    conn.execute("INSERT INTO raw_items (id) VALUES (41)")
    conn.execute(
        """INSERT INTO weekly_superstar_snapshots
           (id, ticker, company, as_of, pipeline_run_id, feature_version, status,
            security_master_id, missing_stages, source_count, score, rank, created_at)
           VALUES (7, 'ETN', 'Eaton', '2026-09-15', 1,
                   'weekly-superstar-platform-v1', 'qualified', NULL,
                   '[]', 1, 99.0, 1, '2026-09-15T00:00:00+00:00')"""
    )
    conn.execute(
        """INSERT INTO weekly_superstar_stage_evidence
           (id, snapshot_id, stage, status, claim, raw_item_id, source, source_date)
           VALUES (11, 7, 'switching_costs_standard', 'proven',
                   'legacy exact claim', 41, 'legacy-source', '2026-09-14')"""
    )
    conn.execute(
        """INSERT INTO weekly_superstar_stage_evidence
           (id, snapshot_id, stage, status, claim, raw_item_id, source, source_date)
           VALUES (12, 7, 'wedge_product', 'proven',
                   'incompatible stranded row', NULL, NULL, NULL)"""
    )

    db._migrate_weekly_superstar(conn)
    db._migrate_weekly_superstar(conn)

    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(weekly_superstar_snapshots)")
    }
    assert {"coverage_ratio", "missing_evidence"} <= columns
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='weekly_superstar_stage_evidence'"
    ).fetchone()[0]
    assert "switching_costs_rise" in sql
    assert "de_facto_standard" in sql
    assert "switching_costs_standard" not in sql
    migrated = [dict(row) for row in conn.execute(
        """SELECT snapshot_id, stage, status, claim, raw_item_id, source, source_date
           FROM weekly_superstar_stage_evidence ORDER BY stage"""
    )]
    assert migrated == [
        {
            "snapshot_id": 7,
            "stage": "de_facto_standard",
            "status": "missing",
            "claim": "",
            "raw_item_id": None,
            "source": None,
            "source_date": None,
        },
        {
            "snapshot_id": 7,
            "stage": "switching_costs_rise",
            "status": "proven",
            "claim": "legacy exact claim",
            "raw_item_id": 41,
            "source": "legacy-source",
            "source_date": "2026-09-14",
        },
    ]
    assert conn.execute(
        "SELECT COUNT(*) FROM weekly_superstar_stage_evidence_legacy_v0"
    ).fetchone()[0] == 2
    with pytest.raises(sqlite3.IntegrityError, match="v1 NO-GO"):
        conn.execute(
            """INSERT INTO weekly_superstar_snapshots
               (ticker, company, as_of, pipeline_run_id, feature_version, status,
                missing_stages, source_count, score, rank, created_at)
               VALUES ('ETN', 'Eaton', '2026-09-15', 1,
                       'weekly-superstar-platform-v1', 'qualified', '[]', 6,
                       NULL, NULL, '2026-09-15T00:00:00+00:00')"""
        )


def test_weekly_migration_sanitizes_and_enforces_v1_no_go_only() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript("""
        CREATE TABLE pipeline_runs (
            id INTEGER PRIMARY KEY,
            input_items INTEGER NOT NULL DEFAULT 0,
            candidates_considered INTEGER NOT NULL DEFAULT 0,
            candidates_published INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE raw_items (id INTEGER PRIMARY KEY);
        CREATE TABLE security_master (id INTEGER PRIMARY KEY);
        CREATE TABLE weekly_superstar_snapshots (
            id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL,
            company TEXT NOT NULL,
            as_of TEXT NOT NULL,
            pipeline_run_id INTEGER NOT NULL,
            feature_version TEXT NOT NULL,
            status TEXT NOT NULL,
            security_master_id INTEGER,
            missing_stages TEXT NOT NULL,
            source_count INTEGER NOT NULL,
            coverage_ratio REAL NOT NULL DEFAULT 0.0,
            missing_evidence TEXT NOT NULL DEFAULT '[]',
            score REAL,
            rank INTEGER,
            created_at TEXT NOT NULL,
            UNIQUE(ticker, as_of, pipeline_run_id, feature_version)
        );
        CREATE TABLE weekly_superstar_stage_evidence (
            id INTEGER PRIMARY KEY,
            snapshot_id INTEGER NOT NULL,
            stage TEXT NOT NULL,
            status TEXT NOT NULL,
            claim TEXT NOT NULL,
            raw_item_id INTEGER,
            source TEXT,
            source_date TEXT
        );
        INSERT INTO pipeline_runs (id, candidates_published) VALUES (1, 3), (2, 7);
        INSERT INTO raw_items (id) VALUES (101), (102), (103);
        INSERT INTO weekly_superstar_snapshots VALUES
            (10, 'ETN', 'Eaton', '2026-09-15', 1,
             'weekly-superstar-platform-v1', 'qualified', NULL, '[]', 3, 9.0,
             '["independent_sources"]', 88.0, 1, '2026-09-15'),
            (12, 'ABC', 'Unverified', '2026-09-15', 1,
             'weekly-superstar-platform-v1', 'unverified_us_listing', NULL, '[]',
             0, -4.0, '[]', 66.0, 3, '2026-09-15'),
            (11, 'XYZ', 'Other', '2026-09-15', 2,
             'weekly-superstar-platform-v2', 'qualified', NULL, '[]', 0, 0.75,
             '["v2-marker"]', 77.0, 2, '2026-09-15');
        INSERT INTO weekly_superstar_stage_evidence VALUES
            (1, 10, 'wedge_product', 'proven', 'one', 101, 'rss', '2026-09-10'),
            (2, 10, 'adjacent_products_attach', 'proven', 'two', 102, 'sec', '2026-09-11'),
            (3, 10, 'customer_data_workflow_accumulates', 'proven', 'three', 103, 'arxiv', '2026-09-12');
    """)

    db._migrate_weekly_superstar(conn)

    v1 = conn.execute(
        """SELECT status, coverage_ratio, missing_evidence, score, rank
           FROM weekly_superstar_snapshots WHERE id=10"""
    ).fetchone()
    assert {
        **dict(v1),
        "missing_evidence": json.loads(v1["missing_evidence"]),
    } == {
        "status": "insufficient_evidence",
        "coverage_ratio": 0.5,
        "missing_evidence": ["independent_sources", "authoritative_stage_measurements"],
        "score": None,
        "rank": None,
    }
    assert conn.execute(
        "SELECT candidates_published FROM pipeline_runs WHERE id=1"
    ).fetchone()[0] == 0
    assert tuple(conn.execute(
        "SELECT status, coverage_ratio, score, rank FROM weekly_superstar_snapshots WHERE id=12"
    ).fetchone()) == ("unverified_us_listing", 0.0, None, None)
    assert dict(conn.execute(
        """SELECT status, coverage_ratio, missing_evidence, score, rank
           FROM weekly_superstar_snapshots WHERE id=11"""
    ).fetchone()) == {
        "status": "qualified",
        "coverage_ratio": 0.75,
        "missing_evidence": '["v2-marker"]',
        "score": 77.0,
        "rank": 2,
    }
    assert conn.execute(
        "SELECT candidates_published FROM pipeline_runs WHERE id=2"
    ).fetchone()[0] == 7

    for statement in (
        "UPDATE weekly_superstar_snapshots SET score=1 WHERE id=10",
        "UPDATE weekly_superstar_snapshots SET rank=1 WHERE id=10",
        "UPDATE weekly_superstar_snapshots SET status='qualified' WHERE id=10",
        "UPDATE pipeline_runs SET candidates_published=1 WHERE id=1",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="v1 NO-GO"):
            conn.execute(statement)
    with pytest.raises(sqlite3.IntegrityError, match="v1 NO-GO"):
        conn.execute(
            """INSERT INTO weekly_superstar_snapshots
               (id, ticker, company, as_of, pipeline_run_id, feature_version, status,
                missing_stages, source_count, coverage_ratio, missing_evidence,
                score, rank, created_at)
               VALUES (13, 'BAD', 'Bad write', '2026-09-15', 1,
                       'weekly-superstar-platform-v1', 'insufficient_evidence',
                       '[]', 0, 0.0, '["authoritative_stage_measurements"]',
                       1.0, NULL, '2026-09-15')"""
        )


def test_weekly_migration_rolls_back_schema_and_data_on_copy_failure() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE pipeline_runs (id INTEGER PRIMARY KEY);
        CREATE TABLE raw_items (id INTEGER PRIMARY KEY);
        CREATE TABLE security_master (id INTEGER PRIMARY KEY);
        CREATE TABLE weekly_superstar_snapshots (
            id INTEGER PRIMARY KEY, ticker TEXT NOT NULL, company TEXT NOT NULL,
            as_of TEXT NOT NULL, pipeline_run_id INTEGER NOT NULL,
            feature_version TEXT NOT NULL, status TEXT NOT NULL,
            security_master_id INTEGER, missing_stages TEXT NOT NULL,
            source_count INTEGER NOT NULL, score REAL, rank INTEGER,
            created_at TEXT NOT NULL,
            UNIQUE(ticker, as_of, pipeline_run_id, feature_version)
        );
        CREATE TABLE weekly_superstar_stage_evidence (
            id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL,
            stage TEXT NOT NULL CHECK(stage IN ('switching_costs_standard')),
            status TEXT NOT NULL, claim TEXT NOT NULL, raw_item_id INTEGER,
            source TEXT, source_date TEXT
        );
        INSERT INTO weekly_superstar_stage_evidence VALUES
            (1, 7, 'switching_costs_standard', 'proven', 'keep me', 41,
             'legacy-source', '2026-09-14');
    """)

    class FailingConnection:
        def __init__(self, actual: sqlite3.Connection) -> None:
            self.actual = actual
            self.failed = False

        def execute(self, sql: str, parameters=()):
            if (
                not self.failed
                and "INSERT INTO weekly_superstar_stage_evidence" in sql
                and "FROM weekly_superstar_stage_evidence_legacy_v0" in sql
            ):
                self.failed = True
                raise sqlite3.OperationalError("injected copy failure")
            return self.actual.execute(sql, parameters)

    with pytest.raises(sqlite3.OperationalError, match="injected copy failure"):
        db._migrate_weekly_superstar(FailingConnection(conn))  # type: ignore[arg-type]

    table_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='weekly_superstar_stage_evidence'"
    ).fetchone()[0]
    assert "switching_costs_standard" in table_sql
    assert conn.execute(
        "SELECT claim, raw_item_id, source, source_date FROM weekly_superstar_stage_evidence"
    ).fetchone() == ("keep me", 41, "legacy-source", "2026-09-14")
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='weekly_superstar_stage_evidence_legacy_v0'"
    ).fetchone() is None


def test_recent_evidence_window_is_bounded_and_point_in_time() -> None:
    conn = _conn()
    _seed_evidence(conn)
    conn.execute(
        """INSERT INTO raw_items
           (id, source, source_id, title, published_at, collected_at)
           VALUES (7, 'rss', 'future', 'future', '2026-09-15T00:00:01+00:00', '2026-09-15T00:00:01+00:00')"""
    )
    conn.execute(
        "INSERT INTO scored_items (raw_item_id, score, model_used) VALUES (7, 100, 'fixture')"
    )
    conn.commit()

    rows = superstar_weekly.select_recent_evidence(
        conn,
        as_of=datetime(2026, 9, 15, tzinfo=UTC),
        lookback_days=30,
        limit=3,
    )

    assert [row["raw_item_id"] for row in rows] == [1, 2, 3]
    assert all(row["source_date"] <= "2026-09-15" for row in rows)


def test_synthesis_rejects_duplicate_cross_stage_or_wrong_date_citations() -> None:
    conn = _conn()
    _seed_evidence(conn)
    evidence = superstar_weekly.select_recent_evidence(
        conn, as_of=datetime(2026, 9, 15, tzinfo=UTC), lookback_days=30, limit=10
    )

    with pytest.raises(llm.LLMParseError, match="reused across stages"):
        _validate_synthesis(
            {"candidates": [_terra_candidate(duplicate_id=True)]}, evidence
        )

    wrong_date = _terra_candidate()
    wrong_date["stages"][0]["citations"][0]["source_date"] = "2026-09-09"
    with pytest.raises(llm.LLMParseError, match="source date"):
        _validate_synthesis({"candidates": [wrong_date]}, evidence)


def test_all_six_hypotheses_still_fail_closed_without_authoritative_measurements() -> None:
    conn = _conn()
    _seed_evidence(conn)
    evidence = superstar_weekly.select_recent_evidence(
        conn, as_of=datetime(2026, 9, 15, tzinfo=UTC), lookback_days=30, limit=10
    )
    candidate = _validate_synthesis(
        {"candidates": [_terra_candidate()]}, evidence
    )[0]
    run_id = conn.execute(
        "INSERT INTO pipeline_runs (run_type, started_at) VALUES ('superstar_weekly', '2026-09-15T00:00:00+00:00')"
    ).lastrowid
    conn.commit()

    counts = superstar_weekly.persist_weekly_audit(
        conn,
        pipeline_run_id=run_id,
        as_of="2026-09-15",
        candidates=[candidate],
        security_records={"ETN": _verified_security()},
    )

    assert counts == {"considered": 1, "published": 0}
    snapshot = conn.execute("SELECT * FROM weekly_superstar_snapshots").fetchone()
    assert snapshot["status"] == "insufficient_evidence"
    assert json.loads(snapshot["missing_stages"]) == []
    assert json.loads(snapshot["missing_evidence"]) == ["authoritative_stage_measurements"]
    assert snapshot["coverage_ratio"] == 1.0
    assert snapshot["score"] is None and snapshot["rank"] is None
    stages = conn.execute(
        "SELECT stage, status, raw_item_id, source_date FROM weekly_superstar_stage_evidence ORDER BY stage"
    ).fetchall()
    assert len(stages) == 6
    assert all(row["status"] == "proven" and row["raw_item_id"] for row in stages)


def test_incomplete_gate_is_insufficient_with_explicit_missing_stage_and_no_score() -> None:
    conn = _conn()
    _seed_evidence(conn)
    evidence = superstar_weekly.select_recent_evidence(
        conn, as_of=datetime(2026, 9, 15, tzinfo=UTC), lookback_days=30, limit=10
    )
    candidate = _validate_synthesis(
        {"candidates": [_terra_candidate(missing="third_party_developers_join")]}, evidence
    )[0]
    run_id = conn.execute(
        "INSERT INTO pipeline_runs (run_type, started_at) VALUES ('superstar_weekly', '2026-09-15T00:00:00+00:00')"
    ).lastrowid
    conn.commit()

    counts = superstar_weekly.persist_weekly_audit(
        conn,
        pipeline_run_id=run_id,
        as_of="2026-09-15",
        candidates=[candidate],
        security_records={"ETN": _verified_security()},
    )

    assert counts == {"considered": 1, "published": 0}
    row = conn.execute(
        """SELECT status, missing_stages, missing_evidence, coverage_ratio, score, rank
           FROM weekly_superstar_snapshots"""
    ).fetchone()
    assert row["status"] == "insufficient_evidence"
    assert json.loads(row["missing_stages"]) == ["third_party_developers_join"]
    assert json.loads(row["missing_evidence"]) == [
        "third_party_developers_join", "authoritative_stage_measurements"
    ]
    assert row["coverage_ratio"] == pytest.approx(5 / 6)
    assert row["score"] is None and row["rank"] is None


def test_missing_stage_output_is_fail_closed_without_persisting_model_content() -> None:
    conn = _conn()
    _seed_evidence(conn)
    evidence = superstar_weekly.select_recent_evidence(
        conn, as_of=datetime(2026, 9, 15, tzinfo=UTC), lookback_days=30, limit=10
    )
    candidate = _terra_candidate(missing="de_facto_standard")
    missing = candidate["stages"][-1]
    missing["claim"] = "unsupported model explanation"
    missing["citations"] = _citations(6)

    validated = _validate_synthesis({"candidates": [candidate]}, evidence)

    assert validated[0]["stages"][-1] == {
        "stage": "de_facto_standard",
        "status": "missing",
        "claim": "",
        "citations": [],
    }


def test_weekly_synthesis_schema_is_accepted_by_live_boundary_validation() -> None:
    schema = superstar_weekly._synthesis_schema(superstar_weekly.MAX_FINAL_CANDIDATES)

    llm._validate_schema_definition(schema["schema"], "$")


def test_terra_company_is_resolved_only_from_validated_luna_map() -> None:
    conn = _conn()
    _seed_evidence(conn)
    evidence = superstar_weekly.select_recent_evidence(
        conn, as_of=datetime(2026, 9, 15, tzinfo=UTC), lookback_days=30, limit=10
    )

    candidate = _validate_synthesis({"candidates": [_terra_candidate()]}, evidence)[0]

    assert candidate["company"] == "Eaton Corp plc"
    missing_map = {"candidates": [_terra_candidate()]}
    with pytest.raises(llm.LLMParseError, match="company"):
        superstar_weekly.validate_synthesis(
            missing_map, evidence, company_by_ticker={}
        )


def test_superstar_schemas_reject_escaped_non_ascii_and_loose_tickers() -> None:
    prefilter = superstar_weekly._prefilter_schema(6, 1)
    bad_company = {
        "companies": [{"ticker": "ETN", "company": "Eaton é", "raw_item_ids": [1]}]
    }
    bad_ticker = {
        "companies": [{"ticker": "ETN/US", "company": "Eaton", "raw_item_ids": [1]}]
    }
    for payload in (bad_company, bad_ticker):
        with pytest.raises(llm.LLMParseError, match="pattern"):
            llm.CodexOAuthBoundary._parse_json(json.dumps(payload), prefilter)

    terra = _maximal_terra_payload()
    terra["candidates"] = terra["candidates"][:1]
    terra["candidates"][0]["stages"][0]["claim"] = "escaped é"
    with pytest.raises(llm.LLMParseError, match="pattern"):
        llm.CodexOAuthBoundary._parse_json(json.dumps(terra), superstar_weekly._synthesis_schema(1))

    terra = _maximal_terra_payload()
    terra["candidates"] = terra["candidates"][:1]
    terra["candidates"][0]["stages"][0]["citations"][0]["source_date"] = "2026/09/15"
    with pytest.raises(llm.LLMParseError, match="pattern"):
        llm.CodexOAuthBoundary._parse_json(json.dumps(terra), superstar_weekly._synthesis_schema(1))


def test_weekly_llm_contract_uses_luna_then_at_most_one_terra_and_isolated_run(monkeypatch) -> None:
    conn = _conn()
    _seed_evidence(conn)
    calls: list[dict] = []

    class Boundary:
        def complete(self, **kwargs):
            calls.append(kwargs)
            if kwargs["model"] == llm.LUNA_MODEL:
                return SimpleNamespace(parsed={"companies": [{"ticker": "ETN", "company": "Eaton Corp plc", "raw_item_ids": [1, 2, 3, 4, 5, 6]}]})
            return SimpleNamespace(parsed={"candidates": [_terra_candidate()]})

    monkeypatch.setattr(superstar_weekly.llm, "get_boundary", lambda: Boundary())
    candidates, input_count = superstar_weekly.discover_candidates(
        conn,
        as_of=datetime(2026, 9, 15, tzinfo=UTC),
        lookback_days=30,
        evidence_limit=120,
        candidate_limit=8,
        run_id="superstar-weekly-4-isolated",
    )

    assert input_count == 6 and len(candidates) == 1
    assert [call["model"] for call in calls] == [llm.LUNA_MODEL, llm.TERRA_MODEL]
    assert {call["run_id"] for call in calls} == {"superstar-weekly-4-isolated"}
    assert [call["workload"] for call in calls] == [
        "superstar_weekly_prefilter", "superstar_weekly_synthesis"
    ]
    assert "superstar_weekly_synthesis" in llm.TERRA_WORKLOADS


def test_synthesis_rejects_another_prefiltered_candidates_evidence(monkeypatch) -> None:
    conn = _conn()
    _seed_evidence(conn)
    adversarial = _terra_candidate()
    adversarial["stages"][0]["citations"] = _citations(2)
    for stage in adversarial["stages"][1:]:
        stage.update(status="missing", claim="", citations=[])

    class Boundary:
        def complete(self, **kwargs):
            if kwargs["model"] == llm.LUNA_MODEL:
                return SimpleNamespace(parsed={"companies": [
                    {"ticker": "ETN", "company": "Eaton", "raw_item_ids": [1]},
                    {"ticker": "NVDA", "company": "NVIDIA", "raw_item_ids": [2]},
                ]})
            return SimpleNamespace(parsed={"candidates": [adversarial]})

    monkeypatch.setattr(superstar_weekly.llm, "get_boundary", lambda: Boundary())

    with pytest.raises(llm.LLMParseError, match="candidate-scoped"):
        superstar_weekly.discover_candidates(
            conn,
            as_of=datetime(2026, 9, 15, tzinfo=UTC),
            lookback_days=30,
            evidence_limit=120,
            candidate_limit=8,
            run_id="superstar-weekly-cross-candidate",
        )


def test_unverified_sec_candidate_is_audited_but_never_published() -> None:
    conn = _conn()
    _seed_evidence(conn)
    evidence = superstar_weekly.select_recent_evidence(
        conn, as_of=datetime(2026, 9, 15, tzinfo=UTC), lookback_days=30, limit=10
    )
    candidate = _validate_synthesis(
        {"candidates": [_terra_candidate()]}, evidence
    )[0]
    run_id = conn.execute(
        "INSERT INTO pipeline_runs (run_type, started_at) VALUES ('superstar_weekly', '2026-09-15')"
    ).lastrowid
    conn.commit()

    counts = superstar_weekly.persist_weekly_audit(
        conn, pipeline_run_id=run_id, as_of="2026-09-15",
        candidates=[candidate], security_records={},
    )

    assert counts == {"considered": 1, "published": 0}
    row = conn.execute(
        "SELECT status, missing_evidence, coverage_ratio FROM weekly_superstar_snapshots"
    ).fetchone()
    assert row["status"] == "unverified_us_listing"
    assert json.loads(row["missing_evidence"]) == [
        "official_sec_listing", "authoritative_stage_measurements"
    ]
    assert row["coverage_ratio"] == 1.0


def test_weekly_cli_has_own_run_ledger_and_isolated_llm_namespace(monkeypatch) -> None:
    conn = _conn()
    captured: dict[str, object] = {}
    monkeypatch.setattr(main, "get_connection", lambda: conn)
    def start(kind: str) -> int:
        captured["kind"] = kind
        return 1

    monkeypatch.setattr(main, "start_pipeline_run", start)

    def discover(_conn, **kwargs):
        captured["llm_run_id"] = kwargs["run_id"]
        return [], 17

    completed: list[dict] = []
    monkeypatch.setattr(superstar_weekly, "discover_candidates", discover)
    monkeypatch.setattr(main, "complete_pipeline_run", lambda _run_id, **values: completed.append(values))

    result = CliRunner().invoke(main.cli, ["superstar-weekly", "--as-of", "2026-09-15"])

    assert result.exception is None
    assert captured["kind"] == "superstar_weekly"
    assert str(captured["llm_run_id"]).startswith("superstar-weekly-1-")
    assert completed == [{
        "status": "completed",
        "errors": None,
        "duration_secs": completed[0]["duration_secs"],
        "input_items": 17,
        "candidates_considered": 0,
        "candidates_published": 0,
    }]


def test_sec_verification_is_bounded_and_outside_transactions() -> None:
    conn = _conn()
    candidates = [{"ticker": f"T{index}"} for index in range(20)]
    master = [
        {**_verified_security(), "ticker": f"T{index}"}
        for index in range(20)
    ]
    calls: list[str] = []

    def fetch():
        assert not conn.in_transaction
        return master

    def verify(record, as_of):
        assert not conn.in_transaction
        assert as_of == "2026-09-15"
        calls.append(record["ticker"])
        return record

    records, errors = superstar_weekly.verify_candidate_list(
        conn, candidates, as_of="2026-09-15",
        security_master_fetcher=fetch, candidate_verifier=verify,
        request_interval_seconds=0,
    )

    assert errors == []
    assert calls == [f"T{index}" for index in range(8)]
    assert set(records) == set(calls)


def test_same_run_retry_preserves_prior_audit_provenance() -> None:
    conn = _conn()
    _seed_evidence(conn)
    evidence = superstar_weekly.select_recent_evidence(
        conn, as_of=datetime(2026, 9, 15, tzinfo=UTC), lookback_days=30, limit=10
    )
    qualified = _validate_synthesis(
        {"candidates": [_terra_candidate()]}, evidence
    )[0]
    degraded = _validate_synthesis(
        {"candidates": [_terra_candidate(missing="switching_costs_rise")]}, evidence
    )[0]
    run_id = conn.execute(
        "INSERT INTO pipeline_runs (run_type, started_at) VALUES ('superstar_weekly', '2026-09-15')"
    ).lastrowid
    conn.commit()

    superstar_weekly.persist_weekly_audit(
        conn, pipeline_run_id=run_id, as_of="2026-09-15",
        candidates=[qualified], security_records={"ETN": _verified_security()},
    )
    before = [dict(row) for row in conn.execute(
        "SELECT * FROM weekly_superstar_stage_evidence ORDER BY id"
    )]
    superstar_weekly.persist_weekly_audit(
        conn, pipeline_run_id=run_id, as_of="2026-09-15",
        candidates=[degraded], security_records={},
    )

    snapshot = conn.execute("SELECT status FROM weekly_superstar_snapshots").fetchone()
    after = [dict(row) for row in conn.execute(
        "SELECT * FROM weekly_superstar_stage_evidence ORDER BY id"
    )]
    assert snapshot["status"] == "insufficient_evidence"
    assert after == before


def _filter_criterion(
    criterion: str,
    *,
    status: str = "supported",
    raw_item_id: int | None = None,
) -> dict:
    return {
        "criterion": criterion,
        "status": status,
        "claim": f"bounded claim for {criterion}",
        "citations": _citations(raw_item_id) if raw_item_id is not None else [],
    }


def _filter_audit(
    *,
    industry_track: str = "software_platform",
    supported_self_reinforcing: bool = True,
) -> dict:
    findings = []
    if supported_self_reinforcing:
        findings.append(_filter_criterion("self_reinforcing_moat", raw_item_id=1))
    return {
        "industry_track": industry_track,
        "classification": "unclassified",
        "filter_findings": findings,
    }


def test_v2_filter_migration_is_additive_idempotent_and_preserves_v1_rows() -> None:
    conn = _conn()
    _seed_evidence(conn)
    run_id = conn.execute(
        "INSERT INTO pipeline_runs (run_type, started_at) VALUES ('superstar_weekly', '2026-09-15')"
    ).lastrowid
    conn.execute(
        """INSERT INTO weekly_superstar_snapshots
           (ticker, company, as_of, pipeline_run_id, feature_version, status,
            missing_stages, source_count, coverage_ratio, missing_evidence, score, rank)
           VALUES ('ETN', 'Eaton', '2026-09-15', ?, 'weekly-superstar-platform-v1',
                   'insufficient_evidence', '[]', 0, 0.0,
                   '["authoritative_stage_measurements"]', NULL, NULL)""",
        (run_id,),
    )
    conn.commit()

    db._migrate_weekly_superstar(conn)
    db._migrate_weekly_superstar(conn)

    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "weekly_superstar_filter_audits",
        "weekly_superstar_filter_criteria",
        "weekly_superstar_filter_citations",
    } <= tables
    assert conn.execute("SELECT COUNT(*) FROM weekly_superstar_snapshots").fetchone()[0] == 1


def test_v2_filter_migration_rolls_back_all_new_tables_on_failure() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db._SCHEMA)
    conn.execute("CREATE TABLE migration_sentinel (value TEXT)")
    conn.execute("INSERT INTO migration_sentinel VALUES ('keep')")
    conn.commit()

    class FailingConnection:
        def __init__(self, actual: sqlite3.Connection) -> None:
            self.actual = actual

        def execute(self, sql: str, parameters=()):
            if "CREATE TABLE IF NOT EXISTS weekly_superstar_filter_criteria" in sql:
                raise sqlite3.OperationalError("injected v2 migration failure")
            return self.actual.execute(sql, parameters)

    with pytest.raises(sqlite3.OperationalError, match="injected v2 migration failure"):
        db._migrate_weekly_superstar(FailingConnection(conn))  # type: ignore[arg-type]

    assert conn.execute("SELECT value FROM migration_sentinel").fetchone()[0] == "keep"
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'weekly_superstar_filter_%'"
    ).fetchone()[0] == 0


def test_v2_sparse_filter_contract_materializes_every_applicable_criterion() -> None:
    conn = _conn()
    _seed_evidence(conn)
    evidence = superstar_weekly.select_recent_evidence(
        conn, as_of=datetime(2026, 9, 15, tzinfo=UTC), lookback_days=30, limit=10
    )
    candidate = _terra_candidate()
    audit = _filter_audit()
    growth_index = list(superstar_weekly.CORE_FILTER_CRITERIA).index("growth_quality")
    audit["filter_findings"].append(_filter_criterion(
        "growth_quality", status="partial", raw_item_id=2
    ))
    candidate["filter_audit"] = audit

    validated = _validate_synthesis(
        {"candidates": [candidate]},
        evidence,
        allowed_tickers={"ETN"},
        allowed_raw_ids_by_ticker={"ETN": {1, 2, 3, 4, 5, 6}},
    )[0]
    assert [row["criterion"] for row in validated["filter_audit"]["criteria"]] == [
        *superstar_weekly.CORE_FILTER_CRITERIA,
        *superstar_weekly.INDUSTRY_FILTER_CRITERIA["software_platform"],
    ]
    assert validated["filter_audit"]["classification"] == "unclassified"
    assert validated["filter_audit"]["criteria"][growth_index]["status"] == "partial"
    assert validated["filter_audit"]["criteria"][growth_index]["missing_evidence"]

    missing = _terra_candidate()
    bad_audit = _filter_audit()
    bad_audit["filter_findings"] = []
    missing["filter_audit"] = bad_audit
    materialized = _validate_synthesis(
        {"candidates": [missing]}, evidence
    )[0]["filter_audit"]["criteria"]
    assert len(materialized) == (
        len(superstar_weekly.CORE_FILTER_CRITERIA)
        + len(superstar_weekly.INDUSTRY_FILTER_CRITERIA["software_platform"])
    )
    assert all(row["status"] == "missing" for row in materialized)

    cross_candidate = _terra_candidate()
    cross_candidate["filter_audit"] = _filter_audit()
    for stage in cross_candidate["stages"]:
        stage.update(status="missing", claim="", citations=[])
    normalized = _validate_synthesis(
        {"candidates": [cross_candidate]},
        evidence,
        allowed_tickers={"ETN"},
        allowed_raw_ids_by_ticker={"ETN": {2, 3, 4, 5, 6}},
    )[0]["filter_audit"]["criteria"]
    assert normalized[0]["status"] == "missing"
    assert normalized[0]["claim"] == ""
    assert normalized[0]["citations"] == []


def test_v2_filter_persistence_is_unclassified_and_keeps_all_gaps_and_provenance() -> None:
    conn = _conn()
    _seed_evidence(conn)
    evidence = superstar_weekly.select_recent_evidence(
        conn, as_of=datetime(2026, 9, 15, tzinfo=UTC), lookback_days=30, limit=10
    )
    payload = _terra_candidate()
    payload["filter_audit"] = _filter_audit()
    candidate = _validate_synthesis(
        {"candidates": [payload]}, evidence,
        allowed_tickers={"ETN"},
        allowed_raw_ids_by_ticker={"ETN": {1, 2, 3, 4, 5, 6}},
    )[0]
    run_id = conn.execute(
        "INSERT INTO pipeline_runs (run_type, started_at) VALUES ('superstar_weekly', '2026-09-15')"
    ).lastrowid
    conn.commit()

    superstar_weekly.persist_weekly_audit(
        conn, pipeline_run_id=run_id, as_of="2026-09-15",
        candidates=[candidate], security_records={"ETN": _verified_security()},
    )

    audit = conn.execute("SELECT * FROM weekly_superstar_filter_audits").fetchone()
    assert audit["filter_version"] == superstar_weekly.FILTER_VERSION
    assert audit["document_source_url"] == superstar_weekly.FILTER_DOCUMENT_URL
    assert audit["classification"] is None
    assert audit["classification_status"] == "unclassified"
    assert audit["authoritative_measurements_present"] == 0
    assert audit["display_ready"] == 1
    audit_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(weekly_superstar_filter_audits)")
    }
    assert "score" not in audit_columns and "rank" not in audit_columns
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """UPDATE weekly_superstar_filter_audits
               SET classification='confirmed_inflection', classification_status='classified'
               WHERE id=?""",
            (audit["id"],),
        )
    assert "authoritative_4_8q_financial_measurements" in json.loads(audit["missing_evidence"])
    criteria = conn.execute(
        "SELECT criterion_key, status, missing_evidence FROM weekly_superstar_filter_criteria"
    ).fetchall()
    assert len(criteria) == (
        len(superstar_weekly.CORE_FILTER_CRITERIA)
        + len(superstar_weekly.INDUSTRY_FILTER_CRITERIA["software_platform"])
    )
    assert all(json.loads(row["missing_evidence"]) or row["status"] == "supported" for row in criteria)
    citation = conn.execute(
        "SELECT raw_item_id, source_date FROM weekly_superstar_filter_citations"
    ).fetchone()
    assert tuple(citation) == (1, "2026-09-10")


def test_v2_filter_zero_moat_support_is_retained_but_not_display_ready() -> None:
    conn = _conn()
    _seed_evidence(conn)
    evidence = superstar_weekly.select_recent_evidence(
        conn, as_of=datetime(2026, 9, 15, tzinfo=UTC), lookback_days=30, limit=10
    )
    payload = _terra_candidate()
    payload["filter_audit"] = _filter_audit(supported_self_reinforcing=False)
    candidate = _validate_synthesis({"candidates": [payload]}, evidence)[0]
    run_id = conn.execute(
        "INSERT INTO pipeline_runs (run_type, started_at) VALUES ('superstar_weekly', '2026-09-15')"
    ).lastrowid
    conn.commit()

    superstar_weekly.persist_weekly_audit(
        conn, pipeline_run_id=run_id, as_of="2026-09-15",
        candidates=[candidate], security_records={"ETN": _verified_security()},
    )

    assert conn.execute("SELECT COUNT(*) FROM weekly_superstar_filter_audits").fetchone()[0] == 1
    assert conn.execute("SELECT display_ready FROM weekly_superstar_filter_audits").fetchone()[0] == 0


def test_v2_filter_schema_is_bounded_and_forbids_model_scores_ranks_or_classification() -> None:
    schema = superstar_weekly._synthesis_schema(superstar_weekly.MAX_FINAL_CANDIDATES)
    llm._validate_schema_definition(schema["schema"], "$")
    candidate_properties = schema["schema"]["properties"]["candidates"]["items"]["properties"]
    audit_properties = candidate_properties["filter_audit"]["properties"]
    assert set(audit_properties) == {
        "industry_track", "classification", "filter_findings"
    }
    assert audit_properties["classification"]["enum"] == ["unclassified"]
    assert audit_properties["filter_findings"]["maxItems"] == 2
    serialized = json.dumps(schema)
    assert '"score"' not in serialized and '"rank"' not in serialized


def test_filter_config_records_document_requirements_and_system_divergence() -> None:
    config_path = superstar_weekly.FILTER_CONFIG_PATH
    config = json.loads(config_path.read_text())
    assert config["source_url"] == superstar_weekly.FILTER_DOCUMENT_URL
    assert config["version"] == superstar_weekly.FILTER_VERSION
    assert config["document_candidate_range"] == [8, 15]
    assert config["system_candidate_range"] == [0, 8]
    assert config["divergence_note"]
    assert config["core_criteria"] == list(superstar_weekly.CORE_FILTER_CRITERIA)
    assert config["industry_criteria"] == {
        key: list(value) for key, value in superstar_weekly.INDUSTRY_FILTER_CRITERIA.items()
    }


def test_terra_schema_is_sparse_and_every_value_is_bounded() -> None:
    schema = superstar_weekly._synthesis_schema(superstar_weekly.MAX_FINAL_CANDIDATES)
    root = schema["schema"]["properties"]["candidates"]
    candidate = root["items"]["properties"]
    audit = candidate["filter_audit"]["properties"]
    findings = audit["filter_findings"]

    assert root["maxItems"] == 8
    assert set(candidate) == {"ticker", "stages", "filter_audit"}
    assert findings["minItems"] == 0
    assert findings["maxItems"] == 2
    finding = findings["items"]["properties"]
    assert set(finding) == {"criterion", "status", "claim", "citations"}
    assert finding["status"]["enum"] == ["supported", "partial"]
    assert finding["claim"] == {
        "type": "string", "minLength": 1, "maxLength": 64,
        "pattern": superstar_weekly.ASCII_PRINTABLE_PATTERN,
    }
    assert finding["citations"]["minItems"] == 1
    assert finding["citations"]["maxItems"] == 1
    stage = candidate["stages"]["items"]["properties"]
    assert stage["claim"]["maxLength"] == 64
    assert stage["claim"]["pattern"] == superstar_weekly.ASCII_PRINTABLE_PATTERN
    assert stage["citations"]["maxItems"] == 1
    assert candidate["ticker"]["pattern"] == superstar_weekly.TICKER_PATTERN

    serialized = json.dumps(schema, sort_keys=True)
    raw_id_schemas = []
    unbounded_values = []

    def visit(value, path="$"):
        if isinstance(value, dict):
            if value.get("type") == "string" and "maxLength" not in value:
                unbounded_values.append(path)
            if value.get("type") == "array" and "maxItems" not in value:
                unbounded_values.append(path)
            if value.get("type") == "integer" and "maximum" not in value:
                unbounded_values.append(path)
            for key, child in value.items():
                if key == "raw_item_id":
                    raw_id_schemas.append(child)
                visit(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}[{index}]")

    visit(schema)
    visit(superstar_weekly._prefilter_schema(120, 12))
    assert unbounded_values == []
    assert raw_id_schemas
    assert all(
        item["minimum"] == 1
        and item["maximum"] == superstar_weekly.SQLITE_MAX_INTEGER
        for item in raw_id_schemas
    )
    prefilter_raw_id = superstar_weekly._prefilter_schema(120, 12)["schema"][
        "properties"
    ]["companies"]["items"]["properties"]["raw_item_ids"]["items"]
    assert prefilter_raw_id == {
        "type": "integer",
        "minimum": 1,
        "maximum": superstar_weekly.SQLITE_MAX_INTEGER,
    }
    assert '"score"' not in serialized and '"rank"' not in serialized


def _maximal_terra_payload() -> dict:
    longest_claim = "~" * 64
    raw_id = superstar_weekly.SQLITE_MAX_INTEGER
    citation = {"raw_item_id": raw_id, "source_date": "9999-99-99"}
    criteria = sorted(
        (
            *superstar_weekly.CORE_FILTER_CRITERIA,
            *(
                criterion
                for values in superstar_weekly.INDUSTRY_FILTER_CRITERIA.values()
                for criterion in values
            ),
        ),
        key=len,
        reverse=True,
    )[:2]
    candidate = {
        "ticker": "ZZZZZZZZ.A",
        "stages": [
            {
                "stage": stage,
                "status": "missing",
                "claim": longest_claim,
                "citations": [citation],
            }
            for stage in superstar_weekly.PLATFORM_STAGES
        ],
        "filter_audit": {
            "industry_track": "semiconductor_industrial",
            "classification": "unclassified",
            "filter_findings": [
                {
                    "criterion": criterion,
                    "status": "supported",
                    "claim": longest_claim,
                    "citations": [citation],
                }
                for criterion in criteria
            ],
        },
    }
    return {"candidates": [candidate for _ in range(8)]}


def test_maximal_terra_payload_fits_conventional_accepted_byte_cap() -> None:
    payload = _maximal_terra_payload()
    schema = superstar_weekly._synthesis_schema(superstar_weekly.MAX_FINAL_CANDIDATES)
    assert llm.CodexOAuthBoundary._parse_json(
        json.dumps(payload, separators=(",", ":")), schema
    ) == payload
    accepted_cap = (
        superstar_weekly.MAX_OUTPUT_TOKENS * llm.ACCEPTED_OUTPUT_BYTES_PER_TOKEN
        + llm.OUTPUT_FRAMING_BYTES
    )
    sizes = {
        "compact": len(json.dumps(payload, separators=(",", ":")).encode()),
        **{
            f"indent{indent}": len(json.dumps(payload, indent=indent).encode())
            for indent in (2, 3, 4)
        },
    }

    assert accepted_cap == 132_096
    assert sizes == {
        "compact": 14_768,
        "indent2": 24_464,
        "indent3": 28_698,
        "indent4": 32_932,
    }
    assert all(size < accepted_cap for size in sizes.values()), sizes
    # No tokenizer dependency in the suite: exact cardinality assertions above
    # plus locked measured sizes make output-bound growth an explicit review.


def test_sparse_filter_findings_normalize_semantic_errors_fail_closed() -> None:
    conn = _conn()
    _seed_evidence(conn)
    evidence = superstar_weekly.select_recent_evidence(
        conn, as_of=datetime(2026, 9, 15, tzinfo=UTC), lookback_days=30, limit=10
    )
    candidate = _terra_candidate()
    for stage in candidate["stages"]:
        stage.update(status="missing", claim="", citations=[])
    candidate["filter_audit"] = {
        "industry_track": "software_platform",
        "classification": "unclassified",
        "filter_findings": [
            _filter_criterion("growth_quality", status="supported", raw_item_id=999),
            _filter_criterion("growth_quality", status="supported", raw_item_id=2),
            _filter_criterion("management_execution", status="supported", raw_item_id=1),
            _filter_criterion("capex_entry_barrier", status="supported", raw_item_id=1),
        ],
    }
    candidate["filter_audit"]["filter_findings"][2]["citations"][0]["source_date"] = (
        "2026-09-09"
    )

    validated = _validate_synthesis(
        {"candidates": [candidate]},
        evidence,
        allowed_tickers={"ETN"},
        allowed_raw_ids_by_ticker={"ETN": {2, 3, 4, 5, 6}},
    )[0]["filter_audit"]

    assert [row["criterion"] for row in validated["criteria"]] == [
        *superstar_weekly.CORE_FILTER_CRITERIA,
        *superstar_weekly.INDUSTRY_FILTER_CRITERIA["software_platform"],
    ]
    by_key = {row["criterion"]: row for row in validated["criteria"]}
    for key in ("growth_quality", "management_execution", "capex_entry_barrier"):
        assert by_key.get(key, {"status": "missing"})["status"] == "missing"
        if key in by_key:
            assert by_key[key]["claim"] == ""
            assert by_key[key]["citations"] == []
            assert by_key[key]["missing_evidence"]


def test_duplicate_sparse_filter_findings_become_canonical_missing() -> None:
    conn = _conn()
    _seed_evidence(conn)
    evidence = superstar_weekly.select_recent_evidence(
        conn, as_of=datetime(2026, 9, 15, tzinfo=UTC), lookback_days=30, limit=10
    )
    candidate = _terra_candidate()
    candidate["filter_audit"] = {
        "industry_track": "software_platform",
        "classification": "unclassified",
        "filter_findings": [
            _filter_criterion("growth_quality", raw_item_id=2),
            _filter_criterion("growth_quality", raw_item_id=3),
        ],
    }

    criteria = _validate_synthesis(
        {"candidates": [candidate]}, evidence
    )[0]["filter_audit"]["criteria"]
    by_key = {row["criterion"]: row for row in criteria}

    assert by_key["growth_quality"] == {
        "criterion": "growth_quality",
        "status": "missing",
        "claim": "",
        "missing_evidence": ["evidence_gap:growth_quality"],
        "citations": [],
    }


def test_partial_sparse_finding_gets_deterministic_explicit_gap() -> None:
    conn = _conn()
    _seed_evidence(conn)
    evidence = superstar_weekly.select_recent_evidence(
        conn, as_of=datetime(2026, 9, 15, tzinfo=UTC), lookback_days=30, limit=10
    )
    candidate = _terra_candidate()
    candidate["filter_audit"] = _filter_audit(supported_self_reinforcing=False)
    candidate["filter_audit"]["filter_findings"] = [
        _filter_criterion("growth_quality", status="partial", raw_item_id=2)
    ]

    criteria = _validate_synthesis(
        {"candidates": [candidate]}, evidence
    )[0]["filter_audit"]["criteria"]
    growth = next(row for row in criteria if row["criterion"] == "growth_quality")

    assert growth["status"] == "partial"
    assert growth["missing_evidence"] == ["evidence_gap:growth_quality"]


def test_v2_filter_audit_is_required_without_legacy_fallback() -> None:
    conn = _conn()
    _seed_evidence(conn)
    evidence = superstar_weekly.select_recent_evidence(
        conn, as_of=datetime(2026, 9, 15, tzinfo=UTC), lookback_days=30, limit=10
    )
    candidate = _terra_candidate()
    candidate.pop("filter_audit", None)

    with pytest.raises((KeyError, llm.LLMParseError)):
        _validate_synthesis({"candidates": [candidate]}, evidence)
