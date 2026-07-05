"""Toss Securities Open API client (read-only usage).

Official quotes for KR (6-digit codes) and US (tickers), FX rates, market
calendars, candles, and the user's real brokerage holdings.

Order-placement endpoints exist in the API but are intentionally NOT wrapped
here — this module is for data access only.

Env: TOSS_API_KEY (client_id), TOSS_SECRET_KEY (client_secret)
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import date

import httpx

logger = logging.getLogger(__name__)

BASE = "https://openapi.tossinvest.com"
_TOKEN_MARGIN_SECS = 300

_token_lock = threading.Lock()
_token: str | None = None
_token_expires_at: float = 0.0


def _credentials() -> tuple[str, str]:
    return os.environ.get("TOSS_API_KEY", ""), os.environ.get("TOSS_SECRET_KEY", "")


def available() -> bool:
    key, secret = _credentials()
    return bool(key and secret)


def _get_token() -> str | None:
    global _token, _token_expires_at
    with _token_lock:
        if _token and time.time() < _token_expires_at - _TOKEN_MARGIN_SECS:
            return _token
        key, secret = _credentials()
        if not key or not secret:
            return None
        try:
            resp = httpx.post(
                f"{BASE}/oauth2/token",
                data={"grant_type": "client_credentials", "client_id": key, "client_secret": secret},
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
            _token = data["access_token"]
            _token_expires_at = time.time() + int(data.get("expires_in", 3600))
            return _token
        except Exception as e:
            logger.warning("Toss token issuance failed: %s", e)
            return None


def _get(path: str, params: dict | None = None, account: str | None = None) -> dict | None:
    token = _get_token()
    if not token:
        return None
    headers = {"Authorization": f"Bearer {token}"}
    if account:
        headers["X-Tossinvest-Account"] = account
    try:
        resp = httpx.get(f"{BASE}{path}", params=params, headers=headers, timeout=20)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning("Toss GET %s failed: %s", path, e)
        return None


# ── Market data ──────────────────────────────────────────────────────────────

def get_prices(symbols: list[str]) -> dict[str, dict]:
    """Latest official quotes. KR: 6-digit codes, US: tickers. Max 200.

    Returns {symbol: {"price": float, "currency": str, "timestamp": str}}.
    """
    if not symbols:
        return {}
    data = _get("/api/v1/prices", {"symbols": ",".join(symbols[:200])})
    if not data:
        return {}
    out = {}
    for row in data.get("result", []):
        try:
            out[row["symbol"]] = {
                "price": float(row["lastPrice"]),
                "currency": row.get("currency", ""),
                "timestamp": row.get("timestamp", ""),
            }
        except (KeyError, ValueError, TypeError):
            continue
    return out


def get_price(symbol: str) -> float | None:
    return (get_prices([symbol]).get(symbol) or {}).get("price")


def get_candles(symbol: str, interval: str = "1d", count: int = 200, adjusted: bool = True) -> list[dict]:
    """Daily/minute candles, newest first. interval: '1d' or '1m'.

    Each candle: {timestamp, openPrice, highPrice, lowPrice, closePrice, volume, currency}.
    """
    data = _get("/api/v1/candles", {
        "symbol": symbol, "interval": interval, "count": min(count, 200),
        "adjusted": str(adjusted).lower(),
    })
    result = (data or {}).get("result")
    if isinstance(result, dict):
        return result.get("candles", []) or []
    return result or []


def get_exchange_rate(base: str = "USD", quote: str = "KRW") -> float | None:
    data = _get("/api/v1/exchange-rate", {"baseCurrency": base, "quoteCurrency": quote})
    try:
        return float(data["result"]["rate"])
    except (KeyError, TypeError, ValueError):
        return None


# ── Market calendar ──────────────────────────────────────────────────────────

def get_market_calendar(country: str = "KR") -> dict | None:
    """country: 'KR' or 'US'. Returns {'today': {...}, 'previousBusinessDay': {...}}."""
    data = _get(f"/api/v1/market-calendar/{country.upper()}")
    return (data or {}).get("result")


def was_previous_day_session(country: str, today: date) -> bool | None:
    """True if the calendar day before `today` had a trading session.

    Uses the official calendar's previousBusinessDay: if it isn't yesterday,
    yesterday was a weekend/holiday. Returns None when the API is unavailable.
    """
    cal = get_market_calendar(country)
    if not cal:
        return None
    prev = (cal.get("previousBusinessDay") or {}).get("date")
    if not prev:
        return None
    from datetime import timedelta
    return prev == str(today - timedelta(days=1))


# ── Account (read-only) ──────────────────────────────────────────────────────

def get_accounts() -> list[dict]:
    data = _get("/api/v1/accounts")
    return (data or {}).get("result", []) or []


def get_portfolio(account_seq: int | None = None) -> dict | None:
    """Full portfolio summary + positions from the real brokerage account.

    The X-Tossinvest-Account header takes accountSeq (integer), not accountNo.
    Returns {'totalPurchaseAmount', 'marketValue', 'profitLoss', 'dailyProfitLoss', 'items': [...]}.
    """
    if account_seq is None:
        accounts = get_accounts()
        if not accounts:
            return None
        account_seq = accounts[0]["accountSeq"]
    data = _get("/api/v1/holdings", account=str(account_seq))
    return (data or {}).get("result")


def get_holdings(account_seq: int | None = None) -> list[dict]:
    """Position items only (empty list when the account holds nothing)."""
    portfolio = get_portfolio(account_seq)
    return (portfolio or {}).get("items", []) or []
