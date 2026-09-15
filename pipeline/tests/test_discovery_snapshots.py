from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta, timezone

from pipeline import db
from pipeline.db import _migrate_security_master
from pipeline.main import write_current_discovery_snapshots
from pipeline.processing.discovery_snapshots import (
    build_candidate_snapshot,
    build_theme_snapshot,
    persist_discovery_snapshots,
    write_discovery_snapshots,
)
from pipeline.processing.sec_security_master import (
    SEC_COMPANY_TICKERS_URL,
    SEC_SUBMISSIONS_URL_TEMPLATE,
    SEC_USER_AGENT,
    VERIFIED_LISTING_STATUS,
    SecSecurityMasterError,
    fetch_sec_security_master,
    verify_sec_candidate,
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
    assert {"themes", "theme_snapshots", "candidate_snapshots", "security_master"} <= tables
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(candidate_snapshots)")
    }
    assert {"as_of", "pipeline_run_id", "feature_version", "feature_values", "status", "security_master_id"} <= columns
    security_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(security_master)")
    }
    assert {
        "ticker", "company_name", "cik", "exchange", "listing_status",
        "source_url", "source_as_of", "fetched_at", "source_status",
        "verification_source_url", "verification_as_of",
    } <= security_columns
    assert "investment_theses" in tables


def test_security_master_migration_is_idempotent_and_preserves_existing_snapshots() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE candidate_snapshots (
            id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL,
            status TEXT NOT NULL
        );
        INSERT INTO candidate_snapshots (ticker, status) VALUES ('SMCI', 'unverified_us_listing');
    """)

    _migrate_security_master(conn)
    _migrate_security_master(conn)

    row = conn.execute("SELECT ticker, status, security_master_id FROM candidate_snapshots").fetchone()
    assert dict(row) == {
        "ticker": "SMCI",
        "status": "unverified_us_listing",
        "security_master_id": None,
    }
    assert conn.execute("SELECT COUNT(*) FROM security_master").fetchone()[0] == 0


class _FailingAlterConnection(sqlite3.Connection):
    def execute(self, sql: str, parameters=(), /):
        if "ALTER TABLE candidate_snapshots" in sql:
            raise sqlite3.OperationalError("injected ALTER failure")
        return super().execute(sql, parameters)


def test_security_master_migration_adds_guard_without_rewriting_existing_data() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE candidate_snapshots (
            id INTEGER PRIMARY KEY,
            ticker TEXT NOT NULL,
            status TEXT NOT NULL,
            coverage TEXT NOT NULL
        );
        INSERT INTO candidate_snapshots (ticker, status, coverage)
        VALUES ('OLD', 'unverified_us_listing', '{"present": []}');
    """)

    _migrate_security_master(conn)

    assert dict(conn.execute(
        "SELECT id, ticker, status, coverage, security_master_id FROM candidate_snapshots"
    ).fetchone()) == {
        "id": 1,
        "ticker": "OLD",
        "status": "unverified_us_listing",
        "coverage": '{"present": []}',
        "security_master_id": None,
    }
    try:
        conn.execute(
            """INSERT INTO candidate_snapshots
               (ticker, status, coverage, security_master_id)
               VALUES ('BAD', 'insufficient_evidence',
                       '{"present": ["verified_us_listing"]}', NULL)"""
        )
    except sqlite3.IntegrityError as exc:
        assert "verified listing requires security_master_id" in str(exc)
    else:
        raise AssertionError("database must reject verified coverage without provenance FK")
    try:
        conn.execute(
            """UPDATE candidate_snapshots
               SET status='scored', coverage='{"present": []}'
               WHERE ticker='OLD'"""
        )
    except sqlite3.IntegrityError as exc:
        assert "verified listing requires security_master_id" in str(exc)
    else:
        raise AssertionError("database must reject scored updates without provenance FK")
    assert conn.execute(
        "SELECT status FROM candidate_snapshots WHERE ticker='OLD'"
    ).fetchone()[0] == "unverified_us_listing"


def test_security_master_migration_rolls_back_table_when_alter_fails() -> None:
    conn = sqlite3.connect(":memory:", factory=_FailingAlterConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE candidate_snapshots (id INTEGER PRIMARY KEY)")
    conn.commit()

    try:
        _migrate_security_master(conn)
    except sqlite3.OperationalError as exc:
        assert "injected ALTER failure" in str(exc)
    else:
        raise AssertionError("forced migration failure must propagate")

    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(candidate_snapshots)")
    }
    assert "security_master" not in tables
    assert "security_master_id" not in columns


