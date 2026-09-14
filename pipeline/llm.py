"""Fail-closed, transactional OpenAI Responses boundary."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping

LUNA_MODEL = "gpt-5.6-luna"
TERRA_MODEL = "gpt-5.6-terra"
MODEL = LUNA_MODEL
ALLOWED_MODELS = frozenset((LUNA_MODEL, TERRA_MODEL))
DEFAULT_MAX_INPUT_BYTES = 100_000
DEFAULT_MAX_OUTPUT_TOKENS = 8_000
DEFAULT_MAX_REQUESTS_PER_RUN = 40
DEFAULT_MAX_TERRA_REQUESTS_PER_RUN = 8
INPUT_FRAMING_TOKEN_HEADROOM = 64
BUILTIN_PRICING_VERSION = "openai-reviewed-2026-09-14"
BUILTIN_PRICING_EFFECTIVE_DATE = "2026-09-14"
MONTHLY_ALERT_USD = Decimal("60")
MONTHLY_TERRA_STOP_USD = Decimal("80")
MONTHLY_HARD_STOP_USD = Decimal("100")
_MILLION = Decimal(1_000_000)
logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """Base error for worker LLM execution."""


class LLMConfigurationError(LLMError):
    """The worker has not been safely configured."""


class BusinessTransactionActiveError(LLMConfigurationError):
    """A remote call was attempted while business data was transactional."""


def require_no_business_transaction(conn: sqlite3.Connection) -> None:
    """Fail closed without changing a caller-owned SQLite transaction."""
    if conn.in_transaction:
        raise BusinessTransactionActiveError(
            "remote calls are forbidden while the business SQLite connection has an active transaction"
        )


class LLMOutputError(LLMError):
    """The provider returned unusable output."""


class LLMIncompleteError(LLMOutputError):
    pass


class LLMRefusalError(LLMOutputError):
    pass


class LLMParseError(LLMOutputError):
    pass


class LLMMissingUsageError(LLMOutputError):
    pass


class LLMUsageExceededReservationError(LLMOutputError):
    pass


class LLMProviderError(LLMError):
    pass


class DailyBudgetExceededError(LLMError):
    pass


class MonthlyPolicyExceededError(LLMError):
    pass


class RequestLimitExceededError(LLMError):
    pass


class OutputTokenLimitExceededError(LLMError):
    pass


class InputTokenLimitExceededError(LLMError):
    pass


@dataclass(frozen=True, slots=True)
class ModelPricing:
    input_per_million: Decimal
    cached_input_per_million: Decimal
    cache_write_per_million: Decimal
    output_per_million: Decimal
    version: str
    effective_date: str


BUILTIN_PRICING: Mapping[str, ModelPricing] = MappingProxyType({
    LUNA_MODEL: ModelPricing(
        Decimal("0.20"), Decimal("0.02"), Decimal("0.25"), Decimal("1.20"),
        BUILTIN_PRICING_VERSION, BUILTIN_PRICING_EFFECTIVE_DATE,
    ),
    TERRA_MODEL: ModelPricing(
        Decimal("2"), Decimal("0.20"), Decimal("2.50"), Decimal("12"),
        BUILTIN_PRICING_VERSION, BUILTIN_PRICING_EFFECTIVE_DATE,
    ),
})


@dataclass(frozen=True, slots=True)
class LLMResult:
    model: str
    request_id: str | None
    text: str
    parsed: Any | None
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    monthly_policy: str


_LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    finalized_at TEXT,
    day_utc TEXT NOT NULL,
    month_utc TEXT NOT NULL,
    workload TEXT NOT NULL,
    run_id TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    latency_ms INTEGER,
    request_id TEXT,
    input_bytes INTEGER,
    reserved_input_tokens INTEGER,
    reserved_output_tokens INTEGER,
    input_tokens INTEGER,
    cached_input_tokens INTEGER,
    cache_write_tokens INTEGER,
    output_tokens INTEGER,
    reasoning_output_tokens INTEGER,
    reserved_cost_usd TEXT NOT NULL,
    cost_usd TEXT,
    pricing_version TEXT NOT NULL,
    pricing_effective_date TEXT NOT NULL,
    monthly_policy TEXT,
    error_type TEXT,
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_llm_requests_day ON llm_requests(day_utc);
CREATE INDEX IF NOT EXISTS idx_llm_requests_month ON llm_requests(month_utc);
"""


