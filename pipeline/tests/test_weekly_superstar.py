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
        "company": "Eaton Corp plc",
        "stages": stages,
    }


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
        superstar_weekly.validate_synthesis(
            {"candidates": [_terra_candidate(duplicate_id=True)]}, evidence
        )

    wrong_date = _terra_candidate()
    wrong_date["stages"][0]["citations"][0]["source_date"] = "2026-09-09"
    with pytest.raises(llm.LLMParseError, match="source date"):
        superstar_weekly.validate_synthesis({"candidates": [wrong_date]}, evidence)


def test_all_six_hypotheses_still_fail_closed_without_authoritative_measurements() -> None:
    conn = _conn()
    _seed_evidence(conn)
    evidence = superstar_weekly.select_recent_evidence(
        conn, as_of=datetime(2026, 9, 15, tzinfo=UTC), lookback_days=30, limit=10
    )
    candidate = superstar_weekly.validate_synthesis(
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
    candidate = superstar_weekly.validate_synthesis(
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


def test_weekly_synthesis_schema_is_accepted_by_live_boundary_validation() -> None:
    schema = superstar_weekly._synthesis_schema(superstar_weekly.MAX_FINAL_CANDIDATES)

    llm._validate_schema_definition(schema["schema"], "$")


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
    candidate = superstar_weekly.validate_synthesis(
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
    qualified = superstar_weekly.validate_synthesis(
        {"candidates": [_terra_candidate()]}, evidence
    )[0]
    degraded = superstar_weekly.validate_synthesis(
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
