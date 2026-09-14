from __future__ import annotations

import json
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline import llm


def pricing() -> dict[str, llm.ModelPricing]:
    return {
        llm.LUNA_MODEL: llm.ModelPricing(
            input_per_million=Decimal("1"),
            cached_input_per_million=Decimal("0.25"),
            cache_write_per_million=Decimal("1.25"),
            output_per_million=Decimal("4"),
            version="openai-2026-09-01",
            effective_date="2026-09-01",
        ),
        llm.TERRA_MODEL: llm.ModelPricing(
            input_per_million=Decimal("2"),
            cached_input_per_million=Decimal("0.5"),
            cache_write_per_million=Decimal("2.5"),
            output_per_million=Decimal("8"),
            version="openai-2026-09-01",
            effective_date="2026-09-01",
        ),
    }


def response(*, text: str = '{"answer":"ok"}', status: str = "completed", usage=True):
    usage_value = None
    if usage:
        usage_value = SimpleNamespace(
            input_tokens=12,
            output_tokens=7,
            input_tokens_details=SimpleNamespace(cached_tokens=2),
            output_tokens_details=SimpleNamespace(reasoning_tokens=3),
        )
    return SimpleNamespace(id="resp_123", output_text=text, usage=usage_value, status=status, output=[])


def boundary(tmp_path: Path, client, **kwargs) -> llm.OpenAIResponsesBoundary:
    max_input_bytes = kwargs.pop("max_input_bytes", 1000)
    return llm.OpenAIResponsesBoundary(
        client=client,
        pricing_by_model=pricing(),
        ledger_path=tmp_path / "usage.sqlite3",
        max_input_bytes=max_input_bytes,
        **kwargs,
    )