class OpenAIResponsesBoundary:
    """One-shot Responses calls with atomic reservation and outcome auditing."""

    def __init__(
        self,
        *,
        client: Any | None = None,
        client_factory: Callable[[], Any] | None = None,
        pricing_by_model: Mapping[str, ModelPricing] | None = None,
        ledger_path: Path | None = None,
        daily_budget_usd: str | Decimal | None = None,
        max_input_bytes: int = DEFAULT_MAX_INPUT_BYTES,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        max_requests_per_run: int = DEFAULT_MAX_REQUESTS_PER_RUN,
        max_terra_requests_per_run: int = DEFAULT_MAX_TERRA_REQUESTS_PER_RUN,
    ) -> None:
        self._client = client
        self._client_factory = client_factory
        self._ledger_path = ledger_path
        self._daily_budget_usd = self._optional_money(daily_budget_usd, "daily budget")
        self._max_input_bytes = max_input_bytes
        self._max_output_tokens = max_output_tokens
        self._max_requests_per_run = max_requests_per_run
        self._max_terra_requests_per_run = max_terra_requests_per_run
        if min(max_input_bytes, max_output_tokens, max_requests_per_run, max_terra_requests_per_run) <= 0:
            raise LLMConfigurationError("LLM caps must be positive")
        if ledger_path is None:
            raise LLMConfigurationError("an auditable SQLite LLM ledger path is required")
        self._pricing = self._validate_pricing(pricing_by_model)
        self._initialize_ledger()

    @classmethod
    def from_environment(cls) -> "OpenAIResponsesBoundary":
        return cls(
            client_factory=_openai_client,
            pricing_by_model=BUILTIN_PRICING,
            ledger_path=Path(os.environ.get("SIGNALCATCHER_LLM_LEDGER_PATH", "data/openai_usage.sqlite3")),
            daily_budget_usd=os.environ.get("SIGNALCATCHER_OPENAI_DAILY_BUDGET_USD"),
            max_input_bytes=_positive_int("SIGNALCATCHER_OPENAI_MAX_INPUT_BYTES", DEFAULT_MAX_INPUT_BYTES),
            max_output_tokens=_positive_int("SIGNALCATCHER_OPENAI_MAX_OUTPUT_TOKENS", DEFAULT_MAX_OUTPUT_TOKENS),
            max_requests_per_run=_positive_int("SIGNALCATCHER_OPENAI_MAX_REQUESTS_PER_RUN", DEFAULT_MAX_REQUESTS_PER_RUN),
            max_terra_requests_per_run=_positive_int("SIGNALCATCHER_OPENAI_MAX_TERRA_REQUESTS_PER_RUN", DEFAULT_MAX_TERRA_REQUESTS_PER_RUN),
        )

    def complete(
        self,
        *,
        instructions: str,
        input_text: str,
        max_output_tokens: int,
        workload: str,
        run_id: str | int,
        model: str = LUNA_MODEL,
        output_schema: dict[str, Any] | None = None,
    ) -> LLMResult:
        self._validate_request(model, workload, run_id, max_output_tokens, output_schema)
        run_id = str(run_id).strip()
        input_bytes = len(instructions.encode("utf-8")) + len(input_text.encode("utf-8"))
        schema_bytes = len(json.dumps(output_schema, sort_keys=True, separators=(",", ":")).encode("utf-8")) if output_schema else 0
        reserved_input_tokens = input_bytes + schema_bytes + INPUT_FRAMING_TOKEN_HEADROOM
        if reserved_input_tokens > self._max_input_bytes:
            self._audit_rejection(
                "input_rejected", workload, run_id, model, input_bytes,
                reserved_input_tokens, "effective input cap exceeded",
            )
            raise InputTokenLimitExceededError(
                f"request has {reserved_input_tokens} conservative input units; cap is {self._max_input_bytes}"
            )

        pricing = self._pricing[model]
        reservation = self._reservation_cost(pricing, reserved_input_tokens, max_output_tokens)
        ledger_id, monthly_policy = self._reserve(
            workload, run_id, model, input_bytes, reserved_input_tokens,
            max_output_tokens, reservation, pricing
        )
        if monthly_policy == "alert":
            logger.warning(
                "OpenAI monthly spend has reached the $60 alert threshold; workload=%s model=%s",
                workload,
                model,
            )
        payload: dict[str, Any] = {
            "model": model,
            "instructions": instructions,
            "input": input_text,
            "max_output_tokens": max_output_tokens,
        }
        if output_schema is not None:
            payload["text"] = {"format": self._structured_format(output_schema)}

        started = time.monotonic()
        try:
            client = self._configured_client()
        except LLMConfigurationError as error:
            self._update_record(
                ledger_id, "configuration_error", started, None, None,
                Decimal(0), error,
            )
            raise
        try:
            response = client.responses.create(**payload)
        except Exception as error:
            status = "provider_error" if getattr(error, "request_id", None) else "unknown_outcome"
            self._finalize_unknown(ledger_id, status, started, error)
            raise LLMProviderError(f"OpenAI Responses {status.replace('_', ' ')}") from error

        request_id = getattr(response, "id", None)
        refusal = self._refusal(response)
        response_status = getattr(response, "status", None)
        text = getattr(response, "output_text", None)
        try:
            usage = self._read_usage(getattr(response, "usage", None))
        except LLMMissingUsageError as error:
            if response_status == "incomplete":
                self._finalize_response(ledger_id, "incomplete", started, request_id, None, pricing)
                raise LLMIncompleteError("OpenAI Responses returned incomplete output") from error
            if refusal is not None:
                self._finalize_response(ledger_id, "refusal", started, request_id, None, pricing, refusal)
                raise LLMRefusalError("OpenAI Responses refused the request") from error
            if response_status != "completed":
                self._finalize_response(ledger_id, "provider_error", started, request_id, None, pricing)
                raise LLMProviderError(f"OpenAI Responses ended with status {response_status!r}") from error
            self._finalize_response(
                ledger_id, "missing_usage", started, request_id, None, pricing,
                "usage was missing or invalid",
            )
            raise

        if response_status == "incomplete":
            self._finalize_response(ledger_id, "incomplete", started, request_id, usage, pricing)
            raise LLMIncompleteError("OpenAI Responses returned incomplete output")
        if refusal is not None:
            self._finalize_response(ledger_id, "refusal", started, request_id, usage, pricing, refusal)
            raise LLMRefusalError("OpenAI Responses refused the request")
        if response_status != "completed":
            self._finalize_response(ledger_id, "provider_error", started, request_id, usage, pricing)
            raise LLMProviderError(f"OpenAI Responses ended with status {response_status!r}")
        if usage is None:
            self._finalize_response(ledger_id, "missing_usage", started, request_id, None, pricing)
            raise LLMMissingUsageError("OpenAI Responses omitted required usage")
        if usage[0] > reserved_input_tokens or usage[3] > max_output_tokens:
            error = LLMUsageExceededReservationError(
                "provider usage exceeded the tokens reserved for this request"
            )
            self._finalize_response(
                ledger_id, "reservation_exceeded", started, request_id, usage, pricing, str(error)
            )
            raise error
        if not isinstance(text, str) or not text:
            self._finalize_response(ledger_id, "parse_error", started, request_id, usage, pricing)
            raise LLMParseError("OpenAI Responses returned empty output")

        try:
            parsed = self._parse_json(text, output_schema) if output_schema is not None else None
        except LLMParseError as error:
            self._finalize_response(ledger_id, "parse_error", started, request_id, usage, pricing, str(error))
            raise

        cost = self._usage_cost(pricing, usage)
        self._finalize_response(ledger_id, "completed", started, request_id, usage, pricing)
        return LLMResult(
            model, request_id, text, parsed, usage[0], usage[3], cost, monthly_policy
        )

    def _validate_request(
        self, model: str, workload: str, run_id: str | int, max_output_tokens: int, output_schema: dict[str, Any] | None
    ) -> None:
        if model not in ALLOWED_MODELS:
            raise LLMConfigurationError("worker model is not permitted")
        if not isinstance(workload, str) or not workload.strip():
            raise LLMConfigurationError("an explicit workload is required")
        if not isinstance(run_id, (str, int)) or isinstance(run_id, bool) or not str(run_id).strip():
            raise LLMConfigurationError("an explicit run_id is required")
        if max_output_tokens <= 0 or max_output_tokens > self._max_output_tokens:
            raise OutputTokenLimitExceededError(
                f"requested {max_output_tokens} output tokens exceeds cap {self._max_output_tokens}"
            )
        if output_schema is not None:
            self._structured_format(output_schema)

    def _initialize_ledger(self) -> None:
        assert self._ledger_path is not None
        self._ledger_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._connect() as conn:
                conn.executescript(_LEDGER_SCHEMA)
                columns = {row["name"] for row in conn.execute("PRAGMA table_info(llm_requests)")}
                if "run_id" not in columns:
                    conn.execute("ALTER TABLE llm_requests ADD COLUMN run_id TEXT NOT NULL DEFAULT 'legacy'")
                if "cache_write_tokens" not in columns:
                    conn.execute("ALTER TABLE llm_requests ADD COLUMN cache_write_tokens INTEGER")
        except sqlite3.Error as error:
            raise LLMConfigurationError("cannot initialize SQLite LLM ledger") from error

    def _connect(self) -> sqlite3.Connection:
        assert self._ledger_path is not None
        conn = sqlite3.connect(self._ledger_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _reserve(
        self,
        workload: str,
        run_id: str,
        model: str,
        input_bytes: int,
        reserved_input_tokens: int,
        max_output_tokens: int,
        reservation: Decimal,
        pricing: ModelPricing,
    ) -> tuple[int, str]:
        now = datetime.now(timezone.utc)
        day_utc, month_utc = now.date().isoformat(), now.strftime("%Y-%m")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            day_rows = conn.execute(
                "SELECT status, reserved_cost_usd, cost_usd, model, run_id FROM llm_requests WHERE day_utc = ?",
                (day_utc,),
            ).fetchall()
            request_rows = conn.execute(
                """SELECT status, model FROM llm_requests
                   WHERE run_id = ? AND status NOT IN
                   ('input_rejected', 'budget_rejected', 'policy_rejected', 'request_rejected')""",
                (run_id,),
            ).fetchall()
            rejection: tuple[str, type[LLMError], str] | None = None
            if len(request_rows) >= self._max_requests_per_run:
                rejection = ("request_rejected", RequestLimitExceededError, f"shared request cap {self._max_requests_per_run} reached")
            elif model == TERRA_MODEL and sum(row["model"] == TERRA_MODEL for row in request_rows) >= self._max_terra_requests_per_run:
                rejection = ("request_rejected", RequestLimitExceededError, f"shared Terra request cap {self._max_terra_requests_per_run} reached")

            daily_spend = sum((self._charged(row) for row in day_rows), Decimal(0))
            month_rows = conn.execute(
                "SELECT status, reserved_cost_usd, cost_usd FROM llm_requests WHERE month_utc = ?",
                (month_utc,),
            ).fetchall()
            monthly_spend = sum((self._charged(row) for row in month_rows), Decimal(0))
            projected_daily = daily_spend + reservation
            projected_monthly = monthly_spend + reservation
            if rejection is None and self._daily_budget_usd is not None and projected_daily > self._daily_budget_usd:
                rejection = ("budget_rejected", DailyBudgetExceededError, "configured daily LLM budget would be exceeded")
            if rejection is None and projected_monthly > MONTHLY_HARD_STOP_USD:
                rejection = ("policy_rejected", MonthlyPolicyExceededError, "$100 monthly hard stop would be exceeded")
            if rejection is None and model == TERRA_MODEL and projected_monthly > MONTHLY_TERRA_STOP_USD:
                rejection = ("policy_rejected", MonthlyPolicyExceededError, "$80 monthly Terra stop would be exceeded")

            monthly_policy = "alert" if projected_monthly >= MONTHLY_ALERT_USD else "normal"
            if rejection is not None:
                status, error_type, message = rejection
                self._insert_record(
                    conn, now, workload, run_id, model, status, input_bytes, max_output_tokens,
                    0, Decimal(0), pricing, monthly_policy, error_type.__name__, message,
                )
                conn.commit()
                raise error_type(message)

            cursor = self._insert_record(
                conn, now, workload, run_id, model, "reserved", input_bytes, max_output_tokens,
                reserved_input_tokens, reservation, pricing, monthly_policy, None, None,
            )
            conn.commit()
            return int(cursor.lastrowid), monthly_policy
        except LLMError:
            raise
        except (sqlite3.Error, InvalidOperation, TypeError, ValueError) as error:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise LLMConfigurationError("LLM ledger is corrupt; refusing to spend") from error
        finally:
            conn.close()

    @staticmethod
    def _insert_record(
        conn: sqlite3.Connection,
        now: datetime,
        workload: str,
        run_id: str,
        model: str,
        status: str,
        input_bytes: int,
        max_output_tokens: int,
        reserved_input_tokens: int,
        reservation: Decimal,
        pricing: ModelPricing,
        monthly_policy: str,
        error_type: str | None,
        error_message: str | None,
    ) -> sqlite3.Cursor:
        return conn.execute(
            """INSERT INTO llm_requests
               (created_at, day_utc, month_utc, workload, run_id, model, status, attempts,
                input_bytes, reserved_input_tokens, reserved_output_tokens,
                reserved_cost_usd, pricing_version, pricing_effective_date,
                monthly_policy, error_type, error_message)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                now.isoformat(), now.date().isoformat(), now.strftime("%Y-%m"), workload, run_id,
                model, status, input_bytes, reserved_input_tokens, max_output_tokens,
                str(reservation), pricing.version, pricing.effective_date,
                monthly_policy, error_type, error_message,
            ),
        )

    def _audit_rejection(
        self, status: str, workload: str, run_id: str, model: str, input_bytes: int,
        reserved_input_tokens: int, message: str,
    ) -> None:
        now = datetime.now(timezone.utc)
        pricing = self._pricing[model]
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._insert_record(
                conn, now, workload, run_id, model, status, input_bytes, 0,
                reserved_input_tokens, Decimal(0), pricing,
                "normal", InputTokenLimitExceededError.__name__, message,
            )
            conn.commit()

    def _finalize_unknown(self, ledger_id: int, status: str, started: float, error: Exception) -> None:
        self._update_record(
            ledger_id, status, started, request_id=getattr(error, "request_id", None),
            usage=None, cost=None, error=error,
        )

    def _finalize_response(
        self,
        ledger_id: int,
        status: str,
        started: float,
        request_id: str | None,
        usage: tuple[int, int, int, int, int] | None,
        pricing: ModelPricing,
        message: str | None = None,
    ) -> None:
        cost = self._usage_cost(pricing, usage) if usage is not None else None
        error = LLMError(message or status) if status != "completed" else None
        self._update_record(ledger_id, status, started, request_id, usage, cost, error)

    def _update_record(
        self,
        ledger_id: int,
        status: str,
        started: float,
        request_id: str | None,
        usage: tuple[int, int, int, int, int] | None,
        cost: Decimal | None,
        error: Exception | None,
    ) -> None:
        latency_ms = max(0, round((time.monotonic() - started) * 1000))
        values = usage or (None, None, None, None, None)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            reserved = conn.execute(
                "SELECT reserved_cost_usd FROM llm_requests WHERE id = ?", (ledger_id,)
            ).fetchone()
            if reserved is None:
                conn.rollback()
                raise LLMConfigurationError("LLM reservation disappeared")
            charged = str(cost) if cost is not None else reserved["reserved_cost_usd"]
            cursor = conn.execute(
                """UPDATE llm_requests SET finalized_at=?, status=?, latency_ms=?, request_id=?,
                   input_tokens=?, cached_input_tokens=?, cache_write_tokens=?, output_tokens=?, reasoning_output_tokens=?,
                   cost_usd=?, error_type=?, error_message=? WHERE id=? AND status='reserved'""",
                (
                    datetime.now(timezone.utc).isoformat(), status, latency_ms, request_id,
                    values[0], values[1], values[2], values[3], values[4], charged,
                    type(error).__name__ if error else None, str(error) if error else None, ledger_id,
                ),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                raise LLMConfigurationError("LLM ledger conditional finalize failed")
            conn.commit()

    @staticmethod
    def _charged(row: sqlite3.Row) -> Decimal:
        value = row["reserved_cost_usd"] if row["status"] == "reserved" else row["cost_usd"]
        return Decimal(value or "0")

    @staticmethod
    def _cost(pricing: ModelPricing, input_tokens: int, cached_tokens: int, cache_write_tokens: int, output_tokens: int) -> Decimal:
        uncached = max(0, input_tokens - cached_tokens - cache_write_tokens)
        return (
            Decimal(uncached) * pricing.input_per_million
            + Decimal(cached_tokens) * pricing.cached_input_per_million
            + Decimal(cache_write_tokens) * pricing.cache_write_per_million
            + Decimal(output_tokens) * pricing.output_per_million
        ) / _MILLION

    @staticmethod
    def _reservation_cost(pricing: ModelPricing, input_tokens: int, output_tokens: int) -> Decimal:
        return (
            Decimal(input_tokens) * max(pricing.input_per_million, pricing.cache_write_per_million)
            + Decimal(output_tokens) * pricing.output_per_million
        ) / _MILLION

    @classmethod
    def _usage_cost(cls, pricing: ModelPricing, usage: tuple[int, int, int, int, int]) -> Decimal:
        return cls._cost(pricing, usage[0], usage[1], usage[2], usage[3])

    @staticmethod
    def _read_usage(usage: Any) -> tuple[int, int, int, int, int] | None:
        if usage is None:
            return None
        input_tokens = _required_usage_int(usage, "input_tokens")
        output_tokens = _required_usage_int(usage, "output_tokens")
        input_details = getattr(usage, "input_tokens_details", None)
        output_details = getattr(usage, "output_tokens_details", None)
        cached = _optional_usage_int(input_details, "cached_tokens")
        cache_write = _optional_usage_int(input_details, "cache_write_tokens")
        reasoning = _optional_usage_int(output_details, "reasoning_tokens")
        if cached + cache_write > input_tokens or reasoning > output_tokens:
            return None
        return input_tokens, cached, cache_write, output_tokens, reasoning

    @staticmethod
    def _refusal(response: Any) -> str | None:
        for output in getattr(response, "output", None) or []:
            for content in getattr(output, "content", None) or []:
                if getattr(content, "type", None) == "refusal":
                    return str(getattr(content, "refusal", "refused"))
        return None

    def _configured_client(self) -> Any:
        if self._client is not None:
            return self._client
        if self._client_factory is None:
            raise LLMConfigurationError("OpenAI Responses client is not configured")
        try:
            self._client = self._client_factory()
        except Exception as error:
            raise LLMConfigurationError("OpenAI Responses client is not configured") from error
        return self._client

    @staticmethod
    def _structured_format(output_schema: dict[str, Any]) -> dict[str, Any]:
        name, schema = output_schema.get("name"), output_schema.get("schema")
        if not isinstance(name, str) or not name or not isinstance(schema, dict):
            raise LLMConfigurationError("structured output requires a name and JSON schema")
        _validate_schema_definition(schema, "$")
        return {"type": "json_schema", "name": name, "schema": schema, "strict": True}

    @staticmethod
    def _parse_json(text: str, output_schema: dict[str, Any]) -> Any:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as error:
            raise LLMParseError("structured response was not valid JSON") from error
        try:
            _validate_instance(parsed, output_schema["schema"], "$")
        except ValueError as error:
            raise LLMParseError(f"structured response violated schema: {error}") from error
        return parsed

    @staticmethod
    def _validate_pricing(
        pricing_by_model: Mapping[str, ModelPricing] | None,
    ) -> Mapping[str, ModelPricing]:
        if pricing_by_model is None or set(pricing_by_model) != set(ALLOWED_MODELS):
            raise LLMConfigurationError("explicit model-specific Luna and Terra pricing is required")
        result = dict(pricing_by_model)
        for model, pricing in result.items():
            prices = (pricing.input_per_million, pricing.cached_input_per_million, pricing.cache_write_per_million, pricing.output_per_million)
            if any(not value.is_finite() or value <= 0 for value in prices):
                raise LLMConfigurationError(f"{model} prices must be positive finite decimals")
            if pricing.cached_input_per_million > pricing.input_per_million:
                raise LLMConfigurationError(f"{model} cached input price cannot exceed input price")
            if not pricing.version:
                raise LLMConfigurationError(f"{model} pricing version is required")
            try:
                date.fromisoformat(pricing.effective_date)
            except ValueError as error:
                raise LLMConfigurationError(f"{model} pricing effective date must be ISO YYYY-MM-DD") from error
        return MappingProxyType(result)

    @staticmethod
    def _optional_money(value: str | Decimal | None, label: str) -> Decimal | None:
        if value is None:
            return None
        try:
            result = Decimal(value)
        except (InvalidOperation, ValueError) as error:
            raise LLMConfigurationError(f"{label} must be a positive decimal") from error
        if not result.is_finite() or result <= 0:
            raise LLMConfigurationError(f"{label} must be a positive decimal")
        return result


def _validate_schema_definition(schema: dict[str, Any], path: str) -> None:
    schema_type = schema.get("type")
    if schema_type not in {"object", "array", "string", "integer", "number", "boolean", "null"}:
        raise LLMConfigurationError(f"{path} must declare a supported type")
    if schema_type == "object":
        properties = schema.get("properties")
        if not isinstance(properties, dict) or schema.get("additionalProperties") is not False:
            raise LLMConfigurationError(f"{path} object must be closed and declare properties")
        required = schema.get("required")
        if not isinstance(required, list) or set(required) != set(properties):
            raise LLMConfigurationError(f"{path} object must require every property")
        for name, child in properties.items():
            if not isinstance(child, dict):
                raise LLMConfigurationError(f"{path}.{name} must be a schema")
            _validate_schema_definition(child, f"{path}.{name}")
    elif schema_type == "array":
        items = schema.get("items")
        if not isinstance(items, dict):
            raise LLMConfigurationError(f"{path} array must declare typed items")
        _validate_schema_definition(items, f"{path}[]")


def _validate_instance(value: Any, schema: dict[str, Any], path: str) -> None:
    expected = schema["type"]
    valid = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }[expected]
    if not valid:
        raise ValueError(f"{path} must be {expected}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} is not an allowed value")
    if expected == "object":
        properties = schema["properties"]
        if set(value) != set(properties):
            raise ValueError(f"{path} keys do not match schema")
        for name, child in properties.items():
            _validate_instance(value[name], child, f"{path}.{name}")
    elif expected == "array":
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", float("inf")):
            raise ValueError(f"{path} cardinality is out of range")
        for index, item in enumerate(value):
            _validate_instance(item, schema["items"], f"{path}[{index}]")
    elif expected == "string":
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", float("inf")):
            raise ValueError(f"{path} length is out of range")
    elif expected in {"integer", "number"}:
        if value < schema.get("minimum", -float("inf")) or value > schema.get("maximum", float("inf")):
            raise ValueError(f"{path} is out of range")


def strict_object_schema(name: str, properties: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    """Build a closed top-level object; nested definitions are validated at call time."""
    return {
        "name": name,
        "schema": {
            "type": "object",
            "properties": dict(properties),
            "required": list(properties),
            "additionalProperties": False,
        },
    }


def _required_usage_int(usage: Any, field: str) -> int:
    value = getattr(usage, field, None)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise LLMMissingUsageError(f"usage.{field} is missing or invalid")
    return value


def _optional_usage_int(details: Any, field: str) -> int:
    if details is None:
        return 0
    value = getattr(details, field, 0)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise LLMMissingUsageError(f"usage details.{field} is invalid")
    return value


def _positive_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as error:
        raise LLMConfigurationError(f"{name} must be a positive integer") from error
    if parsed <= 0:
        raise LLMConfigurationError(f"{name} must be a positive integer")
    return parsed


def _openai_client() -> Any:
    try:
        from openai import OpenAI
        return OpenAI(max_retries=0)
    except Exception as error:
        raise LLMConfigurationError("OpenAI Responses client is not configured") from error


_boundary: OpenAIResponsesBoundary | None = None


def get_boundary() -> OpenAIResponsesBoundary:
    global _boundary
    if _boundary is None:
        _boundary = OpenAIResponsesBoundary.from_environment()
    return _boundary


def configure_boundary(boundary: OpenAIResponsesBoundary | None) -> None:
    global _boundary
    _boundary = boundary
