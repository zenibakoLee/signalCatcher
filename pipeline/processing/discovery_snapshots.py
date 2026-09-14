"""Deterministic, preliminary discovery snapshots from persisted evidence.

This module deliberately records insufficient coverage instead of inventing a rank.
It does not use LLM text as score authority and does not infer US listing status.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from datetime import date

FEATURE_VERSION = "discovery-preliminary-v1"
_SIGNAL_KIND = "emerging_signal"
_US_TICKER = re.compile(r"^[A-Z][A-Z.]{0,9}$")


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
    score = None
    if not coverage["missing"]:
        score = round((acceleration * 25 + breadth * 20 + evidence_quality * 15) / 60, 2)
    return {
        "theme_key": source["keyword"].strip().lower(), "label": source["keyword"].strip(),
        "signal_kind": _SIGNAL_KIND, "strict_grouping_evidence": False,
        "as_of": source["as_of"], "pipeline_run_id": pipeline_run_id,
        "feature_version": FEATURE_VERSION, "status": "scored" if score is not None else "insufficient_evidence",
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
    listing_ok = bool(source.get("market_verified")) and market == "US" and bool(_US_TICKER.fullmatch(ticker))
    coverage = _coverage({**values, "verified_us_listing": listing_ok, "thesis_link": source.get("thesis_id") is not None, "raw_evidence": len(raw_ids) >= 2, "independent_sources": len(sources) >= 2})
    if market != "US" or not _US_TICKER.fullmatch(ticker):
        status = "ineligible_market"
    elif not source.get("market_verified"):
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
            "as_of": source["as_of"], "pipeline_run_id": pipeline_run_id, "feature_version": FEATURE_VERSION,
            "status": status, "score": score, "rank": None, "coverage": coverage, "feature_values": feature_values,
            "evidence_raw_item_ids": raw_ids, "evidence_sources": sources}


def persist_discovery_snapshots(conn: sqlite3.Connection, themes: list[dict], candidates: list[dict]) -> None:
    """Persist prepared records atomically; ranks exist only for scored candidates."""
    try:
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
            conn.execute("""INSERT INTO candidate_snapshots (ticker, market, hypothesis_category, thesis_id, company_analysis_id, as_of, pipeline_run_id, feature_version, status, score, rank, coverage, feature_values, evidence_raw_item_ids, evidence_sources)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker, as_of, pipeline_run_id, feature_version) DO UPDATE SET hypothesis_category=excluded.hypothesis_category, thesis_id=excluded.thesis_id, company_analysis_id=excluded.company_analysis_id, status=excluded.status, score=excluded.score, rank=excluded.rank, coverage=excluded.coverage, feature_values=excluded.feature_values, evidence_raw_item_ids=excluded.evidence_raw_item_ids, evidence_sources=excluded.evidence_sources""", (item["ticker"], item["market"], item["hypothesis_category"], item["thesis_id"], item["company_analysis_id"], item["as_of"], item["pipeline_run_id"], item["feature_version"], item["status"], item["score"], rank, json.dumps(item["coverage"], sort_keys=True), json.dumps(item["feature_values"], sort_keys=True), json.dumps(item["evidence_raw_item_ids"]), json.dumps(item["evidence_sources"])))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _raw_evidence(conn: sqlite3.Connection, keyword: str, as_of: str) -> tuple[list[int], list[str]]:
    rows = conn.execute("""SELECT DISTINCT r.id, r.source FROM keyword_mentions km JOIN json_each(km.sample_item_ids) evidence JOIN raw_items r ON r.id = evidence.value WHERE km.keyword = ? AND km.mention_date = ?""", (keyword, as_of)).fetchall()
    return [row["id"] for row in rows], [row["source"] for row in rows]


def write_discovery_snapshots(conn: sqlite3.Connection, pipeline_run_id: int | None, as_of: str | None = None) -> dict[str, int]:
    """Read current upstream records and write an honest snapshot only for a completed run."""
    if pipeline_run_id is None:
        return {"themes": 0, "candidates": 0}
    as_of = as_of or date.today().isoformat()
    themes = []
    for alert in conn.execute("SELECT keyword, z_score FROM trend_alerts WHERE alert_date = ? ORDER BY keyword", (as_of,)):
        raw_ids, sources = _raw_evidence(conn, alert["keyword"], as_of)
        themes.append(build_theme_snapshot({"keyword": alert["keyword"], "z_score": alert["z_score"], "raw_item_ids": raw_ids, "sources": sources, "as_of": as_of}, pipeline_run_id))
    candidates = []
    seen_tickers: set[str] = set()
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
            evidence_rows = conn.execute(f"SELECT id, source FROM raw_items WHERE title IN ({placeholders})", tuple(str(title) for title in signal_titles)).fetchall()
        analysis = conn.execute("SELECT id FROM company_analyses WHERE ticker = ? AND market = 'US' ORDER BY generated_at DESC, id DESC LIMIT 1", (ticker,)).fetchone()
        candidates.append(build_candidate_snapshot({"ticker": ticker, "market": thesis["market"], "market_verified": False, "hypothesis_category": thesis["direction"], "thesis_id": thesis["id"], "company_analysis_id": analysis["id"] if analysis else None, "raw_item_ids": [row["id"] for row in evidence_rows], "sources": [row["source"] for row in evidence_rows], "as_of": as_of}, pipeline_run_id))
    persist_discovery_snapshots(conn, themes, candidates)
    return {"themes": len(themes), "candidates": len(candidates)}