def records(path: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM llm_requests ORDER BY id").fetchall()


def answer_schema() -> dict:
    return llm.strict_object_schema(
        "answer",
        {
            "answer": {"type": "string", "minLength": 1},
        },
    )


def test_reserves_worst_case_before_call_and_finalizes_actual_model_price(tmp_path: Path) -> None:
    seen: list[tuple[str, Decimal]] = []
    ledger = tmp_path / "usage.sqlite3"

    def create(**_kwargs):
        row = records(ledger)[0]
        seen.append((row["status"], Decimal(row["reserved_cost_usd"])))
        return response()

    result = boundary(tmp_path, SimpleNamespace(responses=SimpleNamespace(create=create))).complete(
        instructions="system",
        input_text="input",
        max_output_tokens=10,
        model=llm.TERRA_MODEL,
        output_schema=answer_schema(),
        workload="test.answer",
        run_id="test-run",
    )

    assert seen == [("reserved", Decimal("0.000645"))]
    assert result.parsed == {"answer": "ok"}
    row = records(ledger)[0]
    assert row["status"] == "completed"
    assert Decimal(row["cost_usd"]) == Decimal("0.000077")
    assert row["cached_input_tokens"] == 2
    assert row["reasoning_output_tokens"] == 3
    assert row["pricing_version"] == "openai-2026-09-01"
    assert row["workload"] == "test.answer"
    assert row["attempts"] == 1
    assert row["latency_ms"] >= 0


def test_input_cap_blocks_provider_call_and_is_audited(tmp_path: Path) -> None:
    client = SimpleNamespace(responses=SimpleNamespace(create=lambda **_kwargs: pytest.fail("provider called")))
    b = boundary(tmp_path, client, max_input_bytes=5)

    with pytest.raises(llm.InputTokenLimitExceededError):
        b.complete(instructions="abc", input_text="def", max_output_tokens=1, workload="test.cap", run_id="test-run")

    row = records(tmp_path / "usage.sqlite3")[0]
    assert row["status"] == "input_rejected"
    assert row["workload"] == "test.cap"


def test_atomic_reservation_prevents_budget_overshoot(tmp_path: Path) -> None:
    calls = 0

    def create(**_kwargs):
        nonlocal calls
        calls += 1
        return response(usage=False)

    b = boundary(
        tmp_path,
        SimpleNamespace(responses=SimpleNamespace(create=create)),
        daily_budget_usd="0.000010",
    )
    with pytest.raises(llm.DailyBudgetExceededError):
        b.complete(instructions="a", input_text="b", max_output_tokens=10, workload="test.budget", run_id="test-run")
    assert calls == 0
    assert records(tmp_path / "usage.sqlite3")[0]["status"] == "budget_rejected"


def test_shared_sqlite_request_cap_applies_across_boundary_instances(tmp_path: Path) -> None:
    client = SimpleNamespace(responses=SimpleNamespace(create=lambda **_kwargs: response()))
    first = boundary(tmp_path, client, max_requests_per_run=1)
    second = boundary(tmp_path, client, max_requests_per_run=1)
    first.complete(instructions="a", input_text="b", max_output_tokens=10, workload="one", run_id="test-run")

    with pytest.raises(llm.RequestLimitExceededError):
        second.complete(instructions="a", input_text="b", max_output_tokens=10, workload="two", run_id="test-run")


def test_monthly_policy_alerts_at_60_stops_terra_at_80_and_all_at_100(tmp_path: Path, caplog) -> None:
    client = SimpleNamespace(responses=SimpleNamespace(create=lambda **_kwargs: response()))
    b = boundary(tmp_path, client)
    ledger = tmp_path / "usage.sqlite3"

    def add_spend(amount: str) -> None:
        conn = sqlite3.connect(ledger)
        conn.execute(
            """INSERT INTO llm_requests
               (created_at, day_utc, month_utc, workload, run_id, model, status, attempts,
                reserved_cost_usd, cost_usd, pricing_version, pricing_effective_date)
               VALUES (datetime('now'), date('now'), strftime('%Y-%m','now'),
                       'test.seed', 'seed-run', ?, 'completed', 1, ?, ?, 'test', '2026-09-01')""",
            (llm.LUNA_MODEL, amount, amount),
        )
        conn.commit()
        conn.close()

    add_spend("60")
    result = b.complete(instructions="a", input_text="b", max_output_tokens=10, workload="luna", run_id="test-run")
    assert result.monthly_policy == "alert"
    assert "$60" in caplog.text

    add_spend("20")
    with pytest.raises(llm.MonthlyPolicyExceededError):
        b.complete(
            instructions="a", input_text="b", max_output_tokens=10,
            model=llm.TERRA_MODEL, workload="terra", run_id="test-run",
        )
    assert records(ledger)[-1]["status"] == "policy_rejected"

    add_spend("20")
    with pytest.raises(llm.MonthlyPolicyExceededError):
        b.complete(instructions="a", input_text="b", max_output_tokens=10, workload="luna-hard-stop", run_id="test-run")


def test_missing_usage_and_provider_exception_are_conservatively_charged_and_distinct(tmp_path: Path) -> None:
    missing = boundary(
        tmp_path,
        SimpleNamespace(responses=SimpleNamespace(create=lambda **_kwargs: response(usage=False))),
    )
    with pytest.raises(llm.LLMMissingUsageError):
        missing.complete(instructions="a", input_text="b", max_output_tokens=10, workload="missing", run_id="test-run")
    first = records(tmp_path / "usage.sqlite3")[0]
    assert first["status"] == "missing_usage"
    assert first["cost_usd"] == first["reserved_cost_usd"]

    unknown = boundary(
        tmp_path,
        SimpleNamespace(responses=SimpleNamespace(create=lambda **_kwargs: (_ for _ in ()).throw(TimeoutError("lost")))),
    )
    with pytest.raises(llm.LLMProviderError):
        unknown.complete(instructions="a", input_text="b", max_output_tokens=10, workload="unknown", run_id="test-run")
    second = records(tmp_path / "usage.sqlite3")[1]
    assert second["status"] == "unknown_outcome"
    assert second["cost_usd"] == second["reserved_cost_usd"]


def test_invalid_usage_is_audited_as_missing_usage(tmp_path: Path) -> None:
    bad_response = response()
    bad_response.usage.input_tokens = None
    b = boundary(
        tmp_path,
        SimpleNamespace(responses=SimpleNamespace(create=lambda **_kwargs: bad_response)),
    )

    with pytest.raises(llm.LLMMissingUsageError):
        b.complete(instructions="a", input_text="b", max_output_tokens=2, workload="invalid-usage", run_id="test-run")

    row = records(tmp_path / "usage.sqlite3")[0]
    assert row["status"] == "missing_usage"
    assert row["cost_usd"] == row["reserved_cost_usd"]


def test_invalid_cache_write_usage_fails_closed(tmp_path: Path) -> None:
    bad_response = response()
    bad_response.usage.input_tokens_details.cache_write_tokens = "unknown"
    b = boundary(tmp_path, SimpleNamespace(responses=SimpleNamespace(create=lambda **_: bad_response)))

    with pytest.raises(llm.LLMMissingUsageError):
        b.complete(
            instructions="a", input_text="b", max_output_tokens=10,
            workload="invalid-cache-write", run_id="test-run",
        )
    assert records(tmp_path / "usage.sqlite3")[0]["status"] == "missing_usage"


def test_provider_usage_above_reserved_caps_is_audited_and_raises(tmp_path: Path) -> None:
    over = response()
    over.usage.input_tokens = 1020
    over.usage.input_tokens_details.cached_tokens = 0
    b = boundary(
        tmp_path,
        SimpleNamespace(responses=SimpleNamespace(create=lambda **_kwargs: over)),
    )

    with pytest.raises(llm.LLMUsageExceededReservationError):
        b.complete(instructions="a", input_text="b", max_output_tokens=10, workload="over-cap", run_id="test-run")

    row = records(tmp_path / "usage.sqlite3")[0]
    assert row["status"] == "reservation_exceeded"
    assert Decimal(row["cost_usd"]) > Decimal(row["reserved_cost_usd"])


def test_known_pre_call_configuration_failure_releases_reservation(tmp_path: Path) -> None:
    b = llm.OpenAIResponsesBoundary(
        client_factory=lambda: (_ for _ in ()).throw(RuntimeError("no credential")),
        pricing_by_model=pricing(),
        ledger_path=tmp_path / "usage.sqlite3",
        max_input_bytes=1000,
    )

    with pytest.raises(llm.LLMConfigurationError):
        b.complete(instructions="a", input_text="b", max_output_tokens=2, workload="configuration", run_id="test-run")

    row = records(tmp_path / "usage.sqlite3")[0]
    assert row["status"] == "configuration_error"
    assert Decimal(row["cost_usd"]) == Decimal("0")


def test_incomplete_refusal_and_parse_error_are_audited_separately(tmp_path: Path) -> None:
    cases = [
        (SimpleNamespace(**vars(response(status="incomplete"))), llm.LLMIncompleteError, "incomplete"),
        (
            SimpleNamespace(
                id="resp_refusal", status="completed", output_text=None,
                output=[SimpleNamespace(content=[SimpleNamespace(type="refusal", refusal="no")])],
                usage=response().usage,
            ),
            llm.LLMRefusalError,
            "refusal",
        ),
        (response(text="not-json"), llm.LLMParseError, "parse_error"),
    ]
    ledger = tmp_path / "usage.sqlite3"
    for provider_response, error_type, status in cases:
        def create(*, _response=provider_response, **_kwargs):
            return _response

        b = boundary(tmp_path, SimpleNamespace(responses=SimpleNamespace(create=create)))
        with pytest.raises(error_type):
            b.complete(
                instructions="a", input_text="b", max_output_tokens=10,
                output_schema=answer_schema(), workload=status, run_id="test-run",
            )
    assert [row["status"] for row in records(ledger)] == ["incomplete", "refusal", "parse_error"]


def test_schema_rejects_open_nested_objects_and_untyped_arrays_before_provider_call(tmp_path: Path) -> None:
    client = SimpleNamespace(responses=SimpleNamespace(create=lambda **_kwargs: pytest.fail("provider called")))
    bad_schemas = [
        llm.strict_object_schema("bad", {"nested": {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}}),
        llm.strict_object_schema("bad", {"items": {"type": "array"}}),
        llm.strict_object_schema("bad", {"value": {}}),
    ]
    for schema in bad_schemas:
        with pytest.raises(llm.LLMConfigurationError):
            boundary(tmp_path, client).complete(
                instructions="a", input_text="b", max_output_tokens=1,
                output_schema=schema, workload="schema", run_id="test-run",
            )


def test_runtime_validation_enforces_nested_types_enum_range_and_cardinality(tmp_path: Path) -> None:
    schema = llm.strict_object_schema(
        "scores",
        {
            "scores": {
                "type": "array",
                "minItems": 1,
                "maxItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer", "minimum": 1, "maximum": 1},
                        "score": {"type": "integer", "minimum": 0, "maximum": 100},
                        "kind": {"type": "string", "enum": ["signal"]},
                    },
                    "required": ["index", "score", "kind"],
                    "additionalProperties": False,
                },
            }
        },
    )
    client = SimpleNamespace(responses=SimpleNamespace(create=lambda **_kwargs: response(text='{"scores":[{"index":1,"score":101,"kind":"signal"}]}')))
    with pytest.raises(llm.LLMParseError):
        boundary(tmp_path, client).complete(
            instructions="a", input_text="b", max_output_tokens=10,
            output_schema=schema, workload="validation", run_id="test-run",
        )