def test_init_db_rolls_back_schema_and_column_when_migration_fails(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "atomic.db"
    seed = sqlite3.connect(db_path)
    seed.execute(
        "CREATE TABLE candidate_snapshots (id INTEGER PRIMARY KEY, as_of TEXT, rank INTEGER)"
    )
    seed.commit()
    seed.close()
    monkeypatch.setattr(db, "DB_PATH", db_path)
    monkeypatch.setattr(db, "_connection", None)

    def fail_after_alter(conn: sqlite3.Connection) -> None:
        conn.execute(
            "ALTER TABLE candidate_snapshots ADD COLUMN security_master_id INTEGER"
        )
        raise sqlite3.OperationalError("injected post-ALTER failure")

    monkeypatch.setattr(db, "_migrate_security_master", fail_after_alter)

    try:
        db.init_db()
    except sqlite3.OperationalError as exc:
        assert "injected post-ALTER failure" in str(exc)
    else:
        raise AssertionError("forced init migration failure must propagate")

    check = sqlite3.connect(db_path)
    tables = {row[0] for row in check.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    columns = {row[1] for row in check.execute("PRAGMA table_info(candidate_snapshots)")}
    check.close()
    assert "security_master" not in tables
    assert "security_master_id" not in columns


class _SecResponse:
    def __init__(self, payload: object, headers: dict[str, str] | None = None) -> None:
        self._payload = payload
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload


def _sec_payload() -> dict[str, object]:
    return {
        "fields": ["cik", "name", "ticker", "exchange"],
        "data": [
            [1551182, "Eaton Corp plc", "ETN", "NYSE"],
            [1375365, "Super Micro Computer, Inc.", "SMCI", "Nasdaq"],
            [1571996, "Dell Technologies Inc.", "DELL", "NYSE"],
            [1577526, "C3.ai, Inc.", "AI", "NYSE"],
        ],
    }


def _staged_sec_records() -> list[dict[str, object]]:
    return fetch_sec_security_master(
        http_get=lambda _url, **_kwargs: _SecResponse(
            _sec_payload(), {"Last-Modified": "Fri, 11 Sep 2026 12:34:56 GMT"}
        ),
        fetched_at="2026-09-14T00:00:00+00:00",
        expected_min_records=4,
    )


def _submissions_payload(ticker: str, exchange: str, cik: int) -> dict[str, object]:
    return {
        "cik": f"{cik:010d}",
        "entityType": "operating",
        "tickers": [ticker],
        "exchanges": [exchange],
        "filings": {"recent": {
            "form": ["8-K", "10-Q"],
            "acceptanceDateTime": ["2026-09-01T12:00:00.000Z", "2026-08-01T12:00:00.000Z"],
        }},
    }


def _verified_record(index: int = 0) -> dict[str, object]:
    record = _staged_sec_records()[index]
    verified = verify_sec_candidate(
        record,
        http_get=lambda _url, **_kwargs: _SecResponse(
            _submissions_payload(
                str(record["ticker"]), str(record["exchange"]), int(record["cik"])
            )
        ),
        as_of="2026-09-14",
    )
    assert verified is not None
    return verified


def _fixture_candidate_verifier(
    record: dict[str, object], as_of: str
) -> dict[str, object] | None:
    return verify_sec_candidate(
        record,
        http_get=lambda _url, **_kwargs: _SecResponse(
            _submissions_payload(
                str(record["ticker"]), str(record["exchange"]), int(record["cik"])
            )
        ),
        as_of=as_of,
    )


def test_sec_submissions_verifies_operating_issuer_sole_exchange_ticker_and_periodic_filing() -> None:
    for record in _staged_sec_records():
        calls: list[dict[str, object]] = []

        def get(
            url: str,
            _calls: list[dict[str, object]] = calls,
            _record: dict[str, object] = record,
            **kwargs: object,
        ) -> _SecResponse:
            _calls.append({"url": url, **kwargs})
            return _SecResponse(
                _submissions_payload(
                    str(_record["ticker"]),
                    str(_record["exchange"]),
                    int(_record["cik"]),
                )
            )

        verified = verify_sec_candidate(record, http_get=get, as_of="2026-09-14")

        assert verified is not None
        assert verified["listing_status"] == "verified_operating_issuer_sole_exchange_ticker_periodic"
        assert verified["verification_source_url"] == SEC_SUBMISSIONS_URL_TEMPLATE.format(
            cik=int(record["cik"])
        )
        assert verified["verification_as_of"] == "2026-08-01T12:00:00.000Z"
        assert calls == [{
            "url": verified["verification_source_url"],
            "headers": {"User-Agent": SEC_USER_AGENT},
            "timeout": 10.0,
        }]


def test_sec_submissions_fails_closed_when_sole_ticker_operating_periodic_proof_is_missing() -> None:
    record = _staged_sec_records()[0]
    valid = _submissions_payload("ETN", "NYSE", int(record["cik"]))
    cases = [
        {**valid, "entityType": "other"},  # fund / ETF
        {**valid, "tickers": ["OTHER"], "exchanges": ["NYSE"]},
        {**valid, "tickers": ["ETN"], "exchanges": ["Nasdaq"]},
        {**valid, "filings": {"recent": {"form": ["8-K"], "acceptanceDateTime": ["2026-08-01T12:00:00Z"]}}},
        {**valid, "filings": {"recent": {"form": ["10-K"], "acceptanceDateTime": ["2020-01-01T12:00:00Z"]}}},
        {**valid, "cik": f"{int(record['cik']) + 1:010d}"},
        {**valid, "cik": int(record["cik"])},
        {**valid, "filings": {"recent": {"form": ["10-Q"], "acceptanceDateTime": ["2026-08-01T21:00:00+09:00"]}}},
        {"entityType": "operating", "tickers": "ETN", "exchanges": ["NYSE"]},
    ]

    for payload in cases:
        assert verify_sec_candidate(
            record,
            http_get=lambda _url, _payload=payload, **_kwargs: _SecResponse(_payload),
            as_of="2026-09-14",
        ) is None

    overwide_cik = 10_000_000_000
    assert verify_sec_candidate(
        {**record, "cik": overwide_cik},
        http_get=lambda _url, **_kwargs: _SecResponse(
            {**valid, "cik": str(overwide_cik)}
        ),
        as_of="2026-09-14",
    ) is None

    for suffix in ("WS", "U", "RT"):
        security = {**record, "ticker": f"ETN.{suffix}"}
        payload = {
            **valid,
            "tickers": ["ETN", security["ticker"]],
            "exchanges": ["NYSE", "NYSE"],
        }
        assert verify_sec_candidate(
            security,
            http_get=lambda _url, _payload=payload, **_kwargs: _SecResponse(_payload),
            as_of="2026-09-14",
        ) is None


def test_sec_submissions_rejects_every_multi_security_array_ordering() -> None:
    base = _staged_sec_records()[0]
    cases = [
        ({**base, "ticker": "ETN.WS"}, ["ETN.WS", "ETN"]),
        (base, ["ETN", "ETN.WS"]),
        (base, ["OTHER", "ETN", "ETN.WS"]),
    ]

    for record, tickers in cases:
        payload = {
            **_submissions_payload(
                str(record["ticker"]), "NYSE", int(record["cik"])
            ),
            "tickers": tickers,
            "exchanges": ["NYSE"] * len(tickers),
        }
        assert verify_sec_candidate(
            record,
            http_get=lambda _url, _payload=payload, **_kwargs: _SecResponse(_payload),
            as_of="2026-09-14",
        ) is None


def test_sec_fetch_stages_required_tickers_with_exact_official_provenance() -> None:
    calls: list[dict[str, object]] = []

    def get(url: str, **kwargs: object) -> _SecResponse:
        calls.append({"url": url, **kwargs})
        return _SecResponse(_sec_payload(), {"Last-Modified": "Fri, 11 Sep 2026 12:34:56 GMT"})

    staged = fetch_sec_security_master(
        http_get=get,
        fetched_at="2026-09-14T00:00:00+00:00",
        expected_min_records=4,
    )

    assert calls == [{
        "url": SEC_COMPANY_TICKERS_URL,
        "headers": {"User-Agent": SEC_USER_AGENT},
        "timeout": 10.0,
    }]
    assert [record["ticker"] for record in staged] == ["ETN", "SMCI", "DELL", "AI"]
    assert staged[1] == {
        "ticker": "SMCI",
        "company_name": "Super Micro Computer, Inc.",
        "cik": 1375365,
        "exchange": "Nasdaq",
        "listing_status": None,
        "source_url": SEC_COMPANY_TICKERS_URL,
        "source_as_of": "Fri, 11 Sep 2026 12:34:56 GMT",
        "fetched_at": "2026-09-14T00:00:00+00:00",
        "source_status": "current",
    }


def test_sec_fetch_rejects_malformed_source_instead_of_staging_verification() -> None:
    def get(_url: str, **_kwargs: object) -> _SecResponse:
        return _SecResponse({"fields": ["cik", "name", "ticker"], "data": [[1, "Bad", "BAD"]]})

    try:
        fetch_sec_security_master(http_get=get, fetched_at="2026-09-14T00:00:00+00:00")
    except SecSecurityMasterError as exc:
        assert "schema" in str(exc).lower()
    else:
        raise AssertionError("malformed SEC data must fail closed")


def test_sec_fetch_rejects_empty_dataset_instead_of_staling_valid_records() -> None:
    def get(_url: str, **_kwargs: object) -> _SecResponse:
        return _SecResponse({"fields": ["cik", "name", "ticker", "exchange"], "data": []})

    try:
        fetch_sec_security_master(http_get=get, fetched_at="2026-09-14T00:00:00+00:00")
    except SecSecurityMasterError as exc:
        assert "empty" in str(exc).lower()
    else:
        raise AssertionError("empty SEC data must fail closed")


def test_sec_fetch_rejects_structurally_valid_truncated_payload() -> None:
    payload = {
        "fields": ["cik", "name", "ticker", "exchange"],
        "data": [[1551182, "Eaton Corp plc", "ETN", "NYSE"]],
    }

    try:
        fetch_sec_security_master(
            http_get=lambda _url, **_kwargs: _SecResponse(payload),
            fetched_at="2026-09-14T00:00:00+00:00",
            expected_min_records=4,
        )
    except SecSecurityMasterError as exc:
        assert "incomplete" in str(exc).lower()
    else:
        raise AssertionError("truncated SEC data must fail closed")


def test_sec_fetch_rejects_unknown_exchange_vocabulary() -> None:
    payload = {
        "fields": ["cik", "name", "ticker", "exchange"],
        "data": [[1551182, "Eaton Corp plc", "ETN", "Bogus"]],
    }

    try:
        fetch_sec_security_master(
            http_get=lambda _url, **_kwargs: _SecResponse(payload),
            fetched_at="2026-09-14T00:00:00+00:00",
            expected_min_records=1,
        )
    except SecSecurityMasterError as exc:
        assert "exchange" in str(exc).lower()
    else:
        raise AssertionError("unknown exchange vocabulary must fail closed")


def test_keyword_level_emerging_signal_is_never_promoted_to_theme_score() -> None:
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
    assert first["status"] == "insufficient_evidence"
    assert first["score"] is None
    assert first["rank"] is None
    assert first["signal_kind"] == "emerging_signal"
    assert first["strict_grouping_evidence"] is False
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
        "ticker": "ETN",
        "market": "US",
        "security_master_record": _verified_record(),
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

    unverified = build_candidate_snapshot({**base, "security_master_record": None}, pipeline_run_id=7)
    assert unverified["status"] == "unverified_us_listing"
    assert unverified["score"] is None

    non_us = build_candidate_snapshot({**base, "market": "KR"}, pipeline_run_id=7)
    assert non_us["status"] == "ineligible_market"
    assert non_us["score"] is None


def test_candidate_rejects_unverified_company_ticker_mapping() -> None:
    source = {
        "ticker": "ETN", "market": "US", "hypothesis_category": "buy",
        "thesis_id": 1, "raw_item_ids": [1, 2], "sources": ["rss", "arxiv"],
        "as_of": "2026-08-01",
    }

    raw = build_candidate_snapshot(
        {**source, "security_master_record": _staged_sec_records()[0]}, 7
    )
    forged = build_candidate_snapshot(
        {**source, "security_master_record": {
            **_staged_sec_records()[0], "listing_status": VERIFIED_LISTING_STATUS,
        }}, 7
    )
    verified = build_candidate_snapshot(
        {**source, "security_master_record": _verified_record()}, 7
    )

    assert raw["status"] == "unverified_us_listing"
    assert raw["security_master_ticker"] is None
    assert forged["status"] == "unverified_us_listing"
    assert forged["security_master_ticker"] is None
    assert verified["status"] == "insufficient_evidence"
    assert verified["security_master_ticker"] == "ETN"


def test_candidate_does_not_infer_listing_when_sec_exchange_is_absent() -> None:
    record = {**_staged_sec_records()[0], "exchange": None}
    snapshot = build_candidate_snapshot(
        {
            "ticker": "ETN",
            "market": "US",
            "security_master_record": record,
            "hypothesis_category": "buy",
            "thesis_id": 1,
            "raw_item_ids": [1, 2],
            "sources": ["rss", "arxiv"],
            "as_of": "2026-08-01",
        },
        pipeline_run_id=7,
    )

    assert snapshot["status"] == "unverified_us_listing"
    assert snapshot["security_master_ticker"] is None


def test_candidate_rejects_otc_and_unknown_exchanges() -> None:
    base = {
        "ticker": "ETN", "market": "US", "hypothesis_category": "buy",
        "thesis_id": 1, "raw_item_ids": [1, 2], "sources": ["rss", "arxiv"],
        "as_of": "2026-08-01",
    }
    record = _staged_sec_records()[0]

    for exchange in ("OTC", "Bogus", "", None):
        snapshot = build_candidate_snapshot(
            {**base, "security_master_record": {**record, "exchange": exchange}}, 7
        )
        assert snapshot["status"] == "unverified_us_listing"
        assert snapshot["security_master_ticker"] is None


def test_persistence_rolls_back_when_verified_candidate_identity_cannot_resolve() -> None:
    for complete_evidence in (True, False):
        conn = _conn()
        conn.execute("INSERT INTO pipeline_runs (run_type, started_at) VALUES ('daily', '2026-08-01')")
        conn.commit()
        raw_ids = [1, 2] if complete_evidence else []
        sources = ["rss", "arxiv"] if complete_evidence else []
        candidate = build_candidate_snapshot({
            "ticker": "ETN", "market": "US", "security_master_record": _verified_record(),
            "hypothesis_category": "buy", "thesis_id": 1,
            "company_analysis_id": None, "raw_item_ids": raw_ids, "sources": sources,
            "as_of": "2026-08-01", "theme_exposure": 80, "revenue_evidence": 70,
            "bottleneck_evidence": 60, "pricing_unreflected": 50,
            "attention_acceleration": 40, "source_confirmation": 75,
            "liquidity_listing": 100,
        }, 1)
        assert candidate["status"] == ("scored" if complete_evidence else "insufficient_evidence")
        if not complete_evidence:
            candidate["security_master_identity"] = None
            assert VERIFIED_LISTING_STATUS in candidate["coverage"]["present"]
        mismatched_version = {
            **_verified_record(),
            "source_as_of": "Sat, 12 Sep 2026 12:34:56 GMT",
            "fetched_at": "2026-09-15T00:00:00+00:00",
        }

        try:
            persist_discovery_snapshots(conn, [], [candidate], [mismatched_version])
        except RuntimeError as exc:
            assert "security master identity" in str(exc).lower()
        else:
            raise AssertionError("verified listing without exact provenance must fail closed")

        assert conn.execute("SELECT COUNT(*) FROM security_master").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM candidate_snapshots").fetchone()[0] == 0


def test_persisted_snapshots_keep_evidence_links_and_no_rank_for_unscored() -> None:
    conn = _conn()
    conn.execute("INSERT INTO pipeline_runs (run_type, started_at) VALUES ('daily', '2026-08-01T00:00:00+00:00')")
    conn.execute("INSERT INTO raw_items (source, source_id, title, published_at) VALUES ('rss', 'a', 'A', '2026-08-01T00:00:00+00:00')")
    conn.execute("INSERT INTO raw_items (source, source_id, title, published_at) VALUES ('arxiv', 'b', 'B', '2026-08-01T00:00:00+00:00')")
    conn.execute("INSERT INTO raw_items (source, source_id, title, published_at) VALUES ('github', 'c', 'C', '2026-08-01T00:00:00+00:00')")
    conn.commit()
    theme = build_theme_snapshot({"keyword": "cooling", "z_score": 3.0, "raw_item_ids": [1, 2, 3], "sources": ["rss", "arxiv", "github"], "as_of": "2026-08-01"}, pipeline_run_id=1)
    candidate = build_candidate_snapshot({"ticker": "ACME", "market": "US", "security_master_record": None, "hypothesis_category": "avoid", "thesis_id": None, "company_analysis_id": None, "raw_item_ids": [1, 2], "sources": ["rss", "arxiv"], "as_of": "2026-08-01"}, pipeline_run_id=1)

    persist_discovery_snapshots(conn, [theme], [candidate])
    row = conn.execute("SELECT score, rank, evidence_raw_item_ids, hypothesis_category FROM candidate_snapshots").fetchone()
    assert row["score"] is None
    assert row["rank"] is None
    assert json.loads(row["evidence_raw_item_ids"]) == [1, 2]
    assert row["hypothesis_category"] == "avoid"


def test_snapshot_persistence_uses_an_explicit_short_write_transaction() -> None:
    conn = _conn()
    conn.execute("INSERT INTO pipeline_runs (run_type, started_at) VALUES ('daily', '2026-08-01')")
    conn.commit()
    statements: list[str] = []
    conn.set_trace_callback(statements.append)

    persist_discovery_snapshots(conn, [], [])

    assert any(statement == "BEGIN IMMEDIATE" for statement in statements)
    assert statements[-1] == "COMMIT"


def test_daily_flow_snapshot_helper_uses_the_current_run_connection(monkeypatch) -> None:
    from pipeline import main

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

    assert write_discovery_snapshots(
        conn,
        pipeline_run_id=1,
        as_of="2026-08-01",
        security_master_fetcher=_staged_sec_records,
    ) == {"themes": 0, "candidates": 1}
    row = conn.execute("SELECT thesis_id, company_analysis_id, evidence_raw_item_ids, status FROM candidate_snapshots").fetchone()
    assert row["thesis_id"] == 1
    assert row["company_analysis_id"] == 1
    assert json.loads(row["evidence_raw_item_ids"]) == [1]
    assert row["status"] == "unverified_us_listing"


def test_writer_uses_host_business_date_at_0700_kst_when_utc_is_previous_day() -> None:
    conn = _conn()
    conn.execute("INSERT INTO pipeline_runs (run_type, started_at) VALUES ('daily', '2026-09-14')")
    conn.execute("""INSERT INTO investment_theses
                    (thesis_date, direction, company, ticker, market, reasoning, model_used, driving_signals)
                    VALUES ('2026-09-14', 'buy', 'Eaton', 'ETN', 'US', 'reason', 'fixture', '[]')""")
    conn.commit()
    kst = timezone(timedelta(hours=9))
    local_now = datetime(2026, 9, 14, 7, 0, tzinfo=kst)
    assert local_now.astimezone(UTC).date().isoformat() == "2026-09-13"

    result = write_discovery_snapshots(
        conn,
        pipeline_run_id=1,
        security_master_fetcher=list,
        business_date_provider=lambda: local_now.date(),
    )

    assert result == {"themes": 0, "candidates": 1}
    snapshot = conn.execute("SELECT ticker, as_of FROM candidate_snapshots").fetchone()
    assert dict(snapshot) == {"ticker": "ETN", "as_of": "2026-09-14"}


def test_database_reader_is_read_only_when_no_pipeline_run_is_provided() -> None:
    conn = _conn()
    before = conn.total_changes
    assert write_discovery_snapshots(conn, pipeline_run_id=None, as_of="2026-08-01") == {"themes": 0, "candidates": 0}
    assert conn.total_changes == before


def test_writer_verifies_required_sec_tickers_and_persists_exact_provenance() -> None:
    conn = _conn()
    conn.execute("INSERT INTO pipeline_runs (run_type, started_at) VALUES ('daily', '2026-08-01T00:00:00+00:00')")
    for ticker, market in [
        ("ETN", "US"), ("SMCI", "US"), ("DELL", "US"), ("AI", "US"),
        ("UNKNOWN", "US"), ("005930", "KR"),
    ]:
        conn.execute(
            """INSERT INTO investment_theses
               (thesis_date, direction, company, ticker, market, reasoning, model_used, driving_signals)
               VALUES ('2026-08-01', 'buy', ?, ?, ?, 'reason', 'fixture', '[]')""",
            (ticker, ticker, market),
        )
    conn.commit()

    result = write_discovery_snapshots(
        conn,
        pipeline_run_id=1,
        as_of="2026-08-01",
        security_master_fetcher=_staged_sec_records,
        candidate_verifier=_fixture_candidate_verifier,
    )

    assert result == {"themes": 0, "candidates": 6}
    snapshots = {
        row["ticker"]: dict(row)
        for row in conn.execute(
            "SELECT ticker, status, security_master_id, coverage FROM candidate_snapshots"
        )
    }
    for ticker in ("ETN", "SMCI", "DELL", "AI"):
        assert snapshots[ticker]["status"] == "insufficient_evidence"
        assert snapshots[ticker]["security_master_id"] is not None
        assert "verified_operating_issuer_sole_exchange_ticker_periodic" in json.loads(snapshots[ticker]["coverage"])["present"]
    assert snapshots["UNKNOWN"]["status"] == "unverified_us_listing"
    assert snapshots["UNKNOWN"]["security_master_id"] is None
    assert snapshots["005930"]["status"] == "ineligible_market"
    assert snapshots["005930"]["security_master_id"] is None

    smci = conn.execute("SELECT * FROM security_master WHERE ticker = 'SMCI'").fetchone()
    assert {
        key: smci[key]
        for key in (
            "ticker", "company_name", "cik", "exchange", "listing_status",
            "verification_source_url", "verification_as_of",
            "source_url", "source_as_of", "fetched_at", "source_status",
        )
    } == {
        key: _verified_record(1)[key]
        for key in (
            "ticker", "company_name", "cik", "exchange", "listing_status",
            "verification_source_url", "verification_as_of",
            "source_url", "source_as_of", "fetched_at", "source_status",
        )
    }


def test_candidate_verification_network_failure_is_outside_transaction_and_logged(caplog) -> None:
    conn = _conn()
    conn.execute("INSERT INTO pipeline_runs (run_type, started_at) VALUES ('daily', '2026-08-01')")
    conn.execute("""INSERT INTO investment_theses
                    (thesis_date, direction, company, ticker, market, reasoning, model_used, driving_signals)
                    VALUES ('2026-08-01', 'buy', 'Eaton', 'ETN', 'US', 'reason', 'fixture', '[]')""")
    conn.commit()
    before = conn.total_changes

    def fail(record: dict[str, object], as_of: str) -> None:
        assert record["ticker"] == "ETN"
        assert as_of == "2026-08-01"
        assert not conn.in_transaction
        assert conn.total_changes == before
        raise SecSecurityMasterError("private upstream detail")

    write_discovery_snapshots(
        conn, 1, "2026-08-01",
        security_master_fetcher=lambda: [_staged_sec_records()[0]],
        candidate_verifier=fail,
        request_interval_seconds=0,
    )

    snapshot = conn.execute(
        "SELECT status, security_master_id FROM candidate_snapshots WHERE ticker='ETN'"
    ).fetchone()
    assert dict(snapshot) == {
        "status": "unverified_us_listing", "security_master_id": None,
    }
    assert "SEC candidate listing verification failed for ETN" in caplog.text
    assert "private upstream detail" not in caplog.text


def test_network_failure_happens_before_writes_and_preserves_unverified_candidate(caplog) -> None:
    conn = _conn()
    conn.execute("INSERT INTO pipeline_runs (run_type, started_at) VALUES ('daily', '2026-08-01T00:00:00+00:00')")
    conn.execute("""INSERT INTO investment_theses
                    (thesis_date, direction, company, ticker, market, reasoning, model_used, driving_signals)
                    VALUES ('2026-08-01', 'buy', 'Eaton', 'ETN', 'US', 'reason', 'fixture', '[]')""")
    conn.commit()
    persist_discovery_snapshots(conn, [], [], [_verified_record()])
    before = conn.total_changes

    def fail_fetch() -> list[dict[str, object]]:
        assert not conn.in_transaction
        assert conn.total_changes == before
        raise SecSecurityMasterError("network unavailable")

    result = write_discovery_snapshots(
        conn,
        pipeline_run_id=1,
        as_of="2026-08-01",
        security_master_fetcher=fail_fetch,
    )

    assert result == {"themes": 0, "candidates": 1}
    stored = conn.execute(
        "SELECT source_status FROM security_master WHERE ticker = 'ETN'"
    ).fetchone()
    assert stored["source_status"] == "current"
    snapshot = conn.execute(
        "SELECT status, security_master_id FROM candidate_snapshots WHERE ticker = 'ETN'"
    ).fetchone()
    assert dict(snapshot) == {
        "status": "unverified_us_listing",
        "security_master_id": None,
    }
    assert "SEC security-master refresh failed" in caplog.text
    assert "network unavailable" not in caplog.text


def test_writer_refuses_network_fetch_during_an_open_business_transaction() -> None:
    conn = _conn()
    conn.execute("INSERT INTO pipeline_runs (run_type, started_at) VALUES ('daily', '2026-08-01T00:00:00+00:00')")
    fetch_calls = 0

    def fetch() -> list[dict[str, object]]:
        nonlocal fetch_calls
        fetch_calls += 1
        return _staged_sec_records()

    try:
        write_discovery_snapshots(
            conn,
            pipeline_run_id=1,
            as_of="2026-08-01",
            security_master_fetcher=fetch,
        )
    except RuntimeError as exc:
        assert "transaction" in str(exc).lower()
    else:
        raise AssertionError("network fetch must not run inside a business transaction")

    assert fetch_calls == 0
    assert conn.execute("SELECT COUNT(*) FROM security_master").fetchone()[0] == 0


def test_security_master_refresh_preserves_snapshot_source_version() -> None:
    conn = _conn()
    for day in ("2026-08-01", "2026-08-02"):
        conn.execute("INSERT INTO pipeline_runs (run_type, started_at) VALUES ('daily', ?)", (day,))
        conn.execute(
            """INSERT INTO investment_theses
               (thesis_date, direction, company, ticker, market, reasoning, model_used, driving_signals)
               VALUES (?, 'buy', 'Eaton', 'ETN', 'US', 'reason', 'fixture', '[]')""",
            (day,),
        )
    conn.commit()
    first = _staged_sec_records()[0]
    second = {
        **first,
        "source_as_of": "Sat, 12 Sep 2026 12:34:56 GMT",
        "fetched_at": "2026-09-15T00:00:00+00:00",
    }

    write_discovery_snapshots(
        conn, 1, "2026-08-01", security_master_fetcher=lambda: [first],
        candidate_verifier=_fixture_candidate_verifier, request_interval_seconds=0,
    )
    write_discovery_snapshots(
        conn, 2, "2026-08-02", security_master_fetcher=lambda: [second],
        candidate_verifier=_fixture_candidate_verifier, request_interval_seconds=0,
    )

    rows = conn.execute(
        """SELECT cs.as_of, sm.source_as_of, sm.fetched_at, sm.source_status
           FROM candidate_snapshots cs
           JOIN security_master sm ON sm.id = cs.security_master_id
           ORDER BY cs.as_of"""
    ).fetchall()
    assert [dict(row) for row in rows] == [
        {
            "as_of": "2026-08-01",
            "source_as_of": first["source_as_of"],
            "fetched_at": first["fetched_at"],
            "source_status": "stale",
        },
        {
            "as_of": "2026-08-02",
            "source_as_of": second["source_as_of"],
            "fetched_at": second["fetched_at"],
            "source_status": "current",
        },
    ]


def test_unchanged_sec_source_version_is_reused_without_duplicate_rows() -> None:
    conn = _conn()
    conn.execute("INSERT INTO pipeline_runs (run_type, started_at) VALUES ('daily', '2026-08-01')")
    conn.commit()
    first = _verified_record()
    refetched = {**first, "fetched_at": "2026-09-15T00:00:00+00:00"}
    candidate = build_candidate_snapshot({
        "ticker": "ETN", "market": "US", "security_master_record": refetched,
        "hypothesis_category": "buy", "thesis_id": None, "company_analysis_id": None,
        "raw_item_ids": [], "sources": [], "as_of": "2026-08-01",
    }, 1)

    persist_discovery_snapshots(conn, [], [], [first])
    persist_discovery_snapshots(conn, [], [candidate], [refetched])

    rows = conn.execute(
        "SELECT id, fetched_at, source_status FROM security_master WHERE ticker = 'ETN'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["fetched_at"] == first["fetched_at"]
    assert rows[0]["source_status"] == "current"
    snapshot_id = conn.execute(
        "SELECT security_master_id FROM candidate_snapshots WHERE ticker = 'ETN'"
    ).fetchone()[0]
    assert snapshot_id == rows[0]["id"]


def test_missing_last_modified_uses_fetch_time_versions_without_ambiguous_links() -> None:
    conn = _conn()
    for day in ("2026-08-01", "2026-08-02"):
        conn.execute("INSERT INTO pipeline_runs (run_type, started_at) VALUES ('daily', ?)", (day,))
    conn.commit()
    first = {**_verified_record(), "source_as_of": None}
    second = {**first, "fetched_at": "2026-09-15T00:00:00+00:00"}
    first_candidate = build_candidate_snapshot({
        "ticker": "ETN", "market": "US", "security_master_record": first,
        "hypothesis_category": "buy", "thesis_id": None, "company_analysis_id": None,
        "raw_item_ids": [], "sources": [], "as_of": "2026-08-01",
    }, 1)
    second_candidate = build_candidate_snapshot({
        "ticker": "ETN", "market": "US", "security_master_record": second,
        "hypothesis_category": "buy", "thesis_id": None, "company_analysis_id": None,
        "raw_item_ids": [], "sources": [], "as_of": "2026-08-02",
    }, 2)

    persist_discovery_snapshots(conn, [], [first_candidate], [first])
    persist_discovery_snapshots(conn, [], [second_candidate], [second])

    rows = conn.execute(
        """SELECT cs.as_of, sm.fetched_at, sm.source_status
           FROM candidate_snapshots cs
           JOIN security_master sm ON sm.id = cs.security_master_id
           ORDER BY cs.as_of"""
    ).fetchall()
    assert [dict(row) for row in rows] == [
        {"as_of": "2026-08-01", "fetched_at": first["fetched_at"], "source_status": "stale"},
        {"as_of": "2026-08-02", "fetched_at": second["fetched_at"], "source_status": "current"},
    ]
    assert conn.execute(
        "SELECT COUNT(*) FROM security_master WHERE ticker='ETN'"
    ).fetchone()[0] == 2


def test_same_sec_version_verification_failure_never_downgrades_prior_verified_row() -> None:
    conn = _conn()
    for day in ("2026-08-01", "2026-08-02"):
        conn.execute("INSERT INTO pipeline_runs (run_type, started_at) VALUES ('daily', ?)", (day,))
        conn.execute(
            """INSERT INTO investment_theses
               (thesis_date, direction, company, ticker, market, reasoning, model_used, driving_signals)
               VALUES (?, 'buy', 'Eaton', 'ETN', 'US', 'reason', 'fixture', '[]')""",
            (day,),
        )
    conn.commit()
    first = _staged_sec_records()[0]
    refetched_same_version = {**first, "fetched_at": "2026-09-15T00:00:00+00:00"}

    write_discovery_snapshots(
        conn, 1, "2026-08-01", security_master_fetcher=lambda: [first],
        candidate_verifier=_fixture_candidate_verifier, request_interval_seconds=0,
    )
    prior = conn.execute("SELECT * FROM security_master WHERE ticker='ETN'").fetchone()
    prior_snapshot_fk = conn.execute(
        "SELECT security_master_id FROM candidate_snapshots WHERE as_of='2026-08-01'"
    ).fetchone()[0]

    def fail(_record: dict[str, object], _as_of: str) -> None:
        raise SecSecurityMasterError("submissions unavailable")

    write_discovery_snapshots(
        conn, 2, "2026-08-02", security_master_fetcher=lambda: [refetched_same_version],
        candidate_verifier=fail, request_interval_seconds=0,
    )

    after = conn.execute("SELECT * FROM security_master WHERE ticker='ETN'").fetchone()
    assert dict(after) == dict(prior)
    assert conn.execute(
        "SELECT security_master_id FROM candidate_snapshots WHERE as_of='2026-08-01'"
    ).fetchone()[0] == prior_snapshot_fk
    current = conn.execute(
        "SELECT status, security_master_id FROM candidate_snapshots WHERE as_of='2026-08-02'"
    ).fetchone()
    assert dict(current) == {
        "status": "unverified_us_listing",
        "security_master_id": None,
    }


def test_same_snapshot_retry_never_replaces_verified_provenance_with_unverified_data() -> None:
    conn = _conn()
    conn.execute("INSERT INTO pipeline_runs (run_type, started_at) VALUES ('daily', '2026-08-01')")
    conn.execute(
        """INSERT INTO investment_theses
           (thesis_date, direction, company, ticker, market, reasoning, model_used, driving_signals)
           VALUES ('2026-08-01', 'buy', 'Eaton', 'ETN', 'US', 'reason', 'fixture', '[]')"""
    )
    conn.commit()

    write_discovery_snapshots(
        conn, 1, "2026-08-01", security_master_fetcher=_staged_sec_records,
        candidate_verifier=_fixture_candidate_verifier, request_interval_seconds=0,
    )
    prior = dict(conn.execute(
        """SELECT status, coverage, security_master_id
           FROM candidate_snapshots WHERE ticker='ETN' AND pipeline_run_id=1"""
    ).fetchone())
    assert prior["security_master_id"] is not None

    def fail_fetch() -> list[dict[str, object]]:
        raise SecSecurityMasterError("temporary failure")

    write_discovery_snapshots(
        conn, 1, "2026-08-01", security_master_fetcher=fail_fetch,
        request_interval_seconds=0,
    )

    after = dict(conn.execute(
        """SELECT status, coverage, security_master_id
           FROM candidate_snapshots WHERE ticker='ETN' AND pipeline_run_id=1"""
    ).fetchone())
    assert after == prior


def test_partial_retry_recomputes_unique_ranks_from_effective_preserved_rows() -> None:
    conn = _conn()
    conn.execute("INSERT INTO pipeline_runs (run_type, started_at) VALUES ('daily', '2026-08-01')")
    conn.commit()
    etn_record = _verified_record(0)
    dell_record = _verified_record(2)

    def candidate(
        ticker: str,
        record: dict[str, object] | None,
        strength: float,
    ) -> dict:
        return build_candidate_snapshot({
            "ticker": ticker, "market": "US", "security_master_record": record,
            "hypothesis_category": "buy", "thesis_id": 1, "company_analysis_id": None,
            "raw_item_ids": [1, 2], "sources": ["rss", "arxiv"],
            "as_of": "2026-08-01", "theme_exposure": strength,
            "revenue_evidence": strength, "bottleneck_evidence": strength,
            "pricing_unreflected": strength, "attention_acceleration": strength,
            "source_confirmation": strength, "liquidity_listing": strength,
        }, 1)

    persist_discovery_snapshots(
        conn,
        [],
        [candidate("ETN", etn_record, 90), candidate("DELL", dell_record, 80)],
        [etn_record, dell_record],
    )
    assert [tuple(row) for row in conn.execute(
        "SELECT ticker, rank FROM candidate_snapshots ORDER BY rank"
    )] == [("ETN", 1), ("DELL", 2)]

    unverified_etn_mapping = {
        key: value for key, value in etn_record.items()
        if key not in {"listing_status", "verification_source_url", "verification_as_of"}
    }
    unverified_etn_mapping["listing_status"] = None
    persist_discovery_snapshots(
        conn,
        [],
        [candidate("ETN", None, 90), candidate("DELL", dell_record, 80)],
        [unverified_etn_mapping, dell_record],
    )

    assert [tuple(row) for row in conn.execute(
        "SELECT ticker, rank FROM candidate_snapshots ORDER BY rank"
    )] == [("ETN", 1), ("DELL", 2)]
    assert conn.execute(
        "SELECT COUNT(DISTINCT rank) FROM candidate_snapshots WHERE score IS NOT NULL"
    ).fetchone()[0] == 2


def test_candidate_fk_uses_exact_staged_source_version_not_current_ticker_lookup() -> None:
    conn = _conn()
    conn.execute("INSERT INTO pipeline_runs (run_type, started_at) VALUES ('daily', '2026-08-01')")
    conn.commit()
    first = _verified_record()
    second = {
        **first,
        "source_as_of": "Sat, 12 Sep 2026 12:34:56 GMT",
        "fetched_at": "2026-09-15T00:00:00+00:00",
    }
    candidate = build_candidate_snapshot({
        "ticker": "ETN", "market": "US", "security_master_record": first,
        "hypothesis_category": "buy", "thesis_id": None, "company_analysis_id": None,
        "raw_item_ids": [], "sources": [], "as_of": "2026-08-01",
    }, 1)

    persist_discovery_snapshots(conn, [], [candidate], [first, second])

    linked = conn.execute(
        """SELECT sm.source_as_of FROM candidate_snapshots cs
           JOIN security_master sm ON sm.id = cs.security_master_id
           WHERE cs.ticker = 'ETN'"""
    ).fetchone()[0]
    assert linked == first["source_as_of"]
