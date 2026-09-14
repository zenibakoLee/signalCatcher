from __future__ import annotations

import pytest

from pipeline import llm
from pipeline.generators import company_analysis, conference_briefing, daily_digest, keyword_suggestions, thesis_scout
from pipeline.processing import scorer, trend_detector


WORKER_MODULES = (
    scorer,
    trend_detector,
    company_analysis,
    conference_briefing,
    daily_digest,
    keyword_suggestions,
    thesis_scout,
)


def test_worker_llm_metadata_is_fixed_to_luna() -> None:
    assert scorer.MODEL == llm.MODEL
    assert trend_detector.MODEL == llm.MODEL
    assert company_analysis.MODEL == llm.TERRA_MODEL
    assert conference_briefing.PRE_EVENT_MODEL == llm.TERRA_MODEL
    assert conference_briefing.POST_EVENT_MODEL == llm.TERRA_MODEL
    assert daily_digest.MODEL == llm.TERRA_MODEL
    assert keyword_suggestions.MODEL == llm.MODEL
    assert thesis_scout.MODEL == llm.TERRA_MODEL


def test_scorer_does_not_persist_neutral_scores_when_openai_is_unavailable(monkeypatch) -> None:
    class Connection:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("no score rows may be inserted after an LLM failure")

    monkeypatch.setattr(scorer.llm, "get_boundary", lambda: (_ for _ in ()).throw(llm.LLMConfigurationError("missing config")))

    with pytest.raises(llm.LLMConfigurationError):
        scorer._score_batch("system", [
            {"id": 1, "source": "rss", "title": "item", "content_snippet": "text", "metadata": None}
        ], run_id="test-run")