def test_pricing_must_be_positive_model_specific_and_versioned(tmp_path: Path) -> None:
    bad = pricing()
    bad[llm.LUNA_MODEL] = llm.ModelPricing(
        input_per_million=Decimal("0"), cached_input_per_million=Decimal("0"),
        cache_write_per_million=Decimal("0"), output_per_million=Decimal("0"), version="", effective_date="",
    )
    with pytest.raises(llm.LLMConfigurationError):
        llm.OpenAIResponsesBoundary(
            client=object(), pricing_by_model=bad, ledger_path=tmp_path / "usage.sqlite3"
        )

    unsafe_cached = pricing()
    unsafe_cached[llm.LUNA_MODEL] = llm.ModelPricing(
        input_per_million=Decimal("1"), cached_input_per_million=Decimal("2"),
        cache_write_per_million=Decimal("1.25"), output_per_million=Decimal("4"), version="v", effective_date="2026-09-01",
    )
    with pytest.raises(llm.LLMConfigurationError):
        llm.OpenAIResponsesBoundary(
            client=object(), pricing_by_model=unsafe_cached,
            ledger_path=tmp_path / "unsafe.sqlite3",
        )


def test_production_client_disables_sdk_retries(monkeypatch) -> None:
    captured = {}

    class OpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=OpenAI))
    llm._openai_client()
    assert captured == {"max_retries": 0}


