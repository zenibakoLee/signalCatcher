"""Deterministic, preliminary discovery snapshots from persisted evidence.

This module deliberately records insufficient coverage instead of inventing a rank.
It does not use LLM text as score authority and does not infer US listing status.
The verified listing coverage key means only that SEC submissions proved an
operating issuer with one eligible exchange/ticker pair and a recent periodic
filing; it is not common-stock or primary-instrument classification.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from collections.abc import Callable, Iterable
from datetime import date

from pipeline.processing.sec_security_master import (
    SEC_COMPANY_TICKERS_URL,
    SEC_SUBMISSIONS_URL_TEMPLATE,
    VERIFIED_LISTING_STATUS,
    SecSecurityMasterError,
    fetch_sec_security_master,
    verify_sec_candidate,
)

FEATURE_VERSION = "discovery-preliminary-v1"
_SIGNAL_KIND = "emerging_signal"
_US_TICKER = re.compile(r"^[A-Z][A-Z.]{0,9}$")
_ELIGIBLE_EXCHANGES = {"NYSE", "Nasdaq"}
logger = logging.getLogger(__name__)
_MAX_CANDIDATE_VERIFICATIONS = 50
_SEC_REQUEST_INTERVAL_SECONDS = 0.11


def _verify_candidate_from_sec(
    record: dict[str, object], as_of: str
) -> dict[str, object] | None:
    return verify_sec_candidate(record, as_of=as_of)


def _bounded(value: object) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError):
        return None


def _unique_ints(values: Iterable[object]) -> list[int]:
    return sorted({int(value) for value in values if isinstance(value, int) and value > 0})


def _unique_strings(values: Iterable[object]) -> list[str]:
    return sorted({str(value).strip().lower() for value in values if str(value).strip()})


def _coverage(required: dict[str, object]) -> dict[str, object]:
    missing = sorted(name for name, value in required.items() if value is None or value is False)
    return {"required": sorted(required), "present": sorted(set(required) - set(missing)), "missing": missing, "ratio": round((len(required) - len(missing)) / len(required), 4) if required else 0.0}


def _security_identity(record: dict[str, object]) -> tuple[object, object, object]:
    return (
        record.get("ticker"),
        record.get("source_url"),
        record.get("source_as_of") or record.get("fetched_at"),
    )


def build_theme_snapshot(source: dict, pipeline_run_id: int) -> dict:
    """Build one emerging-signal snapshot; trend keywords never become themes implicitly."""
    raw_ids = _unique_ints(source.get("raw_item_ids", []))
    sources = _unique_strings(source.get("sources", []))
    z_score = source.get("z_score")
    acceleration = None if z_score is None else _bounded(float(z_score) / 5 * 100)
    breadth = _bounded(len(sources) / 3 * 100)
    evidence_quality = _bounded(len(raw_ids) / 3 * 100)
    feature_values = {
        "acceleration": acceleration,
        "source_breadth": len(sources),
        "source_breadth_score": breadth,
        "raw_evidence_count": len(raw_ids),
        "evidence_quality": evidence_quality,
        "z_score": z_score,
        "score_components": {"acceleration": 25, "source_breadth": 20, "evidence_quality": 15},
    }
    required = {"acceleration": acceleration, "independent_sources": len(sources) >= 2, "raw_evidence": len(raw_ids) >= 2}
    coverage = _coverage(required)
    # A keyword-level trend is a preliminary signal, not a grouped investable
    # theme. Preserve its measured inputs for monitoring, but never normalize
    # this partial feature set into a ThemeScore.
    score = None
    return {
        "theme_key": source["keyword"].strip().lower(), "label": source["keyword"].strip(),
        "signal_kind": _SIGNAL_KIND, "strict_grouping_evidence": False,
        "as_of": source["as_of"], "pipeline_run_id": pipeline_run_id,
        "feature_version": FEATURE_VERSION, "status": "insufficient_evidence",
        "score": score, "rank": None, "coverage": coverage, "feature_values": feature_values,
        "evidence_raw_item_ids": raw_ids, "evidence_sources": sources,
    }


def build_candidate_snapshot(source: dict, pipeline_run_id: int) -> dict:
    """Build only an explicitly verified US-listing candidate snapshot."""
    ticker = str(source.get("ticker") or "").strip().upper()
    market = str(source.get("market") or "").strip().upper()
    raw_ids = _unique_ints(source.get("raw_item_ids", []))
    sources = _unique_strings(source.get("sources", []))
    values = {name: _bounded(source.get(name)) for name in (
        "theme_exposure", "revenue_evidence", "bottleneck_evidence", "pricing_unreflected",
        "attention_acceleration", "source_confirmation", "liquidity_listing",
    )}
    feature_values = {**values, "raw_evidence_count": len(raw_ids), "source_breadth": len(sources),
                      "score_components": {"theme_exposure": 25, "revenue_evidence": 20, "bottleneck_evidence": 15, "pricing_unreflected": 15, "attention_acceleration": 10, "source_confirmation": 10, "liquidity_listing": 5}}
    security_record = source.get("security_master_record")
    listing_ok = (
        isinstance(security_record, dict)
        and security_record.get("ticker") == ticker
        and security_record.get("source_url") == SEC_COMPANY_TICKERS_URL
        and security_record.get("source_status") == "current"
        and security_record.get("exchange") in _ELIGIBLE_EXCHANGES
        and security_record.get("listing_status") == VERIFIED_LISTING_STATUS
        and isinstance(security_record.get("cik"), int)
        and security_record.get("verification_source_url")
            == SEC_SUBMISSIONS_URL_TEMPLATE.format(cik=security_record["cik"])
        and isinstance(security_record.get("verification_as_of"), str)
        and bool(str(security_record["verification_as_of"]).strip())
        and market == "US"
        and bool(_US_TICKER.fullmatch(ticker))
    )
    coverage = _coverage({**values, VERIFIED_LISTING_STATUS: listing_ok, "thesis_link": source.get("thesis_id") is not None, "raw_evidence": len(raw_ids) >= 2, "independent_sources": len(sources) >= 2})
    if market != "US" or not _US_TICKER.fullmatch(ticker):
        status = "ineligible_market"
    elif not listing_ok:
        status = "unverified_us_listing"
    elif coverage["missing"]:
        status = "insufficient_evidence"
    else:
        status = "scored"
    score = None
    if status == "scored":
        weights = feature_values["score_components"]
        score = round(sum(values[name] * weight for name, weight in weights.items()) / sum(weights.values()), 2)
    return {"ticker": ticker, "market": market, "hypothesis_category": source.get("hypothesis_category") if source.get("hypothesis_category") in {"buy", "avoid"} else None,
            "thesis_id": source.get("thesis_id"), "company_analysis_id": source.get("company_analysis_id"),
            "security_master_ticker": ticker if listing_ok else None,
            "security_master_identity": _security_identity(security_record) if listing_ok else None,
            "as_of": source["as_of"], "pipeline_run_id": pipeline_run_id, "feature_version": FEATURE_VERSION,
            "status": status, "score": score, "rank": None, "coverage": coverage, "feature_values": feature_values,
            "evidence_raw_item_ids": raw_ids, "evidence_sources": sources}


def persist_discovery_snapshots(
    conn: sqlite3.Connection,
    themes: list[dict],
    candidates: list[dict],
    security_master_records: list[dict[str, object]] | None = None,
) -> None:
    """Persist prepared records atomically; ranks exist only for scored candidates."""
    if conn.in_transaction:
        raise RuntimeError("snapshot persistence requires no open transaction")
    try:
        conn.execute("BEGIN IMMEDIATE")
        security_master_records = security_master_records or []
        if security_master_records:
            conn.execute(
                "UPDATE security_master SET source_status = 'stale' WHERE source_url = ?",
                (SEC_COMPANY_TICKERS_URL,),
            )
        security_ids: dict[tuple[object, object, object], int] = {}
        for record in security_master_records:
            identity = _security_identity(record)
            if record.get("source_as_of"):
                existing = conn.execute(
                    """SELECT id FROM security_master
                       WHERE ticker = ? AND source_url = ? AND source_as_of = ?""",
                    identity,
                ).fetchone()
            else:
                existing = conn.execute(
                    """SELECT id FROM security_master
                       WHERE ticker = ? AND source_url = ? AND source_as_of IS NULL
                         AND fetched_at = ?""",
                    identity,
                ).fetchone()
            if existing:
                record_id = existing["id"]
                conn.execute(
                    """UPDATE security_master SET
                       listing_status=COALESCE(listing_status, ?),
                       verification_source_url=COALESCE(verification_source_url, ?),
                       verification_as_of=COALESCE(verification_as_of, ?),
                       source_status='current' WHERE id=?""",
                    (record.get("listing_status"), record.get("verification_source_url"),
                     record.get("verification_as_of"), record_id),
                )
                security_ids[identity] = record_id
                continue
            cursor = conn.execute("""INSERT INTO security_master
                (ticker, company_name, cik, exchange, listing_status,
                 verification_source_url, verification_as_of,
                 source_url, source_as_of, fetched_at, source_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(record.get(name) for name in (
                    "ticker", "company_name", "cik", "exchange", "listing_status",
                    "verification_source_url", "verification_as_of",
                    "source_url", "source_as_of", "fetched_at", "source_status",
                )),
            )
            security_ids[identity] = cursor.lastrowid
        for item in candidates:
            identity = item.get("security_master_identity")
            coverage = item.get("coverage")
            present = coverage.get("present", []) if isinstance(coverage, dict) else []
            claims_verified_listing = identity is not None or any(
                key in present
                for key in ("verified_us_listing", VERIFIED_LISTING_STATUS)
            )
            if not claims_verified_listing:
                continue
            security_master_id = (
                security_ids.get(identity)
                if isinstance(identity, tuple) and len(identity) == 3
                else None
            )
            if security_master_id is None and isinstance(identity, tuple) and len(identity) == 3:
                existing = conn.execute(
                    """SELECT id FROM security_master
                       WHERE ticker = ? AND source_url = ?
                         AND (source_as_of = ? OR (source_as_of IS NULL AND fetched_at = ?))""",
                    (*identity, identity[2]),
                ).fetchone()
                if existing:
                    security_master_id = existing["id"]
                    security_ids[identity] = security_master_id
            if security_master_id is None:
                raise RuntimeError(
                    "verified candidate security master identity did not resolve exactly"
                )
        for item in themes:
            conn.execute("INSERT INTO themes (theme_key, label, signal_kind, strict_grouping_evidence) VALUES (?, ?, ?, ?) ON CONFLICT(theme_key) DO UPDATE SET label=excluded.label", (item["theme_key"], item["label"], item["signal_kind"], int(item["strict_grouping_evidence"])))
            theme_id = conn.execute("SELECT id FROM themes WHERE theme_key = ?", (item["theme_key"],)).fetchone()[0]
            conn.execute("""INSERT INTO theme_snapshots (theme_id, as_of, pipeline_run_id, feature_version, status, score, rank, coverage, feature_values, evidence_raw_item_ids, evidence_sources)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(theme_id, as_of, pipeline_run_id, feature_version) DO UPDATE SET status=excluded.status, score=excluded.score, rank=excluded.rank, coverage=excluded.coverage, feature_values=excluded.feature_values, evidence_raw_item_ids=excluded.evidence_raw_item_ids, evidence_sources=excluded.evidence_sources""", (theme_id, item["as_of"], item["pipeline_run_id"], item["feature_version"], item["status"], item["score"], item["rank"], json.dumps(item["coverage"], sort_keys=True), json.dumps(item["feature_values"], sort_keys=True), json.dumps(item["evidence_raw_item_ids"]), json.dumps(item["evidence_sources"])))
        scored = sorted((item for item in candidates if item["score"] is not None), key=lambda item: (-item["score"], item["ticker"]))
        ranks = {item["ticker"]: index for index, item in enumerate(scored, 1)}
        for item in candidates:
            rank = ranks.get(item["ticker"])
            security_master_id = security_ids.get(item.get("security_master_identity"))
            conn.execute("""INSERT INTO candidate_snapshots (ticker, market, hypothesis_category, thesis_id, company_analysis_id, security_master_id, as_of, pipeline_run_id, feature_version, status, score, rank, coverage, feature_values, evidence_raw_item_ids, evidence_sources)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker, as_of, pipeline_run_id, feature_version) DO UPDATE SET hypothesis_category=excluded.hypothesis_category, thesis_id=excluded.thesis_id, company_analysis_id=excluded.company_analysis_id, security_master_id=excluded.security_master_id, status=excluded.status, score=excluded.score, rank=excluded.rank, coverage=excluded.coverage, feature_values=excluded.feature_values, evidence_raw_item_ids=excluded.evidence_raw_item_ids, evidence_sources=excluded.evidence_sources
                WHERE NOT (
                    candidate_snapshots.security_master_id IS NOT NULL
                    AND excluded.security_master_id IS NULL
                ) AND NOT (
                    candidate_snapshots.status = 'scored'
                    AND excluded.status <> 'scored'
                )""", (item["ticker"], item["market"], item["hypothesis_category"], item["thesis_id"], item["company_analysis_id"], security_master_id, item["as_of"], item["pipeline_run_id"], item["feature_version"], item["status"], item["score"], rank, json.dumps(item["coverage"], sort_keys=True), json.dumps(item["feature_values"], sort_keys=True), json.dumps(item["evidence_raw_item_ids"]), json.dumps(item["evidence_sources"])))
        rank_groups = {
            (item["as_of"], item["pipeline_run_id"], item["feature_version"])
            for item in candidates
        }
        for as_of, pipeline_run_id, feature_version in rank_groups:
            rows = conn.execute(
                """SELECT id FROM candidate_snapshots
                   WHERE as_of = ? AND pipeline_run_id = ? AND feature_version = ?
                     AND score IS NOT NULL
                   ORDER BY score DESC, ticker ASC, id ASC""",
                (as_of, pipeline_run_id, feature_version),
            ).fetchall()
            conn.execute(
                """UPDATE candidate_snapshots SET rank = NULL
                   WHERE as_of = ? AND pipeline_run_id = ? AND feature_version = ?""",
                (as_of, pipeline_run_id, feature_version),
            )
            for effective_rank, row in enumerate(rows, 1):
                conn.execute(
                    "UPDATE candidate_snapshots SET rank = ? WHERE id = ?",
                    (effective_rank, row["id"]),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _raw_evidence(conn: sqlite3.Connection, keyword: str, as_of: str) -> tuple[list[int], list[str]]:
    rows = conn.execute("""SELECT DISTINCT r.id, r.source FROM keyword_mentions km JOIN json_each(km.sample_item_ids) evidence JOIN raw_items r ON r.id = evidence.value WHERE km.keyword = ? AND km.mention_date = ?""", (keyword, as_of)).fetchall()
    return [row["id"] for row in rows], [row["source"] for row in rows]


def write_theme_snapshots(
    conn: sqlite3.Connection,
    pipeline_run_id: int,
    *,
    as_of: str | None = None,
    business_date_provider: Callable[[], date] = date.today,
) -> int:
    """Persist daily emerging-theme monitoring without any candidate work."""
    as_of = as_of or business_date_provider().isoformat()
    themes = []
    for alert in conn.execute(
        "SELECT keyword, z_score FROM trend_alerts WHERE alert_date = ? ORDER BY keyword",
        (as_of,),
    ):
        raw_ids, sources = _raw_evidence(conn, alert["keyword"], as_of)
        themes.append(build_theme_snapshot({
            "keyword": alert["keyword"],
            "z_score": alert["z_score"],
            "raw_item_ids": raw_ids,
            "sources": sources,
            "as_of": as_of,
        }, pipeline_run_id))
    persist_discovery_snapshots(conn, themes, [])
    return len(themes)


def write_discovery_snapshots(
    conn: sqlite3.Connection,
    pipeline_run_id: int | None,
    as_of: str | None = None,
    security_master_fetcher: Callable[[], list[dict[str, object]]] = fetch_sec_security_master,
    candidate_verifier: Callable[[dict[str, object], str], dict[str, object] | None] = _verify_candidate_from_sec,
    request_interval_seconds: float = _SEC_REQUEST_INTERVAL_SECONDS,
    business_date_provider: Callable[[], date] = date.today,
) -> dict[str, int]:
    """Read current upstream records and write an honest snapshot only for a completed run."""
    if pipeline_run_id is None:
        return {"themes": 0, "candidates": 0}
    if conn.in_transaction:
        raise RuntimeError("SEC security-master fetch requires no open business transaction")
    try:
        staged_security_master = security_master_fetcher()
    except SecSecurityMasterError:
        logger.warning("SEC security-master refresh failed; retaining prior current version")
        staged_security_master = []
    official_by_ticker = {
        str(record["ticker"]): record for record in staged_security_master
    }
    staged_index_by_ticker = {
        str(record["ticker"]): index for index, record in enumerate(staged_security_master)
    }
    as_of = as_of or business_date_provider().isoformat()
    themes = []
    for alert in conn.execute("SELECT keyword, z_score FROM trend_alerts WHERE alert_date = ? ORDER BY keyword", (as_of,)):
        raw_ids, sources = _raw_evidence(conn, alert["keyword"], as_of)
        themes.append(build_theme_snapshot({"keyword": alert["keyword"], "z_score": alert["z_score"], "raw_item_ids": raw_ids, "sources": sources, "as_of": as_of}, pipeline_run_id))
    candidates = []
    seen_tickers: set[str] = set()
    verification_count = 0
    thesis_rows = conn.execute("SELECT id, ticker, market, direction, driving_signals FROM investment_theses WHERE thesis_date = ? AND ticker IS NOT NULL ORDER BY id", (as_of,)).fetchall()
    for thesis in thesis_rows:
        ticker = str(thesis["ticker"] or "").strip().upper()
        if ticker in seen_tickers:
            continue
        seen_tickers.add(ticker)
        try:
            signal_titles = json.loads(thesis["driving_signals"] or "[]")
        except json.JSONDecodeError:
            signal_titles = []
        if not isinstance(signal_titles, list):
            signal_titles = []
        evidence_rows = []
        if signal_titles:
            placeholders = ", ".join("?" for _ in signal_titles)
            evidence_rows = conn.execute(
                "SELECT id, source FROM raw_items WHERE title IN (" + placeholders + ")",
                tuple(str(title) for title in signal_titles),
            ).fetchall()
        analysis = conn.execute("SELECT id FROM company_analyses WHERE ticker = ? AND market = 'US' ORDER BY generated_at DESC, id DESC LIMIT 1", (ticker,)).fetchone()
        security_record = official_by_ticker.get(ticker)
        if (
            thesis["market"] == "US"
            and security_record is not None
            and security_record.get("exchange") in _ELIGIBLE_EXCHANGES
            and verification_count < _MAX_CANDIDATE_VERIFICATIONS
        ):
            if verification_count and request_interval_seconds > 0:
                time.sleep(request_interval_seconds)
            verification_count += 1
            try:
                verified_record = candidate_verifier(security_record, as_of)
            except SecSecurityMasterError:
                logger.warning("SEC candidate listing verification failed for %s", ticker)
                verified_record = None
            if verified_record is not None:
                security_record = verified_record
                official_by_ticker[ticker] = verified_record
                staged_security_master[staged_index_by_ticker[ticker]] = verified_record
        candidates.append(build_candidate_snapshot({"ticker": ticker, "market": thesis["market"], "security_master_record": security_record, "hypothesis_category": thesis["direction"], "thesis_id": thesis["id"], "company_analysis_id": analysis["id"] if analysis else None, "raw_item_ids": [row["id"] for row in evidence_rows], "sources": [row["source"] for row in evidence_rows], "as_of": as_of}, pipeline_run_id))
    persist_discovery_snapshots(conn, themes, candidates, staged_security_master)
    return {"themes": len(themes), "candidates": len(candidates)}
