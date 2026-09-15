"""Official SEC company-ticker staging for US listing verification."""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

SEC_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SEC_SUBMISSIONS_URL_TEMPLATE = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SEC_USER_AGENT = "SignalCatcher research 0oooceanhigh@gmail.com"
SEC_TIMEOUT_SECONDS = 10.0
SEC_PRODUCTION_MIN_RECORDS = 10_000
_REQUIRED_FIELDS = ("cik", "name", "ticker", "exchange")
_KNOWN_EXCHANGES = {"CBOE", "NYSE", "Nasdaq", "OTC", None}
_ELIGIBLE_EXCHANGES = {"NYSE", "Nasdaq"}
_RECENT_PERIODIC_MAX_AGE = timedelta(days=550)
VERIFIED_LISTING_STATUS = "verified_operating_issuer_sole_exchange_ticker_periodic"


class SecSecurityMasterError(RuntimeError):
    """The official SEC source could not be fetched or validated."""


def verify_sec_candidate(
    record: dict[str, object],
    *,
    http_get: Callable[..., Any] = httpx.get,
    as_of: str | date | None = None,
) -> dict[str, object] | None:
    """Verify a sole eligible ticker/exchange pair for an operating SEC filer.

    SEC submissions arrays do not classify instruments. Multi-security issuers
    therefore remain unverified regardless of array order.
    """
    ticker = record.get("ticker")
    exchange = record.get("exchange")
    cik = record.get("cik")
    if (
        not isinstance(ticker, str)
        or exchange not in _ELIGIBLE_EXCHANGES
        or isinstance(cik, bool)
        or not isinstance(cik, int)
        or cik <= 0
        or cik > 9_999_999_999
    ):
        return None
    url = SEC_SUBMISSIONS_URL_TEMPLATE.format(cik=cik)
    try:
        response = http_get(
            url,
            headers={"User-Agent": SEC_USER_AGENT},
            timeout=SEC_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        raise SecSecurityMasterError("SEC submissions verification fetch failed") from exc
    if not isinstance(payload, dict):
        raise SecSecurityMasterError("SEC submissions schema is not an object")
    tickers = payload.get("tickers")
    exchanges = payload.get("exchanges")
    filings = payload.get("filings")
    recent = filings.get("recent") if isinstance(filings, dict) else None
    forms = recent.get("form") if isinstance(recent, dict) else None
    accepted = recent.get("acceptanceDateTime") if isinstance(recent, dict) else None
    if (
        payload.get("cik") != f"{cik:010d}"
        or payload.get("entityType") != "operating"
        or not isinstance(tickers, list)
        or not isinstance(exchanges, list)
        or len(tickers) != 1
        or len(exchanges) != 1
        or tickers[0] != ticker
        or exchanges[0] != exchange
        or not isinstance(forms, list)
        or not isinstance(accepted, list)
        or len(forms) != len(accepted)
    ):
        return None
    reference_date = (
        date.fromisoformat(as_of)
        if isinstance(as_of, str)
        else (as_of or datetime.now(UTC).date())
    )
    cutoff = reference_date - _RECENT_PERIODIC_MAX_AGE
    for form, accepted_at in zip(forms, accepted, strict=True):
        if form not in {"10-K", "10-Q"} or not isinstance(accepted_at, str):
            continue
        try:
            accepted_datetime = datetime.fromisoformat(accepted_at)
        except ValueError:
            continue
        if (
            accepted_datetime.tzinfo is None
            or accepted_datetime.utcoffset() != timedelta(0)
        ):
            continue
        accepted_date = accepted_datetime.astimezone(UTC).date()
        if cutoff <= accepted_date <= reference_date:
            return {
                **record,
                "listing_status": VERIFIED_LISTING_STATUS,
                "verification_source_url": url,
                "verification_as_of": accepted_at,
            }
    return None


def fetch_sec_security_master(
    *,
    http_get: Callable[..., Any] = httpx.get,
    fetched_at: str | None = None,
    expected_min_records: int = SEC_PRODUCTION_MIN_RECORDS,
) -> list[dict[str, object]]:
    """Fetch and fully validate SEC ticker data before returning staged rows."""
    try:
        response = http_get(
            SEC_COMPANY_TICKERS_URL,
            headers={"User-Agent": SEC_USER_AGENT},
            timeout=SEC_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        raise SecSecurityMasterError(f"SEC ticker fetch failed: {exc}") from exc

    if not isinstance(payload, dict):
        raise SecSecurityMasterError("SEC ticker schema is not an object")
    fields = payload.get("fields")
    data = payload.get("data")
    if not isinstance(fields, list) or not isinstance(data, list):
        raise SecSecurityMasterError("SEC ticker schema requires fields and data arrays")
    if not data:
        raise SecSecurityMasterError("SEC ticker data is empty")
    if any(field not in fields for field in _REQUIRED_FIELDS):
        raise SecSecurityMasterError("SEC ticker schema is missing required fields")
    if len(data) < expected_min_records:
        raise SecSecurityMasterError(
            f"SEC ticker data is incomplete: expected at least {expected_min_records} records"
        )

    indexes = {field: fields.index(field) for field in _REQUIRED_FIELDS}
    fetched_at = fetched_at or datetime.now(UTC).isoformat()
    source_as_of = response.headers.get("Last-Modified")
    staged: list[dict[str, object]] = []
    seen: set[str] = set()
    try:
        for row in data:
            if not isinstance(row, list) or len(row) < len(fields):
                raise ValueError("row length does not match fields")
            cik = row[indexes["cik"]]
            name = row[indexes["name"]]
            ticker = row[indexes["ticker"]]
            exchange = row[indexes["exchange"]]
            if isinstance(cik, bool) or not isinstance(cik, int) or cik <= 0:
                raise ValueError("invalid CIK")
            if not isinstance(name, str) or not name.strip():
                raise ValueError("invalid company name")
            if not isinstance(ticker, str) or not ticker.strip():
                raise ValueError("invalid ticker")
            if exchange not in _KNOWN_EXCHANGES:
                raise ValueError("invalid exchange")
            normalized_ticker = ticker.strip().upper()
            if normalized_ticker in seen:
                raise ValueError("duplicate ticker")
            seen.add(normalized_ticker)
            staged.append({
                "ticker": normalized_ticker,
                "company_name": name.strip(),
                "cik": cik,
                "exchange": exchange.strip() if isinstance(exchange, str) and exchange.strip() else None,
                "listing_status": None,
                "source_url": SEC_COMPANY_TICKERS_URL,
                "source_as_of": source_as_of,
                "fetched_at": fetched_at,
                "source_status": "current",
            })
    except (IndexError, ValueError) as exc:
        raise SecSecurityMasterError(f"SEC ticker schema contains invalid data: {exc}") from exc
    return staged