def test_cache_write_usage_is_audited_and_charged(tmp_path: Path) -> None:
    provider_response = response()
    provider_response.usage.input_tokens = 100
    provider_response.usage.input_tokens_details.cached_tokens = 20
    provider_response.usage.input_tokens_details.cache_write_tokens = 30
    b = boundary(tmp_path, SimpleNamespace(responses=SimpleNamespace(create=lambda **_: provider_response)))

    result = b.complete(
        instructions="a" * 40, input_text="b", max_output_tokens=10,
        workload="cache-write", run_id="run-a",
    )

    row = records(tmp_path / "usage.sqlite3")[0]
    assert row["cache_write_tokens"] == 30
    assert result.cost_usd == Decimal("0.0001205")


def test_environment_pricing_is_reviewed_immutable_and_ignores_price_overrides(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SIGNALCATCHER_LLM_LEDGER_PATH", str(tmp_path / "usage.sqlite3"))
    monkeypatch.setenv("SIGNALCATCHER_OPENAI_LUNA_INPUT_COST_PER_MILLION_USD", "999")
    boundary_from_env = llm.OpenAIResponsesBoundary.from_environment()

    assert boundary_from_env._pricing[llm.LUNA_MODEL] == llm.ModelPricing(
        input_per_million=Decimal("0.20"), cached_input_per_million=Decimal("0.02"),
        cache_write_per_million=Decimal("0.25"), output_per_million=Decimal("1.20"),
        version=llm.BUILTIN_PRICING_VERSION, effective_date=llm.BUILTIN_PRICING_EFFECTIVE_DATE,
    )
    with pytest.raises(TypeError):
        boundary_from_env._pricing[llm.LUNA_MODEL] = pricing()[llm.LUNA_MODEL]


def test_builtin_pricing_rejects_runtime_mutation() -> None:
    with pytest.raises(TypeError):
        llm.BUILTIN_PRICING[llm.LUNA_MODEL] = pricing()[llm.LUNA_MODEL]


def test_reservation_includes_schema_and_framing_headroom_and_enforces_it(tmp_path: Path) -> None:
    provider_response = response(text='{"answer":"ok"}')
    literal_bytes = len("ab".encode())
    provider_response.usage.input_tokens = literal_bytes + 1
    b = boundary(tmp_path, SimpleNamespace(responses=SimpleNamespace(create=lambda **_: provider_response)))
    b.complete(
        instructions="a", input_text="b", max_output_tokens=10, workload="headroom",
        run_id="run-a", output_schema=answer_schema(),
    )
    row = records(tmp_path / "usage.sqlite3")[0]
    assert row["reserved_input_tokens"] > literal_bytes

    provider_response.usage.input_tokens = row["reserved_input_tokens"] + 1
    with pytest.raises(llm.LLMUsageExceededReservationError):
        b.complete(
            instructions="a", input_text="b", max_output_tokens=10, workload="headroom-over",
            run_id="run-a", output_schema=answer_schema(),
        )


def test_effective_input_cap_includes_schema_and_framing_before_provider(tmp_path: Path) -> None:
    schema = answer_schema()
    total = (
        len(b"ab")
        + len(json.dumps(schema, sort_keys=True, separators=(",", ":")).encode())
        + llm.INPUT_FRAMING_TOKEN_HEADROOM
    )
    client = SimpleNamespace(responses=SimpleNamespace(create=lambda **_: pytest.fail("provider called")))
    b = boundary(tmp_path, client, max_input_bytes=total - 1)

    with pytest.raises(llm.InputTokenLimitExceededError, match=f"request has {total}"):
        b.complete(
            instructions="a", input_text="b", max_output_tokens=1,
            workload="effective-cap", run_id="run-a", output_schema=schema,
        )

    row = records(tmp_path / "usage.sqlite3")[0]
    assert row["status"] == "input_rejected"
    assert row["reserved_input_tokens"] == total


def test_request_caps_are_per_run_but_daily_spend_is_shared(tmp_path: Path) -> None:
    def create(**_):
        result = response()
        result.usage.output_tokens = 1
        result.usage.output_tokens_details.reasoning_tokens = 0
        return result
    client = SimpleNamespace(responses=SimpleNamespace(create=create))
    b = boundary(tmp_path, client, max_requests_per_run=1)
    b.complete(instructions="a", input_text="b", max_output_tokens=1, workload="one", run_id="run-a")
    b.complete(instructions="a", input_text="b", max_output_tokens=1, workload="two", run_id="run-b")
    with pytest.raises(llm.RequestLimitExceededError):
        b.complete(instructions="a", input_text="b", max_output_tokens=1, workload="three", run_id="run-a")

    tiny = boundary(tmp_path, client, daily_budget_usd="0.000001")
    with pytest.raises(llm.DailyBudgetExceededError):
        tiny.complete(instructions="a", input_text="b", max_output_tokens=1, workload="shared", run_id="run-c")


@pytest.mark.parametrize(
    ("model", "max_requests", "max_terra"),
    [(llm.LUNA_MODEL, 1, 8), (llm.TERRA_MODEL, 2, 1)],
)
def test_run_request_caps_include_prior_utc_days(
    tmp_path: Path, model: str, max_requests: int, max_terra: int
) -> None:
    client = SimpleNamespace(responses=SimpleNamespace(create=lambda **_: pytest.fail("provider called")))
    b = boundary(
        tmp_path, client, max_requests_per_run=max_requests,
        max_terra_requests_per_run=max_terra,
    )
    conn = sqlite3.connect(tmp_path / "usage.sqlite3")
    conn.execute(
        """INSERT INTO llm_requests
           (created_at, day_utc, month_utc, workload, run_id, model, status, attempts,
            reserved_cost_usd, cost_usd, pricing_version, pricing_effective_date)
           VALUES (datetime('now', '-1 day'), date('now', '-1 day'),
                   strftime('%Y-%m', 'now', '-1 day'), 'seed', 'overnight-run', ?,
                   'completed', 1, '0', '0', 'test', '2026-09-01')""",
        (model,),
    )
    conn.commit()
    conn.close()

    with pytest.raises(llm.RequestLimitExceededError):
        b.complete(
            instructions="a", input_text="b", max_output_tokens=1,
            workload="after-midnight", run_id="overnight-run", model=model,
        )


def test_run_id_is_required_and_audited(tmp_path: Path) -> None:
    def create(**_):
        result = response()
        result.usage.output_tokens = 1
        result.usage.output_tokens_details.reasoning_tokens = 0
        return result
    b = boundary(tmp_path, SimpleNamespace(responses=SimpleNamespace(create=create)) )
    with pytest.raises(llm.LLMConfigurationError):
        b.complete(instructions="a", input_text="b", max_output_tokens=1, workload="missing", run_id="")
    b.complete(instructions="a", input_text="b", max_output_tokens=1, workload="ok", run_id="daily-7")
    assert records(tmp_path / "usage.sqlite3")[0]["run_id"] == "daily-7"


def test_conditional_finalization_requires_exactly_one_reserved_row(tmp_path: Path, monkeypatch) -> None:
    b = boundary(tmp_path, SimpleNamespace(responses=SimpleNamespace(create=lambda **_: response())))
    original_connect = b._connect

    class CursorProxy:
        rowcount = 0

    class ConnectionProxy:
        def __init__(self, conn):
            self.conn = conn
        def execute(self, sql, params=()):
            cursor = self.conn.execute(sql, params)
            return CursorProxy() if sql.lstrip().startswith("UPDATE llm_requests") else cursor
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.conn.close()
        def __getattr__(self, name):
            return getattr(self.conn, name)

    calls = 0
    def connect():
        nonlocal calls
        calls += 1
        conn = original_connect()
        return ConnectionProxy(conn) if calls == 2 else conn

    monkeypatch.setattr(b, "_connect", connect)
    with pytest.raises(llm.LLMConfigurationError, match="finalize"):
        b.complete(instructions="a", input_text="b", max_output_tokens=1, workload="race", run_id="run-a")
    assert records(tmp_path / "usage.sqlite3")[0]["status"] == "reserved"
