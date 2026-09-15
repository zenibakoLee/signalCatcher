from __future__ import annotations

from pathlib import Path

import pytest

from pipeline import llm, main
from pipeline.generators import (
    company_analysis,
    conference_briefing,
    daily_digest,
    keyword_suggestions,
    thesis_scout,
)
from pipeline.processing import scorer, trend_detector

ROOT = Path(__file__).resolve().parents[2]


def test_worker_model_policy_is_explicit_and_sol_is_forbidden() -> None:
    assert scorer.MODEL == llm.LUNA_MODEL
    assert trend_detector.MODEL == llm.LUNA_MODEL
    assert keyword_suggestions.MODEL == llm.LUNA_MODEL
    assert company_analysis.MODEL == llm.TERRA_MODEL
    assert conference_briefing.PRE_EVENT_MODEL == llm.TERRA_MODEL
    assert conference_briefing.POST_EVENT_MODEL == llm.TERRA_MODEL
    assert daily_digest.MODEL == llm.TERRA_MODEL
    assert thesis_scout.MODEL == llm.TERRA_MODEL
    assert llm.ALLOWED_MODELS == {llm.LUNA_MODEL, llm.TERRA_MODEL}
    assert all("sol" not in model.lower() for model in llm.ALLOWED_MODELS)


def test_scorer_fails_closed_when_codex_oauth_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(
        scorer.llm, "get_boundary",
        lambda: (_ for _ in ()).throw(llm.LLMConfigurationError("missing OAuth")),
    )
    with pytest.raises(llm.LLMConfigurationError):
        scorer._score_batch("system", [
            {"id": 1, "source": "rss", "title": "item", "content_snippet": "text", "metadata": None}
        ], run_id="test-run")


def test_safe_bulk_sizes_target_one_hundred_without_unbounded_payloads() -> None:
    assert scorer.BATCH_SIZE == 100
    assert scorer.MAX_SNIPPET_CHARS == 2_000
    assert scorer.BATCH_SIZE * scorer.MAX_SNIPPET_CHARS < llm.DEFAULT_MAX_INPUT_BYTES
    assert scorer.SCORING_MAX_OUTPUT_TOKENS <= llm.DEFAULT_MAX_OUTPUT_TOKENS
    assert main.TRANSLATION_BATCH_SIZE == 100
    assert main.TRANSLATION_MAX_OUTPUT_TOKENS <= llm.DEFAULT_MAX_OUTPUT_TOKENS


def test_daily_llm_namespace_is_uuid_backed_and_separate_from_business_run_id() -> None:
    first = main._new_llm_run_id("daily", 7)
    second = main._new_llm_run_id("daily", 7)
    assert first != second
    assert first.startswith("daily-7-")
    assert second.startswith("daily-7-")
    import uuid
    uuid.UUID(first.removeprefix("daily-7-"))
    uuid.UUID(second.removeprefix("daily-7-"))
    source = (ROOT / "pipeline" / "main.py").read_text()
    assert "llm_run_id = _new_llm_run_id(\"daily\", run_id)" in source
    for call in ("auto_manage_keywords", "detect_trends", "generate_digest", "run_thesis_scout"):
        assert f"{call}(run_id=llm_run_id" in source
    assert "since=scoring_since, until=scoring_until, run_id=llm_run_id" in source


def test_launch_script_preflights_codex_oauth_without_openai_key_or_dotenv() -> None:
    runner = (ROOT / "scripts" / "run-pipeline.sh").read_text()
    daily = (ROOT / "scripts" / "run-daily.sh").read_text()
    event = (ROOT / "scripts" / "run-event.sh").read_text()
    event_plist = (ROOT / "launchd" / "com.signalcatcher.event.plist").read_text()

    assert "codex login status" in runner
    assert "command -v codex" in runner
    assert '$HOME/.local/bin' in runner
    assert "/opt/homebrew/bin" in runner
    assert "OPENAI_API_KEY" not in runner
    assert "SIGNALCATCHER_OPENAI" not in runner
    assert "source " not in runner
    assert ".env" not in runner
    main_source = (ROOT / "pipeline" / "main.py").read_text()
    assert "load_dotenv" not in main_source
    assert "MAX_RETRIES" not in runner
    assert "for attempt" not in runner
    assert 'exec "$VENV" -m pipeline "$MODE"' in runner
    assert "run-pipeline.sh\" daily" in daily
    assert "run-pipeline.sh\" event" in event
    assert "scripts/run-event.sh" in event_plist


def test_no_active_openai_anthropic_claude_or_sol_execution_dependency() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text().lower()
    active_python = "\n".join(
        path.read_text(errors="ignore")
        for path in (ROOT / "pipeline").rglob("*.py")
        if "tests" not in path.parts
    ).lower()
    assert '"openai' not in pyproject
    assert "from openai import" not in active_python
    assert "import openai" not in active_python
    assert "import anthropic" not in active_python
    assert "from anthropic" not in active_python
    assert "claude_code" not in active_python
    assert "gpt-5.6-sol" not in active_python
