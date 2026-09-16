"""Bounded, source-backed weekly CUDA-moment hypothesis audit.

The model may classify supplied evidence, but it cannot create evidence. Every
proven platform-stage hypothesis resolves to an exact raw_items row and source
date. V1 is fail-closed because this corpus lacks authoritative stage and
adoption measurements; it never qualifies or publishes a candidate.
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from pipeline import llm
from pipeline.processing.sec_security_master import (
    SEC_COMPANY_TICKERS_URL,
    SEC_SUBMISSIONS_URL_TEMPLATE,
    VERIFIED_LISTING_STATUS,
    SecSecurityMasterError,
    fetch_sec_security_master,
    verify_sec_candidate,
)

FEATURE_VERSION = "weekly-superstar-platform-v1"
PLATFORM_STAGES = (
    "wedge_product",
    "adjacent_products_attach",
    "customer_data_workflow_accumulates",
    "third_party_developers_join",
    "switching_costs_rise",
    "de_facto_standard",
)
DEFAULT_LOOKBACK_DAYS = 30
MAX_EVIDENCE_ITEMS = 120
MAX_PREFILTER_CANDIDATES = 12
MAX_FINAL_CANDIDATES = 8
MIN_SCORE = 60
MIN_INDEPENDENT_SOURCES = 3
MAX_OUTPUT_TOKENS = 8_000


def _default_candidate_verifier(
    record: dict[str, object], as_of: str,
) -> dict[str, object] | None:
    return verify_sec_candidate(record, as_of=as_of)


def select_recent_evidence(
    conn: sqlite3.Connection,
    *,
    as_of: datetime,
    lookback_days: int,
    limit: int,
) -> list[dict[str, Any]]:
    """Return a deterministic point-in-time scored window with hard bounds."""
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    if not 1 <= lookback_days <= 90:
        raise ValueError("lookback_days must be between 1 and 90")
    if not 1 <= limit <= MAX_EVIDENCE_ITEMS:
        raise ValueError(f"limit must be between 1 and {MAX_EVIDENCE_ITEMS}")
    end = as_of.astimezone(UTC)
    start = end - timedelta(days=lookback_days)
    rows = conn.execute(
        """SELECT r.id AS raw_item_id, r.source, r.title, r.url,
                  r.content_snippet, r.published_at, s.score, s.score_reasoning,
                  s.category
           FROM scored_items s
           JOIN raw_items r ON r.id = s.raw_item_id
           WHERE julianday(r.published_at) >= julianday(?)
             AND julianday(r.published_at) <= julianday(?)
             AND s.score >= ?
           ORDER BY s.score DESC, julianday(r.published_at) DESC, r.id DESC
           LIMIT ?""",
        (start.isoformat(), end.isoformat(), MIN_SCORE, limit),
    ).fetchall()
    evidence: list[dict[str, Any]] = []
    for row in rows:
        published = datetime.fromisoformat(str(row["published_at"]))
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        item = dict(row)
        item["source_date"] = published.astimezone(UTC).date().isoformat()
        evidence.append(item)
    return evidence


def _prefilter_schema(item_count: int, candidate_limit: int) -> dict[str, Any]:
    return llm.strict_object_schema("superstar_weekly_prefilter", {
        "companies": {
            "type": "array", "minItems": 0, "maxItems": candidate_limit,
            "items": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "minLength": 1, "maxLength": 10},
                    "company": {"type": "string", "minLength": 1, "maxLength": 200},
                    "raw_item_ids": {
                        "type": "array", "minItems": 1, "maxItems": min(20, item_count),
                        "items": {"type": "integer", "minimum": 1},
                    },
                },
                "required": ["ticker", "company", "raw_item_ids"],
                "additionalProperties": False,
            },
        }
    })


def _synthesis_schema(candidate_limit: int) -> dict[str, Any]:
    citation = {
        "type": "object",
        "properties": {
            "raw_item_id": {"type": "integer", "minimum": 1},
            "source_date": {
                "type": "string", "maxLength": 10,
                "pattern": r"^\d{4}-\d{2}-\d{2}$",
            },
        },
        "required": ["raw_item_id", "source_date"],
        "additionalProperties": False,
    }
    stage = {
        "type": "object",
        "properties": {
            "stage": {
                "type": "string", "maxLength": 64,
                "enum": list(PLATFORM_STAGES),
            },
            "status": {
                "type": "string", "maxLength": 16,
                "enum": ["proven", "missing"],
            },
            "claim": {"type": "string", "maxLength": 1000},
            "citations": {"type": "array", "minItems": 0, "maxItems": 4, "items": citation},
        },
        "required": ["stage", "status", "claim", "citations"],
        "additionalProperties": False,
    }
    candidate = {
        "type": "object",
        "properties": {
            "ticker": {"type": "string", "minLength": 1, "maxLength": 10},
            "company": {"type": "string", "minLength": 1, "maxLength": 200},
            "stages": {
                "type": "array", "minItems": len(PLATFORM_STAGES),
                "maxItems": len(PLATFORM_STAGES), "items": stage,
            },
        },
        "required": ["ticker", "company", "stages"],
        "additionalProperties": False,
    }
    return llm.strict_object_schema("superstar_weekly_synthesis", {
        "candidates": {
            "type": "array", "minItems": 0, "maxItems": candidate_limit,
            "items": candidate,
        }
    })


def _evidence_payload(evidence: list[dict[str, Any]]) -> str:
    return json.dumps([
        {
            "raw_item_id": item["raw_item_id"],
            "source": item["source"],
            "source_date": item["source_date"],
            "title": item["title"],
            "content_snippet": item["content_snippet"],
            "score": item["score"],
            "score_reasoning": item["score_reasoning"],
            "category": item["category"],
        }
        for item in evidence
    ], ensure_ascii=False, sort_keys=True)


def _validate_prefilter(
    payload: dict[str, Any], evidence_by_id: dict[int, dict[str, Any]], candidate_limit: int
) -> list[dict[str, Any]]:
    companies = payload.get("companies")
    if not isinstance(companies, list) or len(companies) > candidate_limit:
        raise llm.LLMParseError("weekly prefilter candidate count is invalid")
    seen_tickers: set[str] = set()
    validated: list[dict[str, Any]] = []
    for company in companies:
        ticker = str(company["ticker"]).strip().upper()
        ids = company["raw_item_ids"]
        if not ticker or ticker in seen_tickers:
            raise llm.LLMParseError("weekly prefilter tickers must be non-empty and unique")
        if len(ids) != len(set(ids)) or any(raw_id not in evidence_by_id for raw_id in ids):
            raise llm.LLMParseError("weekly prefilter must cite unique supplied raw item IDs")
        seen_tickers.add(ticker)
        validated.append({**company, "ticker": ticker})
    return validated


def validate_synthesis(
    payload: dict[str, Any], evidence: list[dict[str, Any]],
    *,
    allowed_tickers: set[str] | None = None,
    allowed_raw_ids_by_ticker: dict[str, set[int]] | None = None,
) -> list[dict[str, Any]]:
    """Resolve citations exactly; reject generated, wrong-date, and reused evidence."""
    evidence_by_id = {int(item["raw_item_id"]): item for item in evidence}
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or len(candidates) > MAX_FINAL_CANDIDATES:
        raise llm.LLMParseError("weekly synthesis candidate count is invalid")
    seen_tickers: set[str] = set()
    validated: list[dict[str, Any]] = []
    for candidate in candidates:
        ticker = str(candidate["ticker"]).strip().upper()
        if (
            not ticker or ticker in seen_tickers
            or (allowed_tickers is not None and ticker not in allowed_tickers)
        ):
            raise llm.LLMParseError("weekly synthesis ticker was not uniquely prefiltered")
        candidate_raw_ids = (
            allowed_raw_ids_by_ticker.get(ticker)
            if allowed_raw_ids_by_ticker is not None
            else None
        )
        if allowed_raw_ids_by_ticker is not None and candidate_raw_ids is None:
            raise llm.LLMParseError("weekly synthesis ticker has no candidate-scoped evidence")
        stages = candidate["stages"]
        if [stage["stage"] for stage in stages] != list(PLATFORM_STAGES):
            raise llm.LLMParseError("weekly synthesis must return every platform stage in order")
        used_ids: set[int] = set()
        resolved_stages: list[dict[str, Any]] = []
        for stage in stages:
            status = stage["status"]
            citations = stage["citations"]
            claim = str(stage["claim"])
            if status == "missing":
                if citations or claim:
                    raise llm.LLMParseError("missing stages cannot contain generated evidence")
                resolved_stages.append({**stage, "citations": []})
                continue
            if not citations or not claim.strip():
                raise llm.LLMParseError("proven stages require a claim and exact citations")
            resolved = []
            for citation in citations:
                raw_id = citation["raw_item_id"]
                if candidate_raw_ids is not None and raw_id not in candidate_raw_ids:
                    raise llm.LLMParseError(
                        "synthesis citation is outside candidate-scoped evidence"
                    )
                item = evidence_by_id.get(raw_id)
                if item is None:
                    raise llm.LLMParseError("synthesis cited an unsupplied raw item ID")
                if citation["source_date"] != item["source_date"]:
                    raise llm.LLMParseError("synthesis citation source date does not match raw evidence")
                if raw_id in used_ids:
                    raise llm.LLMParseError("raw evidence was reused across stages")
                used_ids.add(raw_id)
                resolved.append({
                    "raw_item_id": raw_id,
                    "source": item["source"],
                    "source_date": item["source_date"],
                })
            resolved_stages.append({**stage, "claim": claim.strip(), "citations": resolved})
        seen_tickers.add(ticker)
        validated.append({
            "ticker": ticker,
            "company": str(candidate["company"]).strip(),
            "stages": resolved_stages,
        })
    return validated


def discover_candidates(
    conn: sqlite3.Connection,
    *,
    as_of: datetime,
    lookback_days: int,
    evidence_limit: int,
    candidate_limit: int,
    run_id: str,
) -> tuple[list[dict[str, Any]], int]:
    """Use one Luna bulk prefilter and no more than one Terra synthesis call."""
    if not 1 <= candidate_limit <= MAX_FINAL_CANDIDATES:
        raise ValueError(f"candidate_limit must be between 1 and {MAX_FINAL_CANDIDATES}")
    evidence = select_recent_evidence(
        conn, as_of=as_of, lookback_days=lookback_days, limit=evidence_limit
    )
    if not evidence:
        return [], 0
    evidence_by_id = {item["raw_item_id"]: item for item in evidence}
    llm.require_no_business_transaction(conn)
    boundary = llm.get_boundary()
    prefilter_result = boundary.complete(
        instructions=(
            "Select only US-listed operating-company hypotheses explicitly named in the supplied "
            "evidence. Cite only supplied raw_item_ids. Do not infer or generate evidence."
        ),
        input_text=_evidence_payload(evidence),
        max_output_tokens=MAX_OUTPUT_TOKENS,
        model=llm.LUNA_MODEL,
        workload="superstar_weekly_prefilter",
        run_id=run_id,
        output_schema=_prefilter_schema(len(evidence), MAX_PREFILTER_CANDIDATES),
    )
    prefiltered = _validate_prefilter(
        prefilter_result.parsed, evidence_by_id, MAX_PREFILTER_CANDIDATES
    )
    if not prefiltered:
        return [], len(evidence)
    selected_ids = {
        raw_id for company in prefiltered for raw_id in company["raw_item_ids"]
    }
    synthesis_evidence = [item for item in evidence if item["raw_item_id"] in selected_ids]
    synthesis_input = json.dumps({
        "prefiltered_companies": prefiltered,
        "platform_stages": list(PLATFORM_STAGES),
        "evidence": json.loads(_evidence_payload(synthesis_evidence)),
    }, ensure_ascii=False, sort_keys=True)
    llm.require_no_business_transaction(conn)
    final_result = boundary.complete(
        instructions=(
            "Audit the six CUDA-like platform-stage hypotheses. A 'proven' label means only that "
            "the supplied corpus supports the hypothesis; it is not audited primary/adoption proof. "
            "Mark a stage proven only with exact supplied "
            "raw_item_id and source_date citations. Evidence may be used once per company. Mark all "
            "unproven stages missing; do not paraphrase or invent evidence."
        ),
        input_text=synthesis_input,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        model=llm.TERRA_MODEL,
        workload="superstar_weekly_synthesis",
        run_id=run_id,
        output_schema=_synthesis_schema(candidate_limit),
    )
    return validate_synthesis(
        final_result.parsed,
        synthesis_evidence,
        allowed_tickers={company["ticker"] for company in prefiltered},
        allowed_raw_ids_by_ticker={
            company["ticker"]: set(company["raw_item_ids"])
            for company in prefiltered
        },
    ), len(evidence)


def _is_verified_security(ticker: str, record: dict[str, object] | None) -> bool:
    if not record or record.get("ticker") != ticker:
        return False
    cik = record.get("cik")
    return (
        isinstance(cik, int) and not isinstance(cik, bool)
        and record.get("source_url") == SEC_COMPANY_TICKERS_URL
        and record.get("source_status") == "current"
        and record.get("exchange") in {"NYSE", "Nasdaq"}
        and record.get("listing_status") == VERIFIED_LISTING_STATUS
        and record.get("verification_source_url") == SEC_SUBMISSIONS_URL_TEMPLATE.format(cik=cik)
        and bool(record.get("verification_as_of"))
    )


def verify_candidate_list(
    conn: sqlite3.Connection,
    candidates: list[dict[str, Any]],
    *,
    as_of: str,
    security_master_fetcher: Callable[[], list[dict[str, object]]] = fetch_sec_security_master,
    candidate_verifier: Callable[
        [dict[str, object], str], dict[str, object] | None
    ] = _default_candidate_verifier,
    request_interval_seconds: float = 0.11,
) -> tuple[dict[str, dict[str, object]], list[str]]:
    """Verify at most the publishable candidate bound against official SEC data."""
    if conn.in_transaction:
        raise RuntimeError("SEC verification requires no open business transaction")
    bounded = candidates[:MAX_FINAL_CANDIDATES]
    if not bounded:
        return {}, []
    try:
        master = security_master_fetcher()
    except SecSecurityMasterError:
        return {}, ["SEC security-master refresh unavailable"]
    by_ticker = {str(record["ticker"]): record for record in master}
    verified: dict[str, dict[str, object]] = {}
    errors: list[str] = []
    for index, candidate in enumerate(bounded):
        ticker = candidate["ticker"]
        record = by_ticker.get(ticker)
        if record is None:
            continue
        if index and request_interval_seconds > 0:
            time.sleep(request_interval_seconds)
        try:
            result = candidate_verifier(record, as_of)
        except SecSecurityMasterError:
            errors.append(f"SEC candidate verification unavailable for {ticker}")
            continue
        if result is not None:
            verified[ticker] = result
    return verified, errors


def persist_weekly_audit(
    conn: sqlite3.Connection,
    *,
    pipeline_run_id: int,
    as_of: str,
    candidates: list[dict[str, Any]],
    security_records: dict[str, dict[str, object]],
) -> dict[str, int]:
    """Atomically persist the audit after all LLM/SEC network work has finished."""
    if conn.in_transaction:
        raise RuntimeError("weekly audit persistence requires no open transaction")
    published = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        for candidate in candidates:
            ticker = candidate["ticker"]
            prior = conn.execute(
                """SELECT id, status, security_master_id FROM weekly_superstar_snapshots
                   WHERE ticker=? AND as_of=? AND pipeline_run_id=?
                     AND feature_version=?""",
                (ticker, as_of, pipeline_run_id, FEATURE_VERSION),
            ).fetchone()
            if prior is not None:
                continue
            record = security_records.get(ticker)
            verified = _is_verified_security(ticker, record)
            security_master_id = prior["security_master_id"] if prior is not None else None
            if verified and record is not None:
                identity_value = record.get("source_as_of") or record.get("fetched_at")
                existing = conn.execute(
                    """SELECT id FROM security_master WHERE ticker=? AND source_url=?
                       AND (source_as_of=? OR (source_as_of IS NULL AND fetched_at=?))""",
                    (ticker, record["source_url"], identity_value, identity_value),
                ).fetchone()
                if existing:
                    security_master_id = existing["id"]
                else:
                    cursor = conn.execute(
                        """INSERT INTO security_master
                           (ticker, company_name, cik, exchange, listing_status,
                            verification_source_url, verification_as_of, source_url,
                            source_as_of, fetched_at, source_status)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        tuple(record.get(key) for key in (
                            "ticker", "company_name", "cik", "exchange", "listing_status",
                            "verification_source_url", "verification_as_of", "source_url",
                            "source_as_of", "fetched_at", "source_status",
                        )),
                    )
                    security_master_id = cursor.lastrowid
            verified = verified or security_master_id is not None
            missing = [
                stage["stage"] for stage in candidate["stages"]
                if stage["status"] != "proven"
            ]
            coverage_ratio = (len(PLATFORM_STAGES) - len(missing)) / len(PLATFORM_STAGES)
            missing_evidence = list(missing)
            if not verified:
                missing_evidence.append("official_sec_listing")
            sources = {
                citation["source"]
                for stage in candidate["stages"]
                for citation in stage["citations"]
            }
            if len(sources) < MIN_INDEPENDENT_SOURCES:
                missing_evidence.append("independent_sources")
            missing_evidence.append("authoritative_stage_measurements")
            if not verified:
                status = "unverified_us_listing"
            else:
                status = "insufficient_evidence"
            cursor = conn.execute(
                """INSERT INTO weekly_superstar_snapshots
                   (ticker, company, as_of, pipeline_run_id, feature_version, status,
                    security_master_id, missing_stages, source_count, coverage_ratio,
                    missing_evidence, score, rank)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                   ON CONFLICT(ticker, as_of, pipeline_run_id, feature_version) DO NOTHING
                   RETURNING id""",
                (ticker, candidate["company"], as_of, pipeline_run_id, FEATURE_VERSION,
                 status, security_master_id, json.dumps(missing), len(sources), coverage_ratio,
                 json.dumps(missing_evidence)),
            )
            result = cursor.fetchone()
            if result is None:
                continue
            snapshot_id = result["id"]
            for stage in candidate["stages"]:
                if stage["status"] == "missing":
                    conn.execute(
                        """INSERT INTO weekly_superstar_stage_evidence
                           (snapshot_id, stage, status, claim)
                           VALUES (?, ?, 'missing', '')""",
                        (snapshot_id, stage["stage"]),
                    )
                else:
                    for citation in stage["citations"]:
                        conn.execute(
                            """INSERT INTO weekly_superstar_stage_evidence
                               (snapshot_id, stage, status, claim, raw_item_id, source, source_date)
                               VALUES (?, ?, 'proven', ?, ?, ?, ?)""",
                            (snapshot_id, stage["stage"], stage["claim"],
                             citation["raw_item_id"], citation["source"], citation["source_date"]),
                        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"considered": len(candidates), "published": published}
